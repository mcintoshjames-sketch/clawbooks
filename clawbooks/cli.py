from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import typer
from rich.console import Console
from rich.table import Table

from clawbooks.config import ledger_paths, load_config, write_default_config
from clawbooks.db import create_schema, session_scope
from clawbooks.exceptions import AppError, ValidationError
from clawbooks.ledger import (
    JournalLineInput,
    add_account,
    close_period,
    close_reconciliation,
    deactivate_account,
    import_csv,
    import_stripe,
    infer_kind_from_subtype,
    list_accounts,
    list_reconciliation_sessions,
    match_reconciliation,
    period_status,
    post_journal_entry,
    record_expense,
    reopen_period,
    reverse_entry,
    seed_defaults,
    start_reconciliation,
)
from clawbooks.reports import (
    balance_sheet,
    cash_flow,
    export_bundle,
    export_year_end,
    general_ledger,
    owner_equity,
    pnl,
    reconciliation_summary,
    tax_liabilities,
    tax_rollforward,
    trial_balance,
)
from clawbooks.schemas import ResultEnvelope
from clawbooks.utils import format_money, parse_date, parse_money

app = typer.Typer(no_args_is_help=True, add_completion=False)
coa_app = typer.Typer(no_args_is_help=True)
account_app = typer.Typer(no_args_is_help=True)
expense_app = typer.Typer(no_args_is_help=True)
journal_app = typer.Typer(no_args_is_help=True)
import_app = typer.Typer(no_args_is_help=True)
reconcile_app = typer.Typer(no_args_is_help=True)
report_app = typer.Typer(no_args_is_help=True)
tax_app = typer.Typer(no_args_is_help=True)
period_app = typer.Typer(no_args_is_help=True)
export_app = typer.Typer(no_args_is_help=True)

app.add_typer(coa_app, name="coa")
app.add_typer(account_app, name="account")
app.add_typer(expense_app, name="expense")
app.add_typer(journal_app, name="journal")
app.add_typer(import_app, name="import")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(report_app, name="report")
app.add_typer(tax_app, name="tax")
app.add_typer(period_app, name="period")
app.add_typer(export_app, name="export")

console = Console()


@dataclass(slots=True)
class CLIState:
    ledger_dir: Path
    json_mode: bool
    as_of: date | None


@app.callback()
def main(
    ctx: typer.Context,
    ledger: Path = typer.Option(Path("."), "--ledger", help="Ledger directory"),
    json_mode: bool = typer.Option(False, "--json", help="Emit machine-readable JSON output"),
    as_of: str | None = typer.Option(None, "--as-of", help="Default as-of date"),
) -> None:
    ctx.obj = CLIState(ledger_dir=ledger.resolve(), json_mode=json_mode, as_of=parse_date(as_of))


def _normalize_value(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    return str(value)


def _render_table(title: str, rows: list[dict[str, object]]) -> None:
    table = Table(title=title)
    if not rows:
        console.print(f"{title}: no rows")
        return
    columns = list(rows[0].keys())
    for column in columns:
        table.add_column(column)
    for row in rows:
        rendered = []
        for column in columns:
            value = row[column]
            if column.endswith("_cents") and isinstance(value, int):
                rendered.append(format_money(value))
            else:
                rendered.append(_normalize_value(value))
        table.add_row(*rendered)
    console.print(table)


def _render_human(command: str, data: dict[str, Any], warnings: list[str]) -> None:
    if warnings:
        for warning in warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")

    if "rows" in data and isinstance(data["rows"], list):
        _render_table(command, data["rows"])
        return
    if "accounts" in data and isinstance(data["accounts"], list):
        _render_table(command, data["accounts"])
        return
    if "obligations" in data and isinstance(data["obligations"], list):
        _render_table(f"{command} obligations", data["obligations"])
        if data.get("accounts"):
            _render_table(f"{command} accounts", data["accounts"])
        return
    if "entries" in data and isinstance(data["entries"], list):
        rows = [
            {"entry_id": entry["entry_id"], "entry_date": entry["entry_date"], "description": entry["description"]}
            for entry in data["entries"]
        ]
        _render_table(command, rows)
        return
    if "sessions" in data and isinstance(data["sessions"], list):
        _render_table(command, data["sessions"])
        return
    if "imports" in data and isinstance(data["imports"], list):
        _render_table(command, data["imports"])
        return
    if "events" in data and isinstance(data["events"], list):
        _render_table(command, data["events"])
        return
    console.print_json(data=ResultEnvelope(ok=True, command=command, data=data, warnings=warnings).model_dump_json())


def _emit_success(state: CLIState, command: str, data: dict[str, Any], warnings: list[str] | None = None) -> None:
    warnings = warnings or []
    envelope = ResultEnvelope(ok=True, command=command, data=data, warnings=warnings, errors=[])
    if state.json_mode:
        typer.echo(envelope.model_dump_json(indent=2))
    else:
        _render_human(command, data, warnings)
    raise typer.Exit(code=0)


def _emit_error(state: CLIState, command: str, exc: AppError) -> None:
    envelope = ResultEnvelope(ok=False, command=command, data=exc.data, warnings=[], errors=[exc.message])
    if state.json_mode:
        typer.echo(envelope.model_dump_json(indent=2))
    else:
        console.print(f"[red]error:[/red] {exc.message}")
        if exc.data:
            console.print_json(data=envelope.model_dump_json())
    raise typer.Exit(code=exc.exit_code)


def _run_session_command(
    ctx: typer.Context,
    command: str,
    action: Callable[..., dict[str, Any] | tuple[dict[str, Any], list[str]]],
    *,
    dry_run: bool = False,
) -> None:
    state: CLIState = ctx.obj
    try:
        with session_scope(state.ledger_dir) as session:
            result = action(session)
            if isinstance(result, tuple):
                data, warnings = result
            else:
                data, warnings = result, []
            if dry_run:
                session.rollback()
            else:
                session.commit()
    except AppError as exc:
        _emit_error(state, command, exc)
    _emit_success(state, command, data, warnings)


def _require_as_of(state: CLIState, override: str | None) -> date:
    value = parse_date(override) or state.as_of or date.today()
    return value


def _parse_line_specs(line_specs: list[str]) -> list[JournalLineInput]:
    parsed: list[JournalLineInput] = []
    for raw in line_specs:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise ValidationError(f"Invalid --line specification: {raw}")
        memo = parts[2] if len(parts) == 3 else None
        parsed.append(JournalLineInput(parts[0], parse_money(parts[1]), memo))
    return parsed


@app.command("init")
def init_command(
    ctx: typer.Context,
    business_name: str = typer.Option(..., "--business-name"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config if present"),
) -> None:
    state: CLIState = ctx.obj
    try:
        paths = ledger_paths(state.ledger_dir)
        paths["root"].mkdir(parents=True, exist_ok=True)
        paths["imports"].mkdir(parents=True, exist_ok=True)
        paths["exports"].mkdir(parents=True, exist_ok=True)
        paths["attachments"].mkdir(parents=True, exist_ok=True)
        if paths["config"].exists() and not force:
            raise ValidationError(f"Ledger already initialized at {state.ledger_dir}. Use --force to overwrite config.")
        write_default_config(paths["config"], business_name)
        create_schema(state.ledger_dir)
        with session_scope(state.ledger_dir) as session:
            seed_defaults(session, year=date.today().year)
            session.commit()
    except AppError as exc:
        _emit_error(state, "init", exc)
    _emit_success(
        state,
        "init",
        {
            "ledger_dir": str(state.ledger_dir),
            "db_path": str(paths["db"]),
            "config_path": str(paths["config"]),
        },
    )


@coa_app.command("show")
def coa_show(ctx: typer.Context, all_accounts: bool = typer.Option(False, "--all")) -> None:
    _run_session_command(
        ctx,
        "coa show",
        lambda session: {
            "rows": [
                {
                    "code": account.code,
                    "name": account.name,
                    "kind": account.kind,
                    "subtype": account.subtype,
                    "currency": account.currency,
                    "is_active": account.is_active,
                }
                for account in list_accounts(session, include_inactive=all_accounts)
            ]
        },
    )


@coa_app.command("add")
def coa_add(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    name: str = typer.Option(..., "--name"),
    kind: str = typer.Option(..., "--kind"),
    subtype: str = typer.Option("other", "--subtype"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "coa add",
        lambda session: {
            "account": {
                "code": add_account(session, code=code, name=name, kind=kind, subtype=subtype).code,
                "name": name,
                "kind": kind,
                "subtype": subtype,
                "dry_run": dry_run,
            }
        },
        dry_run=dry_run,
    )


@coa_app.command("deactivate")
def coa_deactivate(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "coa deactivate",
        lambda session: {"account": {"code": deactivate_account(session, code).code, "is_active": False}},
        dry_run=dry_run,
    )


@account_app.command("list")
def account_list(ctx: typer.Context, all_accounts: bool = typer.Option(False, "--all")) -> None:
    coa_show(ctx, all_accounts=all_accounts)


@account_app.command("open")
def account_open(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    name: str = typer.Option(..., "--name"),
    subtype: str = typer.Option(..., "--subtype"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    kind = infer_kind_from_subtype(subtype)
    _run_session_command(
        ctx,
        "account open",
        lambda session: {
            "account": {
                "code": add_account(session, code=code, name=name, kind=kind, subtype=subtype).code,
                "name": name,
                "kind": kind,
                "subtype": subtype,
            }
        },
        dry_run=dry_run,
    )


@account_app.command("deactivate")
def account_deactivate(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    coa_deactivate(ctx, code=code, dry_run=dry_run)


@expense_app.command("record")
def expense_record(
    ctx: typer.Context,
    entry_date: str = typer.Option(..., "--date"),
    vendor: str = typer.Option(..., "--vendor"),
    amount: str = typer.Option(..., "--amount"),
    category: str = typer.Option(..., "--category"),
    payment_account: str | None = typer.Option(None, "--payment-account"),
    memo: str | None = typer.Option(None, "--memo"),
    receipt_path: Path | None = typer.Option(None, "--receipt-path"),
    paid_personally: bool = typer.Option(False, "--paid-personally"),
    reimbursement: bool = typer.Option(False, "--reimbursement"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "expense record",
        lambda session: record_expense(
            session,
            entry_date=parse_date(entry_date),
            vendor=vendor,
            amount=amount,
            category_code=category,
            payment_account_code=payment_account,
            memo=memo,
            receipt_path=receipt_path,
            paid_personally=paid_personally,
            reimbursement=reimbursement,
            dry_run=dry_run,
        ),
        dry_run=dry_run,
    )


@journal_app.command("add")
def journal_add(
    ctx: typer.Context,
    entry_date: str = typer.Option(..., "--date"),
    description: str = typer.Option(..., "--description"),
    line: list[str] = typer.Option(..., "--line"),
    non_cash: bool = typer.Option(False, "--non-cash"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    lines = _parse_line_specs(line)
    _run_session_command(
        ctx,
        "journal add",
        lambda session: {
            "entry": {
                "entry_id": post_journal_entry(
                    session,
                    entry_date=parse_date(entry_date),
                    description=description,
                    lines=lines,
                    source_type="manual",
                    cash_basis_included=not non_cash,
                ).id,
                "cash_basis_included": not non_cash,
            }
        },
        dry_run=dry_run,
    )


@journal_app.command("reverse")
def journal_reverse(
    ctx: typer.Context,
    entry_id: int = typer.Option(..., "--entry-id"),
    reversal_date: str = typer.Option(..., "--date"),
    reason: str = typer.Option(..., "--reason"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "journal reverse",
        lambda session: reverse_entry(
            session,
            entry_id=entry_id,
            reversal_date=parse_date(reversal_date),
            reason=reason,
        ),
        dry_run=dry_run,
    )


@import_app.command("stripe")
def import_stripe_command(
    ctx: typer.Context,
    from_date: str = typer.Option(..., "--from-date"),
    to_date: str = typer.Option(..., "--to-date"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    state: CLIState = ctx.obj
    config = load_config(state.ledger_dir)
    _run_session_command(
        ctx,
        "import stripe",
        lambda session: import_stripe(
            session,
            config=config,
            start=parse_date(from_date),
            end=parse_date(to_date),
            dry_run=dry_run,
        ),
        dry_run=dry_run,
    )


@import_app.command("csv")
def import_csv_command(
    ctx: typer.Context,
    account_code: str = typer.Option(..., "--account-code"),
    csv_path: Path = typer.Option(..., "--csv-path"),
    profile_path: Path = typer.Option(..., "--profile-path"),
    statement_ending_balance: str | None = typer.Option(None, "--statement-ending-balance"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "import csv",
        lambda session: import_csv(
            session,
            account_code=account_code,
            csv_path=csv_path,
            profile_path=profile_path,
            statement_ending_balance=statement_ending_balance,
            dry_run=dry_run,
        ),
        dry_run=dry_run,
    )


@reconcile_app.command("start")
def reconcile_start(
    ctx: typer.Context,
    account_code: str = typer.Option(..., "--account-code"),
    statement_path: Path = typer.Option(..., "--statement-path"),
    statement_start: str = typer.Option(..., "--statement-start"),
    statement_end: str = typer.Option(..., "--statement-end"),
    statement_ending_balance: str = typer.Option(..., "--statement-ending-balance"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "reconcile start",
        lambda session: start_reconciliation(
            session,
            account_code=account_code,
            statement_path=statement_path,
            statement_start=parse_date(statement_start),
            statement_end=parse_date(statement_end),
            statement_ending_balance=statement_ending_balance,
        ),
        dry_run=dry_run,
    )


@reconcile_app.command("match")
def reconcile_match(
    ctx: typer.Context,
    session_id: int = typer.Option(..., "--session-id"),
    line_id: int = typer.Option(..., "--line-id"),
    entry_id: int = typer.Option(..., "--entry-id"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "reconcile match",
        lambda session: match_reconciliation(session, session_id=session_id, line_id=line_id, entry_id=entry_id),
        dry_run=dry_run,
    )


@reconcile_app.command("close")
def reconcile_close(
    ctx: typer.Context,
    session_id: int = typer.Option(..., "--session-id"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "reconcile close",
        lambda session: close_reconciliation(session, session_id=session_id),
        dry_run=dry_run,
    )


@reconcile_app.command("list")
def reconcile_list(ctx: typer.Context) -> None:
    _run_session_command(
        ctx,
        "reconcile list",
        lambda session: {
            "sessions": [
                {
                    "session_id": item.id,
                    "account_id": item.account_id,
                    "statement_start": item.statement_start,
                    "statement_end": item.statement_end,
                    "status": item.status,
                }
                for item in list_reconciliation_sessions(session)
            ]
        },
    )


@report_app.command("pnl")
def report_pnl(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
    basis: str | None = typer.Option(None, "--basis"),
) -> None:
    state: CLIState = ctx.obj
    config = load_config(state.ledger_dir)
    _run_session_command(
        ctx,
        "report pnl",
        lambda session: pnl(
            session,
            period_start=parse_date(period_start),
            period_end=parse_date(period_end),
            basis=basis or config.default_report_basis,
        ),
    )


@report_app.command("balance-sheet")
def report_balance_sheet(ctx: typer.Context, as_of: str | None = typer.Option(None, "--as-of")) -> None:
    state: CLIState = ctx.obj
    config = load_config(state.ledger_dir)
    _run_session_command(
        ctx,
        "report balance-sheet",
        lambda session: balance_sheet(session, as_of=_require_as_of(state, as_of), basis=config.default_report_basis),
    )


@report_app.command("cash-flow")
def report_cash_flow(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
) -> None:
    _run_session_command(
        ctx,
        "report cash-flow",
        lambda session: cash_flow(session, period_start=parse_date(period_start), period_end=parse_date(period_end)),
    )


@report_app.command("general-ledger")
def report_general_ledger(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
) -> None:
    _run_session_command(
        ctx,
        "report general-ledger",
        lambda session: general_ledger(session, period_start=parse_date(period_start), period_end=parse_date(period_end)),
    )


@report_app.command("trial-balance")
def report_trial_balance(ctx: typer.Context, as_of: str | None = typer.Option(None, "--as-of")) -> None:
    state: CLIState = ctx.obj
    _run_session_command(
        ctx,
        "report trial-balance",
        lambda session: trial_balance(session, as_of=_require_as_of(state, as_of)),
    )


@report_app.command("tax-liabilities")
def report_tax_liabilities(ctx: typer.Context, as_of: str | None = typer.Option(None, "--as-of")) -> None:
    state: CLIState = ctx.obj
    _run_session_command(
        ctx,
        "report tax-liabilities",
        lambda session: tax_liabilities(session, as_of=_require_as_of(state, as_of)),
    )


@report_app.command("owner-equity")
def report_owner_equity(ctx: typer.Context, as_of: str | None = typer.Option(None, "--as-of")) -> None:
    state: CLIState = ctx.obj
    _run_session_command(
        ctx,
        "report owner-equity",
        lambda session: owner_equity(session, as_of=_require_as_of(state, as_of)),
    )


@tax_app.command("obligations")
def tax_obligations(ctx: typer.Context, as_of: str | None = typer.Option(None, "--as-of")) -> None:
    report_tax_liabilities(ctx, as_of=as_of)


@tax_app.command("rollforward")
def tax_rollforward_command(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
) -> None:
    _run_session_command(
        ctx,
        "tax rollforward",
        lambda session: tax_rollforward(session, period_start=parse_date(period_start), period_end=parse_date(period_end)),
    )


@period_app.command("close")
def period_close(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
    lock_type: str = typer.Option("month", "--lock-type"),
    reason: str | None = typer.Option(None, "--reason"),
    acknowledge_review_entry: list[int] = typer.Option([], "--acknowledge-review-entry"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "period close",
        lambda session: close_period(
            session,
            period_start=parse_date(period_start),
            period_end=parse_date(period_end),
            lock_type=lock_type,
            reason=reason,
            acknowledge_review_ids=acknowledge_review_entry,
        ),
        dry_run=dry_run,
    )


@period_app.command("reopen")
def period_reopen(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
    reason: str = typer.Option(..., "--reason"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    _run_session_command(
        ctx,
        "period reopen",
        lambda session: reopen_period(
            session,
            period_start=parse_date(period_start),
            period_end=parse_date(period_end),
            reason=reason,
        ),
        dry_run=dry_run,
    )


@period_app.command("status")
def period_status_command(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
) -> None:
    _run_session_command(
        ctx,
        "period status",
        lambda session: period_status(session, period_start=parse_date(period_start), period_end=parse_date(period_end)),
    )


@export_app.command("period-end")
def export_period_end(
    ctx: typer.Context,
    period_start: str = typer.Option(..., "--period-start"),
    period_end: str = typer.Option(..., "--period-end"),
) -> None:
    state: CLIState = ctx.obj
    config = load_config(state.ledger_dir)
    _run_session_command(
        ctx,
        "export period-end",
        lambda session: export_bundle(
            session,
            ledger_dir=state.ledger_dir,
            config=config,
            period_start=parse_date(period_start),
            period_end=parse_date(period_end),
            name=f"period-end_{period_start}_{period_end}",
        ),
    )


@export_app.command("year-end")
def export_year_end_command(ctx: typer.Context, year: int = typer.Option(..., "--year")) -> None:
    state: CLIState = ctx.obj
    config = load_config(state.ledger_dir)
    _run_session_command(
        ctx,
        "export year-end",
        lambda session: export_year_end(session, ledger_dir=state.ledger_dir, config=config, year=year),
    )
