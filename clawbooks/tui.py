from __future__ import annotations

from datetime import date
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, DirectoryTree, Footer, Header, Input, Label, Markdown, Static, TabbedContent, TabPane

from clawbooks.config import validate_ledger_dir
from clawbooks.db import inspect_ledger_bootstrap
from clawbooks.exceptions import ValidationError
from clawbooks.tui_facade import REPORTS, TuiFacade
from clawbooks.tui_models import DashboardSummary, ExportResult, HelpCommand, Metric, ReportView, StatusView, TableSection
from clawbooks.utils import format_money, parse_date


def _format_cell(column: str, value: object) -> str:
    if value is None:
        return ""
    if column.endswith("_cents") and isinstance(value, int):
        return format_money(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _pretty_label(column: str) -> str:
    return column.replace("_", " ").title()


class MetricStrip(Static):
    def __init__(self, metrics: list[Metric] | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.metrics = metrics or []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="metrics-row"):
            for metric in self.metrics:
                classes = "metric-card"
                if metric.tone == "warning":
                    classes += " warning"
                yield Static(f"{metric.label}\n[bold]{metric.value}[/bold]", classes=classes)


class SectionTableWidget(Static):
    def __init__(self, section: TableSection, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.section = section

    def compose(self) -> ComposeResult:
        yield Label(self.section.title, classes="section-title")
        if self.section.rows:
            yield DataTable(id="data")
        else:
            yield Static(self.section.empty_message, classes="empty-state")

    def on_mount(self) -> None:
        if not self.section.rows:
            return
        table = self.query_one("#data", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(*[_pretty_label(column) for column in self.section.columns])
        for row in self.section.rows:
            table.add_row(*[_format_cell(column, row.get(column)) for column in self.section.columns])


class DashboardPane(Static):
    def __init__(self, facade: TuiFacade, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facade = facade
        self.summary: DashboardSummary | None = None

    def on_mount(self) -> None:
        self.summary = self.facade.dashboard()
        self.refresh(recompose=True)

    def compose(self) -> ComposeResult:
        if not self.summary:
            yield Static("Loading dashboard...", classes="empty-state")
            return
        yield Static(
            f"[bold]{self.summary.business_name}[/bold]\nLedger: {self.summary.ledger_dir}\nAs of: {self.summary.as_of.isoformat()}",
            id="dashboard-header",
        )
        if self.summary.alerts:
            for alert in self.summary.alerts:
                yield Static(alert, classes="dashboard-alert")
        yield MetricStrip(self.summary.metrics)
        with VerticalScroll(id="dashboard-scroll"):
            for index, section in enumerate(self.summary.sections):
                yield SectionTableWidget(section, id=f"dashboard-section-{index}")


class SectionedView(Static):
    def __init__(
        self,
        *,
        title: str = "",
        metrics: list[Metric] | None = None,
        sections: list[TableSection] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.title = title
        self.metrics = metrics or []
        self.sections = sections or []

    def set_content(self, title: str, metrics: list[Metric], sections: list[TableSection]) -> None:
        self.title = title
        self.metrics = metrics
        self.sections = sections
        self.refresh(recompose=True)

    def compose(self) -> ComposeResult:
        if not self.sections and not self.metrics:
            yield Static("No data loaded.", classes="empty-state")
            return
        if self.title:
            yield Static(f"[bold]{self.title}[/bold]", classes="view-title")
        if self.metrics:
            yield MetricStrip(self.metrics)
        with VerticalScroll(classes="section-scroll"):
            for index, section in enumerate(self.sections):
                yield SectionTableWidget(section, id=f"section-{index}")


class ReportPane(Static):
    def __init__(self, facade: TuiFacade, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facade = facade
        self.current_report_key = "pnl"
        self.current_view: ReportView | None = None

    def on_mount(self) -> None:
        self.load_default_report()

    def compose(self) -> ComposeResult:
        descriptor = REPORTS[self.current_report_key]
        defaults = self.facade.report_defaults(self.current_report_key)
        range_start = defaults["start"].isoformat() if defaults["start"] else ""
        range_end = defaults["end"].isoformat() if defaults["end"] else ""
        as_of = defaults["as_of"].isoformat() if defaults["as_of"] else date.today().isoformat()

        with Horizontal(id="reports-layout"):
            with Vertical(id="report-nav"):
                yield Static("Reports", classes="pane-title")
                for report in REPORTS.values():
                    classes = "report-nav-button"
                    if report.key == self.current_report_key:
                        classes += " active"
                    yield Button(report.title, id=f"report-nav-{report.key}", classes=classes)
            with Vertical(id="report-main"):
                yield Static(f"[bold]{descriptor.title}[/bold]", id="report-title")
                with ContentSwitcher(initial="range-toolbar" if descriptor.mode == "range" else "asof-toolbar", id="report-toolbar-switcher"):
                    with Horizontal(id="range-toolbar"):
                        yield Button("MTD", id="report-preset-mtd")
                        yield Button("QTD", id="report-preset-qtd")
                        yield Button("YTD", id="report-preset-ytd")
                        yield Input(value=range_start, placeholder="YYYY-MM-DD", id="report-start")
                        yield Input(value=range_end, placeholder="YYYY-MM-DD", id="report-end")
                        yield Button("Apply", id="report-apply-range")
                    with Horizontal(id="asof-toolbar"):
                        yield Input(value=as_of, placeholder="YYYY-MM-DD", id="report-as-of")
                        yield Button("Apply", id="report-apply-as-of")
                yield SectionedView(
                    id="report-display",
                    title=self.current_view.title if self.current_view else "",
                    metrics=self.current_view.metrics if self.current_view else [],
                    sections=self.current_view.sections if self.current_view else [],
                )

    def load_default_report(self) -> None:
        defaults = self.facade.report_defaults(self.current_report_key)
        if defaults["mode"] == "range":
            self.current_view = self.facade.report(
                self.current_report_key,
                preset=defaults["preset"],
                start=defaults["start"],
                end=defaults["end"],
            )
        else:
            self.current_view = self.facade.report(self.current_report_key, as_of=defaults["as_of"])
        self.refresh(recompose=True)

    def load_range_report(self, *, preset: str | None = None, start: str | None = None, end: str | None = None) -> None:
        if preset:
            self.current_view = self.facade.report(self.current_report_key, preset=preset)
        else:
            self.current_view = self.facade.report(
                self.current_report_key,
                preset="CUSTOM",
                start=parse_date(start),
                end=parse_date(end),
            )
        self.refresh(recompose=True)

    def load_as_of_report(self, value: str) -> None:
        self.current_view = self.facade.report(self.current_report_key, as_of=parse_date(value))
        self.refresh(recompose=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("report-nav-"):
            self.current_report_key = button_id.removeprefix("report-nav-")
            self.load_default_report()
            return
        if button_id == "report-preset-mtd":
            self.load_range_report(preset="MTD")
            return
        if button_id == "report-preset-qtd":
            self.load_range_report(preset="QTD")
            return
        if button_id == "report-preset-ytd":
            self.load_range_report(preset="YTD")
            return
        if button_id == "report-apply-range":
            start = self.query_one("#report-start", Input).value
            end = self.query_one("#report-end", Input).value
            try:
                self.load_range_report(start=start, end=end)
            except ValidationError as exc:
                self.app.notify(exc.message, title="Invalid dates", severity="error")
            return
        if button_id == "report-apply-as-of":
            value = self.query_one("#report-as-of", Input).value
            try:
                self.load_as_of_report(value)
            except ValidationError as exc:
                self.app.notify(exc.message, title="Invalid date", severity="error")


class StatusPane(Static):
    def __init__(self, facade: TuiFacade, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facade = facade
        self.view: StatusView | None = None
        self.packet_year = date.today().year

    def on_mount(self) -> None:
        self.load_status()

    def load_status(self) -> None:
        self.view = self.facade.status(packet_year=self.packet_year)
        self.refresh(recompose=True)

    def compose(self) -> ComposeResult:
        if not self.view:
            yield Static("Loading status...", classes="empty-state")
            return
        yield Static(
            f"[bold]Status[/bold]\nAs of: {self.view.as_of.isoformat()}\nPacket year: {self.view.packet_year}",
            classes="view-title",
        )
        with Horizontal(id="status-toolbar"):
            yield Input(value=str(self.packet_year), placeholder="YYYY", id="status-year")
            yield Button("Reload", id="reload-status")
        with VerticalScroll(id="status-scroll"):
            for index, section in enumerate(self.view.sections):
                yield SectionTableWidget(section, id=f"status-section-{index}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "reload-status":
            return
        raw_year = self.query_one("#status-year", Input).value
        if not raw_year.isdigit():
            self.app.notify("Packet year must be numeric.", title="Invalid year", severity="error")
            return
        self.packet_year = int(raw_year)
        self.load_status()


class ExportPane(Static):
    def __init__(self, facade: TuiFacade, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facade = facade
        self.pending_export: tuple[str, dict[str, object]] | None = None
        self.result: ExportResult | None = None
        self.error_text = ""
        self.document_notice = ""

    def compose(self) -> ComposeResult:
        month_start = date.today().replace(day=1).isoformat()
        today = date.today().isoformat()
        year = str(date.today().year)
        yield Static("[bold]Exports[/bold]\nUse this pane to add supporting documents and generate accountant-ready export bundles.", classes="view-title")
        with VerticalScroll(id="exports-scroll"):
            with Horizontal(id="exports-layout"):
                with Vertical(classes="export-card"):
                    yield Label("Manual Document Intake", classes="section-title")
                    yield Static(
                        "Examples: stripe_1099_k, estimated_tax_confirmation, prior_year_return, tax_notice",
                        classes="form-note",
                    )
                    yield Input(placeholder="/path/to/file.pdf", id="document-source-path")
                    yield Input(value="stripe_1099_k", placeholder="document type", id="document-type")
                    yield Input(value=year, placeholder="YYYY", id="document-year")
                    yield Input(value="business", placeholder="business or owner", id="document-scope")
                    yield Input(placeholder="YYYY-MM-DD", id="document-period-start")
                    yield Input(placeholder="YYYY-MM-DD", id="document-period-end")
                    yield Input(placeholder="Notes", id="document-notes")
                    yield Button("Add Document", id="add-document")
                with Vertical(classes="export-card"):
                    yield Label("Accountant Packet Export", classes="section-title")
                    yield Input(value=year, placeholder="YYYY", id="export-packet-year")
                    yield Button("Prepare Accountant Packet", id="prepare-accountant-packet-export")
            with Horizontal(id="exports-secondary"):
                with Vertical(classes="export-card"):
                    yield Label("Period-End Export", classes="section-title")
                    yield Input(value=month_start, placeholder="YYYY-MM-DD", id="export-period-start")
                    yield Input(value=today, placeholder="YYYY-MM-DD", id="export-period-end")
                    yield Button("Prepare Period-End Export", id="prepare-period-export")
                with Vertical(classes="export-card"):
                    yield Label("Year-End Export", classes="section-title")
                    yield Input(value=year, placeholder="YYYY", id="export-year")
                    yield Button("Prepare Year-End Export", id="prepare-year-export")
        if self.pending_export:
            kind, values = self.pending_export
            summary = (
                f"Confirm {kind.replace('_', ' ')} export:\n"
                + "\n".join(f"{key}: {value}" for key, value in values.items())
            )
            yield Static(summary, id="export-confirmation")
            with Horizontal(id="export-confirm-buttons"):
                yield Button("Confirm Export", id="confirm-export", variant="success")
                yield Button("Cancel", id="cancel-export")
        if self.error_text:
            yield Static(self.error_text, classes="dashboard-alert")
        if self.document_notice:
            yield Static(self.document_notice, id="document-notice")
        if self.result:
            yield Static(f"Output: {self.result.output_dir}", id="export-output-dir")
            if self.result.zip_path:
                yield Static(f"Zip: {self.result.zip_path}", id="export-zip-path")
            yield SectionTableWidget(
                TableSection(
                    title=self.result.title,
                    columns=["file"],
                    rows=[{"file": file_name} for file_name in self.result.files],
                    empty_message="No export files generated.",
                ),
                id="export-result-table",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        self.error_text = ""
        if button_id == "add-document":
            raw_year = self.query_one("#document-year", Input).value
            if not raw_year.isdigit():
                self.error_text = "Document year must be numeric."
                self.refresh(recompose=True)
                return
            try:
                document = self.facade.add_document(
                    source_path=Path(self.query_one("#document-source-path", Input).value).expanduser(),
                    document_type=self.query_one("#document-type", Input).value.strip(),
                    year=int(raw_year),
                    scope=self.query_one("#document-scope", Input).value.strip() or "business",
                    period_start=parse_date(self.query_one("#document-period-start", Input).value or None),
                    period_end=parse_date(self.query_one("#document-period-end", Input).value or None),
                    notes=self.query_one("#document-notes", Input).value or None,
                )
            except ValidationError as exc:
                self.error_text = exc.message
                self.refresh(recompose=True)
                return
            self.document_notice = f"Added document {document['document_id']}: {document['original_filename']}"
            self.app.notify(self.document_notice, title="Document added")
            self.refresh(recompose=True)
            return
        if button_id == "prepare-period-export":
            try:
                start = parse_date(self.query_one("#export-period-start", Input).value)
                end = parse_date(self.query_one("#export-period-end", Input).value)
            except ValidationError as exc:
                self.error_text = exc.message
                self.refresh(recompose=True)
                return
            self.result = None
            self.pending_export = ("period_end", {"period_start": start.isoformat(), "period_end": end.isoformat()})
            self.refresh(recompose=True)
            return
        if button_id == "prepare-year-export":
            raw_year = self.query_one("#export-year", Input).value
            if not raw_year.isdigit():
                self.error_text = "Year must be numeric."
                self.refresh(recompose=True)
                return
            self.result = None
            self.pending_export = ("year_end", {"year": raw_year})
            self.refresh(recompose=True)
            return
        if button_id == "prepare-accountant-packet-export":
            raw_year = self.query_one("#export-packet-year", Input).value
            if not raw_year.isdigit():
                self.error_text = "Packet year must be numeric."
                self.refresh(recompose=True)
                return
            self.result = None
            self.pending_export = ("accountant_packet", {"year": raw_year})
            self.refresh(recompose=True)
            return
        if button_id == "cancel-export":
            self.pending_export = None
            self.refresh(recompose=True)
            return
        if button_id == "confirm-export" and self.pending_export:
            kind, values = self.pending_export
            if kind == "period_end":
                result = self.facade.export_period_end(
                    start=parse_date(values["period_start"]),
                    end=parse_date(values["period_end"]),
                )
            elif kind == "year_end":
                result = self.facade.export_year_end(year=int(values["year"]))
            else:
                result = self.facade.export_accountant_packet(year=int(values["year"]))
            self.pending_export = None
            self.result = result
            self.refresh(recompose=True)
            self.app.notify(f"{result.title} created at {result.output_dir}", title="Export ready")


class AuditPane(Static):
    def __init__(self, facade: TuiFacade, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.facade = facade
        self.summary: dict[str, object] | None = None
        self.period_result: dict[str, object] | None = None
        self.period_start = date.today().replace(day=1)
        self.period_end = date.today()

    def on_mount(self) -> None:
        self.load_audit()

    def load_audit(self) -> None:
        self.summary = self.facade.audit_summary()
        self.period_result = self.facade.audit_period(period_start=self.period_start, period_end=self.period_end)
        self.refresh(recompose=True)

    def compose(self) -> ComposeResult:
        if not self.summary:
            yield Static("Loading audit...", classes="empty-state")
            return
        summary_counts = self.summary["summary"]
        yield Static("[bold]Audit[/bold]\nIntegrity findings, close readiness, and close-snapshot drift.", classes="view-title")
        yield MetricStrip(
            [
                Metric("Critical", str(summary_counts["critical"]), tone="warning" if summary_counts["critical"] else "default"),
                Metric("High", str(summary_counts["high"]), tone="warning" if summary_counts["high"] else "default"),
                Metric("Medium", str(summary_counts["medium"])),
                Metric("Low", str(summary_counts["low"])),
            ]
        )
        with Horizontal(id="audit-toolbar"):
            yield Input(value=self.period_start.isoformat(), placeholder="YYYY-MM-DD", id="audit-period-start")
            yield Input(value=self.period_end.isoformat(), placeholder="YYYY-MM-DD", id="audit-period-end")
            yield Button("Reload Audit", id="reload-audit")
        with VerticalScroll(id="audit-scroll"):
            yield SectionTableWidget(
                TableSection(
                    "Doctor Findings",
                    ["severity", "category", "title"],
                    [
                        {
                            "severity": finding["severity"],
                            "category": finding["category"],
                            "title": finding["title"],
                        }
                        for finding in self.summary["findings"]
                    ],
                    "No integrity findings.",
                ),
                id="audit-findings",
            )
            if self.period_result:
                yield SectionTableWidget(
                    TableSection(
                        "Period Audit Blocking Findings",
                        ["severity", "category", "title"],
                        [
                            {
                                "severity": finding["severity"],
                                "category": finding["category"],
                                "title": finding["title"],
                            }
                            for finding in self.period_result["blocking_findings"]
                        ],
                        "No blocking findings for the selected period.",
                    ),
                    id="audit-period-blocking",
                )
                yield SectionTableWidget(
                    TableSection(
                        "Period Audit Advisory Findings",
                        ["severity", "category", "title"],
                        [
                            {
                                "severity": finding["severity"],
                                "category": finding["category"],
                                "title": finding["title"],
                            }
                            for finding in self.period_result["advisory_findings"]
                        ],
                        "No advisory findings for the selected period.",
                    ),
                    id="audit-period-advisory",
                )
                drift = self.period_result["snapshot_drift"]
                yield SectionTableWidget(
                    TableSection(
                        "Snapshot Drift Summary",
                        ["component", "count"],
                        [
                            {"component": "accounting_data_drift", "count": len(drift["accounting_data_drift"])},
                            {"component": "admin_state_drift", "count": len(drift["admin_state_drift"])},
                            {"component": "advisory_context_drift", "count": len(drift["advisory_context_drift"])},
                            {"component": "report_version_drift", "count": len(drift["report_version_drift"])},
                        ],
                        "No snapshot drift data available.",
                    ),
                    id="audit-period-drift",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "reload-audit":
            return
        try:
            self.period_start = parse_date(self.query_one("#audit-period-start", Input).value)
            self.period_end = parse_date(self.query_one("#audit-period-end", Input).value)
            self.load_audit()
        except ValidationError as exc:
            self.app.notify(exc.message, title="Invalid audit period", severity="error")


class HelpPane(Static):
    def __init__(self, commands: list[HelpCommand], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.commands = commands

    def compose(self) -> ComposeResult:
        parts = ["# CLI Handoff\n", "These workflows remain CLI-first even though packet exports and document intake are available in the TUI.\n"]
        for command in self.commands:
            parts.append(f"## {command.title}\n")
            parts.append(f"{command.description}\n")
            parts.append(f"```bash\n{command.command}\n```\n")
        yield Markdown("\n".join(parts), id="help-markdown")


class MigrationRequiredScreen(Screen[None]):
    BINDINGS = [Binding("q", "app.quit", "Quit")]

    def __init__(self, *, ledger_dir: Path, migration_state: dict[str, object]) -> None:
        super().__init__()
        self.ledger_dir = ledger_dir
        self.migration_state = migration_state

    def compose(self) -> ComposeResult:
        current_revision = self.migration_state.get("current_revision") or "none"
        expected_head = self.migration_state.get("expected_head") or "unknown"
        yield Header(show_clock=True)
        yield Static(
            "[bold]Migration Required[/bold]\n"
            f"Ledger: {self.ledger_dir}\n"
            f"Current revision: {current_revision}\n"
            f"Expected head: {expected_head}\n\n"
            "Run the CLI migration command before opening this ledger in the TUI.\n\n"
            f"uv run clawbooks --ledger '{self.ledger_dir}' migrate",
            id="migration-required",
        )
        yield Footer()


class LedgerPickerScreen(Screen[Path]):
    BINDINGS = [Binding("q", "app.quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.selected_path: Path | None = None
        self._updating_input = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "[bold]Open Ledger[/bold]\nChoose a ledger directory from the tree or type a path below.",
            id="picker-title",
        )
        with Horizontal(id="picker-body"):
            yield DirectoryTree(str(Path.home()), id="ledger-tree")
            with Vertical(id="picker-panel"):
                yield Input(placeholder="Path to ledger directory", id="ledger-path")
                yield Button("Open Ledger", id="open-ledger", disabled=True, variant="success")
                yield Static("Select a directory containing ledger.db and config.toml.", id="picker-status")
        yield Footer()

    def _set_candidate(self, path: Path) -> None:
        self.selected_path = path
        self._updating_input = True
        self.query_one("#ledger-path", Input).value = str(path)
        self._updating_input = False
        if TuiFacade.is_ledger_dir(path):
            self.query_one("#picker-status", Static).update(f"Ready: {path}")
            self.query_one("#open-ledger", Button).disabled = False
        else:
            self.query_one("#picker-status", Static).update(f"Not a clawbooks ledger: {path}")
            self.query_one("#open-ledger", Button).disabled = True

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._set_candidate(event.path)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.query_one("#picker-status", Static).update(f"Select a directory, not a file: {event.path.name}")
        self.query_one("#open-ledger", Button).disabled = True

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "ledger-path" or self._updating_input:
            return
        candidate = Path(event.value).expanduser()
        self.selected_path = candidate
        if TuiFacade.is_ledger_dir(candidate):
            self.query_one("#picker-status", Static).update(f"Ready: {candidate}")
            self.query_one("#open-ledger", Button).disabled = False
        else:
            self.query_one("#picker-status", Static).update(f"Not a clawbooks ledger: {candidate}")
            self.query_one("#open-ledger", Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-ledger" and self.selected_path and TuiFacade.is_ledger_dir(self.selected_path):
            self.dismiss(self.selected_path.resolve())


class MainScreen(Screen[None]):
    def __init__(self, facade: TuiFacade) -> None:
        super().__init__()
        self.facade = facade

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard", id="main-tabs"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardPane(self.facade, id="dashboard-pane")
            with TabPane("Reports", id="reports"):
                yield ReportPane(self.facade, id="reports-pane")
            with TabPane("Status", id="status"):
                yield StatusPane(self.facade, id="status-pane")
            with TabPane("Audit", id="audit"):
                yield AuditPane(self.facade, id="audit-pane")
            with TabPane("Exports", id="exports"):
                yield ExportPane(self.facade, id="exports-pane")
            with TabPane("Help", id="help"):
                yield HelpPane(self.facade.help_commands(), id="help-pane")
        yield Footer()

    def action_show_section(self, section: str) -> None:
        self.query_one("#main-tabs", TabbedContent).active = section


class ClawbooksTuiApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #picker-title,
    .view-title,
    #dashboard-header,
    .pane-title {
        padding: 1 2;
    }

    #picker-body,
    #main-tabs,
    #dashboard-scroll,
    #status-scroll,
    .section-scroll,
    #reports-layout,
    #report-main,
    #exports-layout,
    #exports-secondary,
    #exports-scroll {
        height: 1fr;
    }

    #ledger-tree {
        width: 1fr;
        min-width: 40;
    }

    #picker-panel {
        width: 50;
        padding: 1 2;
    }

    .metrics-row {
        height: auto;
        padding: 0 1 1 1;
    }

    .metric-card {
        width: 1fr;
        min-height: 5;
        padding: 1;
        margin: 0 1 0 0;
        border: round $surface;
        background: $boost;
    }

    .metric-card.warning,
    .dashboard-alert {
        color: $warning;
    }

    .section-title {
        padding: 1 1 0 1;
        text-style: bold;
    }

    .empty-state {
        padding: 1 2;
        color: $text-muted;
    }

    #report-nav {
        width: 28;
        padding: 1;
        border-right: solid $panel;
    }

    .report-nav-button {
        width: 1fr;
        margin-bottom: 1;
    }

    .report-nav-button.active {
        text-style: bold;
    }

    #report-main,
    #status-pane,
    #dashboard-pane,
    #exports-pane,
    #help-pane {
        padding: 1;
    }

    #range-toolbar,
    #asof-toolbar,
    #export-confirm-buttons,
    #status-toolbar {
        height: auto;
        padding: 0 1 1 1;
    }

    Input {
        margin-right: 1;
    }

    .form-note {
        padding: 0 0 1 0;
        color: $text-muted;
    }

    .export-card {
        width: 1fr;
        padding: 1;
        margin-right: 1;
        border: round $surface;
    }

    #export-confirmation,
    #export-output-dir,
    #export-zip-path,
    #document-notice {
        padding: 1;
    }

    Markdown {
        padding: 1;
    }
    """

    BINDINGS = [
        Binding("d", "show_section('dashboard')", "Dashboard"),
        Binding("r", "show_section('reports')", "Reports"),
        Binding("s", "show_section('status')", "Status"),
        Binding("a", "show_section('audit')", "Audit"),
        Binding("e", "show_section('exports')", "Exports"),
        Binding("h", "show_section('help')", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ledger_dir: Path | None = None) -> None:
        super().__init__()
        self.initial_ledger_dir = ledger_dir
        self.facade: TuiFacade | None = None

    def on_mount(self) -> None:
        if self.initial_ledger_dir:
            self._open_ledger(validate_ledger_dir(self.initial_ledger_dir))
        else:
            self.push_screen(LedgerPickerScreen(), callback=self._ledger_selected)

    def _ledger_selected(self, path: Path | None) -> None:
        if path:
            self._open_ledger(path)

    def _open_ledger(self, ledger_dir: Path) -> None:
        migration_state = inspect_ledger_bootstrap(ledger_dir)
        if not migration_state["full_open_safe"]:
            self.push_screen(MigrationRequiredScreen(ledger_dir=ledger_dir, migration_state=migration_state))
            return
        self.facade = TuiFacade(ledger_dir)
        self.push_screen(MainScreen(self.facade))

    def action_show_section(self, section: str) -> None:
        if isinstance(self.screen, MainScreen):
            self.screen.action_show_section(section)
