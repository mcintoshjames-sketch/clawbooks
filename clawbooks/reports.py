from __future__ import annotations

import csv
import json
import shutil
import zipfile
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from clawbooks.config import ledger_paths
from clawbooks.exceptions import ValidationError
from clawbooks.ledger import (
    account_balance_as_of,
    display_balance,
    entry_has_immediate_cash_pnl,
    get_compliance_profile,
    is_immediate_cash_source_line,
    list_documents,
    reconciliation_coverage_summary,
    serialize_document,
)
from clawbooks.models import (
    Account,
    ImportRun,
    JournalEntry,
    JournalLine,
    ReconciliationLine,
    ReconciliationMatch,
    ReconciliationSession,
    ReviewBlocker,
    SettlementApplication,
    TaxObligation,
)
from clawbooks.schemas import AppConfig, ExportManifest
from clawbooks.utils import json_dumps, utcnow, year_bounds


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        if rows:
            writer.writerows(
                {
                    key: json_dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in row.items()
                }
                for row in rows
            )
        else:
            writer.writerow({"empty": ""})


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json_dumps(payload), encoding="utf-8")


def trial_balance(session: Session, *, as_of: date) -> dict[str, object]:
    rows = []
    for account in session.scalars(select(Account).order_by(Account.code)):
        raw_balance = account_balance_as_of(session, account_id=account.id, as_of=as_of)
        rows.append(
            {
                "code": account.code,
                "name": account.name,
                "kind": account.kind,
                "subtype": account.subtype,
                "raw_balance_cents": raw_balance,
                "display_balance_cents": display_balance(account, raw_balance),
            }
        )
    return {"as_of": as_of, "report_basis": "accrual", "rows": rows}


def _group_pnl_amounts(rows: list[tuple[JournalLine, Account]]) -> tuple[list[dict[str, object]], dict[str, int]]:
    grouped: dict[str, dict[str, object]] = {}
    for line, account in rows:
        if account.kind not in {"revenue", "expense", "contra_revenue"}:
            continue
        bucket = grouped.setdefault(
            account.code,
            {
                "code": account.code,
                "name": account.name,
                "kind": account.kind,
                "amount_cents": 0,
            },
        )
        bucket["amount_cents"] += line.amount_cents

    rendered = []
    revenue_total = 0
    contra_total = 0
    expense_total = 0
    for row in sorted(grouped.values(), key=lambda item: item["code"]):
        amount_cents = int(row["amount_cents"])
        if row["kind"] == "revenue":
            display_cents = -amount_cents
            revenue_total += display_cents
        elif row["kind"] == "contra_revenue":
            display_cents = amount_cents
            contra_total += display_cents
        else:
            display_cents = amount_cents
            expense_total += display_cents
        rendered.append({**row, "display_amount_cents": display_cents})

    return rendered, {
        "revenue_cents": revenue_total,
        "contra_revenue_cents": contra_total,
        "expense_cents": expense_total,
        "net_income_cents": revenue_total - contra_total - expense_total,
    }


def _accrual_pnl_rows(session: Session, *, period_start: date, period_end: date) -> list[tuple[JournalLine, Account]]:
    query = (
        select(JournalLine, Account)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
            Account.kind.in_(("revenue", "expense", "contra_revenue")),
        )
    )
    return list(session.execute(query))


def cash_basis_snapshot(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    grouped: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    excluded_lines: list[dict[str, object]] = []
    ignored_invalid_settlement_applications: list[dict[str, object]] = []

    applications = list(
        session.scalars(
            select(SettlementApplication)
            .options(
                selectinload(SettlementApplication.source_line).selectinload(JournalLine.account),
                selectinload(SettlementApplication.source_line).selectinload(JournalLine.entry),
                selectinload(SettlementApplication.settlement_line).selectinload(JournalLine.account),
                selectinload(SettlementApplication.settlement_line).selectinload(JournalLine.entry).selectinload(JournalEntry.lines).selectinload(JournalLine.account),
            )
            .where(
                SettlementApplication.reversed_at.is_(None),
                SettlementApplication.applied_date >= period_start,
                SettlementApplication.applied_date <= period_end,
            )
        )
    )
    for application in applications:
        source_line = application.source_line
        settlement_line = application.settlement_line
        if entry_has_immediate_cash_pnl(settlement_line.entry):
            ignored_invalid_settlement_applications.append(
                {
                    "settlement_application_id": application.id,
                    "source_line_id": source_line.id,
                    "source_entry_id": source_line.entry_id,
                    "settlement_line_id": settlement_line.id,
                    "settlement_entry_id": settlement_line.entry_id,
                    "applied_amount_cents": application.applied_amount_cents,
                    "applied_date": application.applied_date,
                    "reason": "Settlement line comes from an immediate-cash entry and was ignored to avoid double counting.",
                }
            )
            warnings.append(
                f"Ignored invalid settlement application {application.id} because settlement line {settlement_line.id} belongs to an immediate-cash entry."
            )
            continue
        account = source_line.account
        bucket = grouped.setdefault(
            account.code,
            {
                "code": account.code,
                "name": account.name,
                "kind": account.kind,
                "amount_cents": 0,
            },
        )
        bucket["amount_cents"] += (1 if source_line.amount_cents > 0 else -1) * application.applied_amount_cents

    source_lines = list(
        session.scalars(
            select(JournalLine)
            .join(JournalEntry)
            .join(Account)
            .options(selectinload(JournalLine.account), selectinload(JournalLine.entry).selectinload(JournalEntry.lines).selectinload(JournalLine.account))
            .where(
                Account.kind.in_(("revenue", "expense", "contra_revenue")),
                JournalEntry.entry_date >= period_start,
                JournalEntry.entry_date <= period_end,
            )
            .order_by(JournalEntry.entry_date, JournalLine.id)
        )
    )
    for source_line in source_lines:
        immediate_cash, reason = is_immediate_cash_source_line(source_line)
        if immediate_cash:
            bucket = grouped.setdefault(
                source_line.account.code,
                {
                    "code": source_line.account.code,
                    "name": source_line.account.name,
                    "kind": source_line.account.kind,
                    "amount_cents": 0,
                },
            )
            bucket["amount_cents"] += source_line.amount_cents
            continue

        settled_in_period = session.scalar(
            select(func.coalesce(func.sum(SettlementApplication.applied_amount_cents), 0)).where(
                SettlementApplication.source_line_id == source_line.id,
                SettlementApplication.reversed_at.is_(None),
                SettlementApplication.applied_date >= period_start,
                SettlementApplication.applied_date <= period_end,
            )
        )
        settled_in_period = int(settled_in_period or 0)
        if settled_in_period >= abs(source_line.amount_cents):
            continue
        excluded_amount = abs(source_line.amount_cents) - settled_in_period
        exclusion_reason = reason or "Manual accrual requires explicit settlement"
        if (
            source_line.entry.source_type == "expense"
            and source_line.account.kind == "expense"
            and any(line.account.code == "2300" for line in source_line.entry.lines if line.id != source_line.id)
        ):
            exclusion_reason = "Reimbursable owner-paid expense requires exact one-source reimbursement auto-link or manual settlement"
        excluded_lines.append(
            {
                "line_id": source_line.id,
                "entry_id": source_line.entry_id,
                "entry_date": source_line.entry.entry_date,
                "account_code": source_line.account.code,
                "account_name": source_line.account.name,
                "excluded_amount_cents": excluded_amount,
                "reason": exclusion_reason,
            }
        )
        warnings.append(
            f"Excluded unsupported cash-basis amount on line {source_line.id} ({source_line.account.code}) until explicitly settled."
        )

    rows, totals = _group_pnl_amounts(
        [
            (
                type(
                    "SyntheticLine",
                    (),
                    {"amount_cents": value["amount_cents"]},
                )(),
                type(
                    "SyntheticAccount",
                    (),
                    {"code": value["code"], "name": value["name"], "kind": value["kind"]},
                )(),
            )
            for value in grouped.values()
        ]
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "cash",
        "rows": rows,
        "totals": totals,
        "warnings": warnings,
        "excluded_lines": excluded_lines,
        "ignored_invalid_settlement_applications": ignored_invalid_settlement_applications,
    }


def pnl(session: Session, *, period_start: date, period_end: date, basis: str) -> dict[str, object]:
    if basis == "cash":
        return cash_basis_snapshot(session, period_start=period_start, period_end=period_end)

    rows, totals = _group_pnl_amounts(_accrual_pnl_rows(session, period_start=period_start, period_end=period_end))
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "accrual",
        "rows": rows,
        "totals": totals,
        "warnings": [],
        "excluded_lines": [],
    }


def balance_sheet(session: Session, *, as_of: date) -> dict[str, object]:
    tb = trial_balance(session, as_of=as_of)["rows"]
    current_period = pnl(session, period_start=date(as_of.year, 1, 1), period_end=as_of, basis="accrual")
    prior_period_end = date(as_of.year - 1, 12, 31)
    prior_earnings = pnl(session, period_start=date(1900, 1, 1), period_end=prior_period_end, basis="accrual") if as_of.year > 1900 else {"totals": {"net_income_cents": 0}}

    assets = []
    liabilities = []
    equity = []
    for row in tb:
        account = {"code": row["code"], "name": row["name"], "amount_cents": row["display_balance_cents"]}
        if row["kind"] == "asset" and account["amount_cents"]:
            assets.append(account)
        elif row["kind"] == "liability" and account["amount_cents"]:
            liabilities.append(account)
        elif row["kind"] == "equity" and account["amount_cents"]:
            equity.append(account)

    retained_earnings = int(prior_period_end.year >= 1900 and prior_earnings["totals"]["net_income_cents"] or 0)
    current_earnings = int(current_period["totals"]["net_income_cents"])
    if retained_earnings:
        equity.append({"code": "retained_earnings", "name": "Retained Earnings", "amount_cents": retained_earnings})
    if current_earnings:
        equity.append({"code": "current_earnings", "name": "Current Earnings", "amount_cents": current_earnings})

    return {
        "as_of": as_of,
        "report_basis": "accrual",
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "totals": {
            "assets_cents": sum(item["amount_cents"] for item in assets),
            "liabilities_cents": sum(item["amount_cents"] for item in liabilities),
            "equity_cents": sum(item["amount_cents"] for item in equity),
        },
    }


def cash_flow(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    entries = list(
        session.scalars(
            select(JournalEntry)
            .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
            .where(JournalEntry.entry_date >= period_start, JournalEntry.entry_date <= period_end)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
        )
    )
    sections: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in entries:
        cash_change = sum(
            line.amount_cents
            for line in entry.lines
            if line.account.kind == "asset" and line.account.subtype in {"bank", "stripe_clearing"}
        )
        if not cash_change:
            continue
        section = "financing" if any(line.account.kind == "equity" for line in entry.lines) else "operating"
        sections[section].append(
            {
                "entry_id": entry.id,
                "entry_date": entry.entry_date,
                "description": entry.description,
                "cash_change_cents": cash_change,
            }
        )

    totals = {key: sum(item["cash_change_cents"] for item in value) for key, value in sections.items()}
    totals["net_change_cents"] = sum(totals.values())
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "cash_flow",
        "sections": dict(sections),
        "totals": totals,
    }


def general_ledger(session: Session, *, period_start: date, period_end: date, include_line_ids: bool = True) -> dict[str, object]:
    entries = list(
        session.scalars(
            select(JournalEntry)
            .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
            .where(JournalEntry.entry_date >= period_start, JournalEntry.entry_date <= period_end)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
        )
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "accrual_ledger",
        "entries": [
            {
                "entry_id": entry.id,
                "entry_date": entry.entry_date,
                "description": entry.description,
                "source_type": entry.source_type,
                "source_ref": entry.source_ref,
                "lines": [
                    {
                        **({"line_id": line.id} if include_line_ids else {}),
                        "account_code": line.account.code,
                        "account_name": line.account.name,
                        "amount_cents": line.amount_cents,
                        "memo": line.memo,
                    }
                    for line in entry.lines
                ],
            }
            for entry in entries
        ],
    }


def tax_liabilities(session: Session, *, as_of: date) -> dict[str, object]:
    accounts = list(session.scalars(select(Account).where(Account.subtype == "tax_liability").order_by(Account.code)))
    account_rows = []
    for account in accounts:
        raw = account_balance_as_of(session, account_id=account.id, as_of=as_of)
        account_rows.append(
            {
                "code": account.code,
                "name": account.name,
                "balance_cents": display_balance(account, raw),
            }
        )

    obligations = list(
        session.scalars(
            select(TaxObligation).where(TaxObligation.due_date <= as_of).order_by(TaxObligation.due_date, TaxObligation.code)
        )
    )
    return {
        "as_of": as_of,
        "report_basis": "accrual",
        "accounts": account_rows,
        "obligations": [
            {
                "code": item.code,
                "description": item.description,
                "jurisdiction": item.jurisdiction,
                "due_date": item.due_date,
                "status": item.status,
                "notes": item.notes,
                "amount_cents": item.amount_cents,
            }
            for item in obligations
        ],
    }


def equity_rollforward(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    opening_equity = balance_sheet(session, as_of=period_start - timedelta(days=1))["totals"]["equity_cents"]
    ending_equity = balance_sheet(session, as_of=period_end)["totals"]["equity_cents"]
    current_period_earnings = pnl(session, period_start=period_start, period_end=period_end, basis="accrual")["totals"]["net_income_cents"]

    entries = list(
        session.scalars(
            select(JournalEntry)
            .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
            .where(JournalEntry.entry_date >= period_start, JournalEntry.entry_date <= period_end)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
        )
    )
    owner_contributions = 0
    owner_draws = 0
    for entry in entries:
        if entry.source_type == "expense":
            owner_contributions += sum(-line.amount_cents for line in entry.lines if line.account.code == "3000")
        elif entry.source_type == "owner_contribution":
            owner_contributions += sum(-line.amount_cents for line in entry.lines if line.account.code == "3000")
        elif entry.source_type == "owner_draw":
            owner_draws += sum(line.amount_cents for line in entry.lines if line.account.code == "3100")

    other_equity_adjustments = ending_equity - opening_equity - current_period_earnings - owner_contributions + owner_draws
    rows = [
        {"component": "opening_equity", "title": "Opening Equity", "amount_cents": opening_equity},
        {"component": "owner_contributions", "title": "Owner Contributions", "amount_cents": owner_contributions},
        {"component": "owner_draws", "title": "Owner Draws", "amount_cents": owner_draws},
        {"component": "current_period_earnings", "title": "Current Period Earnings", "amount_cents": current_period_earnings},
        {"component": "other_equity_adjustments", "title": "Other Equity Adjustments", "amount_cents": other_equity_adjustments},
        {"component": "ending_equity", "title": "Ending Equity", "amount_cents": ending_equity},
    ]
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "accrual",
        "rows": rows,
        "totals": {row["component"]: row["amount_cents"] for row in rows},
    }


def owner_equity(session: Session, *, as_of: date) -> dict[str, object]:
    payload = equity_rollforward(session, period_start=date(as_of.year, 1, 1), period_end=as_of)
    payload["deprecated_alias"] = True
    payload["as_of"] = as_of
    return payload


def tax_rollforward(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    opening_as_of = period_start - timedelta(days=1)
    rows = []
    for account in session.scalars(select(Account).where(Account.subtype == "tax_liability").order_by(Account.code)):
        opening = display_balance(account, account_balance_as_of(session, account_id=account.id, as_of=opening_as_of))
        closing = display_balance(account, account_balance_as_of(session, account_id=account.id, as_of=period_end))
        activity = closing - opening
        rows.append(
            {
                "code": account.code,
                "name": account.name,
                "opening_cents": opening,
                "activity_cents": activity,
                "closing_cents": closing,
            }
        )
    return {"period_start": period_start, "period_end": period_end, "report_basis": "accrual", "rows": rows}


def reconciliation_summary(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    return reconciliation_coverage_summary(session, period_start=period_start, period_end=period_end)


def review_blocker_summary(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    rows = [
        {
            "review_blocker_id": item.id,
            "provider": item.provider,
            "external_id": item.external_id,
            "blocker_type": item.blocker_type,
            "status": item.status,
            "blocker_date": item.blocker_date,
            "resolution_type": item.resolution_type,
        }
        for item in session.scalars(
            select(ReviewBlocker)
            .where(ReviewBlocker.blocker_date >= period_start, ReviewBlocker.blocker_date <= period_end)
            .order_by(ReviewBlocker.blocker_date, ReviewBlocker.id)
        )
    ]
    return {"period_start": period_start, "period_end": period_end, "report_basis": "control", "rows": rows}


def import_manifest(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    rows = list(
        session.scalars(
            select(ImportRun)
            .where(
                ImportRun.from_date <= period_end,
                ImportRun.to_date >= period_start,
            )
            .order_by(ImportRun.started_at, ImportRun.id)
        )
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "control",
        "imports": [
            {
                "import_run_id": item.id,
                "source": item.source,
                "status": item.status,
                "started_at": item.started_at,
                "completed_at": item.completed_at,
                "source_path": item.source_path,
                "warnings": json.loads(item.warnings_json),
                "summary": json.loads(item.summary_json),
            }
            for item in rows
        ],
    }


def _account_activity_count(session: Session, *, account_code: str, period_start: date, period_end: date) -> int:
    count = session.scalar(
        select(func.count(JournalLine.id))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            Account.code == account_code,
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    )
    return int(count or 0)


def _subtype_activity_count(session: Session, *, subtype: str, period_start: date, period_end: date) -> int:
    count = session.scalar(
        select(func.count(JournalLine.id))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            Account.subtype == subtype,
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    )
    return int(count or 0)


def _reconciliation_count(session: Session, *, subtype: str, period_start: date, period_end: date) -> int:
    count = session.scalar(
        select(func.count(ReconciliationSession.id))
        .join(Account, ReconciliationSession.account_id == Account.id)
        .where(
            Account.subtype == subtype,
            ReconciliationSession.statement_start <= period_end,
            ReconciliationSession.statement_end >= period_start,
        )
    )
    return int(count or 0)


def _stripe_activity_count(session: Session, *, period_start: date, period_end: date) -> int:
    count = session.scalar(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.source_type == "stripe",
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    )
    return int(count or 0)


def _flat_document_row(payload: dict[str, object], *, packet_path: str | None = None) -> dict[str, object]:
    row = {key: value for key, value in payload.items() if key != "links"}
    if packet_path is not None:
        row["packet_path"] = packet_path
    return row


def _checklist_row(
    *,
    item_key: str,
    title: str,
    status: str,
    document_count: int,
    required_count: int,
    document_types: str,
    notes: str,
    slot_details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    row = {
        "item_key": item_key,
        "title": title,
        "status": status,
        "document_count": document_count,
        "required_count": required_count,
        "document_types": document_types,
        "notes": notes,
    }
    if slot_details is not None:
        row["slot_details"] = slot_details
    return row


def _normalized_jurisdiction(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace(" ", "_")
    aliases = {
        "il": "illinois",
        "ill": "illinois",
        "irs": "federal",
        "us": "federal",
        "usa": "federal",
    }
    return aliases.get(normalized, normalized) or None


def _document_type_for_sales_tax(kind: str, jurisdiction: str) -> str | None:
    mappings = {
        ("return", "illinois"): "illinois_sales_tax_return",
        ("payment", "illinois"): "illinois_sales_tax_payment",
    }
    return mappings.get((kind, jurisdiction))


def _estimated_tax_slots(year: int) -> list[dict[str, object]]:
    return [
        {
            "slot_id": f"estimated_q{index}_{year}",
            "title": f"Estimated Tax Q{index} {year}",
            "jurisdiction": "federal",
            "period_start": start,
            "period_end": end,
            "filing_due_date": due_date,
        }
        for index, start, end, due_date in (
            (1, date(year, 1, 1), date(year, 3, 31), date(year, 4, 15)),
            (2, date(year, 4, 1), date(year, 5, 31), date(year, 6, 15)),
            (3, date(year, 6, 1), date(year, 8, 31), date(year, 9, 15)),
            (4, date(year, 9, 1), date(year, 12, 31), date(year + 1, 1, 15)),
        )
    ]


def _next_month_anchor(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 20)
    return date(value.year, value.month + 1, 20)


def _sales_tax_return_slots(profile, year: int) -> tuple[list[dict[str, object]], bool]:
    slots: list[dict[str, object]] = []
    unsupported_cadence = False
    for registration in profile.sales_tax_registrations:
        if not registration.active:
            continue
        jurisdiction = _normalized_jurisdiction(registration.jurisdiction)
        cadence = registration.filing_cadence.strip().lower()
        if cadence not in {"monthly", "quarterly", "annual"} or jurisdiction is None:
            unsupported_cadence = True
            continue
        if cadence == "monthly":
            for month in range(1, 13):
                period_start = date(year, month, 1)
                period_end = date(year, month, monthrange(year, month)[1])
                slots.append(
                    {
                        "slot_id": f"{jurisdiction}_{year}_{month:02d}",
                        "title": f"{jurisdiction.title()} {period_start.strftime('%b %Y')}",
                        "jurisdiction": jurisdiction,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filing_due_date": _next_month_anchor(period_end),
                    }
                )
        elif cadence == "quarterly":
            quarter_ranges = (
                (date(year, 1, 1), date(year, 3, 31)),
                (date(year, 4, 1), date(year, 6, 30)),
                (date(year, 7, 1), date(year, 9, 30)),
                (date(year, 10, 1), date(year, 12, 31)),
            )
            for index, (period_start, period_end) in enumerate(quarter_ranges, start=1):
                slots.append(
                    {
                        "slot_id": f"{jurisdiction}_{year}_q{index}",
                        "title": f"{jurisdiction.title()} Q{index} {year}",
                        "jurisdiction": jurisdiction,
                        "period_start": period_start,
                        "period_end": period_end,
                        "filing_due_date": _next_month_anchor(period_end),
                    }
                )
        else:
            period_start = date(year, 1, 1)
            period_end = date(year, 12, 31)
            slots.append(
                {
                    "slot_id": f"{jurisdiction}_{year}_annual",
                    "title": f"{jurisdiction.title()} Annual {year}",
                    "jurisdiction": jurisdiction,
                    "period_start": period_start,
                    "period_end": period_end,
                    "filing_due_date": _next_month_anchor(period_end),
                }
            )
    return slots, unsupported_cadence


def _slot_match_key(*, jurisdiction: str | None, period_start: date | None, period_end: date | None) -> tuple[str | None, date | None, date | None]:
    return (jurisdiction, period_start, period_end)


def _slot_detail_status(
    *,
    slots: list[dict[str, object]],
    documents: list[dict[str, object]],
    document_type_for_slot: Callable[[dict[str, object]], str | None],
    require_jurisdiction: bool,
) -> tuple[list[dict[str, object]], int, bool]:
    expected_keys = {
        _slot_match_key(
            jurisdiction=_normalized_jurisdiction(slot.get("jurisdiction")) if require_jurisdiction else None,
            period_start=slot.get("period_start"),
            period_end=slot.get("period_end"),
        )
        for slot in slots
    }
    ambiguous = False
    matched_count = 0
    slot_details: list[dict[str, object]] = []
    for document in documents:
        normalized_jurisdiction = _normalized_jurisdiction(document.get("jurisdiction"))
        if require_jurisdiction and normalized_jurisdiction is None:
            ambiguous = True
            continue
        if document.get("period_start") is None or document.get("period_end") is None:
            ambiguous = True
            continue
        match_key = _slot_match_key(
            jurisdiction=normalized_jurisdiction if require_jurisdiction else None,
            period_start=document.get("period_start"),
            period_end=document.get("period_end"),
        )
        if require_jurisdiction and normalized_jurisdiction not in {key[0] for key in expected_keys}:
            continue
        if match_key not in expected_keys:
            ambiguous = True

    for slot in slots:
        expected_type = document_type_for_slot(slot)
        matched_documents = [
            document
            for document in documents
            if document.get("document_type") == expected_type
            and document.get("period_start") == slot["period_start"]
            and document.get("period_end") == slot["period_end"]
            and (
                not require_jurisdiction
                or _normalized_jurisdiction(document.get("jurisdiction")) == _normalized_jurisdiction(slot.get("jurisdiction"))
            )
        ]
        matched_count += len(matched_documents)
        slot_details.append(
            {
                "slot_id": slot["slot_id"],
                "title": slot["title"],
                "jurisdiction": slot.get("jurisdiction"),
                "period_start": slot["period_start"],
                "period_end": slot["period_end"],
                "filing_due_date": slot["filing_due_date"],
                "status": "present" if matched_documents else "missing",
                "document_ids": [document["document_id"] for document in matched_documents],
            }
        )
    return slot_details, matched_count, ambiguous


def _linked_reconciliation_session_ids(document: dict[str, object]) -> set[int]:
    return {
        int(link["target_id"])
        for link in document.get("links", [])
        if link.get("target_type") == "reconciliation_session"
    }


def _statement_support_checklist_row(
    *,
    coverage: dict[str, object],
    documents: list[dict[str, object]],
    subtype: str,
    item_key: str,
    title: str,
    doc_type: str,
) -> dict[str, object]:
    subtype_rows = [row for row in coverage["coverage_rows"] if row.get("account_subtype") == subtype]
    relevant_account_codes = {row["account_code"] for row in subtype_rows}
    relevant_sessions = [
        session_row
        for session_row in coverage["sessions"]
        if session_row["account_code"] in relevant_account_codes
    ]
    required_for_close = any(row["required_for_close"] for row in subtype_rows)
    supporting_document_ids: set[int] = set()
    session_details: list[dict[str, object]] = []
    for session_row in relevant_sessions:
        matching_documents = [
            document
            for document in documents
            if session_row["session_id"] in _linked_reconciliation_session_ids(document)
            and document.get("period_start") == session_row["statement_start"]
            and document.get("period_end") == session_row["statement_end"]
        ]
        supporting_document_ids.update(int(document["document_id"]) for document in matching_documents)
        session_details.append(
            {
                "session_id": session_row["session_id"],
                "account_code": session_row["account_code"],
                "statement_start": session_row["statement_start"],
                "statement_end": session_row["statement_end"],
                "status": "present" if matching_documents else "missing",
                "document_ids": [int(document["document_id"]) for document in matching_documents],
            }
        )

    if not required_for_close:
        status = "not_applicable"
    elif relevant_sessions and all(item["status"] == "present" for item in session_details):
        status = "present"
    else:
        status = "missing"

    return _checklist_row(
        item_key=item_key,
        title=title,
        status=status,
        document_count=len(supporting_document_ids),
        required_count=len(relevant_sessions) if relevant_sessions else (1 if required_for_close else 0),
        document_types=doc_type,
        notes="Statement support only counts when a statement document is linked to the relevant reconciliation session and matches that statement window.",
        slot_details=session_details or None,
    )


def document_checklist(session: Session, *, ledger_dir: Path, year: int) -> dict[str, object]:
    period_start, period_end = year_bounds(year)
    documents = list_documents(session, tax_year=year)
    payloads = [serialize_document(document) for document in documents]
    by_type: dict[str, list[dict[str, object]]] = defaultdict(list)
    for payload in payloads:
        by_type[payload["document_type"]].append(payload)

    profile = get_compliance_profile(session)
    paths = ledger_paths(ledger_dir)
    year_end_manifest = paths["exports"] / f"year-end_{year}" / "manifest.json"
    coverage = reconciliation_summary(session, period_start=period_start, period_end=period_end)

    rows: list[dict[str, object]] = [
        _checklist_row(
            item_key="year_end_books_package",
            title="Year-End Books Package",
            status="present" if year_end_manifest.exists() else "missing",
            document_count=1 if year_end_manifest.exists() else 0,
            required_count=1,
            document_types="year_end_export",
            notes="Books export is required for the accountant packet and is only present after generation.",
        )
    ]

    stripe_docs = len(by_type["stripe_1099_k"]) + len(by_type["stripe_tax_summary"])
    stripe_activity = _stripe_activity_count(session, period_start=period_start, period_end=period_end)
    rows.append(
        _checklist_row(
            item_key="stripe_tax_documents",
            title="Stripe Tax Documents",
            status="present" if stripe_docs else ("missing" if stripe_activity else "not_applicable"),
            document_count=stripe_docs,
            required_count=1 if stripe_activity else 0,
            document_types="stripe_1099_k,stripe_tax_summary",
            notes="Stripe activity is a direct source fact. If Stripe activity exists, include a Stripe 1099-K or annual Stripe tax summary.",
        )
    )

    estimated_slot_details, estimated_document_count, estimated_ambiguous = _slot_detail_status(
        slots=_estimated_tax_slots(year),
        documents=by_type["estimated_tax_confirmation"],
        document_type_for_slot=lambda _slot: "estimated_tax_confirmation",
        require_jurisdiction=False,
    )
    if estimated_ambiguous:
        estimated_status = "unknown"
    elif not profile.owner_tracking.estimated_tax_confirmations:
        estimated_status = "present" if estimated_document_count else "optional"
    else:
        estimated_status = "present" if all(slot["status"] == "present" for slot in estimated_slot_details) else "missing"
    rows.append(
        _checklist_row(
            item_key="estimated_tax_confirmations",
            title="Estimated Tax Confirmations",
            status=estimated_status,
            document_count=estimated_document_count,
            required_count=len(estimated_slot_details) if profile.owner_tracking.estimated_tax_confirmations else 0,
            document_types="estimated_tax_confirmation",
            notes="Owner-level estimate confirmations are advisory unless you explicitly choose to track them in the compliance profile.",
            slot_details=estimated_slot_details,
        )
    )

    active_regs = [registration for registration in profile.sales_tax_registrations if registration.active]
    sales_tax_slots, unsupported_sales_tax_cadence = _sales_tax_return_slots(profile, year)
    unsupported_sales_tax_documents = any(
        _document_type_for_sales_tax("return", _normalized_jurisdiction(slot.get("jurisdiction")) or "") is None
        or _document_type_for_sales_tax("payment", _normalized_jurisdiction(slot.get("jurisdiction")) or "") is None
        for slot in sales_tax_slots
    )
    sales_tax_return_docs = [
        document
        for document in payloads
        if document["document_type"] in {"illinois_sales_tax_return"}
    ]
    if not profile.sales_tax_profile_confirmed:
        sales_tax_returns_status = "unknown"
        sales_tax_return_details: list[dict[str, object]] = []
        sales_tax_return_count = 0
    elif not active_regs:
        sales_tax_returns_status = "not_applicable"
        sales_tax_return_details = []
        sales_tax_return_count = 0
    elif unsupported_sales_tax_cadence or unsupported_sales_tax_documents:
        sales_tax_returns_status = "unknown"
        sales_tax_return_details = []
        sales_tax_return_count = 0
    else:
        sales_tax_return_details, sales_tax_return_count, sales_tax_returns_ambiguous = _slot_detail_status(
            slots=sales_tax_slots,
            documents=sales_tax_return_docs,
            document_type_for_slot=lambda slot: _document_type_for_sales_tax("return", _normalized_jurisdiction(slot.get("jurisdiction")) or ""),
            require_jurisdiction=True,
        )
        if sales_tax_returns_ambiguous:
            sales_tax_returns_status = "unknown"
        else:
            sales_tax_returns_status = "present" if all(slot["status"] == "present" for slot in sales_tax_return_details) else "missing"
    rows.append(
        _checklist_row(
            item_key="sales_tax_returns",
            title="Configured Sales Tax Returns",
            status=sales_tax_returns_status,
            document_count=sales_tax_return_count,
            required_count=len(sales_tax_slots) if profile.sales_tax_profile_confirmed and active_regs and not unsupported_sales_tax_cadence else 0,
            document_types="illinois_sales_tax_return",
            notes="Sales-tax filing items stay advisory until registrations and cadence are explicitly confirmed in the compliance profile.",
            slot_details=sales_tax_return_details,
        )
    )

    sales_tax_payment_docs = [
        document
        for document in payloads
        if document["document_type"] in {"illinois_sales_tax_payment"}
    ]
    payment_slot_map = {
        (
            _normalized_jurisdiction(slot.jurisdiction),
            slot.period_start,
            slot.period_end,
            slot.filing_due_date,
        ): slot
        for slot in profile.sales_tax_payment_slots
    }
    payment_slot_details: list[dict[str, object]] = []
    payment_required_count = 0
    payment_document_count = 0
    payments_unknown = False
    payments_missing = False
    if not profile.sales_tax_profile_confirmed:
        sales_tax_payments_status = "unknown"
    elif not active_regs:
        sales_tax_payments_status = "not_applicable"
    elif unsupported_sales_tax_cadence or unsupported_sales_tax_documents:
        sales_tax_payments_status = "unknown"
    else:
        expected_slot_keys = {
            (_normalized_jurisdiction(slot.get("jurisdiction")), slot.get("period_start"), slot.get("period_end"))
            for slot in sales_tax_slots
        }
        for document in sales_tax_payment_docs:
            normalized_jurisdiction = _normalized_jurisdiction(document.get("jurisdiction"))
            if normalized_jurisdiction is None or document.get("period_start") is None or document.get("period_end") is None:
                payments_unknown = True
                continue
            if (
                normalized_jurisdiction,
                document.get("period_start"),
                document.get("period_end"),
            ) not in expected_slot_keys:
                payments_unknown = True
        for slot in sales_tax_slots:
            meta = payment_slot_map.get(
                (
                    _normalized_jurisdiction(slot.get("jurisdiction")),
                    slot["period_start"],
                    slot["period_end"],
                    slot["filing_due_date"],
                )
            )
            matching_documents = [
                document
                for document in sales_tax_payment_docs
                if document["document_type"] == _document_type_for_sales_tax("payment", _normalized_jurisdiction(slot.get("jurisdiction")) or "")
                and document.get("period_start") == slot["period_start"]
                and document.get("period_end") == slot["period_end"]
                and _normalized_jurisdiction(document.get("jurisdiction")) == _normalized_jurisdiction(slot.get("jurisdiction"))
            ]
            payment_document_count += len(matching_documents)
            if meta is None or meta.payment_expected == "unknown":
                slot_status = "unknown"
                payments_unknown = True
            elif meta.payment_expected == "false":
                slot_status = "not_applicable"
            else:
                payment_required_count += 1
                slot_status = "present" if matching_documents else "missing"
                payments_missing = payments_missing or slot_status == "missing"
            payment_slot_details.append(
                {
                    "slot_id": slot["slot_id"],
                    "title": slot["title"],
                    "jurisdiction": slot.get("jurisdiction"),
                    "period_start": slot["period_start"],
                    "period_end": slot["period_end"],
                    "filing_due_date": slot["filing_due_date"],
                    "payment_expected": None if meta is None else meta.payment_expected,
                    "status": slot_status,
                    "document_ids": [document["document_id"] for document in matching_documents],
                }
            )
        if payments_unknown:
            sales_tax_payments_status = "unknown"
        elif payments_missing:
            sales_tax_payments_status = "missing"
        else:
            sales_tax_payments_status = "present"
    rows.append(
        _checklist_row(
            item_key="sales_tax_payments",
            title="Configured Sales Tax Payments",
            status=sales_tax_payments_status,
            document_count=payment_document_count,
            required_count=payment_required_count,
            document_types="illinois_sales_tax_payment",
            notes="Sales-tax payment completeness stays unknown until explicit filing-slot payment expectations are recorded in the compliance profile.",
            slot_details=payment_slot_details,
        )
    )

    for subtype, item_key, title, doc_type in (
        ("bank", "bank_statement_support", "Bank Statement Support", "bank_statement"),
        ("card", "card_statement_support", "Card Statement Support", "card_statement"),
        ("stripe_clearing", "stripe_statement_support", "Stripe Statement Support", "stripe_statement"),
    ):
        rows.append(
            _statement_support_checklist_row(
                coverage=coverage,
                documents=by_type[doc_type],
                subtype=subtype,
                item_key=item_key,
                title=title,
                doc_type=doc_type,
            )
        )

    prior_year_return_docs = len(by_type["prior_year_return"])
    rows.append(
        _checklist_row(
            item_key="prior_year_return",
            title="Prior-Year Return",
            status="present" if prior_year_return_docs else "optional",
            document_count=prior_year_return_docs,
            required_count=0,
            document_types="prior_year_return",
            notes="Useful preparer continuity item, but not required by the ledger.",
        )
    )

    notice_docs = len(by_type["tax_notice"])
    rows.append(
        _checklist_row(
            item_key="tax_notices",
            title="IRS or Illinois Notices",
            status="present" if notice_docs else "optional",
            document_count=notice_docs,
            required_count=0,
            document_types="tax_notice",
            notes="Advisory support item for correspondence affecting the filing year.",
        )
    )

    contractor_docs = len(by_type["contractor_w9"]) + len(by_type["contractor_1099_nec"])
    if contractor_docs:
        contractor_status = "present"
        contractor_required = 0
    elif not profile.contractor_profile.confirmed:
        contractor_status = "unknown"
        contractor_required = 0
    elif profile.contractor_profile.requires_1099_nec_documents is True:
        contractor_status = "missing"
        contractor_required = 1
    elif profile.contractor_profile.requires_1099_nec_documents is False:
        contractor_status = "not_applicable"
        contractor_required = 0
    else:
        contractor_status = "unknown"
        contractor_required = 0
    rows.append(
        _checklist_row(
            item_key="contractor_documents",
            title="Contractor W-9 and 1099-NEC Items",
            status=contractor_status,
            document_count=contractor_docs,
            required_count=contractor_required,
            document_types="contractor_w9,contractor_1099_nec",
            notes="Contractor applicability remains unknown until the compliance profile explicitly confirms 1099 document expectations.",
        )
    )

    payroll_docs = len(by_type["payroll_report"]) + len(by_type["payroll_tax_form"])
    if payroll_docs:
        payroll_status = "present"
        payroll_required = 0
    elif not profile.payroll.confirmed:
        payroll_status = "unknown"
        payroll_required = 0
    elif profile.payroll.enabled is True:
        payroll_status = "missing"
        payroll_required = 1
    elif profile.payroll.enabled is False:
        payroll_status = "not_applicable"
        payroll_required = 0
    else:
        payroll_status = "unknown"
        payroll_required = 0
    rows.append(
        _checklist_row(
            item_key="payroll_documents",
            title="Payroll Reports and Forms",
            status=payroll_status,
            document_count=payroll_docs,
            required_count=payroll_required,
            document_types="payroll_report,payroll_tax_form",
            notes="Payroll checklist items are advisory and require explicit profile confirmation before becoming missing.",
        )
    )

    missing_items = [row for row in rows if row["status"] == "missing"]
    unknown_items = [row for row in rows if row["status"] == "unknown"]
    return {
        "year": year,
        "advisory": True,
        "rows": rows,
        "missing_items": missing_items,
        "unknown_items": unknown_items,
        "compliance_profile": profile.model_dump(),
        "warnings": [
            "Checklist statuses are advisory and depend on explicit compliance profile facts plus direct source-document evidence.",
        ],
    }


def _copy_tree(source_dir: Path, destination_dir: Path) -> list[str]:
    copied: list[str] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_dir)
        destination = destination_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(str(destination.relative_to(destination_dir.parent)))
    return copied


def _write_zip(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=str(path.relative_to(source_dir)))


def export_bundle(
    session: Session,
    *,
    ledger_dir: Path,
    config: AppConfig,
    period_start: date,
    period_end: date,
    name: str,
) -> dict[str, object]:
    paths = ledger_paths(ledger_dir)
    output_dir = paths["exports"] / name
    output_dir.mkdir(parents=True, exist_ok=True)

    pnl_payload = pnl(session, period_start=period_start, period_end=period_end, basis=config.default_report_basis)
    datasets = {
        "pnl": pnl_payload,
        "balance_sheet": balance_sheet(session, as_of=period_end),
        "cash_flow": cash_flow(session, period_start=period_start, period_end=period_end),
        "trial_balance": trial_balance(session, as_of=period_end),
        "general_ledger": general_ledger(session, period_start=period_start, period_end=period_end, include_line_ids=True),
        "tax_liabilities": tax_liabilities(session, as_of=period_end),
        "tax_rollforward": tax_rollforward(session, period_start=period_start, period_end=period_end),
        "equity_rollforward": equity_rollforward(session, period_start=period_start, period_end=period_end),
        "reconciliation_summary": reconciliation_summary(session, period_start=period_start, period_end=period_end),
        "review_blockers": review_blocker_summary(session, period_start=period_start, period_end=period_end),
        "import_manifest": import_manifest(session, period_start=period_start, period_end=period_end),
        "accounts": {"report_basis": "accrual", "rows": trial_balance(session, as_of=period_end)["rows"]},
    }

    files: list[str] = []
    report_metadata: dict[str, dict[str, object]] = {}
    for dataset_name, payload in datasets.items():
        report_metadata[dataset_name] = {"report_basis": payload.get("report_basis"), "advisory": False}
        json_path = output_dir / f"{dataset_name}.json"
        _write_json(json_path, payload)
        files.append(json_path.name)
        rows = payload.get("rows") or payload.get("entries") or payload.get("sessions") or payload.get("imports") or payload.get("accounts")
        if isinstance(rows, list):
            csv_path = output_dir / f"{dataset_name}.csv"
            _write_csv(csv_path, rows)
            files.append(csv_path.name)

    cash_basis_path = output_dir / "cash_basis_snapshot.json"
    _write_json(cash_basis_path, cash_basis_snapshot(session, period_start=period_start, period_end=period_end))
    files.append(cash_basis_path.name)

    manifest = ExportManifest(
        name=name,
        generated_at=utcnow(),
        files=sorted(files),
        period_start=period_start,
        period_end=period_end,
        ledger_path=ledger_dir,
    )
    manifest_path = output_dir / "manifest.json"
    manifest_payload = manifest.model_dump()
    manifest_payload["report_metadata"] = report_metadata
    manifest_payload["advisory"] = False
    _write_json(manifest_path, manifest_payload)
    files.append(manifest_path.name)
    return {"output_dir": str(output_dir), "files": sorted(files), "report_metadata": report_metadata}


def export_year_end(session: Session, *, ledger_dir: Path, config: AppConfig, year: int) -> dict[str, object]:
    start, end = year_bounds(year)
    return export_bundle(
        session,
        ledger_dir=ledger_dir,
        config=config,
        period_start=start,
        period_end=end,
        name=f"year-end_{year}",
    )


def export_accountant_packet(session: Session, *, ledger_dir: Path, config: AppConfig, year: int) -> dict[str, object]:
    paths = ledger_paths(ledger_dir)
    output_dir = paths["exports"] / f"accountant-packet_{year}"
    zip_path = paths["exports"] / f"accountant-packet_{year}.zip"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    if zip_path.exists():
        zip_path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)

    books_export = export_year_end(session, ledger_dir=ledger_dir, config=config, year=year)
    books_dir = Path(books_export["output_dir"])
    _copy_tree(books_dir, output_dir / "books" / books_dir.name)

    checklist = document_checklist(session, ledger_dir=ledger_dir, year=year)
    documents = [serialize_document(item) for item in list_documents(session, tax_year=year)]
    document_rows: list[dict[str, object]] = []
    included_documents: list[dict[str, object]] = []
    for payload in documents:
        source = ledger_dir / str(payload["stored_path"])
        if not source.exists():
            raise ValidationError(f"Document file missing from ledger: {source}")
        packet_path = Path("documents") / Path(str(payload["stored_path"])).relative_to("attachments")
        destination = output_dir / packet_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        flat = _flat_document_row(payload, packet_path=str(packet_path))
        document_rows.append(flat)
        included_documents.append({**payload, "packet_path": str(packet_path)})

    cash_snapshot = cash_basis_snapshot(session, period_start=date(year, 1, 1), period_end=date(year, 12, 31))
    compliance_profile = get_compliance_profile(session).model_dump()
    assumptions = {
        "advisory": True,
        "basis_policy": {
            "balance_sheet": "accrual",
            "pnl": config.default_report_basis,
            "cash_equivalent_policy": ["bank", "stripe_clearing", "card", "owner contribution for owner-paid non-reimbursable expenses"],
        },
        "warnings": checklist["warnings"],
        "limitations": [
            "Checklist status is not tax-law advice.",
            "Unsupported cash-basis exclusions are surfaced explicitly and are not guessed into the packet.",
        ],
        "ignored_invalid_settlement_applications": cash_snapshot.get("ignored_invalid_settlement_applications", []),
    }

    document_index_payload = {"year": year, "rows": document_rows}
    _write_json(output_dir / "document_index.json", document_index_payload)
    _write_csv(output_dir / "document_index.csv", document_rows)
    _write_json(output_dir / "checklist.json", checklist)
    _write_csv(output_dir / "checklist.csv", checklist["rows"])
    _write_json(output_dir / "missing_items.json", {"year": year, "rows": checklist["missing_items"]})
    _write_json(output_dir / "compliance_profile.json", compliance_profile)
    _write_json(output_dir / "cash_basis_snapshot.json", cash_snapshot)
    _write_json(output_dir / "assumptions.json", assumptions)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "name": f"accountant-packet_{year}",
        "generated_at": utcnow(),
        "year": year,
        "ledger_path": str(ledger_dir),
        "zip_path": str(zip_path),
        "advisory": True,
        "files": [],
        "documents": included_documents,
        "missing_items": checklist["missing_items"],
        "unknown_items": checklist["unknown_items"],
        "compliance_profile": compliance_profile,
        "cash_basis_snapshot_file": "cash_basis_snapshot.json",
        "assumptions_file": "assumptions.json",
        "ignored_invalid_settlement_applications": cash_snapshot.get("ignored_invalid_settlement_applications", []),
        "report_metadata": {
            "books": books_export.get("report_metadata", {}),
            "packet_checklist_basis": "advisory",
        },
    }
    _write_json(manifest_path, manifest)

    files = sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file())
    manifest["files"] = files
    _write_json(manifest_path, manifest)
    _write_zip(output_dir, zip_path)
    return {
        "output_dir": str(output_dir),
        "zip_path": str(zip_path),
        "files": files,
        "document_count": len(document_rows),
        "missing_items": checklist["missing_items"],
        "unknown_items": checklist["unknown_items"],
    }
