from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from clawbooks.cli import app
from clawbooks.db import session_scope
from clawbooks.models import JournalEntry, ReconciliationLine
from clawbooks.schemas import StripeEvent

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


def test_cash_basis_excludes_non_cash_manual_journal(tmp_path: Path) -> None:
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

    cash_result = invoke(
        ledger,
        "report",
        "pnl",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    accrual_result = invoke(
        ledger,
        "report",
        "pnl",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
        "--basis",
        "accrual",
    )
    cash_body = payload(cash_result)
    accrual_body = payload(accrual_result)
    assert cash_body["data"]["totals"]["revenue_cents"] == 0
    assert accrual_body["data"]["totals"]["revenue_cents"] == 10000


def test_stripe_import_dry_run_and_idempotency(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end):
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
        "--statement-ending-balance",
        "-84.12",
    )
    body = payload(imported)
    assert imported.exit_code == 0
    session_id = body["data"]["reconciliation_session_id"]

    closed = invoke(ledger, "reconcile", "close", "--session-id", str(session_id))
    assert closed.exit_code == 0


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
    rows = {row["code"]: row["amount_cents"] for row in body["data"]["rows"]}
    assert rows["3000"] == 12500


def test_period_close_blocks_unresolved_tax_review_then_reopen_unlocks(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end):
        return [
            StripeEvent(
                external_id="btx_review",
                event_type="charge",
                occurred_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
                amount_cents=10000,
                fee_cents=0,
                tax_cents=0,
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
    assert imported.exit_code == 0

    statement_path = write_text(
        tmp_path / "stripe_statement.csv",
        "date,description,amount,external_ref\n2026-04-08,Review me,100.00,st-1\n",
    )
    started = invoke(
        ledger,
        "reconcile",
        "start",
        "--account-code",
        "1010",
        "--statement-path",
        str(statement_path),
        "--statement-start",
        "2026-04-01",
        "--statement-end",
        "2026-04-30",
        "--statement-ending-balance",
        "100.00",
    )
    assert started.exit_code == 0
    session_id = payload(started)["data"]["session_id"]

    with session_scope(ledger) as session:
        line_id = session.query(ReconciliationLine.id).filter_by(session_id=session_id).first()[0]
        entry_id = session.query(JournalEntry.id).filter_by(source_ref="btx_review").first()[0]

    matched = invoke(
        ledger,
        "reconcile",
        "match",
        "--session-id",
        str(session_id),
        "--line-id",
        str(line_id),
        "--entry-id",
        str(entry_id),
    )
    assert matched.exit_code == 0
    assert invoke(ledger, "reconcile", "close", "--session-id", str(session_id)).exit_code == 0

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

    acknowledged_close = invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
        "--acknowledge-review-entry",
        str(entry_id),
    )
    assert acknowledged_close.exit_code == 0

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

    reopened = invoke(
        ledger,
        "period",
        "reopen",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
        "--reason",
        "Adjustment required",
    )
    assert reopened.exit_code == 0

    unlocked_expense = invoke(
        ledger,
        "expense",
        "record",
        "--date",
        "2026-04-09",
        "--vendor",
        "Unlocked vendor",
        "--amount",
        "10.00",
        "--category",
        "5110",
        "--payment-account",
        "1000",
    )
    assert unlocked_expense.exit_code == 0


def test_year_end_export_writes_bundle(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert (
        invoke(
            ledger,
            "expense",
            "record",
            "--date",
            "2026-01-15",
            "--vendor",
            "Hosting Co",
            "--amount",
            "50.00",
            "--category",
            "5110",
            "--payment-account",
            "1000",
        ).exit_code
        == 0
    )
    exported = invoke(ledger, "export", "year-end", "--year", "2026")
    body = payload(exported)
    assert exported.exit_code == 0
    output_dir = Path(body["data"]["output_dir"])
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "pnl.json").exists()
    assert (output_dir / "trial_balance.csv").exists()
