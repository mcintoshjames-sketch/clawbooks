from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from clawbooks.config import ledger_paths
from clawbooks.ledger import account_balance_as_of, display_balance
from clawbooks.models import Account, ImportRun, JournalEntry, JournalLine, ReconciliationSession, TaxObligation
from clawbooks.schemas import AppConfig, ExportManifest
from clawbooks.utils import json_dumps, utcnow, year_bounds


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        if rows:
            writer.writerows(rows)
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
    return {"as_of": as_of, "rows": rows}


def pnl(session: Session, *, period_start: date, period_end: date, basis: str) -> dict[str, object]:
    query = (
        select(JournalLine, JournalEntry, Account)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(JournalEntry.entry_date >= period_start, JournalEntry.entry_date <= period_end)
    )
    if basis == "cash":
        query = query.where(JournalEntry.cash_basis_included.is_(True))

    grouped: dict[str, dict[str, object]] = {}
    for line, entry, account in session.execute(query):
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

    rows = []
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
        rows.append({**row, "display_amount_cents": display_cents})

    return {
        "period_start": period_start,
        "period_end": period_end,
        "basis": basis,
        "rows": rows,
        "totals": {
            "revenue_cents": revenue_total,
            "contra_revenue_cents": contra_total,
            "expense_cents": expense_total,
            "net_income_cents": revenue_total - contra_total - expense_total,
        },
    }


def balance_sheet(session: Session, *, as_of: date, basis: str) -> dict[str, object]:
    tb = trial_balance(session, as_of=as_of)["rows"]
    pnl_data = pnl(session, period_start=date(as_of.year, 1, 1), period_end=as_of, basis=basis)
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

    if pnl_data["totals"]["net_income_cents"]:
        equity.append(
            {
                "code": "current_earnings",
                "name": "Current Earnings",
                "amount_cents": pnl_data["totals"]["net_income_cents"],
            }
        )

    return {
        "as_of": as_of,
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
        if any(line.account.kind == "equity" for line in entry.lines):
            section = "financing"
        elif any(line.account.kind == "asset" and line.account.subtype == "receivable" for line in entry.lines):
            section = "operating"
        else:
            section = "operating"
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
    return {"period_start": period_start, "period_end": period_end, "sections": dict(sections), "totals": totals}


def general_ledger(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
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
        "entries": [
            {
                "entry_id": entry.id,
                "entry_date": entry.entry_date,
                "description": entry.description,
                "source_type": entry.source_type,
                "review_required": entry.review_required,
                "lines": [
                    {
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


def owner_equity(session: Session, *, as_of: date) -> dict[str, object]:
    rows = []
    for code in ("3000", "3100"):
        account = session.scalar(select(Account).where(Account.code == code))
        if not account:
            continue
        raw = account_balance_as_of(session, account_id=account.id, as_of=as_of)
        rows.append({"code": account.code, "name": account.name, "amount_cents": display_balance(account, raw)})
    totals = {row["code"]: row["amount_cents"] for row in rows}
    return {"as_of": as_of, "rows": rows, "totals": totals}


def tax_rollforward(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    rows = []
    for account in session.scalars(select(Account).where(Account.subtype == "tax_liability").order_by(Account.code)):
        opening = display_balance(account, account_balance_as_of(session, account_id=account.id, as_of=period_start))
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
    return {"period_start": period_start, "period_end": period_end, "rows": rows}


def reconciliation_summary(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    sessions = list(
        session.scalars(
            select(ReconciliationSession)
            .options(selectinload(ReconciliationSession.account))
            .where(
                ReconciliationSession.statement_start >= period_start,
                ReconciliationSession.statement_end <= period_end,
            )
            .order_by(ReconciliationSession.statement_end, ReconciliationSession.id)
        )
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "sessions": [
            {
                "session_id": item.id,
                "account_code": item.account.code,
                "account_name": item.account.name,
                "statement_start": item.statement_start,
                "statement_end": item.statement_end,
                "status": item.status,
                "statement_ending_balance_cents": item.statement_ending_balance_cents,
            }
            for item in sessions
        ],
    }


def import_manifest(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    rows = list(
        session.scalars(
            select(ImportRun)
            .where(
                ImportRun.started_at >= period_start,
                ImportRun.started_at <= period_end,
            )
            .order_by(ImportRun.started_at, ImportRun.id)
        )
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
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

    datasets = {
        "pnl": pnl(session, period_start=period_start, period_end=period_end, basis=config.default_report_basis),
        "balance_sheet": balance_sheet(session, as_of=period_end, basis=config.default_report_basis),
        "cash_flow": cash_flow(session, period_start=period_start, period_end=period_end),
        "trial_balance": trial_balance(session, as_of=period_end),
        "general_ledger": general_ledger(session, period_start=period_start, period_end=period_end),
        "tax_liabilities": tax_liabilities(session, as_of=period_end),
        "tax_rollforward": tax_rollforward(session, period_start=period_start, period_end=period_end),
        "owner_equity": owner_equity(session, as_of=period_end),
        "reconciliation_summary": reconciliation_summary(session, period_start=period_start, period_end=period_end),
        "import_manifest": import_manifest(session, period_start=period_start, period_end=period_end),
        "accounts": {"rows": trial_balance(session, as_of=period_end)["rows"]},
    }

    files: list[str] = []
    for dataset_name, payload in datasets.items():
        json_path = output_dir / f"{dataset_name}.json"
        _write_json(json_path, payload)
        files.append(json_path.name)
        rows = payload.get("rows") or payload.get("entries") or payload.get("sessions") or payload.get("imports") or payload.get("accounts")
        if isinstance(rows, list):
            csv_path = output_dir / f"{dataset_name}.csv"
            _write_csv(csv_path, rows)
            files.append(csv_path.name)

    manifest = ExportManifest(
        name=name,
        generated_at=utcnow(),
        files=sorted(files),
        period_start=period_start,
        period_end=period_end,
        ledger_path=ledger_dir,
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    files.append(manifest_path.name)
    return {"output_dir": str(output_dir), "files": sorted(files)}


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
