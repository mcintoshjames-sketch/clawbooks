from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from clawbooks.db import session_scope
from clawbooks.models import Attachment, Document, DocumentLink
from clawbooks.schemas import StripeEvent
from tests.helpers import add_document, init_ledger, invoke_cli


def payload(result) -> dict:
    assert result.stdout, result
    return json.loads(result.stdout)


def write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_expense_receipt_creates_linked_document(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    receipt = write_text(tmp_path / "receipt.pdf", "receipt-content")

    result = invoke_cli(
        ledger,
        "expense",
        "record",
        "--date",
        "2026-04-03",
        "--vendor",
        "Hosting Vendor",
        "--amount",
        "25.00",
        "--category",
        "5110",
        "--payment-account",
        "1000",
        "--receipt-path",
        str(receipt),
    )

    body = payload(result)
    assert result.exit_code == 0
    document_id = body["data"]["document_id"]
    assert document_id is not None

    with session_scope(ledger) as session:
        document = session.get(Document, document_id)
        assert document is not None
        assert document.document_type == "expense_receipt"
        assert document.scope == "business"
        assert (ledger / document.stored_path).exists()
        links = session.query(DocumentLink).filter_by(document_id=document_id).all()
        assert [(link.target_type, link.target_id) for link in links] == [("journal_entry", body["data"]["entry_id"])]


def test_statement_documents_link_from_import_and_reconciliation(tmp_path: Path) -> None:
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

    imported = invoke_cli(
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
    import_body = payload(imported)
    assert imported.exit_code == 0

    statement_path = write_text(
        tmp_path / "stripe_statement.csv",
        "date,description,amount,external_ref\n2026-04-08,Stripe payout,100.00,st-1\n",
    )
    started = invoke_cli(
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
        "--statement-starting-balance",
        "0.00",
        "--statement-ending-balance",
        "100.00",
    )
    start_body = payload(started)
    assert started.exit_code == 0

    with session_scope(ledger) as session:
        documents = session.query(Document).order_by(Document.id).all()
        assert [document.document_type for document in documents] == ["bank_statement", "stripe_statement"]
        bank_links = session.query(DocumentLink).filter_by(document_id=documents[0].id).all()
        assert {link.target_type for link in bank_links} == {"reconciliation_session", "import_run"}
        stripe_links = session.query(DocumentLink).filter_by(document_id=documents[1].id).all()
        assert [(link.target_type, link.target_id) for link in stripe_links] == [
            ("reconciliation_session", start_body["data"]["session_id"])
        ]
        assert any(link.target_id == import_body["data"]["import_run_id"] for link in bank_links)
        assert (ledger / documents[0].stored_path).exists()
        assert (ledger / documents[1].stored_path).exists()


def test_document_checklist_uses_advisory_unknowns_and_real_books_exports(tmp_path: Path, monkeypatch) -> None:
    ledger = init_ledger(tmp_path)

    def fake_fetch(_api_key, _start, _end, timezone_name=None):
        return [
            StripeEvent(
                external_id="stripe_charge_1",
                event_type="charge",
                occurred_at=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
                amount_cents=10800,
                fee_cents=300,
                tax_cents=800,
                net_cents=10500,
                description="Monthly subscription",
            )
        ]

    monkeypatch.setattr("clawbooks.ledger.fetch_stripe_events", fake_fetch)
    imported = invoke_cli(
        ledger,
        "import",
        "stripe",
        "--from-date",
        "2026-04-01",
        "--to-date",
        "2026-04-30",
    )
    assert imported.exit_code == 0

    checklist_before = payload(invoke_cli(ledger, "document", "checklist", "--year", "2026"))
    rows_before = {row["item_key"]: row for row in checklist_before["data"]["rows"]}
    assert rows_before["year_end_books_package"]["status"] == "missing"
    assert rows_before["stripe_tax_documents"]["status"] == "missing"
    assert rows_before["sales_tax_returns"]["status"] == "unknown"
    assert rows_before["sales_tax_payments"]["status"] == "unknown"
    assert rows_before["payroll_documents"]["status"] == "unknown"
    assert rows_before["contractor_documents"]["status"] == "unknown"

    stripe_form = write_text(tmp_path / "stripe_1099_k.pdf", "stripe-tax-form")
    add_document(ledger, source_path=stripe_form, document_type="stripe_1099_k", year=2026)
    assert invoke_cli(ledger, "export", "year-end", "--year", "2026").exit_code == 0

    checklist_after = payload(invoke_cli(ledger, "document", "checklist", "--year", "2026"))
    rows_after = {row["item_key"]: row for row in checklist_after["data"]["rows"]}
    assert rows_after["year_end_books_package"]["status"] == "present"
    assert rows_after["stripe_tax_documents"]["status"] == "present"


def test_document_checklist_uses_slot_based_tax_support_matching(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke_cli(
        ledger,
        "compliance",
        "profile",
        "update",
        "--json",
        json.dumps(
            {
                "sales_tax_profile_confirmed": True,
                "sales_tax_registrations": [
                    {"jurisdiction": "illinois", "filing_cadence": "monthly", "active": True}
                ],
                "owner_tracking": {"estimated_tax_confirmations": True},
            }
        ),
    ).exit_code == 0

    january_return = write_text(tmp_path / "il-jan-return.pdf", "jan-return")
    estimated_q1 = write_text(tmp_path / "estimated-q1.pdf", "q1")
    add_document(
        ledger,
        source_path=january_return,
        document_type="illinois_sales_tax_return",
        year=2026,
        jurisdiction="illinois",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
    )
    add_document(
        ledger,
        source_path=estimated_q1,
        document_type="estimated_tax_confirmation",
        year=2026,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )

    checklist = payload(invoke_cli(ledger, "document", "checklist", "--year", "2026"))
    rows = {row["item_key"]: row for row in checklist["data"]["rows"]}
    assert rows["sales_tax_returns"]["status"] == "missing"
    assert len(rows["sales_tax_returns"]["slot_details"]) == 12
    assert rows["estimated_tax_confirmations"]["status"] == "missing"
    assert len(rows["estimated_tax_confirmations"]["slot_details"]) == 4
    assert rows["sales_tax_payments"]["status"] == "unknown"


def test_document_checklist_requires_reconciliation_linked_statement_support(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    assert invoke_cli(
        ledger,
        "journal",
        "add",
        "--date",
        "2026-03-31",
        "--description",
        "Opening bank balance",
        "--line",
        "1000:500.00",
        "--line",
        "3000:-500.00",
    ).exit_code == 0

    generic_statement = write_text(tmp_path / "bank-statement.pdf", "generic-statement")
    add_document(ledger, source_path=generic_statement, document_type="bank_statement", year=2026)

    checklist = payload(invoke_cli(ledger, "document", "checklist", "--year", "2026"))
    rows = {row["item_key"]: row for row in checklist["data"]["rows"]}
    assert rows["bank_statement_support"]["status"] == "missing"
    assert rows["bank_statement_support"]["document_count"] == 0
    assert rows["bank_statement_support"]["required_count"] == 1


def test_accountant_packet_export_includes_documents_and_excludes_legacy_attachments(tmp_path: Path) -> None:
    ledger = init_ledger(tmp_path)
    receipt = write_text(tmp_path / "receipt.pdf", "receipt-content")
    prior_year_return = write_text(tmp_path / "prior-year-return.pdf", "return-content")
    legacy_path = write_text(ledger / "attachments" / "legacy_only.pdf", "legacy-content")

    recorded = invoke_cli(
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
        "--receipt-path",
        str(receipt),
    )
    assert recorded.exit_code == 0
    add_document(ledger, source_path=prior_year_return, document_type="prior_year_return", year=2026, scope="owner")

    with session_scope(ledger) as session:
        session.add(Attachment(path=str(legacy_path), sha256="legacy", description="Legacy", created_at=datetime.now(UTC)))
        session.commit()

    exported = invoke_cli(ledger, "export", "accountant-packet", "--year", "2026")
    body = payload(exported)
    assert exported.exit_code == 0
    output_dir = Path(body["data"]["output_dir"])
    zip_path = Path(body["data"]["zip_path"])
    assert output_dir.exists()
    assert zip_path.exists()
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "checklist.json").exists()
    assert (output_dir / "document_index.csv").exists()
    assert (output_dir / "missing_items.json").exists()
    assert (output_dir / "compliance_profile.json").exists()
    assert (output_dir / "cash_basis_snapshot.json").exists()
    assert (output_dir / "assumptions.json").exists()

    document_index = json.loads((output_dir / "document_index.json").read_text(encoding="utf-8"))
    document_types = {row["document_type"] for row in document_index["rows"]}
    assert {"expense_receipt", "prior_year_return"} <= document_types
    packet_files = {str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()}
    assert any(file_name.startswith("documents/") for file_name in packet_files)
    assert "documents/legacy_only.pdf" not in packet_files

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["advisory"] is True
    assert manifest["cash_basis_snapshot_file"] == "cash_basis_snapshot.json"
    assert "ignored_invalid_settlement_applications" in manifest
