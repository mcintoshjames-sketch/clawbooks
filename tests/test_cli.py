from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from clawbooks.cli import app
from clawbooks.db import session_scope
from clawbooks.models import (
    ExternalEvent,
    ExternalEventRefreshHistory,
    JournalEntry,
    JournalLine,
    ReconciliationLine,
    ReconciliationSession,
    SettlementApplication,
)
from clawbooks.schemas import StripeEvent, StripeFetchResult, StripeUnsupportedEvent
from clawbooks.utils import utcnow

runner = CliRunner()


def invoke(ledger: Path, *args: str):
    return runner.invoke(app, ["--ledger", str(ledger), "--json", *args])


def payload(result) -> dict:
    assert result.stdout, result
    return json.loads(result.stdout)


def init_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "ledger"
    result = invoke(ledger, "init", "--business-name", "Example LLC")
    assert result.exit_code == 0, result.stdout
    return ledger


def write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_init_creates_ledger_and_default_accounts(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert (ledger / "ledger.db").exists()
    assert (ledger / "config.toml").exists()
    result = invoke(ledger, "coa", "show")
    body = payload(result)
    assert result.exit_code == 0
    assert any(row["code"] == "1000" for row in body["data"]["rows"])
    assert any(row["code"] == "2100" for row in body["data"]["rows"])


def test_cli_help_hides_dead_flags_and_describes_owner_equity_alias() -> None:
    journal_help = runner.invoke(app, ["journal", "add", "--help"])
    assert journal_help.exit_code == 0
    assert "--non-cash" not in journal_help.stdout

    period_close_help = runner.invoke(app, ["period", "close", "--help"])
    assert period_close_help.exit_code == 0
    assert "--acknowledge-review-entry" not in period_close_help.stdout

    owner_equity_help = runner.invoke(app, ["report", "owner-equity", "--help"])
    assert owner_equity_help.exit_code == 0
    assert "Deprecated alias for equity-rollforward" in owner_equity_help.stdout
    assert "calendar-year-to-date" in owner_equity_help.stdout


def test_unbalanced_journal_returns_validation_error(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    result = invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-01",
        "--description",
        "bad entry",
        "--line",
        "1000:10.00",
        "--line",
        "4000:-9.00",
    )
    body = payload(result)
    assert result.exit_code == 2
    assert body["ok"] is False
    assert "not balanced" in body["errors"][0]


def test_cash_basis_requires_explicit_settlement_for_manual_accruals(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    result = invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-05",
        "--description",
        "Accrual revenue",
        "--line",
        "1100:100.00",
        "--line",
        "4000:-100.00",
        "--non-cash",
    )
    assert result.exit_code == 0

    cash_before = payload(
        invoke(
            ledger,
            "report",
            "pnl",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
            "--basis",
            "cash",
        )
    )
    assert cash_before["data"]["totals"]["revenue_cents"] == 0
    assert len(cash_before["data"]["excluded_lines"]) == 1

    cash_receipt = invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Customer payment",
        "--line",
        "1000:100.00",
        "--line",
        "1100:-100.00",
    )
    assert cash_receipt.exit_code == 0

    with session_scope(ledger) as session:
        revenue_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .join(JournalLine.account)
            .where(JournalEntry.description == "Accrual revenue", JournalLine.amount_cents == -10000)
        )
        bank_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .join(JournalLine.account)
            .where(JournalEntry.description == "Customer payment", JournalLine.amount_cents == 10000)
        )
    applied = invoke(
        ledger,
        "settlement",
        "apply",
        "--source-line-id",
        str(revenue_line_id),
        "--settlement-line-id",
        str(bank_line_id),
        "--amount",
        "100.00",
    )
    assert applied.exit_code == 0

    cash_after = payload(
        invoke(
            ledger,
            "report",
            "pnl",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
            "--basis",
            "cash",
        )
    )
    assert cash_after["data"]["totals"]["revenue_cents"] == 10000
    assert cash_after["data"]["excluded_lines"] == []


def test_settlement_rejects_immediate_cash_settlement_entries(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-05",
        "--description",
        "Accrual revenue",
        "--line",
        "1100:100.00",
        "--line",
        "4000:-100.00",
        "--non-cash",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Immediate sale",
        "--line",
        "1000:100.00",
        "--line",
        "4000:-100.00",
    ).exit_code == 0
    with session_scope(ledger) as session:
        revenue_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .where(JournalEntry.description == "Accrual revenue", JournalLine.amount_cents == -10000)
        )
        bank_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .where(JournalEntry.description == "Immediate sale", JournalLine.amount_cents == 10000)
        )

    rejected = invoke(
        ledger,
        "settlement",
        "apply",
        "--source-line-id",
        str(revenue_line_id),
        "--settlement-line-id",
        str(bank_line_id),
        "--amount",
        "100.00",
    )
    body = payload(rejected)
    assert rejected.exit_code == 2
    assert "immediate-cash entry" in body["errors"][0]


def test_cash_basis_ignores_legacy_invalid_settlement_applications(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-05",
        "--description",
        "Accrual revenue",
        "--line",
        "1100:100.00",
        "--line",
        "4000:-100.00",
        "--non-cash",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Immediate sale",
        "--line",
        "1000:100.00",
        "--line",
        "4000:-100.00",
    ).exit_code == 0
    with session_scope(ledger) as session:
        revenue_line = session.scalar(
            select(JournalLine)
            .join(JournalEntry)
            .where(JournalEntry.description == "Accrual revenue", JournalLine.amount_cents == -10000)
        )
        bank_line = session.scalar(
            select(JournalLine)
            .join(JournalEntry)
            .where(JournalEntry.description == "Immediate sale", JournalLine.amount_cents == 10000)
        )
        session.add(
            SettlementApplication(
                source_line_id=revenue_line.id,
                settlement_line_id=bank_line.id,
                applied_amount_cents=10000,
                applied_date=date(2026, 4, 20),
                application_type="manual",
                created_at=utcnow(),
            )
        )
        session.commit()

    cash_basis = payload(
        invoke(
            ledger,
            "report",
            "pnl",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
            "--basis",
            "cash",
        )
    )
    assert cash_basis["data"]["totals"]["revenue_cents"] == 10000
    assert len(cash_basis["data"]["ignored_invalid_settlement_applications"]) == 1
    assert any("Ignored invalid settlement application" in warning for warning in cash_basis["data"]["warnings"])


def test_stripe_import_dry_run_and_idempotency(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        assert timezone_name == "America/Chicago"
        return [
            StripeEvent(
                external_id="btx_charge",
                event_type="charge",
                occurred_at=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
                amount_cents=10800,
                fee_cents=400,
                tax_cents=800,
                net_cents=10400,
                description="Monthly subscription",
            ),
            StripeEvent(
                external_id="btx_payout",
                event_type="payout",
                occurred_at=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
                amount_cents=10400,
                description="Stripe payout",
            ),
        ]

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)

    dry_run = invoke(
        ledger,
        "import",
        "stripe",
        "--from-date",
        "2026-04-01",
        "--to-date",
        "2026-04-30",
        "--dry-run",
    )
    assert dry_run.exit_code == 0
    gl = invoke(
        ledger,
        "report",
        "general-ledger",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    assert payload(gl)["data"]["entries"] == []

    live = invoke(
        ledger,
        "import",
        "stripe",
        "--from-date",
        "2026-04-01",
        "--to-date",
        "2026-04-30",
    )
    live_body = payload(live)
    assert live.exit_code == 0
    assert live_body["data"]["entries_posted"] == 3
    assert live_body["data"]["blocked_events"] == 0

    rerun = invoke(
        ledger,
        "import",
        "stripe",
        "--from-date",
        "2026-04-01",
        "--to-date",
        "2026-04-30",
    )
    rerun_body = payload(rerun)
    assert rerun.exit_code == 0
    assert rerun_body["data"]["duplicates"] == 2


def test_import_stripe_rerun_refreshes_open_blocker(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)
    current_tax = {"value": None}

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        return [
            StripeEvent(
                external_id="btx_refresh",
                event_type="charge",
                occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
                amount_cents=10800,
                fee_cents=0,
                tax_cents=current_tax["value"],
                net_cents=10800,
                description="Refresh me",
            )
        ]

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)
    first = invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30")
    first_body = payload(first)
    assert first.exit_code == 0
    assert first_body["data"]["blocked_events"] == 1

    current_tax["value"] = 800
    rerun = invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30")
    rerun_body = payload(rerun)
    assert rerun.exit_code == 0
    assert rerun_body["data"]["blocked_events"] == 0
    assert rerun_body["data"]["entries_posted"] == 1

    blockers = payload(invoke(ledger, "review", "list"))
    row = next(item for item in blockers["data"]["rows"] if item["external_id"] == "btx_refresh")
    assert row["status"] == "resolved"
    assert row["resolution_type"] == "posted_after_refresh"
    assert row["refresh_history_count"] == 1

    with session_scope(ledger) as session:
        history_count = session.scalar(select(func.count(ExternalEventRefreshHistory.id)))
        event = session.scalar(select(ExternalEvent).where(ExternalEvent.external_id == "btx_refresh"))
        assert history_count == 1
        assert event.journal_entry_id is not None


def test_review_retry_refreshes_current_stripe_facts(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def initial_fetch(_api_key, _start, _end, timezone_name=None):
        return [
            StripeEvent(
                external_id="btx_retry",
                event_type="charge",
                occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
                amount_cents=10800,
                fee_cents=0,
                tax_cents=None,
                net_cents=10800,
                description="Retry me",
            )
        ]

    def refresh_fetch(_api_key, external_id):
        assert external_id == "btx_retry"
        return StripeEvent(
            external_id="btx_retry",
            event_type="charge",
            occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
            amount_cents=10800,
            fee_cents=0,
            tax_cents=800,
            net_cents=10800,
            description="Retry me",
        )

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", initial_fetch)
    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_event", refresh_fetch)
    assert invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30").exit_code == 0
    blocker_id = payload(invoke(ledger, "review", "list", "--status", "open"))["data"]["rows"][0]["review_blocker_id"]

    retried = invoke(ledger, "review", "retry", "--blocker-id", str(blocker_id))
    body = payload(retried)
    assert retried.exit_code == 0
    assert body["data"]["review_blocker"]["status"] == "resolved"
    assert body["data"]["review_blocker"]["resolution_type"] == "posted_after_refresh"
    assert body["data"]["review_blocker"]["refresh_history_count"] == 1


def test_csv_import_dedupes_across_different_file_paths(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    csv_one = tmp_path / "a" / "bank.csv"
    csv_one.parent.mkdir(parents=True, exist_ok=True)
    csv_one.write_text("date,description,amount,external_ref\n2026-04-09,AWS,-84.12,stmt-1\n", encoding="utf-8")
    csv_two = tmp_path / "b" / "bank.csv"
    csv_two.parent.mkdir(parents=True, exist_ok=True)
    csv_two.write_text("date,description,amount,external_ref\n2026-04-09,AWS,-84.12,stmt-1\n", encoding="utf-8")
    profile_path = write_text(
        tmp_path / "profile.json",
        json.dumps(
            {
                "date_column": "date",
                "description_column": "description",
                "amount_column": "amount",
                "external_ref_column": "external_ref",
                "rules": [{"match": "AWS", "account_code": "5110", "entry_kind": "expense"}],
            }
        ),
    )

    first = invoke(
        ledger,
        "import",
        "csv",
        "--account-code",
        "1000",
        "--csv-path",
        str(csv_one),
        "--profile-path",
        str(profile_path),
    )
    assert first.exit_code == 0

    second = invoke(
        ledger,
        "import",
        "csv",
        "--account-code",
        "1000",
        "--csv-path",
        str(csv_two),
        "--profile-path",
        str(profile_path),
    )
    body = payload(second)
    assert second.exit_code == 0
    assert body["data"]["duplicate_rows"] == 1

    gl = payload(
        invoke(
            ledger,
            "report",
            "general-ledger",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
        )
    )
    assert len(gl["data"]["entries"]) == 1


def test_csv_import_creates_reconciliation_and_can_close(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    csv_path = write_text(
        tmp_path / "bank.csv",
        "date,description,amount,external_ref\n2026-04-09,AWS,-84.12,stmt-1\n",
    )
    profile_path = write_text(
        tmp_path / "profile.json",
        json.dumps(
            {
                "date_column": "date",
                "description_column": "description",
                "amount_column": "amount",
                "external_ref_column": "external_ref",
                "rules": [{"match": "AWS", "account_code": "5110", "entry_kind": "expense"}],
            }
        ),
    )

    imported = invoke(
        ledger,
        "import",
        "csv",
        "--account-code",
        "1000",
        "--csv-path",
        str(csv_path),
        "--profile-path",
        str(profile_path),
        "--statement-starting-balance",
        "1000.00",
        "--statement-ending-balance",
        "915.88",
    )
    body = payload(imported)
    assert imported.exit_code == 0
    session_id = body["data"]["reconciliation_session_id"]

    closed = invoke(ledger, "reconcile", "close", "--session-id", str(session_id))
    assert closed.exit_code == 0


def test_reconciliation_supports_many_to_one_matching(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "Deposit A",
        "--line",
        "1000:60.00",
        "--line",
        "4000:-60.00",
    )
    invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "Deposit B",
        "--line",
        "1000:40.00",
        "--line",
        "4000:-40.00",
    )
    statement_path = write_text(
        tmp_path / "statement.csv",
        "date,description,amount,external_ref\n2026-04-10,Batched deposit,100.00,st-1\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement_path),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    session_id = payload(started)["data"]["session_id"]
    with session_scope(ledger) as session:
        line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
    candidates = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))
    assert len(candidates["data"]["rows"]) == 2
    first_line = candidates["data"]["rows"][0]["journal_line_id"]
    second_line = candidates["data"]["rows"][1]["journal_line_id"]
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(line_id),
        "--journal-line-id",
        str(first_line),
        "--amount",
        "60.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(line_id),
        "--journal-line-id",
        str(second_line),
        "--amount",
        "40.00",
    ).exit_code == 0
    assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0


def test_reconciliation_void_retires_mistaken_session_and_allows_replacement(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    statement_path = write_text(
        tmp_path / "statement.csv",
        "date,description,amount,external_ref\n2026-04-10,Deposit,100.00,st-1\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement_path),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    session_id = payload(started)["data"]["session_id"]

    overlapping = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement_path),
        "--statement-start",
        "2026-04-15",
        "--statement-end",
        "2026-05-15",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    assert overlapping.exit_code == 3

    assert invoke(
        ledger,
        "reconcile",
        "void",
        "--session-id",
        str(session_id),
        "--reason",
        "Wrong statement file",
    ).exit_code == 0

    replacement = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement_path),
        "--statement-start",
        "2026-04-15",
        "--statement-end",
        "2026-05-15",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    assert replacement.exit_code == 0


def test_reconciliation_candidates_and_match_reject_overlapping_legacy_shared_lines(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "Deposit A",
        "--line",
        "1000:100.00",
        "--line",
        "4000:-100.00",
    ).exit_code == 0
    first_statement = write_text(
        tmp_path / "first.csv",
        "date,description,amount,external_ref\n2026-04-10,Deposit,50.00,st-1\n",
    )
    second_statement = write_text(
        tmp_path / "second.csv",
        "date,description,amount,external_ref\n2026-04-10,Deposit,50.00,st-2\n",
    )
    first_started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(first_statement),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "50.00",
    )
    first_session_id = payload(first_started)["data"]["session_id"]

    with session_scope(ledger) as session:
        journal_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .where(JournalEntry.description == "Deposit A", JournalLine.amount_cents == 10000)
        )
        first_line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == first_session_id))
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(first_session_id),
        "--line-id",
        str(first_line_id),
        "--journal-line-id",
        str(journal_line_id),
        "--amount",
        "50.00",
    ).exit_code == 0

    with session_scope(ledger) as session:
        account_id = session.scalar(select(JournalLine.account_id).where(JournalLine.id == journal_line_id))
        overlapping = ReconciliationSession(
            account_id=account_id,
            statement_path=str(second_statement),
            statement_start=date(2026, 4, 15),
            statement_end=date(2026, 5, 15),
            statement_starting_balance_cents=0,
            statement_ending_balance_cents=50_00,
            status="open",
            created_at=utcnow(),
        )
        session.add(overlapping)
        session.flush()
        session.add(ReconciliationLine(
            session_id=overlapping.id,
            transaction_date=date(2026, 4, 10),
            description="Deposit",
            amount_cents=5000,
            external_ref="st-2",
            status="open",
        ))
        session.commit()
        second_session_id = overlapping.id
        second_line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == second_session_id))

    candidates = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(second_session_id)))
    candidate = next(item for item in candidates["data"]["rows"] if item["journal_line_id"] == journal_line_id)
    assert candidate["matchable"] is False
    assert candidate["rejection_reason"] == "journal_line_already_matched_in_overlapping_session"

    blocked_match = invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(second_session_id),
        "--line-id",
        str(second_line_id),
        "--journal-line-id",
        str(journal_line_id),
        "--amount",
        "50.00",
    )
    assert blocked_match.exit_code == 3


def test_owner_paid_expense_hits_owner_contributions(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    result = invoke(
        ledger,
        "expense",
        "record",
        "--date",
        "2026-04-03",
        "--vendor",
        "CPA LLC",
        "--amount",
        "125.00",
        "--category",
        "5120",
        "--paid-personally",
    )
    assert result.exit_code == 0
    equity = invoke(ledger, "report", "owner-equity", "--as-of", "2026-04-30")
    body = payload(equity)
    rows = {row["component"]: row["amount_cents"] for row in body["data"]["rows"]}
    assert rows["owner_contributions"] == 12500
    assert rows["current_period_earnings"] == -12500
    assert rows["ending_equity"] == 0


def test_review_blockers_block_close_until_resolved_then_reopen_unlocks(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        return [
            StripeEvent(
                external_id="btx_review",
                event_type="charge",
                occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
                amount_cents=10000,
                fee_cents=0,
                tax_cents=None,
                net_cents=10000,
                description="Review me",
            )
        ]

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)
    imported = invoke(
        ledger,
        "import",
        "stripe",
        "--from-date",
        "2026-04-01",
        "--to-date",
        "2026-04-30",
    )
    imported_body = payload(imported)
    assert imported.exit_code == 0
    assert imported_body["data"]["blocked_events"] == 1

    close_fail = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    assert close_fail.exit_code == 6

    blockers = payload(invoke(ledger, "review", "list", "--status", "open"))
    blocker_id = blockers["data"]["rows"][0]["review_blocker_id"]
    resolved = invoke(
        ledger,
        "review",
        "resolve",
        "--blocker-id",
        str(blocker_id),
        "--resolution-type",
        "skip",
        "--note",
        "Handled offline",
    )
    assert resolved.exit_code == 0

    close_ok = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    assert close_ok.exit_code == 0


def test_settlement_mutations_require_period_reopen(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-05",
        "--description",
        "Accrual revenue",
        "--line",
        "1100:100.00",
        "--line",
        "4000:-100.00",
        "--non-cash",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Customer payment",
        "--line",
        "1000:100.00",
        "--line",
        "1100:-100.00",
    ).exit_code == 0
    statement = write_text(
        tmp_path / "april-receipt.csv",
        "date,description,amount,external_ref\n2026-04-20,Customer payment,100.00,apr-receipt\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    session_id = payload(started)["data"]["session_id"]
    with session_scope(ledger) as session:
        statement_line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
        revenue_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .where(JournalEntry.description == "Accrual revenue", JournalLine.amount_cents == -10000)
        )
        bank_line_id = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .where(JournalEntry.description == "Customer payment", JournalLine.amount_cents == 10000)
        )
    candidate = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))["data"]["rows"][0]
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(statement_line_id),
        "--journal-line-id",
        str(candidate["journal_line_id"]),
        "--amount",
        "100.00",
    ).exit_code == 0
    assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    blocked_apply = invoke(
        ledger,
        "settlement",
        "apply",
        "--source-line-id",
        str(revenue_line_id),
        "--settlement-line-id",
        str(bank_line_id),
        "--amount",
        "100.00",
        "--applied-date",
        "2026-04-20",
    )
    assert blocked_apply.exit_code == 4

    assert invoke(
        ledger,
        "period",
        "reopen",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
        "--reason",
        "Need to finalize settlement",
    ).exit_code == 0
    applied = invoke(
        ledger,
        "settlement",
        "apply",
        "--source-line-id",
        str(revenue_line_id),
        "--settlement-line-id",
        str(bank_line_id),
        "--amount",
        "100.00",
        "--applied-date",
        "2026-04-20",
    )
    assert applied.exit_code == 0
    settlement_application_id = payload(applied)["data"]["settlement_application_id"]

    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0
    blocked_reverse = invoke(
        ledger,
        "settlement",
        "reverse",
        "--settlement-application-id",
        str(settlement_application_id),
        "--reason",
        "Should be locked",
    )
    assert blocked_reverse.exit_code == 4


def test_reconciliation_mutations_require_period_reopen(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "April expense",
        "--line",
        "5110:20.00",
        "--line",
        "1000:-20.00",
    ).exit_code == 0
    statement = write_text(
        tmp_path / "april-bank.csv",
        "date,description,amount,external_ref\n2026-04-10,Expense,-20.00,apr-bank\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "1000.00",
        "--statement-ending-balance",
        "980.00",
    )
    session_id = payload(started)["data"]["session_id"]
    with session_scope(ledger) as session:
        statement_line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
    candidate = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))["data"]["rows"][0]
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(statement_line_id),
        "--journal-line-id",
        str(candidate["journal_line_id"]),
        "--amount",
        "20.00",
    ).exit_code == 0
    assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    assert invoke(
        ledger,
        "reconcile",
        "reopen",
        "--session-id",
        str(session_id),
        "--reason",
        "Should be blocked while locked",
    ).exit_code == 4
    assert invoke(
        ledger,
        "reconcile",
        "void",
        "--session-id",
        str(session_id),
        "--reason",
        "Should be blocked while locked",
    ).exit_code == 4
    blocked_start = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement),
        "--statement-start",
        "2026-04-15",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "980.00",
        "--statement-ending-balance",
        "980.00",
    )
    assert blocked_start.exit_code == 4


def test_period_close_accepts_continuous_monthly_reconciliation_coverage(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "April expense",
        "--line",
        "5110:20.00",
        "--line",
        "1000:-20.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-05-10",
        "--description",
        "May expense",
        "--line",
        "5110:30.00",
        "--line",
        "1000:-30.00",
    ).exit_code == 0

    def close_month(statement_name: str, stmt_date: str, amount: str, start: str, end: str, starting_balance: str, ending_balance: str):
        statement = write_text(
            tmp_path / statement_name,
            f"date,description,amount,external_ref\n{stmt_date},Expense,{amount},ref-{statement_name}\n",
        )
        started = invoke(
            ledger,
            "reconcile",
            "start",
            "--account-code",
            "1000",
            "--statement-path",
            str(statement),
            "--statement-start",
            start,
            "--statement-end",
            end,
            "--statement-starting-balance",
            starting_balance,
            "--statement-ending-balance",
            ending_balance,
        )
        session_id = payload(started)["data"]["session_id"]
        with session_scope(ledger) as session:
            line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
        candidate = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))["data"]["rows"][0]
        assert invoke(
            ledger,
            "reconcile",
            "match",
            "--session-id",
            str(session_id),
            "--line-id",
            str(line_id),
            "--journal-line-id",
            str(candidate["journal_line_id"]),
            "--amount",
            amount.replace("-", ""),
        ).exit_code == 0
        assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0

    close_month("april.csv", "2026-04-10", "-20.00", "2026-04-01", "2026-04-30", "1000.00", "980.00")
    close_month("may.csv", "2026-05-10", "-30.00", "2026-05-01", "2026-05-31", "980.00", "950.00")

    closed = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-05-31",
    )
    assert closed.exit_code == 0

    locked_expense = invoke(
        ledger,
        "expense",
        "record",
        "--date",
        "2026-04-09",
        "--vendor",
        "Locked vendor",
        "--amount",
        "10.00",
        "--category",
        "5110",
        "--payment-account",
        "1000",
    )
    assert locked_expense.exit_code == 4
    assert invoke(
        ledger,
        "period",
        "reopen",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-05-31",
        "--reason",
        "Need an adjusting entry",
    ).exit_code == 0


def test_period_close_fails_when_reconciliation_coverage_has_a_gap(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "April expense",
        "--line",
        "5110:20.00",
        "--line",
        "1000:-20.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-05-10",
        "--description",
        "May expense",
        "--line",
        "5110:30.00",
        "--line",
        "1000:-30.00",
    ).exit_code == 0

    april_statement = write_text(
        tmp_path / "april.csv",
        "date,description,amount,external_ref\n2026-04-10,Expense,-20.00,ref-april\n",
    )
    may_statement = write_text(
        tmp_path / "may.csv",
        "date,description,amount,external_ref\n2026-05-10,Expense,-30.00,ref-may\n",
    )
    started_april = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(april_statement),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-starting-balance",
        "1000.00",
        "--statement-ending-balance",
        "980.00",
    )
    april_session_id = payload(started_april)["data"]["session_id"]
    started_may = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(may_statement),
        "--statement-start",
        "2026-05-02",
        "--statement-end",
        "2026-05-31",
        "--statement-starting-balance",
        "980.00",
        "--statement-ending-balance",
        "950.00",
    )
    may_session_id = payload(started_may)["data"]["session_id"]

    for session_id, amount in ((april_session_id, "20.00"), (may_session_id, "30.00")):
        with session_scope(ledger) as session:
            line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
        candidate = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))["data"]["rows"][0]
        assert invoke(
            ledger,
            "reconcile",
            "match",
            "--session-id",
            str(session_id),
            "--line-id",
            str(line_id),
            "--journal-line-id",
            str(candidate["journal_line_id"]),
            "--amount",
            amount,
        ).exit_code == 0
        assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0

    close_fail = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-05-31",
    )
    assert close_fail.exit_code == 3


def test_review_override_posts_once_and_rerun_honors_resolution(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        return [
            StripeEvent(
                external_id="btx_override",
                event_type="charge",
                occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
                amount_cents=10000,
                fee_cents=0,
                tax_cents=None,
                net_cents=10000,
                description="Override me",
            )
        ]

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)
    assert invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30").exit_code == 0
    blocker_id = payload(invoke(ledger, "review", "list", "--status", "open"))["data"]["rows"][0]["review_blocker_id"]
    resolved = invoke(
        ledger,
        "review",
        "resolve",
        "--blocker-id",
        str(blocker_id),
        "--resolution-type",
        "post_with_override",
        "--override-tax-cents",
        "0",
    )
    assert resolved.exit_code == 0

    rerun = invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30")
    body = payload(rerun)
    assert rerun.exit_code == 0
    assert body["data"]["duplicates"] == 1

    with session_scope(ledger) as session:
        count = session.scalar(select(func.count(JournalEntry.id)).where(JournalEntry.source_ref.like("btx_override%")))
        assert count == 1


def test_period_close_requires_reconciliation_for_dormant_financial_balances(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-03-31",
        "--description",
        "Opening bank balance",
        "--source-type",
        "owner_contribution",
        "--line",
        "1000:500.00",
        "--line",
        "3000:-500.00",
    ).exit_code == 0

    close_fail = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    body = payload(close_fail)
    assert close_fail.exit_code == 3
    assert "1000" in body["errors"][0] or "1000" in json.dumps(body["data"])


def test_reconciliation_summary_includes_spanning_session_used_for_close(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-03-31",
        "--description",
        "Opening bank balance",
        "--source-type",
        "owner_contribution",
        "--line",
        "1000:500.00",
        "--line",
        "3000:-500.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "April expense",
        "--line",
        "5110:20.00",
        "--line",
        "1000:-20.00",
    ).exit_code == 0

    statement = write_text(
        tmp_path / "spanning.csv",
        "date,description,amount,external_ref\n2026-04-10,Expense,-20.00,span-1\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1000",
        "--statement-path",
        str(statement),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-05-31",
        "--statement-starting-balance",
        "500.00",
        "--statement-ending-balance",
        "480.00",
    )
    session_id = payload(started)["data"]["session_id"]
    with session_scope(ledger) as session:
        statement_line_id = session.scalar(select(ReconciliationLine.id).where(ReconciliationLine.session_id == session_id))
    candidates = payload(invoke(ledger, "reconcile", "candidates", "--session-id", str(session_id)))["data"]["rows"]
    candidate = next(row for row in candidates if row["amount_cents"] == -2000)
    assert invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(statement_line_id),
        "--journal-line-id",
        str(candidate["journal_line_id"]),
        "--amount",
        "20.00",
    ).exit_code == 0
    assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    summary = payload(
        invoke(
            ledger,
            "report",
            "general-ledger",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
        )
    )
    assert summary["ok"] is True
    audit = payload(
        invoke(
            ledger,
            "period",
            "audit",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
        )
    )
    coverage = audit["data"]["reconciliation_coverage"]
    assert any(row["session_id"] == session_id for row in coverage["sessions"])
    bank_row = next(row for row in coverage["coverage_rows"] if row["account_code"] == "1000")
    assert bank_row["covered"] is True
    assert session_id in bank_row["session_ids"]


def test_import_stripe_blocks_unsupported_events_instead_of_dropping(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        return StripeFetchResult(
            supported_events=[],
            unsupported_events=[
                StripeUnsupportedEvent(
                    external_id="btx_eur_1",
                    occurred_at=datetime(2026, 4, 12, 12, 0, tzinfo=UTC),
                    raw_type="charge",
                    currency="EUR",
                    description="EUR subscription",
                    reason="Stripe balance transaction uses unsupported currency EUR.",
                    payload={"id": "btx_eur_1", "type": "charge", "currency": "eur"},
                )
            ],
        )

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)
    imported = invoke(ledger, "import", "stripe", "--from-date", "2026-04-01", "--to-date", "2026-04-30")
    body = payload(imported)
    assert imported.exit_code == 0
    assert body["data"]["blocked_events"] == 1
    assert body["data"]["unsupported_events"] == 1

    blockers = payload(invoke(ledger, "review", "list", "--status", "open"))
    assert blockers["data"]["rows"][0]["blocker_type"] == "stripe_unsupported_event"
    with session_scope(ledger) as session:
        assert session.scalar(select(func.count(ExternalEvent.id)).where(ExternalEvent.external_id == "btx_eur_1")) == 1


def test_reimbursable_owner_payment_auto_links_only_single_exact_source(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "expense",
        "record",
        "--date",
        "2026-04-01",
        "--vendor",
        "Legal filing",
        "--amount",
        "100.00",
        "--category",
        "5140",
        "--paid-personally",
        "--reimbursement",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Owner reimbursement",
        "--line",
        "2300:100.00",
        "--line",
        "1000:-100.00",
    ).exit_code == 0

    cash = payload(
        invoke(
            ledger,
            "report",
            "pnl",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
            "--basis",
            "cash",
        )
    )
    assert cash["data"]["totals"]["expense_cents"] == 10000
    assert cash["data"]["excluded_lines"] == []
    with session_scope(ledger) as session:
        assert session.scalar(
            select(func.count(SettlementApplication.id)).where(SettlementApplication.application_type == "reimbursement_auto")
        ) == 1


def test_reimbursable_owner_payment_requires_manual_settlement_when_multiple_sources_exist(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    for entry_date, vendor in (("2026-03-30", "March filing"), ("2026-04-01", "April filing")):
        assert invoke(
            ledger,
            "expense",
            "record",
            "--date",
            entry_date,
            "--vendor",
            vendor,
            "--amount",
            "100.00",
            "--category",
            "5140",
            "--paid-personally",
            "--reimbursement",
        ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-20",
        "--description",
        "Owner reimbursement",
        "--line",
        "2300:100.00",
        "--line",
        "1000:-100.00",
    ).exit_code == 0

    cash = payload(
        invoke(
            ledger,
            "report",
            "pnl",
            "--period-start",
            "2026-04-01",
            "--period-end",
            "2026-04-30",
            "--basis",
            "cash",
        )
    )
    assert cash["data"]["totals"]["expense_cents"] == 0
    assert any("reimbursement" in row["reason"].lower() for row in cash["data"]["excluded_lines"])
    with session_scope(ledger) as session:
        assert session.scalar(
            select(func.count(SettlementApplication.id)).where(SettlementApplication.application_type == "reimbursement_auto")
        ) == 0


def test_equity_rollforward_classifies_manual_equity_postings_as_other_adjustments(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-01-02",
        "--description",
        "Initial capital",
        "--source-type",
        "owner_contribution",
        "--line",
        "1000:500.00",
        "--line",
        "3000:-500.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-10",
        "--description",
        "CPA equity cleanup",
        "--line",
        "1000:40.00",
        "--line",
        "3000:-40.00",
    ).exit_code == 0

    rollforward = payload(
        invoke(
            ledger,
            "report",
            "equity-rollforward",
            "--period-start",
            "2026-01-01",
            "--period-end",
            "2026-04-30",
        )
    )
    rows = {row["component"]: row["amount_cents"] for row in rollforward["data"]["rows"]}
    assert rows["owner_contributions"] == 50000
    assert rows["other_equity_adjustments"] == 4000


def test_compliance_profile_update_persists_sales_tax_payment_slots(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    updated = invoke(
        ledger,
        "compliance",
        "profile",
        "update",
        "--json",
        json.dumps(
            {
                "sales_tax_profile_confirmed": True,
                "sales_tax_registrations": [
                    {"jurisdiction": "illinois", "filing_cadence": "annual", "active": True}
                ],
                "sales_tax_payment_slots": [
                    {
                        "jurisdiction": "illinois",
                        "period_start": "2026-01-01",
                        "period_end": "2026-12-31",
                        "filing_due_date": "2027-01-20",
                        "payment_expected": "true",
                        "source": "operator",
                        "reason": "Annual return payment expected",
                    }
                ],
            }
        ),
    )
    assert updated.exit_code == 0, updated.stdout

    shown = payload(invoke(ledger, "compliance", "profile", "show"))
    slots = shown["data"]["profile"]["sales_tax_payment_slots"]
    assert slots == [
        {
            "filing_due_date": "2027-01-20",
            "jurisdiction": "illinois",
            "payment_expected": "true",
            "period_end": "2026-12-31",
            "period_start": "2026-01-01",
            "reason": "Annual return payment expected",
            "source": "operator",
        }
    ]

    checklist = payload(invoke(ledger, "document", "checklist", "--year", "2026"))
    rows = {row["item_key"]: row for row in checklist["data"]["rows"]}
    assert rows["sales_tax_payments"]["status"] == "missing"
    assert rows["sales_tax_payments"]["slot_details"][0]["payment_expected"] == "true"


def test_sales_tax_slot_set_payment_expectation_round_trips(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "compliance",
        "profile",
        "update",
        "--json",
        json.dumps(
            {
                "sales_tax_profile_confirmed": True,
                "sales_tax_registrations": [
                    {"jurisdiction": "illinois", "filing_cadence": "annual", "active": True}
                ],
            }
        ),
    ).exit_code == 0

    updated = invoke(
        ledger,
        "compliance",
        "sales-tax-slot",
        "set-payment-expectation",
        "--jurisdiction",
        "illinois",
        "--period-start",
        "2026-01-01",
        "--period-end",
        "2026-12-31",
        "--filing-due-date",
        "2027-01-20",
        "--payment-expected",
        "true",
        "--source",
        "operator",
        "--reason",
        "Annual filing payment expected",
    )
    assert updated.exit_code == 0, updated.stdout

    listed = payload(invoke(ledger, "compliance", "sales-tax-slot", "list", "--year", "2026"))
    assert listed["data"]["rows"] == [
        {
            "filing_due_date": "2027-01-20",
            "jurisdiction": "illinois",
            "payment_expected": "true",
            "period_end": "2026-12-31",
            "period_start": "2026-01-01",
            "reason": "Annual filing payment expected",
            "source": "operator",
        }
    ]

    checklist = payload(invoke(ledger, "document", "checklist", "--year", "2026"))
    rows = {row["item_key"]: row for row in checklist["data"]["rows"]}
    assert rows["sales_tax_payments"]["status"] == "missing"
    assert rows["sales_tax_payments"]["slot_details"][0]["payment_expected"] == "true"
