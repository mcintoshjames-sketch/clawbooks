from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from textual.widgets import Button, Input, Static, TabbedContent

from clawbooks.cli import app as cli_app
from clawbooks.db import session_scope
from clawbooks.models import JournalEntry
from clawbooks.tui import ClawbooksTuiApp, ExportPane, HelpPane, MainScreen, ReportPane, StatusPane
from tests.helpers import init_ledger, record_expense, runner


def patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def widget_text(widget) -> str:
    return str(getattr(widget, "content", ""))


def test_tui_help_smoke() -> None:
    result = runner.invoke(cli_app, ["tui", "--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "tui" in result.stdout


@pytest.mark.asyncio
async def test_tui_picker_accepts_valid_ledger(tmp_path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    patch_home(monkeypatch, fake_home)

    app = ClawbooksTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.screen.query_one("#ledger-path", Input).value = str(ledger)
        await pilot.pause()
        assert app.screen.query_one("#open-ledger", Button).disabled is False
        await pilot.click("#open-ledger")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert app.screen.query_one("#main-tabs", TabbedContent).active == "dashboard"


@pytest.mark.asyncio
async def test_tui_picker_rejects_invalid_ledger(tmp_path, monkeypatch) -> None:
    invalid = tmp_path / "not-a-ledger"
    invalid.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    patch_home(monkeypatch, fake_home)

    app = ClawbooksTuiApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.screen.query_one("#ledger-path", Input).value = str(invalid)
        await pilot.pause()
        assert app.screen.query_one("#open-ledger", Button).disabled is True
        status = widget_text(app.screen.query_one("#picker-status", Static))
        assert "Not a clawbooks ledger" in status


@pytest.mark.asyncio
async def test_tui_opens_directly_and_navigates_sections(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tabs = app.screen.query_one("#main-tabs", TabbedContent)
        assert tabs.active == "dashboard"
        await pilot.press("r")
        assert tabs.active == "reports"
        await pilot.press("s")
        assert tabs.active == "status"
        await pilot.press("e")
        assert tabs.active == "exports"
        await pilot.press("h")
        assert tabs.active == "help"
        await pilot.press("d")
        assert tabs.active == "dashboard"


@pytest.mark.asyncio
async def test_tui_report_presets_refresh_report_state(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    today = date.today()
    record_expense(ledger, entry_date=date(today.year, 1, 15), vendor="January Hosting", amount="100.00")
    record_expense(ledger, entry_date=date(today.year, today.month, 5), vendor="Current Hosting", amount="50.00")

    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("r")
        reports = app.screen.query_one("#reports-pane", ReportPane)
        await pilot.pause()
        default_net_income = next(metric.value for metric in reports.current_view.metrics if metric.label == "Net Income")
        await pilot.click("#report-preset-ytd")
        await pilot.pause()
        ytd_net_income = next(metric.value for metric in reports.current_view.metrics if metric.label == "Net Income")
        assert default_net_income != ytd_net_income
        assert reports.current_view.start == date(today.year, 1, 1)


@pytest.mark.asyncio
async def test_tui_renders_empty_states_for_new_ledger(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("r")
        reports = app.screen.query_one("#reports-pane", ReportPane)
        await pilot.pause()
        assert reports.current_view.sections[0].rows == []
        empty_widgets = app.screen.query("#report-display .empty-state")
        assert len(empty_widgets) >= 1


@pytest.mark.asyncio
async def test_tui_exports_and_surfaces_output_path(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    today = date.today()
    record_expense(ledger, entry_date=today, vendor="Hosting", amount="10.00")
    packet_doc = tmp_path / "stripe_1099_k.pdf"
    packet_doc.write_text("stripe-doc", encoding="utf-8")

    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("e")
        exports = app.screen.query_one("#exports-pane", ExportPane)
        app.screen.query_one("#document-source-path", Input).value = str(packet_doc)
        app.screen.query_one("#document-type", Input).value = "stripe_1099_k"
        app.screen.query_one("#document-year", Input).value = str(today.year)
        app.screen.query_one("#document-scope", Input).value = "business"
        app.screen.query_one("#add-document", Button).press()
        await pilot.pause()
        assert "Added document" in widget_text(app.screen.query_one("#document-notice", Static))

        app.screen.query_one("#export-period-start", Input).value = today.replace(day=1).isoformat()
        app.screen.query_one("#export-period-end", Input).value = today.isoformat()
        await pilot.click("#prepare-period-export")
        await pilot.pause()
        assert exports.pending_export is not None
        app.screen.query_one("#confirm-export", Button).press()
        await pilot.pause()
        assert exports.result is not None
        assert exports.result.output_dir.exists()

        app.screen.query_one("#export-year", Input).value = str(today.year)
        app.screen.query_one("#prepare-year-export", Button).press()
        await pilot.pause()
        assert exports.pending_export is not None
        app.screen.query_one("#confirm-export", Button).press()
        await pilot.pause()
        assert exports.result is not None
        assert any(file_name == "manifest.json" for file_name in exports.result.files)

        app.screen.query_one("#export-packet-year", Input).value = str(today.year)
        app.screen.query_one("#prepare-accountant-packet-export", Button).press()
        await pilot.pause()
        assert exports.pending_export is not None
        app.screen.query_one("#confirm-export", Button).press()
        await pilot.pause()
        assert exports.result is not None
        assert exports.result.zip_path is not None
        assert exports.result.zip_path.exists()


@pytest.mark.asyncio
async def test_tui_status_includes_packet_sections(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("s")
        status_pane = app.screen.query_one("#status-pane", StatusPane)
        await pilot.pause()
        assert status_pane.view is not None
        titles = [section.title for section in status_pane.view.sections]
        assert "Compliance Profile" in titles
        assert "Document Registry" in titles
        assert "Accountant Packet Checklist" in titles
        assert "Missing Packet Items" in titles
        assert "Unknown Packet Items" in titles
        assert app.screen.query_one("#status-year", Input).value == str(date.today().year)


@pytest.mark.asyncio
async def test_tui_help_shows_cli_handoff_without_mutating_ledger(tmp_path) -> None:
    ledger = init_ledger(tmp_path)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        with session_scope(ledger) as session:
            before_count = session.query(JournalEntry).count()
        await pilot.press("h")
        await pilot.pause()
        help_pane = app.screen.query_one("#help-pane", HelpPane)
        commands = help_pane.commands
        assert any("review list" in command.command for command in commands)
        assert any("expense record" in command.command for command in commands)
        with session_scope(ledger) as session:
            after_count = session.query(JournalEntry).count()
        assert before_count == after_count
