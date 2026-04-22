from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from clawbooks.cli import app

runner = CliRunner()


def invoke_cli(ledger: Path, *args: str):
    return runner.invoke(app, ["--ledger", str(ledger), "--json", *args])


def init_ledger(tmp_path: Path, *, business_name: str = "Example LLC") -> Path:
    ledger = tmp_path / "ledger"
    result = invoke_cli(ledger, "init", "--business-name", business_name)
    assert result.exit_code == 0, result.stdout
    return ledger


def record_expense(ledger: Path, *, entry_date: date, vendor: str, amount: str, category: str = "5110", payment_account: str = "1000") -> None:
    result = invoke_cli(
        ledger,
        "expense",
        "record",
        "--date",
        entry_date.isoformat(),
        "--vendor",
        vendor,
        "--amount",
        amount,
        "--category",
        category,
        "--payment-account",
        payment_account,
    )
    assert result.exit_code == 0, result.stdout
