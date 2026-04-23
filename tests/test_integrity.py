from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select
from textual.widgets import Static, TabbedContent

from clawbooks.cli import app
from clawbooks.db import _alembic_config, _fingerprint_diff, _legacy_baseline_actual_spec, session_scope
from clawbooks.legacy_baseline import LEGACY_BASELINE_SPEC
from clawbooks.models import AuditEvent, CloseSnapshot, Document, JournalEntry, JournalLine, PeriodLock, ReviewBlocker, SettlementApplication
from clawbooks.tui import ClawbooksTuiApp, MigrationRequiredScreen
from clawbooks.utils import utcnow
from tests.helpers import add_document, init_ledger, runner


def invoke(ledger: Path, *args: str):
    return runner.invoke(app, ["--ledger", str(ledger), "--json", *args])


def payload(result) -> dict:
    return json.loads(result.stdout)


def legacyize_ledger(ledger: Path) -> None:
    connection = sqlite3.connect(ledger / "ledger.db")
    try:
        connection.execute("DROP TABLE alembic_version")
        connection.execute("DROP TABLE close_snapshots")
        connection.execute("DROP TABLE audit_events")
        connection.execute("DROP INDEX IF EXISTS ix_documents_jurisdiction")
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            CREATE TABLE documents_legacy (
                id INTEGER NOT NULL PRIMARY KEY,
                document_type VARCHAR(64) NOT NULL,
                tax_year INTEGER NOT NULL,
                period_start DATE,
                period_end DATE,
                scope VARCHAR(32) NOT NULL,
                original_filename VARCHAR(255) NOT NULL,
                stored_path VARCHAR(500) NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                notes TEXT,
                created_via VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents_legacy (
                id, document_type, tax_year, period_start, period_end, scope,
                original_filename, stored_path, sha256, notes, created_via, created_at
            )
            SELECT
                id, document_type, tax_year, period_start, period_end, scope,
                original_filename, stored_path, sha256, notes, created_via, created_at
            FROM documents
            """
        )
        connection.execute("DROP TABLE documents")
        connection.execute("ALTER TABLE documents_legacy RENAME TO documents")
        connection.execute("CREATE INDEX ix_documents_document_type ON documents (document_type)")
        connection.execute("CREATE INDEX ix_documents_tax_year ON documents (tax_year)")
        connection.execute("CREATE INDEX ix_documents_scope ON documents (scope)")
        connection.execute("CREATE INDEX ix_documents_sha256 ON documents (sha256)")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.commit()
    finally:
        connection.close()


def journal_line_id(ledger: Path, *, description: str, account_code: str) -> int:
    with session_scope(ledger) as session:
        value = session.scalar(
            select(JournalLine.id)
            .join(JournalEntry)
            .join(JournalLine.account)
            .where(JournalEntry.description == description, JournalLine.account.has(code=account_code))
        )
    assert value is not None
    return int(value)


def test_doctor_bootstrap_mode_and_migrate_legacy_ledger(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    legacyize_ledger(ledger)

    blocked = invoke(ledger, "coa", "show")
    blocked_body = payload(blocked)
    assert blocked.exit_code == 2
    assert "clawbooks migrate" in blocked_body["errors"][0]

    bootstrap = invoke(ledger, "doctor")
    bootstrap_body = payload(bootstrap)
    assert bootstrap.exit_code == 7
    assert bootstrap_body["data"]["mode"] == "bootstrap"
    assert bootstrap_body["data"]["full_checks_available"] is False

    migrated = invoke(ledger, "migrate")
    migrated_body = payload(migrated)
    assert migrated.exit_code == 0
    assert migrated_body["data"]["migration_state"]["full_open_safe"] is True

    full = invoke(ledger, "doctor")
    full_body = payload(full)
    assert full.exit_code == 7
    assert full_body["data"]["mode"] == "full"
    assert full_body["data"]["summary"]["high"] == 0
    assert any(
        finding["id"].startswith("year_checklist_missing::")
        for finding in full_body["data"]["findings"]
    )


def test_migrate_rejects_schema_fingerprint_mismatch(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    legacyize_ledger(ledger)
    connection = sqlite3.connect(ledger / "ledger.db")
    try:
        connection.execute("DROP TABLE settings")
        connection.commit()
    finally:
        connection.close()

    result = invoke(ledger, "migrate")
    body = payload(result)
    assert result.exit_code == 2
    assert "legacy baseline" in body["errors"][0]
    assert "settings" in body["data"]["migration_state"]["legacy_baseline_fingerprint_diff"]["missing_tables"]


def test_period_close_persists_snapshot_and_audit_reports_admin_state_drift(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    with session_scope(ledger) as session:
        assert session.scalar(select(CloseSnapshot)) is not None
        close_events = list(session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "close_snapshot")))
        assert close_events

    profile_update = invoke(
        ledger,
        "compliance",
        "profile",
        "update",
        "--json",
        json.dumps({"sales_tax_profile_confirmed": True}),
    )
    assert profile_update.exit_code == 0

    audit = invoke(
        ledger,
        "period",
        "audit",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    body = payload(audit)
    assert audit.exit_code == 0
    assert body["data"]["latest_close_snapshot"] is not None
    assert body["data"]["snapshot_drift"]["accounting_data_drift"] == []
    assert body["data"]["snapshot_drift"]["admin_state_drift"]

    with session_scope(ledger) as session:
        profile_events = list(session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "compliance_profile")))
        assert profile_events


def test_document_changes_append_admin_audit_events(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    source_path = tmp_path / "notice.pdf"
    source_path.write_text("notice", encoding="utf-8")

    created = invoke(
        ledger,
        "document",
        "add",
        "--source-path",
        str(source_path),
        "--type",
        "tax_notice",
        "--year",
        "2026",
        "--scope",
        "business",
    )
    created_body = payload(created)
    assert created.exit_code == 0
    document_id = created_body["data"]["document"]["document_id"]

    updated = invoke(
        ledger,
        "document",
        "update",
        "--document-id",
        str(document_id),
        "--notes",
        "updated",
    )
    assert updated.exit_code == 0

    with session_scope(ledger) as session:
        rows = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_type == "document", AuditEvent.entity_ref == str(document_id))
                .order_by(AuditEvent.id)
            )
        )
        assert [row.action for row in rows] == ["create", "update"]


def test_period_audit_treats_reopened_period_as_historical_context(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
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
        "period",
        "reopen",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
        "--reason",
        "fixing classification",
    ).exit_code == 0

    result = invoke(
        ledger,
        "period",
        "audit",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    body = payload(result)
    assert result.exit_code == 0
    assert body["data"]["current_status"]["status"] == "reopen"
    assert body["data"]["snapshot_drift"]["historical_only"] is True
    assert body["data"]["advisory_findings"] == []


def test_doctor_lifts_closed_period_blocking_findings_for_effectively_closed_intervals(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    with session_scope(ledger) as session:
        session.add(
            ReviewBlocker(
                blocker_type="manual_review",
                provider="manual",
                external_id="closed-period-blocker",
                status="open",
                blocker_date=date(2026, 4, 20),
                opened_at=utcnow(),
            )
        )
        session.commit()

    audit = invoke(
        ledger,
        "period",
        "audit",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    )
    assert audit.exit_code == 0
    assert payload(audit)["data"]["closable_now"] is False

    result = invoke(ledger, "doctor", "--year", "2026")
    body = payload(result)
    assert result.exit_code == 7
    assert any(
        finding["id"].startswith("closed_period_regression::2026-04-01::2026-04-30")
        for finding in body["data"]["findings"]
    )


def test_doctor_treats_partially_reopened_close_interval_as_historical(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke(
        ledger,
        "period",
        "close",
        "--period-start",
        "2026-04-01",
        "--period-end",
        "2026-04-30",
    ).exit_code == 0

    with session_scope(ledger) as session:
        session.add(
            PeriodLock(
                period_start=date(2026, 4, 15),
                period_end=date(2026, 5, 15),
                lock_type="period",
                action="reopen",
                reason="partial reopen",
                created_at=utcnow(),
            )
        )
        session.add(
            ReviewBlocker(
                blocker_type="manual_review",
                provider="manual",
                external_id="historical-interval-blocker",
                status="open",
                blocker_date=date(2026, 4, 20),
                opened_at=utcnow(),
            )
        )
        session.commit()

    result = invoke(ledger, "doctor", "--year", "2026")
    body = payload(result)
    assert not any(
        finding["id"].startswith("closed_period_regression::2026-04-01::2026-04-30")
        for finding in body["data"]["findings"]
    )


def test_doctor_reports_settlement_corruption_and_invariant_violations(tmp_path: Path) -> None:
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
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-21",
        "--description",
        "Card activity",
        "--line",
        "5110:20.00",
        "--line",
        "2000:-20.00",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-22",
        "--description",
        "Other accrual",
        "--line",
        "1100:70.00",
        "--line",
        "4000:-70.00",
        "--non-cash",
    ).exit_code == 0
    assert invoke(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-04-23",
        "--description",
        "Owner funded expense",
        "--line",
        "5110:30.00",
        "--line",
        "3000:-30.00",
    ).exit_code == 0

    with session_scope(ledger) as session:
        revenue_source_id = journal_line_id(ledger, description="Accrual revenue", account_code="4000")
        receivable_source_id = journal_line_id(ledger, description="Accrual revenue", account_code="1100")
        bank_line_id = journal_line_id(ledger, description="Customer payment", account_code="1000")
        card_line_id = journal_line_id(ledger, description="Card activity", account_code="2000")
        unsupported_line_id = journal_line_id(ledger, description="Other accrual", account_code="1100")
        owner_contribution_line_id = journal_line_id(ledger, description="Owner funded expense", account_code="3000")
        expense_source_id = journal_line_id(ledger, description="Owner funded expense", account_code="5110")
        session.add_all(
            [
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=bank_line_id,
                    applied_amount_cents=0,
                    applied_date=date(2026, 4, 20),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=bank_line_id,
                    applied_amount_cents=15000,
                    applied_date=date(2026, 4, 20),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=card_line_id,
                    applied_amount_cents=1000,
                    applied_date=date(2026, 4, 21),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=unsupported_line_id,
                    applied_amount_cents=1000,
                    applied_date=date(2026, 4, 22),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=owner_contribution_line_id,
                    applied_amount_cents=1000,
                    applied_date=date(2026, 4, 23),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=revenue_source_id,
                    settlement_line_id=bank_line_id,
                    applied_amount_cents=500,
                    applied_date=date(2026, 4, 1),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=receivable_source_id,
                    settlement_line_id=bank_line_id,
                    applied_amount_cents=500,
                    applied_date=date(2026, 4, 20),
                    application_type="manual",
                    created_at=utcnow(),
                ),
                SettlementApplication(
                    source_line_id=expense_source_id,
                    settlement_line_id=card_line_id,
                    applied_amount_cents=500,
                    applied_date=date(2026, 4, 23),
                    application_type="manual",
                    created_at=utcnow(),
                ),
            ]
        )
        session.commit()

    result = invoke(ledger, "doctor", "--year", "2026")
    body = payload(result)
    ids = {finding["id"] for finding in body["data"]["findings"]}
    assert result.exit_code == 7
    assert any(item.startswith("settlement_non_positive::") for item in ids)
    assert any(item.startswith("settlement_source_overapplied::") for item in ids)
    assert any(item.startswith("settlement_cash_overapplied::") for item in ids)
    assert any(item.startswith("settlement_same_sign::") for item in ids)
    assert any(item.startswith("settlement_unsupported_account::") for item in ids)
    assert any(item.startswith("settlement_owner_contribution_non_expense::") for item in ids)
    assert any(item.startswith("settlement_invalid_applied_date::") for item in ids)
    assert any(item.startswith("settlement_invalid_source_kind::") for item in ids)
    assert any(item.startswith("settlement_immediate_cash_source::") for item in ids)


def test_doctor_year_scopes_documents_and_export_manifests(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    source_2025 = tmp_path / "2025-notice.pdf"
    source_2026 = tmp_path / "2026-notice.pdf"
    source_2025.write_text("2025", encoding="utf-8")
    source_2026.write_text("2026", encoding="utf-8")
    add_document(ledger, source_path=source_2025, document_type="tax_notice", year=2025)
    add_document(ledger, source_path=source_2026, document_type="tax_notice", year=2026)

    with session_scope(ledger) as session:
        documents = {
            document.tax_year: document
            for document in session.scalars(select(Document).where(Document.tax_year.in_([2025, 2026])))
        }
    (ledger / documents[2025].stored_path).unlink()
    (ledger / documents[2026].stored_path).unlink()

    exports_dir = ledger / "exports"
    export_2025 = exports_dir / "books-2025"
    export_2025.mkdir(parents=True, exist_ok=True)
    (export_2025 / "manifest.json").write_text(
        json.dumps(
            {
                "name": "year-end_2025",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "files": ["pnl.json"],
            }
        ),
        encoding="utf-8",
    )
    export_2026 = exports_dir / "packet-2026"
    export_2026.mkdir(parents=True, exist_ok=True)
    (export_2026 / "manifest.json").write_text(
        json.dumps(
            {
                "name": "accountant-packet_2026",
                "year": 2026,
                "files": ["checklist.json"],
            }
        ),
        encoding="utf-8",
    )
    unscoped = exports_dir / "mystery"
    unscoped.mkdir(parents=True, exist_ok=True)
    (unscoped / "manifest.json").write_text(json.dumps({"name": "mystery", "files": ["unknown.json"]}), encoding="utf-8")

    result = invoke(ledger, "doctor", "--year", "2026")
    body = payload(result)
    findings = body["data"]["findings"]
    missing_document_ids = {finding["data"]["document_id"] for finding in findings if finding["id"].startswith("missing_document::")}
    missing_manifest_years = {
        finding["data"]["associated_year"]
        for finding in findings
        if finding["id"].startswith("manifest_missing::")
    }

    assert result.exit_code == 7
    assert documents[2026].id in missing_document_ids
    assert documents[2025].id not in missing_document_ids
    assert missing_manifest_years == {2026}
    assert any(finding["id"].startswith("unscoped_year_artifact::") for finding in findings)


def test_doctor_surfaces_year_scoped_checklist_findings(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)

    result = invoke(ledger, "doctor", "--year", "2026")
    body = payload(result)

    assert result.exit_code == 7
    assert any(
        finding["id"] == "year_checklist_missing::2026::year_end_books_package"
        for finding in body["data"]["findings"]
    )


def test_frozen_legacy_baseline_matches_0001_initial_schema(tmp_path: Path) -> None:
    ledger = tmp_path / "baseline-ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(ledger), "0001_initial")

    connection = sqlite3.connect(ledger / "ledger.db")
    try:
        actual = _legacy_baseline_actual_spec(connection)
    finally:
        connection.close()

    diff = _fingerprint_diff(actual, LEGACY_BASELINE_SPEC)
    assert diff == {"missing_tables": [], "unexpected_tables": [], "table_diffs": {}}


@pytest.mark.asyncio
async def test_tui_shows_migration_required_state_for_non_head_ledger(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    legacyize_ledger(ledger)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MigrationRequiredScreen)
        text = str(app.screen.query_one("#migration-required", Static).content)
        assert "Migration Required" in text


@pytest.mark.asyncio
async def test_tui_exposes_audit_tab(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    app = ClawbooksTuiApp(ledger_dir=ledger)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tabs = app.screen.query_one("#main-tabs", TabbedContent)
        await pilot.press("a")
        assert tabs.active == "audit"
