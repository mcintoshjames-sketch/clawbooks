from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from clawbooks import __version__
from clawbooks.config import ledger_paths, load_config
from clawbooks.db import inspect_ledger_bootstrap, session_scope
from clawbooks.exceptions import AppError
from clawbooks.ledger import (
    FINANCIAL_SUBTYPES,
    P_AND_L_KINDS,
    close_period,
    entry_has_immediate_cash_pnl,
    get_active_lock,
    get_compliance_profile,
    is_immediate_cash_source_line,
    period_status,
)
from clawbooks.models import AuditEvent, CloseSnapshot, Document, JournalEntry, JournalLine, PeriodLock, ReconciliationSession, ReviewBlocker, SettlementApplication
from clawbooks.reports import (
    balance_sheet,
    cash_basis_snapshot,
    cash_flow,
    document_checklist,
    equity_rollforward,
    general_ledger,
    pnl,
    reconciliation_summary,
    tax_liabilities,
    tax_rollforward,
    trial_balance,
)
from clawbooks.schemas import AppConfig
from clawbooks.utils import json_dumps, utcnow

SNAPSHOT_SCHEMA_VERSION = 2
COMPACT_REPORT_NAMES = (
    "pnl",
    "balance_sheet",
    "cash_flow",
    "trial_balance",
    "tax_liabilities",
    "tax_rollforward",
    "equity_rollforward",
)
HEAVY_ARTIFACT_NAMES = ("general_ledger", "reconciliation_coverage")


def _normalized(value: object) -> dict[str, object] | list[object] | str | int | float | bool | None:
    return json.loads(json_dumps(value))


def _payload_hash(payload: object) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def _canonical_summary(payload: dict[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {"report_basis": payload.get("report_basis")}
    if "totals" in payload:
        summary["totals"] = _normalized(payload.get("totals", {}))
    if "rows" in payload and isinstance(payload["rows"], list):
        summary["row_count"] = len(payload["rows"])
    if "entries" in payload and isinstance(payload["entries"], list):
        summary["entry_count"] = len(payload["entries"])
    if "assets" in payload:
        summary["assets_count"] = len(payload.get("assets", []))
    if "liabilities" in payload:
        summary["liabilities_count"] = len(payload.get("liabilities", []))
    if "equity" in payload:
        summary["equity_count"] = len(payload.get("equity", []))
    if "sections" in payload and isinstance(payload["sections"], dict):
        summary["section_counts"] = {key: len(value) for key, value in payload["sections"].items()}
    if "sessions" in payload and isinstance(payload["sessions"], list):
        summary["session_count"] = len(payload["sessions"])
    if "coverage_rows" in payload and isinstance(payload["coverage_rows"], list):
        summary["coverage_row_count"] = len(payload["coverage_rows"])
    return summary


def _is_full_calendar_year(period_start: date, period_end: date) -> bool:
    return (
        period_start.month == 1
        and period_start.day == 1
        and period_end.month == 12
        and period_end.day == 31
        and period_start.year == period_end.year
    )


def _snapshot_reports(
    session: Session,
    *,
    ledger_dir: Path,
    period_start: date,
    period_end: date,
    config: AppConfig,
) -> dict[str, object]:
    compact_reports = {
        "pnl": pnl(session, period_start=period_start, period_end=period_end, basis=config.default_report_basis),
        "balance_sheet": balance_sheet(session, as_of=period_end),
        "cash_flow": cash_flow(session, period_start=period_start, period_end=period_end),
        "trial_balance": trial_balance(session, as_of=period_end),
        "tax_liabilities": tax_liabilities(session, as_of=period_end),
        "tax_rollforward": tax_rollforward(session, period_start=period_start, period_end=period_end),
        "equity_rollforward": equity_rollforward(session, period_start=period_start, period_end=period_end),
    }
    heavy_artifacts = {
        "general_ledger": general_ledger(session, period_start=period_start, period_end=period_end, include_line_ids=True),
        "reconciliation_coverage": reconciliation_summary(session, period_start=period_start, period_end=period_end),
    }
    cash_snapshot = cash_basis_snapshot(session, period_start=period_start, period_end=period_end)
    compliance_profile = get_compliance_profile(session).model_dump()
    advisory_context = None
    if _is_full_calendar_year(period_start, period_end):
        advisory_context = {
            "year": period_end.year,
            "document_checklist": document_checklist(session, ledger_dir=ledger_dir, year=period_end.year),
        }
    return {
        "compact_reports": compact_reports,
        "heavy_artifacts": heavy_artifacts,
        "cash_snapshot": cash_snapshot,
        "compliance_profile": compliance_profile,
        "advisory_context": advisory_context,
    }


def _snapshot_payload(session: Session, *, ledger_dir: Path, config: AppConfig, period_start: date, period_end: date) -> dict[str, object]:
    report_bundle = _snapshot_reports(
        session,
        ledger_dir=ledger_dir,
        period_start=period_start,
        period_end=period_end,
        config=config,
    )
    compact_reports = report_bundle["compact_reports"]
    heavy_artifacts = report_bundle["heavy_artifacts"]
    cash_snapshot = report_bundle["cash_snapshot"]
    blocker_count = session.scalar(
        select(func.count(ReviewBlocker.id)).where(
            ReviewBlocker.status == "open",
            ReviewBlocker.blocker_date >= period_start,
            ReviewBlocker.blocker_date <= period_end,
        )
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "report_generator_version": __version__,
        "report_basis": {name: payload.get("report_basis") for name, payload in compact_reports.items()},
        "report_hashes": {name: _payload_hash(payload) for name, payload in compact_reports.items()},
        "canonical_summaries": {name: _canonical_summary(payload) for name, payload in compact_reports.items()},
        "normalized_reports": {name: _normalized(payload) for name, payload in compact_reports.items()},
        "heavy_artifact_hashes": {name: _payload_hash(payload) for name, payload in heavy_artifacts.items()},
        "heavy_artifact_summaries": {name: _canonical_summary(payload) for name, payload in heavy_artifacts.items()},
        "open_review_blocker_count": int(blocker_count or 0),
        "cash_basis_warnings": {
            "warnings": cash_snapshot.get("warnings", []),
            "excluded_lines": cash_snapshot.get("excluded_lines", []),
            "ignored_invalid_settlement_applications": cash_snapshot.get("ignored_invalid_settlement_applications", []),
        },
        "compliance_profile": report_bundle["compliance_profile"],
        "advisory_context": report_bundle["advisory_context"],
        "ledger_dir": str(ledger_dir),
    }


def _record_close_snapshot_event(session: Session, *, snapshot: CloseSnapshot, payload: dict[str, object], source: str, reason: str | None) -> None:
    session.add(
        AuditEvent(
            entity_type="close_snapshot",
            entity_ref=str(snapshot.id),
            action="create",
            before_json=None,
            after_json=json_dumps(
                {
                    "close_snapshot_id": snapshot.id,
                    "period_start": payload["period_start"],
                    "period_end": payload["period_end"],
                    "snapshot_schema_version": payload["snapshot_schema_version"],
                    "report_generator_version": payload["report_generator_version"],
                }
            ),
            source=source,
            reason=reason,
            created_at=utcnow(),
        )
    )


def persist_close_snapshot(
    session: Session,
    *,
    ledger_dir: Path,
    config: AppConfig,
    period_start: date,
    period_end: date,
    source: str,
    reason: str | None,
) -> CloseSnapshot:
    payload = _snapshot_payload(session, ledger_dir=ledger_dir, config=config, period_start=period_start, period_end=period_end)
    snapshot = CloseSnapshot(
        period_start=period_start,
        period_end=period_end,
        snapshot_at=utcnow(),
        snapshot_schema_version=payload["snapshot_schema_version"],
        report_generator_version=payload["report_generator_version"],
        report_basis_json=json_dumps(payload["report_basis"]),
        report_hashes_json=json_dumps(payload["report_hashes"]),
        canonical_summaries_json=json_dumps(payload["canonical_summaries"]),
        normalized_reports_json=json_dumps(payload["normalized_reports"]),
        heavy_artifact_hashes_json=json_dumps(payload["heavy_artifact_hashes"]),
        heavy_artifact_summaries_json=json_dumps(payload["heavy_artifact_summaries"]),
        open_review_blocker_count=payload["open_review_blocker_count"],
        cash_basis_warnings_json=json_dumps(payload["cash_basis_warnings"]),
        compliance_profile_json=json_dumps(payload["compliance_profile"]),
        advisory_context_json=None if payload["advisory_context"] is None else json_dumps(payload["advisory_context"]),
    )
    session.add(snapshot)
    session.flush()
    _record_close_snapshot_event(session, snapshot=snapshot, payload=payload, source=source, reason=reason)
    session.flush()
    return snapshot


def latest_close_snapshot(session: Session, *, period_start: date, period_end: date) -> CloseSnapshot | None:
    return session.scalar(
        select(CloseSnapshot)
        .where(CloseSnapshot.period_start == period_start, CloseSnapshot.period_end == period_end)
        .order_by(CloseSnapshot.snapshot_at.desc(), CloseSnapshot.id.desc())
    )


def serialize_close_snapshot(snapshot: CloseSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "period_start": snapshot.period_start,
        "period_end": snapshot.period_end,
        "snapshot_at": snapshot.snapshot_at,
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "report_generator_version": snapshot.report_generator_version,
        "report_basis": json.loads(snapshot.report_basis_json),
        "report_hashes": json.loads(snapshot.report_hashes_json),
        "canonical_summaries": json.loads(snapshot.canonical_summaries_json),
        "normalized_reports": json.loads(snapshot.normalized_reports_json),
        "heavy_artifact_hashes": json.loads(snapshot.heavy_artifact_hashes_json),
        "heavy_artifact_summaries": json.loads(snapshot.heavy_artifact_summaries_json),
        "open_review_blocker_count": snapshot.open_review_blocker_count,
        "cash_basis_warnings": json.loads(snapshot.cash_basis_warnings_json),
        "compliance_profile": json.loads(snapshot.compliance_profile_json),
        "advisory_context": None if snapshot.advisory_context_json is None else json.loads(snapshot.advisory_context_json),
    }


def _finding(
    finding_id: str,
    *,
    severity: str,
    category: str,
    title: str,
    message: str,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title,
        "message": message,
        "data": data or {},
    }


def _summary_for_findings(findings: list[dict[str, object]]) -> dict[str, int]:
    counts = Counter(finding["severity"] for finding in findings)
    return {severity: int(counts.get(severity, 0)) for severity in ("critical", "high", "medium", "low")}


def _period_drift(
    session: Session,
    *,
    ledger_dir: Path,
    config: AppConfig,
    period_start: date,
    period_end: date,
    snapshot: CloseSnapshot | None,
    current_status: str,
) -> dict[str, object]:
    empty = {
        "accounting_data_drift": [],
        "admin_state_drift": [],
        "advisory_context_drift": [],
        "report_version_drift": [],
        "historical_only": False,
    }
    if snapshot is None:
        return empty
    if current_status != "close":
        return {**empty, "historical_only": True}

    serialized = serialize_close_snapshot(snapshot)
    current = _snapshot_payload(session, ledger_dir=ledger_dir, config=config, period_start=period_start, period_end=period_end)
    drift = {key: [] for key in ("accounting_data_drift", "admin_state_drift", "advisory_context_drift", "report_version_drift")}
    drift["historical_only"] = False

    if (
        serialized["snapshot_schema_version"] != current["snapshot_schema_version"]
        or serialized["report_generator_version"] != current["report_generator_version"]
    ):
        drift["report_version_drift"].append(
            {
                "snapshot_schema_version": serialized["snapshot_schema_version"],
                "current_snapshot_schema_version": current["snapshot_schema_version"],
                "snapshot_report_generator_version": serialized["report_generator_version"],
                "current_report_generator_version": current["report_generator_version"],
            }
        )
        return drift

    for report_name, snapshot_hash in serialized["report_hashes"].items():
        current_hash = current["report_hashes"].get(report_name)
        if current_hash != snapshot_hash:
            drift["accounting_data_drift"].append(
                {
                    "report_name": report_name,
                    "snapshot_hash": snapshot_hash,
                    "current_hash": current_hash,
                }
            )

    if serialized["heavy_artifact_hashes"] != current["heavy_artifact_hashes"]:
        drift["accounting_data_drift"].append(
            {
                "report_name": "control_artifacts",
                "snapshot_hashes": serialized["heavy_artifact_hashes"],
                "current_hashes": current["heavy_artifact_hashes"],
            }
        )

    if serialized["compliance_profile"] != current["compliance_profile"]:
        drift["admin_state_drift"].append(
            {
                "component": "compliance_profile",
                "snapshot": serialized["compliance_profile"],
                "current": current["compliance_profile"],
            }
        )

    if serialized["advisory_context"] is not None and serialized["advisory_context"] != current["advisory_context"]:
        drift["advisory_context_drift"].append(
            {
                "component": "year_end_advisory_context",
                "snapshot": serialized["advisory_context"],
                "current": current["advisory_context"],
            }
        )

    return drift


def audit_period(
    session: Session,
    *,
    ledger_dir: Path,
    config: AppConfig,
    period_start: date,
    period_end: date,
) -> dict[str, object]:
    current = period_status(session, period_start=period_start, period_end=period_end)
    snapshot = latest_close_snapshot(session, period_start=period_start, period_end=period_end)

    blocking_findings: list[dict[str, object]] = []
    advisory_findings: list[dict[str, object]] = []
    nested = session.begin_nested()
    try:
        try:
            close_period(
                session,
                period_start=period_start,
                period_end=period_end,
                lock_type="period",
                reason="period_audit_probe",
                ledger_dir=ledger_dir,
                config=config,
                acknowledge_review_ids=[],
            )
            closable_now = True
        except AppError as exc:
            closable_now = False
            blocking_findings.append(
                _finding(
                    "close_prereq_failed",
                    severity="high",
                    category="period_close",
                    title="Period cannot be closed with the current state",
                    message=exc.message,
                    data=exc.data,
                )
            )
    finally:
        nested.rollback()

    drift = _period_drift(
        session,
        ledger_dir=ledger_dir,
        config=config,
        period_start=period_start,
        period_end=period_end,
        snapshot=snapshot,
        current_status=current["status"],
    )
    for key, title in (
        ("accounting_data_drift", "Closed-period accounting outputs differ from the stored close snapshot"),
        ("admin_state_drift", "Administrative state has changed since the stored close snapshot"),
        ("advisory_context_drift", "Year-end advisory context has changed since the stored close snapshot"),
        ("report_version_drift", "Snapshot/report generator version differs from the stored close snapshot"),
    ):
        if drift[key]:
            advisory_findings.append(
                _finding(
                    key,
                    severity="medium",
                    category="close_snapshot_drift",
                    title=title,
                    message=title,
                    data={"changes": drift[key]},
                )
            )

    return {
        "current_status": current,
        "closable_now": closable_now,
        "blocking_findings": blocking_findings,
        "advisory_findings": advisory_findings,
        "reconciliation_coverage": reconciliation_summary(session, period_start=period_start, period_end=period_end),
        "latest_close_snapshot": serialize_close_snapshot(snapshot),
        "snapshot_drift": drift,
    }


def _check_manifest_files(path: Path) -> list[dict[str, object]]:
    return _check_manifest_files_for_year(path, year=date.today().year)


def _manifest_associated_year(manifest: dict[str, object]) -> tuple[int | None, str | None]:
    explicit_year = manifest.get("year")
    if isinstance(explicit_year, int):
        return explicit_year, None
    if isinstance(explicit_year, str):
        try:
            return int(explicit_year), None
        except ValueError:
            return None, "invalid_year_metadata"

    period_start = manifest.get("period_start")
    period_end = manifest.get("period_end")
    if not isinstance(period_start, str) or not isinstance(period_end, str):
        return None, "missing_year_metadata"
    try:
        start = date.fromisoformat(period_start)
        end = date.fromisoformat(period_end)
    except ValueError:
        return None, "invalid_period_metadata"
    if _is_full_calendar_year(start, end):
        return end.year, None
    return None, "non_full_year_export"


def _check_manifest_files_for_year(path: Path, *, year: int) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    if not path.exists():
        return findings
    for manifest_path in sorted(path.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            findings.append(
                _finding(
                    f"unscoped_year_artifact::{manifest_path}",
                    severity="low",
                    category="exports",
                    title="Export manifest cannot be associated to a year",
                    message=f"Could not parse export manifest metadata at {manifest_path}.",
                    data={"manifest_path": str(manifest_path), "reason": "invalid_json", "requested_year": year},
                )
            )
            continue
        associated_year, reason = _manifest_associated_year(manifest)
        if associated_year is None:
            findings.append(
                _finding(
                    f"unscoped_year_artifact::{manifest_path}",
                    severity="low",
                    category="exports",
                    title="Export manifest cannot be associated to a year",
                    message=f"Manifest {manifest_path.name} is missing year metadata needed for year-scoped integrity checks.",
                    data={"manifest_path": str(manifest_path), "reason": reason, "requested_year": year},
                )
            )
            continue
        if associated_year != year:
            continue
        for relative in manifest.get("files", []):
            candidate = manifest_path.parent / relative
            if not candidate.exists():
                findings.append(
                    _finding(
                        f"manifest_missing::{manifest_path}::{relative}",
                        severity="medium",
                        category="exports",
                        title="Manifest references a missing file",
                        message=f"Export manifest {manifest_path.name} references missing file {relative}.",
                        data={
                            "manifest_path": str(manifest_path),
                            "missing_file": relative,
                            "associated_year": associated_year,
                        },
                    )
                )
    return findings


def _year_checklist_findings(session: Session, *, ledger_dir: Path, year: int) -> list[dict[str, object]]:
    checklist = document_checklist(session, ledger_dir=ledger_dir, year=year)
    findings: list[dict[str, object]] = []
    for row in checklist["missing_items"]:
        findings.append(
            _finding(
                f"year_checklist_missing::{year}::{row['item_key']}",
                severity="medium",
                category="year_checklist",
                title=f"{row['title']} is missing for the selected year",
                message=row["notes"],
                data={"year": year, "row": row},
            )
        )
    for row in checklist["unknown_items"]:
        findings.append(
            _finding(
                f"year_checklist_unknown::{year}::{row['item_key']}",
                severity="low",
                category="year_checklist",
                title=f"{row['title']} remains unknown for the selected year",
                message=row["notes"],
                data={"year": year, "row": row},
            )
        )
    return findings


def _candidate_closed_intervals(session: Session) -> list[tuple[date, date]]:
    intervals = {
        (snapshot.period_start, snapshot.period_end)
        for snapshot in session.scalars(select(CloseSnapshot))
    }
    intervals.update(
        (lock.period_start, lock.period_end)
        for lock in session.scalars(select(PeriodLock).where(PeriodLock.action == "close"))
    )
    return sorted(intervals)


def _interval_is_effectively_closed(session: Session, *, period_start: date, period_end: date) -> bool:
    overlapping_locks = list(
        session.scalars(
            select(PeriodLock)
            .where(PeriodLock.period_start <= period_end, PeriodLock.period_end >= period_start)
            .order_by(PeriodLock.period_start, PeriodLock.period_end, PeriodLock.created_at, PeriodLock.id)
        )
    )
    if not overlapping_locks:
        return False
    boundaries = {period_start, period_end + timedelta(days=1)}
    for lock in overlapping_locks:
        boundaries.add(max(period_start, lock.period_start))
        boundaries.add(min(period_end + timedelta(days=1), lock.period_end + timedelta(days=1)))
    ordered_boundaries = sorted(boundaries)
    for boundary in ordered_boundaries[:-1]:
        lock = get_active_lock(session, boundary)
        if lock is None or lock.action != "close":
            return False
    return True


def _is_supported_settlement_account(line: JournalLine) -> bool:
    return line.account.subtype in FINANCIAL_SUBTYPES or line.account.code == "3000"


def _settlement_findings(applications: list[SettlementApplication]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    source_totals: dict[int, int] = defaultdict(int)
    settlement_totals: dict[int, int] = defaultdict(int)
    source_lines: dict[int, JournalLine] = {}
    settlement_lines: dict[int, JournalLine] = {}

    for application in applications:
        source_line = application.source_line
        settlement_line = application.settlement_line
        source_totals[application.source_line_id] += application.applied_amount_cents
        settlement_totals[application.settlement_line_id] += application.applied_amount_cents
        source_lines[application.source_line_id] = source_line
        settlement_lines[application.settlement_line_id] = settlement_line

        if application.applied_amount_cents <= 0:
            findings.append(
                _finding(
                    f"settlement_non_positive::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application amount must be positive",
                    message="Open settlement applications must use a positive applied amount.",
                    data={"settlement_application_id": application.id, "applied_amount_cents": application.applied_amount_cents},
                )
            )
        if source_line.account.kind not in P_AND_L_KINDS:
            findings.append(
                _finding(
                    f"settlement_invalid_source_kind::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application source line is not a supported P&L line",
                    message="Settlement source lines must be revenue, expense, or contra-revenue.",
                    data={
                        "settlement_application_id": application.id,
                        "source_line_id": source_line.id,
                        "source_entry_id": source_line.entry_id,
                        "source_account_code": source_line.account.code,
                        "source_account_kind": source_line.account.kind,
                    },
                )
            )
        immediate_cash_source, source_reason = is_immediate_cash_source_line(source_line)
        if immediate_cash_source:
            findings.append(
                _finding(
                    f"settlement_immediate_cash_source::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application source line is already immediate-cash",
                    message="Immediate-cash P&L lines cannot also receive manual settlement applications.",
                    data={
                        "settlement_application_id": application.id,
                        "source_line_id": source_line.id,
                        "source_entry_id": source_line.entry_id,
                        "reason": source_reason,
                    },
                )
            )
        if not _is_supported_settlement_account(settlement_line):
            findings.append(
                _finding(
                    f"settlement_unsupported_account::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application uses an unsupported settlement account",
                    message="Settlement lines must use a supported cash-equivalent account or owner contribution.",
                    data={
                        "settlement_application_id": application.id,
                        "settlement_line_id": settlement_line.id,
                        "settlement_entry_id": settlement_line.entry_id,
                        "settlement_account_code": settlement_line.account.code,
                        "settlement_account_subtype": settlement_line.account.subtype,
                    },
                )
            )
        if settlement_line.account.code == "3000" and source_line.account.kind != "expense":
            findings.append(
                _finding(
                    f"settlement_owner_contribution_non_expense::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Owner contribution settlement is only valid for expense sources",
                    message="Owner contribution lines can only settle expense sources.",
                    data={
                        "settlement_application_id": application.id,
                        "source_line_id": source_line.id,
                        "source_account_kind": source_line.account.kind,
                        "settlement_line_id": settlement_line.id,
                    },
                )
            )
        if (source_line.amount_cents > 0) == (settlement_line.amount_cents > 0):
            findings.append(
                _finding(
                    f"settlement_same_sign::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement source and settlement lines must have opposite signs",
                    message="Settlement lines must offset the source line rather than share the same sign.",
                    data={
                        "settlement_application_id": application.id,
                        "source_line_id": source_line.id,
                        "source_entry_id": source_line.entry_id,
                        "settlement_line_id": settlement_line.id,
                        "settlement_entry_id": settlement_line.entry_id,
                        "source_amount_cents": source_line.amount_cents,
                        "settlement_amount_cents": settlement_line.amount_cents,
                    },
                )
            )
        if application.applied_date < source_line.entry.entry_date or application.applied_date < settlement_line.entry.entry_date:
            findings.append(
                _finding(
                    f"settlement_invalid_applied_date::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application is dated before an underlying journal entry",
                    message="Settlement applied dates cannot precede either underlying journal entry date.",
                    data={
                        "settlement_application_id": application.id,
                        "applied_date": application.applied_date,
                        "source_entry_date": source_line.entry.entry_date,
                        "settlement_entry_date": settlement_line.entry.entry_date,
                    },
                )
            )
        if entry_has_immediate_cash_pnl(settlement_line.entry):
            findings.append(
                _finding(
                    f"invalid_settlement::{application.id}",
                    severity="high",
                    category="settlement",
                    title="Settlement application points at an immediate-cash entry",
                    message="Immediate-cash entries cannot also be used as settlement cash.",
                    data={"settlement_application_id": application.id, "settlement_line_id": settlement_line.id},
                )
            )

    for source_line_id, total_applied in source_totals.items():
        source_line = source_lines[source_line_id]
        residual_cents = abs(source_line.amount_cents) - total_applied
        if total_applied > abs(source_line.amount_cents):
            findings.append(
                _finding(
                    f"settlement_source_overapplied::{source_line_id}",
                    severity="high",
                    category="settlement",
                    title="Settlement source line is over-applied",
                    message="Open settlement applications exceed the source line amount.",
                    data={
                        "source_line_id": source_line_id,
                        "source_entry_id": source_line.entry_id,
                        "source_amount_cents": source_line.amount_cents,
                        "total_open_applied_cents": total_applied,
                        "residual_cents": residual_cents,
                    },
                )
            )

    for settlement_line_id, total_applied in settlement_totals.items():
        settlement_line = settlement_lines[settlement_line_id]
        residual_cents = abs(settlement_line.amount_cents) - total_applied
        if total_applied > abs(settlement_line.amount_cents):
            findings.append(
                _finding(
                    f"settlement_cash_overapplied::{settlement_line_id}",
                    severity="high",
                    category="settlement",
                    title="Settlement line is over-applied",
                    message="Open settlement applications exceed the settlement line amount.",
                    data={
                        "settlement_line_id": settlement_line_id,
                        "settlement_entry_id": settlement_line.entry_id,
                        "settlement_amount_cents": settlement_line.amount_cents,
                        "total_open_applied_cents": total_applied,
                        "residual_cents": residual_cents,
                    },
                )
            )
    return findings


def _full_doctor(session: Session, *, ledger_dir: Path, config: AppConfig, year: int) -> dict[str, object]:
    findings: list[dict[str, object]] = []

    applications = list(
        session.scalars(
            select(SettlementApplication)
            .options(
                selectinload(SettlementApplication.source_line)
                .selectinload(JournalLine.entry)
                .selectinload(JournalEntry.lines)
                .selectinload(JournalLine.account),
                selectinload(SettlementApplication.settlement_line)
                .selectinload(JournalLine.entry)
                .selectinload(JournalEntry.lines)
                .selectinload(JournalLine.account),
            )
            .where(SettlementApplication.reversed_at.is_(None))
        )
    )
    findings.extend(_settlement_findings(applications))

    for period_start, period_end in _candidate_closed_intervals(session):
        if not _interval_is_effectively_closed(session, period_start=period_start, period_end=period_end):
            continue
        snapshot = latest_close_snapshot(session, period_start=period_start, period_end=period_end)
        if snapshot is None:
            findings.append(
                _finding(
                    f"missing_snapshot::{period_start}::{period_end}",
                    severity="high",
                    category="period_lock",
                    title="Closed period is missing a close snapshot",
                    message="Closed periods should retain a persisted close snapshot.",
                    data={"period_start": period_start, "period_end": period_end},
                )
            )
        audit = audit_period(session, ledger_dir=ledger_dir, config=config, period_start=period_start, period_end=period_end)
        for blocking in audit["blocking_findings"]:
            findings.append(
                _finding(
                    f"closed_period_regression::{period_start}::{period_end}::{blocking['id']}",
                    severity="high",
                    category="period_close",
                    title="Closed period is no longer closable under current controls",
                    message=blocking["message"],
                    data={
                        "period_start": period_start,
                        "period_end": period_end,
                        "blocking_finding": blocking,
                    },
                )
            )
        for key, severity in (
            ("accounting_data_drift", "high"),
            ("admin_state_drift", "medium"),
            ("advisory_context_drift", "medium"),
            ("report_version_drift", "low"),
        ):
            if audit["snapshot_drift"][key]:
                findings.append(
                    _finding(
                        f"{key}::{period_start}::{period_end}",
                        severity=severity,
                        category="close_snapshot_drift",
                        title=f"{key.replace('_', ' ').title()} detected for closed period",
                        message=f"{key.replace('_', ' ').title()} detected for {period_start.isoformat()} to {period_end.isoformat()}.",
                        data={"period_start": period_start, "period_end": period_end, "changes": audit["snapshot_drift"][key]},
                    )
                )

    sessions = list(session.scalars(select(ReconciliationSession).options(selectinload(ReconciliationSession.account)).order_by(ReconciliationSession.account_id, ReconciliationSession.statement_start, ReconciliationSession.statement_end, ReconciliationSession.id)))
    by_account: dict[int, list[ReconciliationSession]] = defaultdict(list)
    for item in sessions:
        if item.status == "voided":
            continue
        by_account[item.account_id].append(item)
    for account_id, account_sessions in by_account.items():
        for index, left in enumerate(account_sessions):
            for right in account_sessions[index + 1 :]:
                if left.statement_start <= right.statement_end and right.statement_start <= left.statement_end:
                    findings.append(
                        _finding(
                            f"overlap::{left.id}::{right.id}",
                            severity="high",
                            category="reconciliation",
                            title="Overlapping non-voided reconciliation sessions exist",
                            message="Non-voided reconciliation sessions for the same account should not overlap.",
                            data={"account_id": account_id, "session_ids": [left.id, right.id]},
                        )
                    )

    stale_before = date.today() - timedelta(days=30)
    stale_blockers = list(
        session.scalars(
            select(ReviewBlocker)
            .where(ReviewBlocker.status == "open", ReviewBlocker.blocker_date <= stale_before)
            .order_by(ReviewBlocker.blocker_date, ReviewBlocker.id)
        )
    )
    for blocker in stale_blockers:
        findings.append(
            _finding(
                f"stale_blocker::{blocker.id}",
                severity="medium",
                category="review_blocker",
                title="Review blocker has remained open for more than 30 days",
                message="Open review blockers should be resolved or intentionally skipped.",
                data={"review_blocker_id": blocker.id, "blocker_date": blocker.blocker_date},
                )
            )

    for document in session.scalars(select(Document).where(Document.tax_year == year).order_by(Document.id)):
        candidate = ledger_paths(ledger_dir)["root"] / document.stored_path
        if not candidate.exists():
            findings.append(
                _finding(
                    f"missing_document::{document.id}",
                    severity="high",
                    category="documents",
                    title="Ledger document is missing from disk",
                    message=f"Document {document.id} points to missing file {document.stored_path}.",
                    data={"document_id": document.id, "stored_path": document.stored_path},
                )
            )

    findings.extend(_check_manifest_files_for_year(ledger_paths(ledger_dir)["exports"], year=year))
    findings.extend(_year_checklist_findings(session, ledger_dir=ledger_dir, year=year))

    return {
        "mode": "full",
        "full_checks_available": True,
        "summary": _summary_for_findings(findings),
        "findings": findings,
        "migration_state": None,
        "year_context": year,
    }


def doctor(ledger_dir: Path, *, year: int | None = None) -> dict[str, object]:
    year = year or date.today().year
    migration_state = inspect_ledger_bootstrap(ledger_dir)
    if not migration_state["full_open_safe"]:
        findings = []
        if migration_state["db_exists"]:
            findings.append(
                _finding(
                    "migration_required",
                    severity="high",
                    category="migration",
                    title="Ledger migration is required before full integrity checks can run",
                    message="This ledger is not at the current Alembic head.",
                    data={"migration_state": migration_state},
                )
            )
        else:
            findings.append(
                _finding(
                    "ledger_not_initialized",
                    severity="high",
                    category="migration",
                    title="Ledger database is not initialized",
                    message="The ledger database file is missing.",
                    data={"migration_state": migration_state},
                )
            )
        return {
            "mode": "bootstrap",
            "full_checks_available": False,
            "summary": _summary_for_findings(findings),
            "findings": findings,
            "migration_state": migration_state,
            "year_context": year,
        }

    config = load_config(ledger_dir)
    with session_scope(ledger_dir) as session:
        return _full_doctor(session, ledger_dir=ledger_dir, config=config, year=year)
