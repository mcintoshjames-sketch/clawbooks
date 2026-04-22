from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select

from clawbooks.config import is_ledger_dir, load_config, validate_ledger_dir
from clawbooks.db import session_scope
from clawbooks.ledger import account_balance_as_of, display_balance, list_accounts
from clawbooks.models import Account, ImportRun, JournalEntry, PeriodLock, ReconciliationSession, TaxObligation
from clawbooks.reports import (
    balance_sheet,
    cash_flow,
    export_bundle,
    export_year_end,
    general_ledger,
    owner_equity,
    pnl,
    tax_liabilities,
    tax_rollforward,
    trial_balance,
)
from clawbooks.tui_models import DashboardSummary, ExportResult, HelpCommand, Metric, ReportMode, ReportView, StatusView, TableSection
from clawbooks.utils import format_money

Preset = Literal["MTD", "QTD", "YTD", "CUSTOM"]


@dataclass(slots=True, frozen=True)
class ReportDescriptor:
    key: str
    title: str
    mode: ReportMode
    default_preset: Preset


REPORTS: dict[str, ReportDescriptor] = {
    "pnl": ReportDescriptor("pnl", "Profit & Loss", "range", "MTD"),
    "balance_sheet": ReportDescriptor("balance_sheet", "Balance Sheet", "as_of", "CUSTOM"),
    "cash_flow": ReportDescriptor("cash_flow", "Cash Flow", "range", "MTD"),
    "trial_balance": ReportDescriptor("trial_balance", "Trial Balance", "as_of", "CUSTOM"),
    "general_ledger": ReportDescriptor("general_ledger", "General Ledger", "range", "MTD"),
    "tax_liabilities": ReportDescriptor("tax_liabilities", "Tax Liabilities", "as_of", "CUSTOM"),
    "owner_equity": ReportDescriptor("owner_equity", "Owner Equity", "as_of", "CUSTOM"),
    "tax_rollforward": ReportDescriptor("tax_rollforward", "Tax Rollforward", "range", "QTD"),
}


def quarter_start(value: date) -> date:
    month = ((value.month - 1) // 3) * 3 + 1
    return date(value.year, month, 1)


def preset_window(preset: Preset, today: date) -> tuple[date, date]:
    if preset == "MTD":
        return today.replace(day=1), today
    if preset == "QTD":
        return quarter_start(today), today
    if preset == "YTD":
        return date(today.year, 1, 1), today
    return today, today


def latest_period_event_label(lock: PeriodLock | None) -> str:
    if not lock:
        return "No period events"
    return f"{lock.action.title()} {lock.period_start.isoformat()} to {lock.period_end.isoformat()}"


class TuiFacade:
    def __init__(self, ledger_dir: Path) -> None:
        self.ledger_dir = validate_ledger_dir(ledger_dir)
        self.config = load_config(self.ledger_dir)

    @property
    def business_name(self) -> str:
        return self.config.business_name

    @staticmethod
    def is_ledger_dir(path: Path) -> bool:
        return is_ledger_dir(path)

    def report_defaults(self, report_key: str, *, today: date | None = None) -> dict[str, object]:
        descriptor = REPORTS[report_key]
        today = today or date.today()
        if descriptor.mode == "range":
            start, end = preset_window(descriptor.default_preset, today)
            return {"mode": descriptor.mode, "preset": descriptor.default_preset, "start": start, "end": end, "as_of": None}
        return {"mode": descriptor.mode, "preset": "CUSTOM", "start": None, "end": None, "as_of": today}

    def dashboard(self, *, as_of: date | None = None) -> DashboardSummary:
        as_of = as_of or date.today()
        with session_scope(self.ledger_dir) as session:
            ytd = pnl(session, period_start=date(as_of.year, 1, 1), period_end=as_of, basis=self.config.default_report_basis)
            pending_obligations = session.scalar(
                select(func.count(TaxObligation.id)).where(TaxObligation.status != "completed")
            ) or 0
            open_reconciliations = session.scalar(
                select(func.count(ReconciliationSession.id)).where(ReconciliationSession.status != "closed")
            ) or 0
            review_required = session.scalar(
                select(func.count(JournalEntry.id)).where(
                    JournalEntry.review_required.is_(True),
                    JournalEntry.review_acknowledged_at.is_(None),
                )
            ) or 0
            latest_event = session.scalar(select(PeriodLock).order_by(PeriodLock.created_at.desc(), PeriodLock.id.desc()))

            balance_rows = []
            for code in ("1000", "1010", "2000", "2100"):
                account = session.scalar(select(Account).where(Account.code == code))
                if not account:
                    continue
                raw_balance = account_balance_as_of(session, account_id=account.id, as_of=as_of)
                balance_rows.append(
                    {
                        "code": account.code,
                        "name": account.name,
                        "balance_cents": display_balance(account, raw_balance),
                    }
                )

        alerts = []
        if review_required:
            alerts.append(f"{review_required} review-required journal entr{'y' if review_required == 1 else 'ies'} need attention.")

        return DashboardSummary(
            business_name=self.business_name,
            ledger_dir=self.ledger_dir,
            as_of=as_of,
            metrics=[
                Metric("YTD Net Income", format_money(ytd["totals"]["net_income_cents"])),
                Metric("Pending Tax Obligations", str(int(pending_obligations))),
                Metric("Open Reconciliations", str(int(open_reconciliations))),
                Metric("Latest Period Event", latest_period_event_label(latest_event), tone="warning" if latest_event else "default"),
            ],
            sections=[
                TableSection(
                    title="Key Balances",
                    columns=["code", "name", "balance_cents"],
                    rows=balance_rows,
                    empty_message="No key balances available.",
                )
            ],
            alerts=alerts,
        )

    def report(
        self,
        report_key: str,
        *,
        preset: Preset | None = None,
        start: date | None = None,
        end: date | None = None,
        as_of: date | None = None,
    ) -> ReportView:
        descriptor = REPORTS[report_key]
        today = date.today()
        if descriptor.mode == "range":
            if preset and preset != "CUSTOM":
                start, end = preset_window(preset, today)
            else:
                defaults = self.report_defaults(report_key, today=today)
                start = start or defaults["start"]
                end = end or defaults["end"]
        else:
            as_of = as_of or today

        with session_scope(self.ledger_dir) as session:
            if report_key == "pnl":
                payload = pnl(session, period_start=start, period_end=end, basis=self.config.default_report_basis)
            elif report_key == "balance_sheet":
                payload = balance_sheet(session, as_of=as_of, basis=self.config.default_report_basis)
            elif report_key == "cash_flow":
                payload = cash_flow(session, period_start=start, period_end=end)
            elif report_key == "trial_balance":
                payload = trial_balance(session, as_of=as_of)
            elif report_key == "general_ledger":
                payload = general_ledger(session, period_start=start, period_end=end)
            elif report_key == "tax_liabilities":
                payload = tax_liabilities(session, as_of=as_of)
            elif report_key == "owner_equity":
                payload = owner_equity(session, as_of=as_of)
            else:
                payload = tax_rollforward(session, period_start=start, period_end=end)

        return self._normalize_report(descriptor, payload, start=start, end=end, as_of=as_of)

    def status(self, *, as_of: date | None = None) -> StatusView:
        as_of = as_of or date.today()
        with session_scope(self.ledger_dir) as session:
            accounts_rows = [
                {
                    "code": account.code,
                    "name": account.name,
                    "kind": account.kind,
                    "subtype": account.subtype,
                    "is_active": account.is_active,
                }
                for account in list_accounts(session, include_inactive=True)
            ]
            obligations_rows = [
                {
                    "code": item.code,
                    "description": item.description,
                    "jurisdiction": item.jurisdiction,
                    "due_date": item.due_date,
                    "status": item.status,
                }
                for item in session.scalars(select(TaxObligation).order_by(TaxObligation.due_date, TaxObligation.code))
            ]
            reconciliation_rows = [
                {
                    "session_id": item.id,
                    "account_id": item.account_id,
                    "statement_start": item.statement_start,
                    "statement_end": item.statement_end,
                    "status": item.status,
                }
                for item in session.scalars(select(ReconciliationSession).order_by(ReconciliationSession.id.desc()).limit(20))
            ]
            import_rows = [
                {
                    "import_run_id": item.id,
                    "source": item.source,
                    "status": item.status,
                    "started_at": item.started_at,
                    "source_path": item.source_path,
                }
                for item in session.scalars(select(ImportRun).order_by(ImportRun.started_at.desc(), ImportRun.id.desc()).limit(20))
            ]
            review_rows = [
                {
                    "entry_id": item.id,
                    "entry_date": item.entry_date,
                    "description": item.description,
                    "source_type": item.source_type,
                    "review_message": item.review_message or "",
                }
                for item in session.scalars(
                    select(JournalEntry)
                    .where(JournalEntry.review_required.is_(True), JournalEntry.review_acknowledged_at.is_(None))
                    .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
                    .limit(20)
                )
            ]
            period_rows = [
                {
                    "action": item.action,
                    "lock_type": item.lock_type,
                    "period_start": item.period_start,
                    "period_end": item.period_end,
                    "reason": item.reason or "",
                    "created_at": item.created_at,
                }
                for item in session.scalars(select(PeriodLock).order_by(PeriodLock.created_at.desc(), PeriodLock.id.desc()).limit(20))
            ]

        return StatusView(
            as_of=as_of,
            sections=[
                TableSection("Chart of Accounts", ["code", "name", "kind", "subtype", "is_active"], accounts_rows, "No accounts found."),
                TableSection("Tax Obligations", ["code", "description", "jurisdiction", "due_date", "status"], obligations_rows, "No tax obligations found."),
                TableSection(
                    "Reconciliation Sessions",
                    ["session_id", "account_id", "statement_start", "statement_end", "status"],
                    reconciliation_rows,
                    "No reconciliation sessions found.",
                ),
                TableSection("Import History", ["import_run_id", "source", "status", "started_at", "source_path"], import_rows, "No imports recorded."),
                TableSection(
                    "Review-Required Entries",
                    ["entry_id", "entry_date", "description", "source_type", "review_message"],
                    review_rows,
                    "No review-required entries.",
                ),
                TableSection(
                    "Period Events",
                    ["action", "lock_type", "period_start", "period_end", "reason", "created_at"],
                    period_rows,
                    "No period events.",
                ),
            ],
        )

    def help_commands(self) -> list[HelpCommand]:
        ledger = str(self.ledger_dir)
        prefix = f"uv run clawbooks --ledger '{ledger}' --json"
        return [
            HelpCommand("Record expense", "Use the CLI for new manual expenses.", f"{prefix} expense record --date YYYY-MM-DD --vendor 'Vendor' --amount 0.00 --category 5199 --payment-account 1000"),
            HelpCommand("Import Stripe", "Stripe imports stay in the CLI in v1.", f"{prefix} import stripe --from-date YYYY-MM-DD --to-date YYYY-MM-DD --dry-run"),
            HelpCommand("Import CSV", "CSV imports stay in the CLI in v1.", f"{prefix} import csv --account-code 1000 --csv-path /path/to/file.csv --profile-path /path/to/profile.json --dry-run"),
            HelpCommand("Reconcile", "Reconciliation stays in the CLI in v1.", f"{prefix} reconcile start --account-code 1000 --statement-path /path/to/statement.csv --statement-start YYYY-MM-DD --statement-end YYYY-MM-DD --statement-ending-balance 0.00"),
            HelpCommand("Close period", "Period close stays in the CLI in v1.", f"{prefix} period close --period-start YYYY-MM-DD --period-end YYYY-MM-DD"),
            HelpCommand("Reopen period", "Period reopen stays in the CLI in v1.", f"{prefix} period reopen --period-start YYYY-MM-DD --period-end YYYY-MM-DD --reason 'Reason'"),
        ]

    def export_period_end(self, *, start: date, end: date) -> ExportResult:
        with session_scope(self.ledger_dir) as session:
            payload = export_bundle(
                session,
                ledger_dir=self.ledger_dir,
                config=self.config,
                period_start=start,
                period_end=end,
                name=f"period-end_{start.isoformat()}_{end.isoformat()}",
            )
        return ExportResult(title="Period-End Export", output_dir=Path(payload["output_dir"]), files=list(payload["files"]))

    def export_year_end(self, *, year: int) -> ExportResult:
        with session_scope(self.ledger_dir) as session:
            payload = export_year_end(session, ledger_dir=self.ledger_dir, config=self.config, year=year)
        return ExportResult(title="Year-End Export", output_dir=Path(payload["output_dir"]), files=list(payload["files"]))

    def _normalize_report(
        self,
        descriptor: ReportDescriptor,
        payload: dict[str, object],
        *,
        start: date | None,
        end: date | None,
        as_of: date | None,
    ) -> ReportView:
        if descriptor.key == "pnl":
            metrics = [
                Metric("Revenue", format_money(payload["totals"]["revenue_cents"])),
                Metric("Expenses", format_money(payload["totals"]["expense_cents"])),
                Metric("Net Income", format_money(payload["totals"]["net_income_cents"])),
            ]
            sections = [
                TableSection("Accounts", ["code", "name", "kind", "display_amount_cents"], payload["rows"], "No profit and loss activity.")
            ]
        elif descriptor.key == "balance_sheet":
            metrics = [
                Metric("Assets", format_money(payload["totals"]["assets_cents"])),
                Metric("Liabilities", format_money(payload["totals"]["liabilities_cents"])),
                Metric("Equity", format_money(payload["totals"]["equity_cents"])),
            ]
            sections = [
                TableSection("Assets", ["code", "name", "amount_cents"], payload["assets"], "No assets."),
                TableSection("Liabilities", ["code", "name", "amount_cents"], payload["liabilities"], "No liabilities."),
                TableSection("Equity", ["code", "name", "amount_cents"], payload["equity"], "No equity balances."),
            ]
        elif descriptor.key == "cash_flow":
            totals = payload["totals"]
            metrics = [
                Metric("Operating", format_money(totals.get("operating", 0))),
                Metric("Financing", format_money(totals.get("financing", 0))),
                Metric("Net Change", format_money(totals["net_change_cents"])),
            ]
            sections = [
                TableSection(section.replace("_", " ").title(), ["entry_id", "entry_date", "description", "cash_change_cents"], rows, f"No {section.replace('_', ' ')} cash activity.")
                for section, rows in payload["sections"].items()
            ] or [TableSection("Cash Flow", ["entry_id", "entry_date", "description", "cash_change_cents"], [], "No cash activity.")]
        elif descriptor.key == "trial_balance":
            metrics = [Metric("Accounts", str(len(payload["rows"])))]
            sections = [
                TableSection(
                    "Accounts",
                    ["code", "name", "kind", "subtype", "display_balance_cents"],
                    payload["rows"],
                    "No balances available.",
                )
            ]
        elif descriptor.key == "general_ledger":
            rows = [
                {
                    "entry_id": entry["entry_id"],
                    "entry_date": entry["entry_date"],
                    "source_type": entry["source_type"],
                    "review_required": entry["review_required"],
                    "description": entry["description"],
                    "lines": "; ".join(
                        f"{line['account_code']} {format_money(int(line['amount_cents']))}"
                        for line in entry["lines"]
                    ),
                }
                for entry in payload["entries"]
            ]
            metrics = [Metric("Entries", str(len(rows)))]
            sections = [
                TableSection(
                    "Journal Entries",
                    ["entry_id", "entry_date", "source_type", "review_required", "description", "lines"],
                    rows,
                    "No journal activity.",
                )
            ]
        elif descriptor.key == "tax_liabilities":
            metrics = [
                Metric("Liability Accounts", str(len(payload["accounts"]))),
                Metric("Obligations", str(len(payload["obligations"]))),
            ]
            sections = [
                TableSection("Liability Accounts", ["code", "name", "balance_cents"], payload["accounts"], "No tax liability balances."),
                TableSection(
                    "Obligations",
                    ["code", "description", "jurisdiction", "due_date", "status", "amount_cents"],
                    payload["obligations"],
                    "No obligations due.",
                ),
            ]
        elif descriptor.key == "owner_equity":
            metrics = [Metric(row["name"], format_money(int(row["amount_cents"]))) for row in payload["rows"]]
            sections = [TableSection("Owner Equity", ["code", "name", "amount_cents"], payload["rows"], "No owner equity activity.")]
        else:
            metrics = [Metric("Accounts", str(len(payload["rows"])))]
            sections = [
                TableSection(
                    "Rollforward",
                    ["code", "name", "opening_cents", "activity_cents", "closing_cents"],
                    payload["rows"],
                    "No tax rollforward activity.",
                )
            ]

        return ReportView(
            key=descriptor.key,
            title=descriptor.title,
            mode=descriptor.mode,
            start=start,
            end=end,
            as_of=as_of,
            metrics=metrics,
            sections=sections,
        )
