from __future__ import annotations

from datetime import date

from clawbooks.tui_facade import TuiFacade
from tests.helpers import add_document, init_ledger, record_expense


def test_tui_facade_normalizes_dashboard_and_reports(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    today = date.today()
    record_expense(ledger, entry_date=date(today.year, 1, 15), vendor="January Hosting", amount="100.00")
    record_expense(ledger, entry_date=date(today.year, today.month, 5), vendor="Current Hosting", amount="50.00")

    facade = TuiFacade(ledger)

    dashboard = facade.dashboard(as_of=today)
    assert dashboard.business_name == "Example LLC"
    assert any(metric.label == "YTD Net Income (Cash)" for metric in dashboard.metrics)
    assert dashboard.sections[0].title == "Key Balances"

    pnl_view = facade.report("pnl", preset="YTD")
    assert pnl_view.title == "Profit & Loss (Cash Basis)"
    assert pnl_view.basis == "cash"
    assert pnl_view.mode == "range"
    assert pnl_view.sections[0].title == "Accounts"
    assert any(metric.label == "Basis" and metric.value == "Cash" for metric in pnl_view.metrics)
    assert any(metric.label == "Net Income" for metric in pnl_view.metrics)

    balance_sheet_view = facade.report("balance_sheet", as_of=today)
    assert [section.title for section in balance_sheet_view.sections] == ["Assets", "Liabilities", "Equity"]


def test_tui_facade_status_and_help_commands(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    support_doc = tmp_path / "prior-year-return.pdf"
    support_doc.write_text("return", encoding="utf-8")
    add_document(ledger, source_path=support_doc, document_type="prior_year_return", year=date.today().year, scope="owner")
    facade = TuiFacade(ledger)

    status = facade.status()
    assert [section.title for section in status.sections] == [
        "Compliance Profile",
        "Chart of Accounts",
        "Tax Obligations",
        "Reconciliation Sessions",
        "Import History",
        "Document Registry",
        "Accountant Packet Checklist",
        "Missing Packet Items",
        "Unknown Packet Items",
        "Review Blockers",
        "Period Events",
    ]
    assert status.packet_year == date.today().year
    assert any(section.title == "Document Registry" and section.rows for section in status.sections)

    commands = facade.help_commands()
    assert sorted(facade.available_document_types())
    assert any(command.title == "Import Stripe" for command in commands)
    assert any(command.title == "Add document" for command in commands)
    assert any(command.title == "Settlement" for command in commands)
    assert any("compliance profile show" in command.command for command in commands)
