from __future__ import annotations

from datetime import date

from clawbooks.tui_facade import TuiFacade
from tests.helpers import init_ledger, record_expense


def test_tui_facade_normalizes_dashboard_and_reports(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    today = date.today()
    record_expense(ledger, entry_date=date(today.year, 1, 15), vendor="January Hosting", amount="100.00")
    record_expense(ledger, entry_date=date(today.year, today.month, 5), vendor="Current Hosting", amount="50.00")

    facade = TuiFacade(ledger)

    dashboard = facade.dashboard(as_of=today)
    assert dashboard.business_name == "Example LLC"
    assert any(metric.label == "YTD Net Income" for metric in dashboard.metrics)
    assert dashboard.sections[0].title == "Key Balances"

    pnl_view = facade.report("pnl", preset="YTD")
    assert pnl_view.title == "Profit & Loss"
    assert pnl_view.mode == "range"
    assert pnl_view.sections[0].title == "Accounts"
    assert any(metric.label == "Net Income" for metric in pnl_view.metrics)

    balance_sheet_view = facade.report("balance_sheet", as_of=today)
    assert [section.title for section in balance_sheet_view.sections] == ["Assets", "Liabilities", "Equity"]


def test_tui_facade_status_and_help_commands(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    facade = TuiFacade(ledger)

    status = facade.status()
    assert [section.title for section in status.sections] == [
        "Chart of Accounts",
        "Tax Obligations",
        "Reconciliation Sessions",
        "Import History",
        "Review-Required Entries",
        "Period Events",
    ]

    commands = facade.help_commands()
    assert any(command.title == "Import Stripe" for command in commands)
    assert any("period close" in command.command for command in commands)
