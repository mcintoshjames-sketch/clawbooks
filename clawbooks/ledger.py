from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from clawbooks.defaults import DEFAULT_ACCOUNTS
from clawbooks.exceptions import ComplianceError, ImportConflictError, LockedPeriodError, ReconciliationError, ValidationError
from clawbooks.models import (
    Account,
    Attachment,
    AuditEvent,
    Document,
    DocumentLink,
    ExternalEvent,
    ExternalEventRefreshHistory,
    ImportRun,
    JournalEntry,
    JournalLine,
    PeriodLock,
    ReconciliationLine,
    ReconciliationMatch,
    ReconciliationSession,
    ReconciliationSessionEvent,
    ReviewBlocker,
    Setting,
    SettlementApplication,
    TaxObligation,
)
from clawbooks.schemas import (
    AppConfig,
    CSVImportProfile,
    ComplianceProfile,
    SalesTaxPaymentSlot,
    StripeEvent,
    StripeFetchResult,
    StripeUnsupportedEvent,
)
from clawbooks.stripe_client import fetch_stripe_event, fetch_stripe_events
from clawbooks.utils import json_dumps, parse_date, parse_money, read_csv_rows, sha256_for_path, stable_external_id, utcnow

ACCOUNT_KINDS = {"asset", "liability", "equity", "revenue", "expense", "contra_revenue"}
FINANCIAL_SUBTYPES = {"bank", "card", "stripe_clearing"}
P_AND_L_KINDS = {"revenue", "expense", "contra_revenue"}
SETTLEMENT_APPLICATION_TYPES = {"manual", "cash_receipt", "cash_disbursement", "owner_paid", "reimbursement_auto"}
DOCUMENT_SCOPES = {"business", "owner"}
STATEMENT_DOCUMENT_TYPES = {
    "bank": "bank_statement",
    "card": "card_statement",
    "stripe_clearing": "stripe_statement",
}
DOCUMENT_TYPES = {
    "bank_statement",
    "card_statement",
    "contractor_1099_nec",
    "contractor_w9",
    "estimated_tax_confirmation",
    "expense_receipt",
    "illinois_sales_tax_payment",
    "illinois_sales_tax_return",
    "payroll_report",
    "payroll_tax_form",
    "prior_year_return",
    "stripe_1099_k",
    "stripe_statement",
    "stripe_tax_summary",
    "tax_notice",
}
REVIEW_RESOLUTION_TYPES = {"skip", "post_with_override", "superseded_by_manual_entry"}
MANUAL_SOURCE_TYPES = {"manual", "owner_contribution", "owner_draw"}
UNSET = object()


@dataclass(slots=True)
class JournalLineInput:
    account_code: str
    amount_cents: int
    memo: str | None = None


def _sanitize_document_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "document"


def _normalize_jurisdiction(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", "_", value.strip().lower())
    aliases = {
        "il": "illinois",
        "ill": "illinois",
        "irs": "federal",
        "us": "federal",
        "usa": "federal",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized or None


def _document_relative_path(*, document_type: str, tax_year: int, source_path: Path, sha256: str) -> Path:
    filename = _sanitize_document_filename(source_path.name)
    return Path("attachments") / "documents" / str(tax_year) / document_type / f"{sha256[:16]}_{filename}"


def _copy_document_to_ledger(
    ledger_dir: Path,
    *,
    source_path: Path,
    document_type: str,
    tax_year: int,
    dry_run: bool = False,
) -> tuple[str, str]:
    resolved = source_path.expanduser().resolve()
    if not resolved.exists():
        raise ValidationError(f"Document path does not exist: {resolved}")
    if not resolved.is_file():
        raise ValidationError(f"Document path is not a file: {resolved}")

    sha256 = sha256_for_path(resolved)
    relative_path = _document_relative_path(
        document_type=document_type,
        tax_year=tax_year,
        source_path=resolved,
        sha256=sha256,
    )
    if not dry_run:
        destination = ledger_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(resolved, destination)
    return sha256, str(relative_path)


def _validate_document_metadata(
    *,
    document_type: str,
    tax_year: int,
    jurisdiction: str | None,
    scope: str,
    period_start: date | None,
    period_end: date | None,
) -> None:
    if document_type not in DOCUMENT_TYPES:
        raise ValidationError(f"Unsupported document type: {document_type}")
    if scope not in DOCUMENT_SCOPES:
        raise ValidationError(f"Unsupported document scope: {scope}")
    if tax_year < 1900 or tax_year > 9999:
        raise ValidationError(f"Invalid tax year: {tax_year}")
    if period_start and period_end and period_start > period_end:
        raise ValidationError("Document period start cannot be after period end")
    if jurisdiction is not None and not _normalize_jurisdiction(jurisdiction):
        raise ValidationError("Document jurisdiction cannot be blank")


def _resolve_document_targets(
    session: Session,
    *,
    journal_entry_id: int | None,
    reconciliation_session_id: int | None,
    tax_obligation_code: str | None,
    import_run_id: int | None,
) -> list[tuple[str, int]]:
    targets: list[tuple[str, int]] = []
    if journal_entry_id is not None:
        if not session.get(JournalEntry, journal_entry_id):
            raise ValidationError(f"Unknown journal entry: {journal_entry_id}")
        targets.append(("journal_entry", journal_entry_id))
    if reconciliation_session_id is not None:
        if not session.get(ReconciliationSession, reconciliation_session_id):
            raise ValidationError(f"Unknown reconciliation session: {reconciliation_session_id}")
        targets.append(("reconciliation_session", reconciliation_session_id))
    if tax_obligation_code is not None:
        obligation = session.scalar(select(TaxObligation).where(TaxObligation.code == tax_obligation_code))
        if not obligation:
            raise ValidationError(f"Unknown tax obligation: {tax_obligation_code}")
        targets.append(("tax_obligation", obligation.id))
    if import_run_id is not None:
        if not session.get(ImportRun, import_run_id):
            raise ValidationError(f"Unknown import run: {import_run_id}")
        targets.append(("import_run", import_run_id))
    return targets


def _document_links_payload(document: Document) -> list[dict[str, object]]:
    return [
        {"target_type": link.target_type, "target_id": link.target_id}
        for link in sorted(document.links, key=lambda item: (item.target_type, item.target_id))
    ]


def _document_payload(document: Document) -> dict[str, object]:
    links = _document_links_payload(document)
    return {
        "document_id": document.id,
        "document_type": document.document_type,
        "tax_year": document.tax_year,
        "jurisdiction": document.jurisdiction,
        "period_start": document.period_start,
        "period_end": document.period_end,
        "scope": document.scope,
        "original_filename": document.original_filename,
        "stored_path": document.stored_path,
        "sha256": document.sha256,
        "notes": document.notes,
        "created_via": document.created_via,
        "created_at": document.created_at,
        "links": links,
        "link_summary": ", ".join(f"{item['target_type']}:{item['target_id']}" for item in links),
    }


def serialize_document(document: Document) -> dict[str, object]:
    return _document_payload(document)


def _record_audit_event(
    session: Session,
    *,
    entity_type: str,
    entity_ref: str,
    action: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    source: str,
    reason: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            entity_type=entity_type,
            entity_ref=entity_ref,
            action=action,
            before_json=None if before is None else json_dumps(before),
            after_json=None if after is None else json_dumps(after),
            source=source,
            reason=reason,
            created_at=utcnow(),
        )
    )


def _apply_document_links(document: Document, targets: list[tuple[str, int]], *, clear_links: bool) -> None:
    if clear_links:
        document.links.clear()
    existing = {(link.target_type, link.target_id) for link in document.links}
    for target_type, target_id in targets:
        if (target_type, target_id) in existing:
            continue
        document.links.append(DocumentLink(target_type=target_type, target_id=target_id))
        existing.add((target_type, target_id))


def create_document(
    session: Session,
    *,
    ledger_dir: Path,
    source_path: Path,
    document_type: str,
    tax_year: int,
    jurisdiction: str | None = None,
    scope: str,
    period_start: date | None = None,
    period_end: date | None = None,
    notes: str | None = None,
    created_via: str = "manual",
    dry_run: bool = False,
    journal_entry_id: int | None = None,
    reconciliation_session_id: int | None = None,
    tax_obligation_code: str | None = None,
    import_run_id: int | None = None,
) -> Document:
    _validate_document_metadata(
        document_type=document_type,
        tax_year=tax_year,
        jurisdiction=jurisdiction,
        scope=scope,
        period_start=period_start,
        period_end=period_end,
    )
    sha256, stored_path = _copy_document_to_ledger(
        ledger_dir,
        source_path=source_path,
        document_type=document_type,
        tax_year=tax_year,
        dry_run=dry_run,
    )
    targets = _resolve_document_targets(
        session,
        journal_entry_id=journal_entry_id,
        reconciliation_session_id=reconciliation_session_id,
        tax_obligation_code=tax_obligation_code,
        import_run_id=import_run_id,
    )
    document = Document(
        document_type=document_type,
        tax_year=tax_year,
        jurisdiction=_normalize_jurisdiction(jurisdiction),
        period_start=period_start,
        period_end=period_end,
        scope=scope,
        original_filename=source_path.name,
        stored_path=stored_path,
        sha256=sha256,
        notes=notes,
        created_via=created_via,
        created_at=utcnow(),
    )
    session.add(document)
    session.flush()
    _apply_document_links(document, targets, clear_links=False)
    session.flush()
    created = session.scalar(select(Document).options(selectinload(Document.links)).where(Document.id == document.id))
    _record_audit_event(
        session,
        entity_type="document",
        entity_ref=str(created.id),
        action="create",
        before=None,
        after=_document_payload(created),
        source=created_via,
    )
    return created


def list_documents(
    session: Session,
    *,
    tax_year: int | None = None,
    document_type: str | None = None,
    scope: str | None = None,
    journal_entry_id: int | None = None,
    reconciliation_session_id: int | None = None,
    tax_obligation_code: str | None = None,
    import_run_id: int | None = None,
) -> list[Document]:
    query = select(Document).options(selectinload(Document.links)).order_by(Document.tax_year.desc(), Document.created_at.desc(), Document.id.desc())
    if tax_year is not None:
        query = query.where(Document.tax_year == tax_year)
    if document_type is not None:
        query = query.where(Document.document_type == document_type)
    if scope is not None:
        query = query.where(Document.scope == scope)
    documents = list(session.scalars(query))
    if not any(value is not None for value in (journal_entry_id, reconciliation_session_id, tax_obligation_code, import_run_id)):
        return documents

    targets = _resolve_document_targets(
        session,
        journal_entry_id=journal_entry_id,
        reconciliation_session_id=reconciliation_session_id,
        tax_obligation_code=tax_obligation_code,
        import_run_id=import_run_id,
    )
    wanted = set(targets)
    return [
        document
        for document in documents
        if wanted.issubset({(link.target_type, link.target_id) for link in document.links})
    ]


def update_document(
    session: Session,
    *,
    document_id: int,
    document_type: str | None = None,
    tax_year: int | None = None,
    jurisdiction: str | object = UNSET,
    scope: str | None = None,
    period_start: date | object = UNSET,
    period_end: date | object = UNSET,
    notes: str | object = UNSET,
    clear_period: bool = False,
    clear_notes: bool = False,
    clear_links: bool = False,
    journal_entry_id: int | None = None,
    reconciliation_session_id: int | None = None,
    tax_obligation_code: str | None = None,
    import_run_id: int | None = None,
) -> Document:
    document = session.scalar(select(Document).options(selectinload(Document.links)).where(Document.id == document_id))
    if not document:
        raise ValidationError(f"Unknown document: {document_id}")
    before_payload = _document_payload(document)

    if document_type is not None:
        document.document_type = document_type
    if tax_year is not None:
        document.tax_year = tax_year
    if jurisdiction is not UNSET:
        document.jurisdiction = _normalize_jurisdiction(jurisdiction)
    if scope is not None:
        document.scope = scope
    if clear_period:
        document.period_start = None
        document.period_end = None
    else:
        if period_start is not UNSET:
            document.period_start = period_start
        if period_end is not UNSET:
            document.period_end = period_end
    if clear_notes:
        document.notes = None
    elif notes is not UNSET:
        document.notes = notes

    _validate_document_metadata(
        document_type=document.document_type,
        tax_year=document.tax_year,
        jurisdiction=document.jurisdiction,
        scope=document.scope,
        period_start=document.period_start,
        period_end=document.period_end,
    )
    targets = _resolve_document_targets(
        session,
        journal_entry_id=journal_entry_id,
        reconciliation_session_id=reconciliation_session_id,
        tax_obligation_code=tax_obligation_code,
        import_run_id=import_run_id,
    )
    if clear_links or targets:
        _apply_document_links(document, targets, clear_links=clear_links)
    session.flush()
    updated = session.scalar(select(Document).options(selectinload(Document.links)).where(Document.id == document.id))
    _record_audit_event(
        session,
        entity_type="document",
        entity_ref=str(updated.id),
        action="update",
        before=before_payload,
        after=_document_payload(updated),
        source="cli",
    )
    return updated


def _get_setting_json(session: Session, key: str, default: dict[str, object]) -> dict[str, object]:
    setting = session.get(Setting, key)
    if not setting:
        return default
    return json.loads(setting.value_json)


def _set_setting_json(session: Session, key: str, value: dict[str, object]) -> None:
    setting = session.get(Setting, key)
    if setting:
        setting.value_json = json_dumps(value)
    else:
        session.add(Setting(key=key, value_json=json_dumps(value)))
    session.flush()


def get_compliance_profile(session: Session) -> ComplianceProfile:
    raw = _get_setting_json(session, "compliance_profile", ComplianceProfile().model_dump(mode="json"))
    return ComplianceProfile.model_validate(raw)


def set_compliance_profile(
    session: Session,
    profile: ComplianceProfile,
    *,
    source: str = "cli",
    reason: str | None = None,
) -> ComplianceProfile:
    before = get_compliance_profile(session).model_dump()
    _set_setting_json(session, "compliance_profile", profile.model_dump(mode="json"))
    updated = get_compliance_profile(session)
    _record_audit_event(
        session,
        entity_type="compliance_profile",
        entity_ref="default",
        action="update",
        before=before,
        after=updated.model_dump(),
        source=source,
        reason=reason,
    )
    return updated


def _sales_tax_slot_key(slot: SalesTaxPaymentSlot) -> tuple[str, date, date, date]:
    return (_normalize_jurisdiction(slot.jurisdiction) or "", slot.period_start, slot.period_end, slot.filing_due_date)


def list_sales_tax_payment_slots(session: Session, *, year: int | None = None) -> dict[str, object]:
    profile = get_compliance_profile(session)
    slots = [
        slot
        for slot in profile.sales_tax_payment_slots
        if year is None or slot.period_start.year == year or slot.period_end.year == year or slot.filing_due_date.year == year
    ]
    rows = [
        {
            "jurisdiction": slot.jurisdiction,
            "period_start": slot.period_start,
            "period_end": slot.period_end,
            "filing_due_date": slot.filing_due_date,
            "payment_expected": slot.payment_expected,
            "source": slot.source,
            "reason": slot.reason,
        }
        for slot in sorted(slots, key=_sales_tax_slot_key)
    ]
    return {"rows": rows}


def set_sales_tax_payment_expectation(
    session: Session,
    *,
    jurisdiction: str,
    period_start: date,
    period_end: date,
    filing_due_date: date,
    payment_expected: str,
    source: str | None = None,
    reason: str | None = None,
) -> ComplianceProfile:
    if period_start > period_end:
        raise ValidationError("Sales-tax slot period start cannot be after period end")
    if payment_expected not in {"true", "false", "unknown"}:
        raise ValidationError("Sales-tax slot payment expectation must be true, false, or unknown")
    normalized_jurisdiction = _normalize_jurisdiction(jurisdiction)
    if normalized_jurisdiction is None:
        raise ValidationError("Sales-tax slot jurisdiction is required")
    profile = get_compliance_profile(session)
    slots = [slot for slot in profile.sales_tax_payment_slots if _sales_tax_slot_key(slot) != (normalized_jurisdiction, period_start, period_end, filing_due_date)]
    slots.append(
        SalesTaxPaymentSlot(
            jurisdiction=normalized_jurisdiction,
            period_start=period_start,
            period_end=period_end,
            filing_due_date=filing_due_date,
            payment_expected=payment_expected,
            source=source,
            reason=reason,
        )
    )
    updated = profile.model_copy(update={"sales_tax_payment_slots": sorted(slots, key=_sales_tax_slot_key)})
    return set_compliance_profile(session, updated, source="cli", reason="sales-tax-slot update")


def seed_defaults(session: Session, year: int) -> None:
    del year
    existing_accounts = session.scalar(select(func.count(Account.id)))
    if not existing_accounts:
        for account in DEFAULT_ACCOUNTS:
            session.add(
                Account(
                    code=account["code"],
                    name=account["name"],
                    kind=account["kind"],
                    subtype=account["subtype"],
                    currency="USD",
                    is_active=True,
                    created_at=utcnow(),
                )
            )
    if not session.get(Setting, "ledger_version"):
        session.add(Setting(key="ledger_version", value_json=json.dumps({"version": 2})))
    if not session.get(Setting, "compliance_profile"):
        session.add(Setting(key="compliance_profile", value_json=json_dumps(ComplianceProfile().model_dump(mode="json"))))


def list_accounts(session: Session, *, include_inactive: bool = False) -> list[Account]:
    query: Select[tuple[Account]] = select(Account).order_by(Account.code)
    if not include_inactive:
        query = query.where(Account.is_active.is_(True))
    return list(session.scalars(query))


def get_account(session: Session, code: str) -> Account:
    account = session.scalar(select(Account).where(Account.code == code))
    if not account:
        raise ValidationError(f"Unknown account code: {code}")
    return account


def infer_kind_from_subtype(subtype: str) -> str:
    if subtype in {"bank", "stripe_clearing", "receivable"}:
        return "asset"
    if subtype in {"card", "tax_liability", "reimbursement"}:
        return "liability"
    if subtype == "equity":
        return "equity"
    raise ValidationError(f"Cannot infer account kind from subtype: {subtype}")


def add_account(
    session: Session,
    *,
    code: str,
    name: str,
    kind: str,
    subtype: str,
    currency: str = "USD",
) -> Account:
    if kind not in ACCOUNT_KINDS:
        raise ValidationError(f"Unsupported account kind: {kind}")
    if session.scalar(select(Account.id).where(Account.code == code)):
        raise ValidationError(f"Account already exists: {code}")
    account = Account(
        code=code,
        name=name,
        kind=kind,
        subtype=subtype,
        currency=currency,
        is_active=True,
        created_at=utcnow(),
    )
    session.add(account)
    session.flush()
    return account


def deactivate_account(session: Session, code: str) -> Account:
    account = get_account(session, code)
    account.is_active = False
    session.flush()
    return account


def get_active_lock(session: Session, value: date) -> PeriodLock | None:
    return session.scalar(
        select(PeriodLock)
        .where(PeriodLock.period_start <= value, PeriodLock.period_end >= value)
        .order_by(PeriodLock.created_at.desc(), PeriodLock.id.desc())
    )


def ensure_unlocked(session: Session, value: date) -> None:
    lock = get_active_lock(session, value)
    if lock and lock.action == "close":
        raise LockedPeriodError(
            f"Date {value.isoformat()} is in a locked period {lock.period_start.isoformat()} to {lock.period_end.isoformat()}",
            data={"period_start": lock.period_start, "period_end": lock.period_end},
        )


def ensure_interval_unlocked(session: Session, start: date, end: date) -> None:
    if end < start:
        raise ValidationError("Interval end cannot be before interval start")
    overlapping_locks = list(
        session.scalars(
            select(PeriodLock)
            .where(PeriodLock.period_start <= end, PeriodLock.period_end >= start)
            .order_by(PeriodLock.period_start, PeriodLock.period_end, PeriodLock.created_at, PeriodLock.id)
        )
    )
    if not overlapping_locks:
        return
    boundaries = {start, end + timedelta(days=1)}
    for lock in overlapping_locks:
        boundaries.add(max(start, lock.period_start))
        boundaries.add(min(end + timedelta(days=1), lock.period_end + timedelta(days=1)))
    ordered_boundaries = sorted(boundaries)
    for boundary in ordered_boundaries[:-1]:
        lock = get_active_lock(session, boundary)
        if lock and lock.action == "close":
            raise LockedPeriodError(
                f"Interval {start.isoformat()} to {end.isoformat()} intersects locked period {lock.period_start.isoformat()} to {lock.period_end.isoformat()}",
                data={
                    "interval_start": start,
                    "interval_end": end,
                    "period_start": lock.period_start,
                    "period_end": lock.period_end,
                },
            )


def post_journal_entry(
    session: Session,
    *,
    entry_date: date,
    description: str,
    lines: list[JournalLineInput],
    source_type: str,
    source_ref: str | None = None,
    reversal_of_entry_id: int | None = None,
    import_run_id: int | None = None,
) -> JournalEntry:
    ensure_unlocked(session, entry_date)
    if not lines:
        raise ValidationError("Journal entry must include at least one line")
    if source_type in MANUAL_SOURCE_TYPES:
        _validate_manual_source_type(session, lines=lines, source_type=source_type)
    total = sum(line.amount_cents for line in lines)
    if total != 0:
        raise ValidationError("Journal entry is not balanced", data={"difference_cents": total})

    entry = JournalEntry(
        entry_date=entry_date,
        description=description,
        source_type=source_type,
        source_ref=source_ref,
        created_at=utcnow(),
        reversal_of_entry_id=reversal_of_entry_id,
        import_run_id=import_run_id,
    )
    session.add(entry)
    session.flush()

    for line in lines:
        account = get_account(session, line.account_code)
        session.add(
            JournalLine(
                entry_id=entry.id,
                account_id=account.id,
                amount_cents=line.amount_cents,
                memo=line.memo,
            )
        )

    session.flush()
    created = session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry.id)
    )
    if created is None:
        raise ValidationError("Failed to load newly-created journal entry")
    _maybe_auto_apply_reimbursement_settlement(session, created)
    return session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry.id)
    )


def _validate_manual_source_type(session: Session, *, lines: list[JournalLineInput], source_type: str) -> None:
    if source_type == "manual":
        return
    resolved = [(line, get_account(session, line.account_code)) for line in lines]
    has_p_and_l = any(account.kind in P_AND_L_KINDS for _line, account in resolved)
    account_codes = {account.code for _line, account in resolved}
    if source_type == "owner_contribution":
        if "3000" not in account_codes:
            raise ValidationError("owner_contribution entries must include account 3000")
        if has_p_and_l:
            raise ValidationError("owner_contribution entries cannot include P&L accounts")
        equity_amount = sum(line.amount_cents for line, account in resolved if account.code == "3000")
        if equity_amount >= 0:
            raise ValidationError("owner_contribution entries must credit account 3000")
        return
    if source_type == "owner_draw":
        if "3100" not in account_codes:
            raise ValidationError("owner_draw entries must include account 3100")
        if has_p_and_l:
            raise ValidationError("owner_draw entries cannot include P&L accounts")
        draw_amount = sum(line.amount_cents for line, account in resolved if account.code == "3100")
        if draw_amount <= 0:
            raise ValidationError("owner_draw entries must debit account 3100")
        return
    raise ValidationError(f"Unsupported manual source type: {source_type}")


def _is_supported_reimbursable_expense_source_line(line: JournalLine) -> bool:
    if line.account.kind != "expense":
        return False
    entry = line.entry
    if entry.source_type != "expense":
        return False
    if len(entry.lines) != 2:
        return False
    companions = [candidate for candidate in entry.lines if candidate.id != line.id]
    return len(companions) == 1 and companions[0].account.code == "2300"


def _maybe_auto_apply_reimbursement_settlement(session: Session, entry: JournalEntry) -> None:
    if len(entry.lines) != 2:
        return
    payable_lines = [line for line in entry.lines if line.account.code == "2300"]
    cash_lines = [line for line in entry.lines if line.account.code in {"1000", "1010", "2000"}]
    if len(payable_lines) != 1 or len(cash_lines) != 1:
        return
    if any(line.account.kind in P_AND_L_KINDS for line in entry.lines):
        return
    payable_line = payable_lines[0]
    cash_line = cash_lines[0]
    reimbursement_amount_cents = abs(cash_line.amount_cents)
    if reimbursement_amount_cents == 0 or payable_line.amount_cents <= 0:
        return

    candidate_lines = list(
        session.scalars(
            select(JournalLine)
            .join(JournalEntry)
            .join(Account)
            .options(
                selectinload(JournalLine.account),
                selectinload(JournalLine.entry).selectinload(JournalEntry.lines).selectinload(JournalLine.account),
            )
            .where(
                Account.kind == "expense",
                JournalEntry.source_type == "expense",
                JournalEntry.entry_date <= entry.entry_date,
            )
        )
    )
    eligible: list[JournalLine] = []
    for source_line in candidate_lines:
        if not _is_supported_reimbursable_expense_source_line(source_line):
            continue
        residual_cents = abs(source_line.amount_cents) - _sum_open_source_applications(session, source_line.id)
        if residual_cents == reimbursement_amount_cents:
            eligible.append(source_line)
    if len(eligible) != 1:
        return
    apply_settlement(
        session,
        source_line_id=eligible[0].id,
        settlement_line_id=cash_line.id,
        amount=f"{reimbursement_amount_cents / 100:.2f}",
        applied_date=entry.entry_date,
        application_type="reimbursement_auto",
    )


def _has_open_settlements_for_entry(session: Session, entry_id: int) -> bool:
    count = session.scalar(
        select(func.count(SettlementApplication.id))
        .join(JournalLine, (SettlementApplication.source_line_id == JournalLine.id) | (SettlementApplication.settlement_line_id == JournalLine.id))
        .where(JournalLine.entry_id == entry_id, SettlementApplication.reversed_at.is_(None))
    )
    return bool(count)


def _has_open_reconciliation_matches_for_entry(session: Session, entry_id: int) -> bool:
    count = session.scalar(
        select(func.count(ReconciliationMatch.id))
        .join(JournalLine, ReconciliationMatch.journal_line_id == JournalLine.id)
        .where(JournalLine.entry_id == entry_id, ReconciliationMatch.reversed_at.is_(None))
    )
    return bool(count)


def record_attachment(session: Session, path: Path, description: str | None = None) -> Attachment:
    attachment = Attachment(
        path=str(path),
        sha256=sha256_for_path(path),
        description=description,
        created_at=utcnow(),
    )
    session.add(attachment)
    session.flush()
    return attachment


def record_expense(
    session: Session,
    *,
    ledger_dir: Path,
    entry_date: date,
    vendor: str,
    amount: str,
    category_code: str,
    payment_account_code: str | None,
    memo: str | None,
    receipt_path: Path | None,
    paid_personally: bool,
    reimbursement: bool,
    dry_run: bool = False,
) -> dict[str, object]:
    amount_cents = parse_money(amount)
    if amount_cents <= 0:
        raise ValidationError("Expense amount must be positive")

    offset_code = payment_account_code
    if paid_personally:
        offset_code = "2300" if reimbursement else "3000"
    if not offset_code:
        raise ValidationError("Payment account is required unless --paid-personally is set")

    entry = post_journal_entry(
        session,
        entry_date=entry_date,
        description=f"Expense: {vendor}",
        lines=[
            JournalLineInput(category_code, amount_cents, memo or vendor),
            JournalLineInput(offset_code, -amount_cents, memo or vendor),
        ],
        source_type="expense",
        source_ref=vendor,
    )
    document_id = None
    if receipt_path:
        document = create_document(
            session,
            ledger_dir=ledger_dir,
            source_path=receipt_path,
            document_type="expense_receipt",
            tax_year=entry_date.year,
            scope="business",
            period_start=entry_date,
            period_end=entry_date,
            notes=f"Receipt for {vendor}",
            created_via="expense",
            dry_run=dry_run,
            journal_entry_id=entry.id,
        )
        document_id = document.id

    return {
        "entry_id": entry.id,
        "vendor": vendor,
        "amount_cents": amount_cents,
        "category_code": category_code,
        "offset_account_code": offset_code,
        "attachment_id": None,
        "document_id": document_id,
        "dry_run": dry_run,
    }


def reverse_entry(session: Session, *, entry_id: int, reversal_date: date, reason: str) -> dict[str, object]:
    original = session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry_id)
    )
    if not original:
        raise ValidationError(f"Unknown journal entry: {entry_id}")
    if original.reversal_of_entry_id:
        raise ValidationError("Cannot reverse an entry that is already a reversal")
    if session.scalar(select(JournalEntry.id).where(JournalEntry.reversal_of_entry_id == entry_id)):
        raise ValidationError(f"Entry {entry_id} already has a reversal")
    if _has_open_settlements_for_entry(session, entry_id):
        raise ValidationError("Cannot reverse an entry with open settlement applications")
    if _has_open_reconciliation_matches_for_entry(session, entry_id):
        raise ValidationError("Cannot reverse an entry with open reconciliation matches")

    reversal = post_journal_entry(
        session,
        entry_date=reversal_date,
        description=f"Reversal of entry {entry_id}: {reason}",
        lines=[JournalLineInput(line.account.code, -line.amount_cents, line.memo) for line in original.lines],
        source_type="reversal",
        source_ref=str(entry_id),
        reversal_of_entry_id=entry_id,
    )
    return {"entry_id": reversal.id, "reversal_of_entry_id": entry_id}


def _start_import_run(
    session: Session,
    *,
    source: str,
    from_date: date | None,
    to_date: date | None,
    dry_run: bool,
    source_path: Path | None = None,
) -> ImportRun:
    import_run = ImportRun(
        source=source,
        status="running",
        started_at=utcnow(),
        from_date=from_date,
        to_date=to_date,
        dry_run=dry_run,
        source_path=str(source_path) if source_path else None,
    )
    session.add(import_run)
    session.flush()
    return import_run


def _complete_import_run(import_run: ImportRun, *, warnings: list[str], summary: dict[str, object]) -> None:
    import_run.status = "dry_run" if import_run.dry_run else "completed"
    import_run.completed_at = utcnow()
    import_run.warnings_json = json.dumps(warnings)
    import_run.summary_json = json.dumps(summary)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _csv_transaction_fingerprint(
    *,
    account_code: str,
    row_date: date,
    description: str,
    amount_cents: int,
    external_ref: str | None,
) -> str:
    return stable_external_id(
        "csv",
        account_code,
        row_date.isoformat(),
        amount_cents,
        _normalize_text(description),
        _normalize_text(external_ref),
    )


def _local_date(config: AppConfig, value: datetime) -> date:
    return value.astimezone(ZoneInfo(config.timezone)).date()


def _raw_payload_to_event(external_event: ExternalEvent) -> StripeEvent | StripeUnsupportedEvent:
    payload = json.loads(external_event.payload_json)
    if "reason" in payload and "raw_type" in payload:
        return StripeUnsupportedEvent.model_validate(payload)
    return StripeEvent.model_validate(payload)


def _find_external_event(session: Session, *, provider: str, external_id: str) -> ExternalEvent | None:
    return session.scalar(
        select(ExternalEvent).where(ExternalEvent.provider == provider, ExternalEvent.external_id == external_id)
    )


def _find_review_blocker(session: Session, *, provider: str, external_id: str, blocker_type: str) -> ReviewBlocker | None:
    return session.scalar(
        select(ReviewBlocker).where(
            ReviewBlocker.provider == provider,
            ReviewBlocker.external_id == external_id,
            ReviewBlocker.blocker_type == blocker_type,
        )
    )


def _find_open_stripe_blocker(session: Session, *, external_id: str) -> ReviewBlocker | None:
    return session.scalar(
        select(ReviewBlocker)
        .where(
            ReviewBlocker.provider == "stripe",
            ReviewBlocker.external_id == external_id,
            ReviewBlocker.status == "open",
        )
        .order_by(ReviewBlocker.id.desc())
    )


def _append_external_event_refresh(
    session: Session,
    *,
    external_event: ExternalEvent,
    refresh_source: str,
    change_note: str | None = None,
) -> ExternalEventRefreshHistory:
    refresh = ExternalEventRefreshHistory(
        external_event_id=external_event.id,
        refreshed_at=utcnow(),
        payload_json=external_event.payload_json,
        refresh_source=refresh_source,
        change_note=change_note,
    )
    session.add(refresh)
    session.flush()
    return refresh


def _external_event_refresh_metadata(session: Session, external_event_id: int | None) -> dict[str, object]:
    if external_event_id is None:
        return {"refresh_history_count": 0, "last_refreshed_at": None, "last_refresh_source": None}
    rows = list(
        session.scalars(
            select(ExternalEventRefreshHistory)
            .where(ExternalEventRefreshHistory.external_event_id == external_event_id)
            .order_by(ExternalEventRefreshHistory.refreshed_at.desc(), ExternalEventRefreshHistory.id.desc())
            .limit(1)
        )
    )
    count = session.scalar(
        select(func.count(ExternalEventRefreshHistory.id)).where(
            ExternalEventRefreshHistory.external_event_id == external_event_id
        )
    )
    latest = rows[0] if rows else None
    return {
        "refresh_history_count": int(count or 0),
        "last_refreshed_at": latest.refreshed_at if latest else None,
        "last_refresh_source": latest.refresh_source if latest else None,
    }


def _upsert_review_blocker(
    session: Session,
    *,
    blocker_type: str,
    provider: str,
    external_id: str,
    blocker_date: date,
    note: str,
    external_event_id: int | None,
) -> ReviewBlocker:
    blocker = _find_review_blocker(session, provider=provider, external_id=external_id, blocker_type=blocker_type)
    if blocker:
        blocker.status = "open"
        blocker.resolved_at = None
        blocker.resolution_type = None
        blocker.resolution_note = note
        blocker.resolution_entry_id = None
        blocker.external_event_id = external_event_id
        blocker.blocker_date = blocker_date
    else:
        blocker = ReviewBlocker(
            blocker_type=blocker_type,
            provider=provider,
            external_id=external_id,
            status="open",
            blocker_date=blocker_date,
            opened_at=utcnow(),
            resolution_note=note,
            external_event_id=external_event_id,
        )
        session.add(blocker)
    session.flush()
    return blocker


def _resolve_review_blocker(
    blocker: ReviewBlocker,
    *,
    resolution_type: str,
    note: str | None,
    resolution_entry_id: int | None = None,
) -> None:
    blocker.status = "resolved"
    blocker.resolved_at = utcnow()
    blocker.resolution_type = resolution_type
    blocker.resolution_note = note
    blocker.resolution_entry_id = resolution_entry_id


def _is_cash_equivalent_account(account: Account) -> bool:
    return account.subtype in FINANCIAL_SUBTYPES


def _is_supported_settlement_account(account: Account) -> bool:
    return _is_cash_equivalent_account(account) or account.code == "3000"


def _allowed_immediate_balance_line(source_line: JournalLine, companion: JournalLine) -> bool:
    account = companion.account
    if account.kind in P_AND_L_KINDS:
        return True
    if _is_cash_equivalent_account(account):
        return True
    if source_line.account.kind in {"revenue", "contra_revenue"} and account.subtype == "tax_liability":
        return True
    if source_line.account.kind == "expense" and account.code == "3000":
        return True
    return False


def is_immediate_cash_source_line(source_line: JournalLine) -> tuple[bool, str | None]:
    if source_line.account.kind not in P_AND_L_KINDS:
        return False, "Line is not a P&L account"
    companions = [line for line in source_line.entry.lines if line.id != source_line.id]
    has_cash_equivalent = False
    for companion in companions:
        if not _allowed_immediate_balance_line(source_line, companion):
            return False, f"Unsupported cash-basis companion account {companion.account.code}"
        if _is_cash_equivalent_account(companion.account):
            has_cash_equivalent = True
        if source_line.account.kind == "expense" and companion.account.code == "3000":
            has_cash_equivalent = True
    if not has_cash_equivalent:
        return False, "No supported cash-equivalent line in entry"
    return True, None


def entry_has_immediate_cash_pnl(entry: JournalEntry) -> bool:
    for line in entry.lines:
        if line.account.kind not in P_AND_L_KINDS:
            continue
        immediate_cash, _reason = is_immediate_cash_source_line(line)
        if immediate_cash:
            return True
    return False


def _line_sign(amount_cents: int) -> int:
    return 1 if amount_cents > 0 else -1


def _sum_open_source_applications(session: Session, line_id: int) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(SettlementApplication.applied_amount_cents), 0)).where(
            SettlementApplication.source_line_id == line_id,
            SettlementApplication.reversed_at.is_(None),
        )
    )
    return int(value or 0)


def _sum_open_settlement_applications(session: Session, line_id: int) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(SettlementApplication.applied_amount_cents), 0)).where(
            SettlementApplication.settlement_line_id == line_id,
            SettlementApplication.reversed_at.is_(None),
        )
    )
    return int(value or 0)


def _sum_open_reconciliation_matches_for_line(session: Session, line_id: int) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(ReconciliationMatch.applied_amount_cents), 0)).where(
            ReconciliationMatch.journal_line_id == line_id,
            ReconciliationMatch.reversed_at.is_(None),
        )
    )
    return int(value or 0)


def _sum_open_reconciliation_matches_for_statement_line(session: Session, line_id: int) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(ReconciliationMatch.applied_amount_cents), 0)).where(
            ReconciliationMatch.reconciliation_line_id == line_id,
            ReconciliationMatch.reversed_at.is_(None),
        )
    )
    return int(value or 0)


def _load_line(session: Session, line_id: int) -> JournalLine:
    line = session.scalar(
        select(JournalLine)
        .options(selectinload(JournalLine.account), selectinload(JournalLine.entry).selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalLine.id == line_id)
    )
    if not line:
        raise ValidationError(f"Unknown journal line: {line_id}")
    return line


def _validate_source_line(session: Session, line: JournalLine) -> None:
    if line.account.kind not in P_AND_L_KINDS:
        raise ValidationError("Settlement source line must be revenue, expense, or contra-revenue")
    immediate_cash, _reason = is_immediate_cash_source_line(line)
    if immediate_cash:
        raise ValidationError("Settlement source line is already immediate-cash and cannot receive manual settlement")
    if _sum_open_source_applications(session, line.id) >= abs(line.amount_cents):
        raise ValidationError("Settlement source line has no remaining residual amount")


def _validate_settlement_line(session: Session, line: JournalLine, *, source_line: JournalLine) -> None:
    if not _is_supported_settlement_account(line.account):
        raise ValidationError("Settlement line must use a supported cash-equivalent or owner-contribution account")
    if line.account.code == "3000" and source_line.account.kind != "expense":
        raise ValidationError("Owner contribution is only a supported settlement line for expense sources")
    if entry_has_immediate_cash_pnl(line.entry):
        raise ValidationError(
            "Settlement line comes from an immediate-cash entry and cannot also be used to settle a prior accrual"
        )
    if _line_sign(line.amount_cents) == _line_sign(source_line.amount_cents):
        raise ValidationError("Settlement line sign must be opposite the source line sign")
    if _sum_open_settlement_applications(session, line.id) >= abs(line.amount_cents):
        raise ValidationError("Settlement line has no remaining residual amount")


def apply_settlement(
    session: Session,
    *,
    source_line_id: int,
    settlement_line_id: int,
    amount: str,
    applied_date: date | None = None,
    application_type: str = "manual",
) -> dict[str, object]:
    if application_type not in SETTLEMENT_APPLICATION_TYPES:
        raise ValidationError(f"Unsupported settlement application type: {application_type}")
    applied_amount_cents = abs(parse_money(amount))
    if applied_amount_cents <= 0:
        raise ValidationError("Settlement amount must be positive")

    source_line = _load_line(session, source_line_id)
    settlement_line = _load_line(session, settlement_line_id)
    _validate_source_line(session, source_line)
    _validate_settlement_line(session, settlement_line, source_line=source_line)

    source_residual = abs(source_line.amount_cents) - _sum_open_source_applications(session, source_line.id)
    settlement_residual = abs(settlement_line.amount_cents) - _sum_open_settlement_applications(session, settlement_line.id)
    if applied_amount_cents > source_residual:
        raise ValidationError("Settlement amount exceeds source line residual")
    if applied_amount_cents > settlement_residual:
        raise ValidationError("Settlement amount exceeds settlement line residual")

    effective_date = applied_date or settlement_line.entry.entry_date
    if effective_date < source_line.entry.entry_date or effective_date < settlement_line.entry.entry_date:
        raise ValidationError("Settlement applied date cannot be before either underlying journal entry date")
    ensure_unlocked(session, effective_date)

    application = SettlementApplication(
        source_line_id=source_line.id,
        settlement_line_id=settlement_line.id,
        applied_amount_cents=applied_amount_cents,
        applied_date=effective_date,
        application_type=application_type,
        created_at=utcnow(),
    )
    session.add(application)
    session.flush()
    return {
        "settlement_application_id": application.id,
        "source_line_id": source_line.id,
        "settlement_line_id": settlement_line.id,
        "applied_amount_cents": applied_amount_cents,
        "applied_date": effective_date,
        "application_type": application_type,
    }


def list_settlements(session: Session) -> dict[str, object]:
    applications = list(
        session.scalars(
            select(SettlementApplication)
            .options(
                selectinload(SettlementApplication.source_line).selectinload(JournalLine.account),
                selectinload(SettlementApplication.source_line).selectinload(JournalLine.entry),
                selectinload(SettlementApplication.settlement_line).selectinload(JournalLine.account),
                selectinload(SettlementApplication.settlement_line).selectinload(JournalLine.entry),
            )
            .order_by(SettlementApplication.applied_date, SettlementApplication.id)
        )
    )
    return {
        "rows": [
            {
                "settlement_application_id": app.id,
                "source_line_id": app.source_line_id,
                "source_account_code": app.source_line.account.code,
                "source_entry_id": app.source_line.entry_id,
                "settlement_line_id": app.settlement_line_id,
                "settlement_account_code": app.settlement_line.account.code,
                "settlement_entry_id": app.settlement_line.entry_id,
                "applied_amount_cents": app.applied_amount_cents,
                "applied_date": app.applied_date,
                "application_type": app.application_type,
                "status": "reversed" if app.reversed_at else "open",
            }
            for app in applications
        ]
    }


def reverse_settlement(session: Session, *, settlement_application_id: int, reason: str) -> dict[str, object]:
    application = session.get(SettlementApplication, settlement_application_id)
    if not application:
        raise ValidationError(f"Unknown settlement application: {settlement_application_id}")
    if application.reversed_at:
        raise ValidationError("Settlement application is already reversed")
    ensure_unlocked(session, application.applied_date)
    application.reversed_at = utcnow()
    application.reversal_reason = reason
    session.flush()
    return {"settlement_application_id": application.id, "status": "reversed"}


def _serialize_blocker(session: Session, blocker: ReviewBlocker) -> dict[str, object]:
    payload = {
        "review_blocker_id": blocker.id,
        "blocker_type": blocker.blocker_type,
        "provider": blocker.provider,
        "external_id": blocker.external_id,
        "status": blocker.status,
        "blocker_date": blocker.blocker_date,
        "opened_at": blocker.opened_at,
        "resolved_at": blocker.resolved_at,
        "resolution_type": blocker.resolution_type,
        "resolution_note": blocker.resolution_note,
        "resolution_entry_id": blocker.resolution_entry_id,
        "external_event_id": blocker.external_event_id,
    }
    payload.update(_external_event_refresh_metadata(session, blocker.external_event_id))
    return payload


def list_review_blockers(session: Session, *, status: str | None = None) -> dict[str, object]:
    query = select(ReviewBlocker).order_by(ReviewBlocker.status, ReviewBlocker.blocker_date, ReviewBlocker.id)
    if status is not None:
        query = query.where(ReviewBlocker.status == status)
    blockers = list(session.scalars(query))
    return {"rows": [_serialize_blocker(session, blocker) for blocker in blockers]}


def _stripe_blocker_reason(config: AppConfig, event: StripeEvent) -> str | None:
    if event.event_type not in {"charge", "refund", "dispute"}:
        return None
    if config.stripe_tax_mode == "manual_review_required":
        return "Stripe tax mode requires manual review before posting revenue-affecting events."
    if event.tax_cents is None:
        return "Stripe event has unknown tax effect and cannot be posted safely."
    return None


def _unsupported_stripe_reason(event: StripeUnsupportedEvent) -> str:
    return event.reason


def _reset_open_blocker(
    blocker: ReviewBlocker,
    *,
    blocker_type: str,
    blocker_date: date,
    note: str,
    external_event_id: int,
) -> None:
    blocker.blocker_type = blocker_type
    blocker.status = "open"
    blocker.blocker_date = blocker_date
    blocker.external_event_id = external_event_id
    blocker.resolution_note = note
    blocker.resolved_at = None
    blocker.resolution_type = None
    blocker.resolution_entry_id = None


def _post_stripe_event(
    session: Session,
    *,
    config: AppConfig,
    import_run: ImportRun | None,
    event: StripeEvent,
    source_ref_suffix: str | None = None,
) -> list[JournalEntry]:
    created: list[JournalEntry] = []
    event_date = _local_date(config, event.occurred_at)
    source_ref = event.external_id if source_ref_suffix is None else f"{event.external_id}:{source_ref_suffix}"
    tax_cents = int(event.tax_cents or 0)

    if event.event_type == "charge":
        revenue_cents = event.amount_cents - tax_cents
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe charge {event.external_id}",
                lines=[
                    JournalLineInput("1010", event.amount_cents, "Stripe gross receipt"),
                    JournalLineInput("4000", -revenue_cents, "Subscription revenue"),
                    *([JournalLineInput("2100", -tax_cents, "Sales tax collected")] if tax_cents else []),
                ],
                source_type="stripe",
                source_ref=source_ref,
                import_run_id=import_run.id if import_run else None,
            )
        )
        if event.fee_cents:
            created.append(
                post_journal_entry(
                    session,
                    entry_date=event_date,
                    description=f"Stripe fee for {event.external_id}",
                    lines=[
                        JournalLineInput("5000", event.fee_cents, "Stripe fee"),
                        JournalLineInput("1010", -event.fee_cents, "Stripe fee"),
                    ],
                    source_type="stripe",
                    source_ref=f"{source_ref}:fee",
                    import_run_id=import_run.id if import_run else None,
                )
            )
    elif event.event_type == "refund":
        revenue_reversal = event.amount_cents - tax_cents
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe refund {event.external_id}",
                lines=[
                    JournalLineInput("4010", revenue_reversal, "Refund"),
                    *([JournalLineInput("2100", tax_cents, "Sales tax refunded")] if tax_cents else []),
                    JournalLineInput("1010", -event.amount_cents, "Refund paid"),
                ],
                source_type="stripe",
                source_ref=source_ref,
                import_run_id=import_run.id if import_run else None,
            )
        )
    elif event.event_type == "dispute":
        loss_cents = event.amount_cents - tax_cents
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe dispute {event.external_id}",
                lines=[
                    JournalLineInput("5160", loss_cents, "Chargeback"),
                    *([JournalLineInput("2100", tax_cents, "Sales tax reversed")] if tax_cents else []),
                    JournalLineInput("1010", -event.amount_cents, "Stripe dispute hold"),
                ],
                source_type="stripe",
                source_ref=source_ref,
                import_run_id=import_run.id if import_run else None,
            )
        )
    elif event.event_type == "payout":
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe payout {event.external_id}",
                lines=[
                    JournalLineInput("1000", event.amount_cents, "Payout to bank"),
                    JournalLineInput("1010", -event.amount_cents, "Stripe clearing"),
                ],
                source_type="stripe",
                source_ref=source_ref,
                import_run_id=import_run.id if import_run else None,
            )
        )
    elif event.event_type == "fee":
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe fee {event.external_id}",
                lines=[
                    JournalLineInput("5000", event.amount_cents, "Stripe fee"),
                    JournalLineInput("1010", -event.amount_cents, "Stripe fee"),
                ],
                source_type="stripe",
                source_ref=source_ref,
                import_run_id=import_run.id if import_run else None,
            )
        )
    return created


def _external_payload_event_type(event: StripeEvent | StripeUnsupportedEvent) -> str:
    if isinstance(event, StripeUnsupportedEvent):
        return "unsupported_balance_transaction"
    return event.event_type


def _record_external_event(
    session: Session,
    *,
    provider: str,
    event: StripeEvent | StripeUnsupportedEvent,
    import_run_id: int | None,
    journal_entry_id: int | None,
) -> ExternalEvent:
    external_event = _find_external_event(session, provider=provider, external_id=event.external_id)
    if external_event:
        external_event.event_type = _external_payload_event_type(event)
        external_event.occurred_at = event.occurred_at
        external_event.payload_json = event.model_dump_json()
        external_event.import_run_id = import_run_id
        external_event.journal_entry_id = journal_entry_id
    else:
        external_event = ExternalEvent(
            provider=provider,
            external_id=event.external_id,
            event_type=_external_payload_event_type(event),
            occurred_at=event.occurred_at,
            payload_json=event.model_dump_json(),
            import_run_id=import_run_id,
            journal_entry_id=journal_entry_id,
        )
        session.add(external_event)
    session.flush()
    return external_event


def _refresh_external_event_payload(
    session: Session,
    *,
    external_event: ExternalEvent,
    event: StripeEvent | StripeUnsupportedEvent,
    import_run_id: int | None,
    refresh_source: str,
    change_note: str | None = None,
) -> ExternalEvent:
    _append_external_event_refresh(
        session,
        external_event=external_event,
        refresh_source=refresh_source,
        change_note=change_note,
    )
    external_event.event_type = _external_payload_event_type(event)
    external_event.occurred_at = event.occurred_at
    external_event.payload_json = event.model_dump_json()
    external_event.import_run_id = import_run_id
    session.flush()
    return external_event


def _fetch_latest_stripe_event(config: AppConfig, external_id: str) -> StripeEvent | StripeUnsupportedEvent:
    return fetch_stripe_event(config.stripe_api_key, external_id)


def _process_open_stripe_blocker(
    session: Session,
    *,
    config: AppConfig,
    import_run: ImportRun | None,
    external_event: ExternalEvent,
    blocker: ReviewBlocker,
    event: StripeEvent | StripeUnsupportedEvent,
    refresh_source: str,
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    _refresh_external_event_payload(
        session,
        external_event=external_event,
        event=event,
        import_run_id=import_run.id if import_run else None,
        refresh_source=refresh_source,
        change_note="Re-evaluating open review blocker against refreshed Stripe facts.",
    )
    blocker_date = _local_date(config, event.occurred_at)
    if isinstance(event, StripeUnsupportedEvent):
        note = _unsupported_stripe_reason(event)
        _reset_open_blocker(
            blocker,
            blocker_type="stripe_unsupported_event",
            blocker_date=blocker_date,
            note=note,
            external_event_id=external_event.id,
        )
        session.flush()
        warnings.append(f"{event.external_id}: {note}")
        return 0, warnings
    reason = _stripe_blocker_reason(config, event)
    if reason:
        _reset_open_blocker(
            blocker,
            blocker_type="stripe_tax_review",
            blocker_date=blocker_date,
            note=reason,
            external_event_id=external_event.id,
        )
        session.flush()
        warnings.append(f"{event.external_id}: {reason}")
        return 0, warnings

    entries = _post_stripe_event(session, config=config, import_run=import_run, event=event)
    external_event.journal_entry_id = entries[0].id if entries else None
    _resolve_review_blocker(
        blocker,
        resolution_type="posted_after_refresh",
        note="Posted after refreshing Stripe facts",
        resolution_entry_id=external_event.journal_entry_id,
    )
    session.flush()
    return len(entries), warnings


def resolve_review_blocker(
    session: Session,
    *,
    blocker_id: int,
    resolution_type: str,
    note: str | None = None,
    override_tax_cents: int | None = None,
    manual_entry_id: int | None = None,
    config: AppConfig,
) -> dict[str, object]:
    if resolution_type not in REVIEW_RESOLUTION_TYPES:
        raise ValidationError(f"Unsupported review resolution type: {resolution_type}")
    blocker = session.get(ReviewBlocker, blocker_id)
    if not blocker:
        raise ValidationError(f"Unknown review blocker: {blocker_id}")
    if blocker.status != "open":
        raise ValidationError("Only open review blockers can be resolved")

    external_event = session.get(ExternalEvent, blocker.external_event_id) if blocker.external_event_id else None
    if resolution_type == "superseded_by_manual_entry":
        if manual_entry_id is None:
            raise ValidationError("--manual-entry-id is required for superseded_by_manual_entry")
        if not session.get(JournalEntry, manual_entry_id):
            raise ValidationError(f"Unknown manual journal entry: {manual_entry_id}")
        _resolve_review_blocker(blocker, resolution_type=resolution_type, note=note, resolution_entry_id=manual_entry_id)
    elif resolution_type == "skip":
        _resolve_review_blocker(blocker, resolution_type=resolution_type, note=note)
    else:
        if blocker.blocker_type == "stripe_unsupported_event":
            raise ValidationError("post_with_override is not allowed for unsupported Stripe events")
        if not external_event:
            raise ValidationError("Review blocker is missing its external event payload")
        raw_event = _raw_payload_to_event(external_event)
        if isinstance(raw_event, StripeUnsupportedEvent):
            raise ValidationError("Unsupported Stripe events cannot be override-posted")
        if override_tax_cents is None:
            raise ValidationError("--override-tax-cents is required for post_with_override")
        event = raw_event.model_copy(update={"tax_cents": override_tax_cents})
        entries = _post_stripe_event(session, config=config, import_run=None, event=event, source_ref_suffix="override")
        external_event.journal_entry_id = entries[0].id if entries else None
        _resolve_review_blocker(
            blocker,
            resolution_type=resolution_type,
            note=note,
            resolution_entry_id=entries[0].id if entries else None,
        )
    session.flush()
    return {"review_blocker": _serialize_blocker(session, blocker)}


def retry_review_blocker(session: Session, *, blocker_id: int, config: AppConfig) -> tuple[dict[str, object], list[str]]:
    blocker = session.get(ReviewBlocker, blocker_id)
    if not blocker:
        raise ValidationError(f"Unknown review blocker: {blocker_id}")
    if blocker.status != "open":
        raise ValidationError("Only open review blockers can be retried")
    external_event = session.get(ExternalEvent, blocker.external_event_id) if blocker.external_event_id else None
    if not external_event or blocker.provider != "stripe":
        raise ValidationError("Retry is only supported for Stripe-backed review blockers")
    warnings: list[str] = []
    event = _fetch_latest_stripe_event(config, blocker.external_id)
    _posted, warnings = _process_open_stripe_blocker(
        session,
        config=config,
        import_run=None,
        external_event=external_event,
        blocker=blocker,
        event=event,
        refresh_source="review_retry",
    )
    session.flush()
    return {"review_blocker": _serialize_blocker(session, blocker)}, warnings


def _coerce_stripe_fetch_result(
    payload: StripeFetchResult | list[StripeEvent] | list[StripeEvent | StripeUnsupportedEvent],
) -> StripeFetchResult:
    if isinstance(payload, StripeFetchResult):
        return payload
    supported_events: list[StripeEvent] = []
    unsupported_events: list[StripeUnsupportedEvent] = []
    for item in payload:
        if isinstance(item, StripeUnsupportedEvent):
            unsupported_events.append(item)
        else:
            supported_events.append(item)
    return StripeFetchResult(supported_events=supported_events, unsupported_events=unsupported_events)


def import_csv(
    session: Session,
    *,
    ledger_dir: Path,
    account_code: str,
    csv_path: Path,
    profile_path: Path,
    statement_starting_balance: str | None = None,
    statement_ending_balance: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, object], list[str]]:
    account = get_account(session, account_code)
    profile = CSVImportProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))
    rows = read_csv_rows(csv_path)
    if not rows:
        raise ValidationError(f"CSV import file is empty: {csv_path}")

    dates = [parse_date(row[profile.date_column]) for row in rows]
    import_run = _start_import_run(
        session,
        source="csv",
        from_date=min(dates),
        to_date=max(dates),
        dry_run=dry_run,
        source_path=csv_path,
    )
    recon_session = None
    if statement_ending_balance is not None or statement_starting_balance is not None:
        if statement_ending_balance is None or statement_starting_balance is None:
            raise ValidationError("CSV imports that create reconciliation sessions require both starting and ending balances")
        ensure_interval_unlocked(session, min(dates), max(dates))
        overlaps = _overlapping_reconciliation_sessions(
            session,
            account_id=account.id,
            statement_start=min(dates),
            statement_end=max(dates),
        )
        if overlaps:
            raise ReconciliationError(
                "Cannot create an overlapping reconciliation session until the existing session is voided",
                data={"session_ids": [item.id for item in overlaps]},
            )
        recon_session = ReconciliationSession(
            account_id=account.id,
            statement_path=str(csv_path),
            statement_start=min(dates),
            statement_end=max(dates),
            statement_starting_balance_cents=parse_money(statement_starting_balance),
            statement_ending_balance_cents=parse_money(statement_ending_balance),
            status="open",
            created_at=utcnow(),
        )
        session.add(recon_session)
        session.flush()
        session.add(ReconciliationSessionEvent(session_id=recon_session.id, event_type="opened", created_at=utcnow()))
        create_document(
            session,
            ledger_dir=ledger_dir,
            source_path=csv_path,
            document_type=STATEMENT_DOCUMENT_TYPES.get(account.subtype, "bank_statement"),
            tax_year=max(dates).year,
            scope="business",
            period_start=min(dates),
            period_end=max(dates),
            notes=f"Statement support for import run {import_run.id}",
            created_via="import_csv",
            dry_run=dry_run,
            reconciliation_session_id=recon_session.id,
            import_run_id=import_run.id,
        )

    warnings: list[str] = []
    posted = 0
    duplicates = 0
    drafts = 0
    matched = 0

    for index, row in enumerate(rows, start=1):
        row_date = parse_date(row[profile.date_column])
        description = row[profile.description_column].strip()
        amount_cents = parse_money(row[profile.amount_column])
        external_ref = row.get(profile.external_ref_column) if profile.external_ref_column else None
        external_id = _csv_transaction_fingerprint(
            account_code=account_code,
            row_date=row_date,
            description=description,
            amount_cents=amount_cents,
            external_ref=external_ref,
        )
        existing = _find_external_event(session, provider=f"csv:{account_code}", external_id=external_id)
        if existing:
            duplicates += 1
            warnings.append(f"Skipped duplicate CSV row {index}")
            continue

        matched_rule = next(
            (rule for rule in profile.rules if re.search(rule.match, description, re.IGNORECASE)),
            None,
        )

        external_event = ExternalEvent(
            provider=f"csv:{account_code}",
            external_id=external_id,
            event_type="statement_row",
            occurred_at=datetime.combine(row_date, datetime.min.time(), tzinfo=utcnow().tzinfo),
            payload_json=json.dumps(row, sort_keys=True),
            import_run_id=import_run.id,
        )
        session.add(external_event)
        session.flush()

        recon_line = None
        if recon_session:
            recon_line = ReconciliationLine(
                session_id=recon_session.id,
                transaction_date=row_date,
                description=description,
                amount_cents=amount_cents,
                external_ref=external_ref,
                status="open",
            )
            session.add(recon_line)
            session.flush()

        if not matched_rule:
            drafts += 1
            warnings.append(f"Draft reconciliation row {index}: no profile rule matched '{description}'")
            if recon_line:
                recon_line.status = "draft"
            continue

        if matched_rule.entry_kind == "expense":
            if amount_cents > 0:
                raise ImportConflictError(f"CSV row {index} looks like income but matched an expense rule")
            lines = [
                JournalLineInput(matched_rule.account_code, abs(amount_cents), description),
                JournalLineInput(account_code, -abs(amount_cents), description),
            ]
        else:
            if amount_cents < 0:
                raise ImportConflictError(f"CSV row {index} looks like expense but matched an income rule")
            lines = [
                JournalLineInput(account_code, abs(amount_cents), description),
                JournalLineInput(matched_rule.account_code, -abs(amount_cents), description),
            ]

        entry = post_journal_entry(
            session,
            entry_date=row_date,
            description=f"CSV import: {description}",
            lines=lines,
            source_type="csv",
            source_ref=external_id,
            import_run_id=import_run.id,
        )
        external_event.journal_entry_id = entry.id
        posted += 1

        if recon_line:
            financial_line = next(line for line in entry.lines if line.account.code == account_code)
            match = ReconciliationMatch(
                reconciliation_line_id=recon_line.id,
                journal_line_id=financial_line.id,
                applied_amount_cents=abs(amount_cents),
                created_at=utcnow(),
            )
            session.add(match)
            recon_line.status = "matched"
            matched += 1

    summary = {
        "import_run_id": import_run.id,
        "posted_entries": posted,
        "duplicate_rows": duplicates,
        "draft_rows": drafts,
        "matched_rows": matched,
        "reconciliation_session_id": recon_session.id if recon_session else None,
    }
    _complete_import_run(import_run, warnings=warnings, summary=summary)
    return summary, warnings


def import_stripe(
    session: Session,
    *,
    config: AppConfig,
    start: date,
    end: date,
    dry_run: bool = False,
    events: StripeFetchResult | list[StripeEvent] | list[StripeEvent | StripeUnsupportedEvent] | None = None,
) -> tuple[dict[str, object], list[str]]:
    import_run = _start_import_run(session, source="stripe", from_date=start, to_date=end, dry_run=dry_run)
    fetch_result = _coerce_stripe_fetch_result(events) if events is not None else _coerce_stripe_fetch_result(
        fetch_stripe_events(
            config.stripe_api_key,
            start,
            end,
            timezone_name=config.timezone,
        )
    )
    stripe_events: list[StripeEvent | StripeUnsupportedEvent] = sorted(
        [*fetch_result.supported_events, *fetch_result.unsupported_events],
        key=lambda item: (item.occurred_at, item.external_id),
    )
    warnings: list[str] = []
    posted = 0
    duplicates = 0
    blocked = 0
    unsupported = 0

    for event in stripe_events:
        existing = _find_external_event(session, provider="stripe", external_id=event.external_id)
        if existing:
            blocker = _find_open_stripe_blocker(session, external_id=event.external_id)
            if blocker and blocker.status == "open":
                posted_count, blocker_warnings = _process_open_stripe_blocker(
                    session,
                    config=config,
                    import_run=import_run,
                    external_event=existing,
                    blocker=blocker,
                    event=event,
                    refresh_source="import_rerun",
                )
                posted += posted_count
                if posted_count == 0:
                    blocked += 1
                    if isinstance(event, StripeUnsupportedEvent):
                        unsupported += 1
                warnings.extend(blocker_warnings)
                continue
            duplicates += 1
            warnings.append(f"Skipped existing Stripe event {event.external_id}")
            continue

        external_event = _record_external_event(
            session,
            provider="stripe",
            event=event,
            import_run_id=import_run.id,
            journal_entry_id=None,
        )
        if isinstance(event, StripeUnsupportedEvent):
            blocker = _upsert_review_blocker(
                session,
                blocker_type="stripe_unsupported_event",
                provider="stripe",
                external_id=event.external_id,
                blocker_date=_local_date(config, event.occurred_at),
                note=_unsupported_stripe_reason(event),
                external_event_id=external_event.id,
            )
            blocked += 1
            unsupported += 1
            warnings.append(f"{event.external_id}: {blocker.resolution_note}")
            continue
        blocker_reason = _stripe_blocker_reason(config, event)
        if blocker_reason:
            blocker = _upsert_review_blocker(
                session,
                blocker_type="stripe_tax_review",
                provider="stripe",
                external_id=event.external_id,
                blocker_date=_local_date(config, event.occurred_at),
                note=blocker_reason,
                external_event_id=external_event.id,
            )
            blocked += 1
            warnings.append(f"{event.external_id}: {blocker.resolution_note}")
            continue

        entries = _post_stripe_event(session, config=config, import_run=import_run, event=event)
        posted += len(entries)
        external_event.journal_entry_id = entries[0].id if entries else None

    summary = {
        "import_run_id": import_run.id,
        "source": "stripe",
        "events_seen": len(stripe_events),
        "entries_posted": posted,
        "duplicates": duplicates,
        "blocked_events": blocked,
        "unsupported_events": unsupported,
    }
    _complete_import_run(import_run, warnings=warnings, summary=summary)
    return summary, warnings


def _load_reconciliation_session(session: Session, session_id: int) -> ReconciliationSession:
    recon = session.scalar(
        select(ReconciliationSession)
        .options(
            selectinload(ReconciliationSession.account),
            selectinload(ReconciliationSession.lines).selectinload(ReconciliationLine.matches),
            selectinload(ReconciliationSession.events),
        )
        .where(ReconciliationSession.id == session_id)
    )
    if not recon:
        raise ValidationError(f"Unknown reconciliation session: {session_id}")
    return recon


def _reconciliation_ranges_overlap(
    *,
    left_start: date,
    left_end: date,
    right_start: date,
    right_end: date,
) -> bool:
    return left_start <= right_end and left_end >= right_start


def _overlapping_reconciliation_sessions(
    session: Session,
    *,
    account_id: int,
    statement_start: date,
    statement_end: date,
    exclude_session_id: int | None = None,
) -> list[ReconciliationSession]:
    query = select(ReconciliationSession).where(
        ReconciliationSession.account_id == account_id,
        ReconciliationSession.status != "voided",
        ReconciliationSession.statement_start <= statement_end,
        ReconciliationSession.statement_end >= statement_start,
    )
    if exclude_session_id is not None:
        query = query.where(ReconciliationSession.id != exclude_session_id)
    return list(session.scalars(query.order_by(ReconciliationSession.statement_start, ReconciliationSession.id)))


def _find_overlap_conflicting_matches(
    session: Session,
    *,
    journal_line_id: int,
    recon: ReconciliationSession,
) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            ReconciliationMatch.id,
            ReconciliationSession.id,
            ReconciliationSession.statement_start,
            ReconciliationSession.statement_end,
            ReconciliationSession.status,
        )
        .join(ReconciliationLine, ReconciliationMatch.reconciliation_line_id == ReconciliationLine.id)
        .join(ReconciliationSession, ReconciliationLine.session_id == ReconciliationSession.id)
        .where(
            ReconciliationMatch.journal_line_id == journal_line_id,
            ReconciliationMatch.reversed_at.is_(None),
            ReconciliationSession.id != recon.id,
            ReconciliationSession.account_id == recon.account_id,
            ReconciliationSession.status != "voided",
            ReconciliationSession.statement_start <= recon.statement_end,
            ReconciliationSession.statement_end >= recon.statement_start,
        )
    ).all()
    return [
        {
            "match_id": row[0],
            "session_id": row[1],
            "statement_start": row[2],
            "statement_end": row[3],
            "status": row[4],
        }
        for row in rows
    ]


def _reconciliation_session_coverage(
    sessions: list[ReconciliationSession],
    *,
    period_start: date,
    period_end: date,
) -> bool:
    relevant = [item for item in sessions if item.status == "closed" and item.statement_start <= period_end and item.statement_end >= period_start]
    if not relevant:
        return False
    relevant.sort(key=lambda item: (item.statement_start, item.statement_end, item.id))
    covered_start = relevant[0].statement_start
    covered_end = relevant[0].statement_end
    if covered_start > period_start:
        return False
    for item in relevant[1:]:
        if item.statement_start > covered_end + timedelta(days=1):
            break
        if item.statement_end > covered_end:
            covered_end = item.statement_end
        if covered_end >= period_end:
            return True
    return covered_end >= period_end


def start_reconciliation(
    session: Session,
    *,
    ledger_dir: Path,
    account_code: str,
    statement_path: Path,
    statement_start: date,
    statement_end: date,
    statement_starting_balance: str,
    statement_ending_balance: str,
    dry_run: bool = False,
) -> dict[str, object]:
    account = get_account(session, account_code)
    if account.subtype not in FINANCIAL_SUBTYPES:
        raise ValidationError(f"Account {account_code} is not reconcilable")
    ensure_interval_unlocked(session, statement_start, statement_end)
    overlaps = _overlapping_reconciliation_sessions(
        session,
        account_id=account.id,
        statement_start=statement_start,
        statement_end=statement_end,
    )
    if overlaps:
        raise ReconciliationError(
            "Cannot create an overlapping reconciliation session until the existing session is voided",
            data={"session_ids": [item.id for item in overlaps]},
        )

    rows = read_csv_rows(statement_path)
    recon = ReconciliationSession(
        account_id=account.id,
        statement_path=str(statement_path),
        statement_start=statement_start,
        statement_end=statement_end,
        statement_starting_balance_cents=parse_money(statement_starting_balance),
        statement_ending_balance_cents=parse_money(statement_ending_balance),
        status="open",
        created_at=utcnow(),
    )
    session.add(recon)
    session.flush()
    session.add(ReconciliationSessionEvent(session_id=recon.id, event_type="opened", created_at=utcnow()))
    create_document(
        session,
        ledger_dir=ledger_dir,
        source_path=statement_path,
        document_type=STATEMENT_DOCUMENT_TYPES.get(account.subtype, "bank_statement"),
        tax_year=statement_end.year,
        scope="business",
        period_start=statement_start,
        period_end=statement_end,
        notes=f"Statement support for reconciliation session {recon.id}",
        created_via="reconciliation",
        dry_run=dry_run,
        reconciliation_session_id=recon.id,
    )

    for row in rows:
        session.add(
            ReconciliationLine(
                session_id=recon.id,
                transaction_date=parse_date(row["date"]),
                description=row["description"],
                amount_cents=parse_money(row["amount"]),
                external_ref=row.get("external_ref"),
                status="open",
            )
        )

    session.flush()
    return {"session_id": recon.id, "line_count": len(rows), "account_code": account_code}


def _statement_line_residual(session: Session, line: ReconciliationLine) -> int:
    return abs(line.amount_cents) - _sum_open_reconciliation_matches_for_statement_line(session, line.id)


def _journal_line_reconciliation_residual(session: Session, line: JournalLine) -> int:
    return abs(line.amount_cents) - _sum_open_reconciliation_matches_for_line(session, line.id)


def reconciliation_candidates(session: Session, *, session_id: int) -> dict[str, object]:
    recon = _load_reconciliation_session(session, session_id)
    lines = list(
        session.scalars(
            select(JournalLine)
            .join(JournalEntry)
            .where(
                JournalLine.account_id == recon.account_id,
                JournalEntry.entry_date <= recon.statement_end,
            )
            .options(selectinload(JournalLine.account), selectinload(JournalLine.entry))
            .order_by(JournalEntry.entry_date, JournalLine.id)
        )
    )
    rows = []
    for line in lines:
        residual = _journal_line_reconciliation_residual(session, line)
        if residual <= 0:
            continue
        overlap_conflicts = _find_overlap_conflicting_matches(session, journal_line_id=line.id, recon=recon)
        rejection_reason = None
        if overlap_conflicts:
            rejection_reason = "journal_line_already_matched_in_overlapping_session"
        rows.append(
            {
                "journal_line_id": line.id,
                "entry_id": line.entry_id,
                "entry_date": line.entry.entry_date,
                "account_code": line.account.code,
                "amount_cents": line.amount_cents,
                "residual_cents": residual,
                "memo": line.memo,
                "prior_outstanding": line.entry.entry_date < recon.statement_start,
                "matchable": rejection_reason is None,
                "rejection_reason": rejection_reason,
                "conflicting_session_ids": [item["session_id"] for item in overlap_conflicts],
            }
        )
    return {"session_id": session_id, "rows": rows}


def match_reconciliation(
    session: Session,
    *,
    session_id: int,
    line_id: int,
    journal_line_id: int,
    amount: str,
) -> dict[str, object]:
    recon = _load_reconciliation_session(session, session_id)
    ensure_interval_unlocked(session, recon.statement_start, recon.statement_end)
    if recon.status != "open":
        raise ReconciliationError("Closed reconciliation sessions are immutable until reopened")
    line = session.scalar(select(ReconciliationLine).where(ReconciliationLine.id == line_id, ReconciliationLine.session_id == session_id))
    if not line:
        raise ValidationError(f"Unknown reconciliation line {line_id} in session {session_id}")

    journal_line = _load_line(session, journal_line_id)
    if journal_line.account_id != recon.account_id:
        raise ReconciliationError("Journal line account does not match the reconciliation account")
    if journal_line.entry.entry_date > recon.statement_end:
        raise ReconciliationError("Journal line is dated after the statement end")
    overlap_conflicts = _find_overlap_conflicting_matches(session, journal_line_id=journal_line.id, recon=recon)
    if overlap_conflicts:
        raise ReconciliationError(
            "Journal line is already matched in an overlapping reconciliation session",
            data={"journal_line_id": journal_line.id, "conflicting_session_ids": [item["session_id"] for item in overlap_conflicts]},
        )

    applied_amount_cents = abs(parse_money(amount))
    if applied_amount_cents <= 0:
        raise ValidationError("Reconciliation amount must be positive")
    statement_residual = _statement_line_residual(session, line)
    journal_residual = _journal_line_reconciliation_residual(session, journal_line)
    if applied_amount_cents > statement_residual:
        raise ReconciliationError("Match amount exceeds statement-line residual", data={"statement_line_id": line.id})
    if applied_amount_cents > journal_residual:
        raise ReconciliationError("Match amount exceeds journal-line residual", data={"journal_line_id": journal_line.id})
    if _line_sign(line.amount_cents) != _line_sign(journal_line.amount_cents):
        raise ReconciliationError("Statement line and journal line must have the same sign for reconciliation")

    match = ReconciliationMatch(
        reconciliation_line_id=line.id,
        journal_line_id=journal_line.id,
        applied_amount_cents=applied_amount_cents,
        created_at=utcnow(),
    )
    session.add(match)
    session.flush()

    if _statement_line_residual(session, line) == 0:
        line.status = "matched"
    session.flush()
    return {
        "session_id": session_id,
        "line_id": line_id,
        "journal_line_id": journal_line_id,
        "applied_amount_cents": applied_amount_cents,
    }


def display_balance(account: Account, raw_balance_cents: int) -> int:
    if account.kind in {"asset", "expense", "contra_revenue"}:
        return raw_balance_cents
    return -raw_balance_cents


def account_balance_as_of(session: Session, *, account_id: int, as_of: date) -> int:
    raw = session.scalar(
        select(func.coalesce(func.sum(JournalLine.amount_cents), 0))
        .join(JournalEntry)
        .where(JournalLine.account_id == account_id, JournalEntry.entry_date <= as_of)
    )
    return int(raw or 0)


def _financial_account_activity_count(session: Session, *, account_id: int, period_start: date, period_end: date) -> int:
    count = session.scalar(
        select(func.count(JournalLine.id))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == account_id,
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    )
    return int(count or 0)


def reconciliation_required_accounts(
    session: Session,
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, object]]:
    opening_as_of = period_start - timedelta(days=1)
    rows: list[dict[str, object]] = []
    accounts = list(
        session.scalars(
            select(Account)
            .where(Account.subtype.in_(FINANCIAL_SUBTYPES))
            .order_by(Account.code)
        )
    )
    for account in accounts:
        opening_raw = account_balance_as_of(session, account_id=account.id, as_of=opening_as_of)
        closing_raw = account_balance_as_of(session, account_id=account.id, as_of=period_end)
        activity_count = _financial_account_activity_count(
            session,
            account_id=account.id,
            period_start=period_start,
            period_end=period_end,
        )
        rows.append(
            {
                "account_id": account.id,
                "account_code": account.code,
                "account_name": account.name,
                "account_subtype": account.subtype,
                "opening_balance_cents": display_balance(account, opening_raw),
                "closing_balance_cents": display_balance(account, closing_raw),
                "activity_count": activity_count,
                "required_for_close": bool(opening_raw or closing_raw or activity_count),
            }
        )
    return rows


def _merged_reconciliation_ranges(
    sessions: list[ReconciliationSession],
    *,
    period_start: date,
    period_end: date,
) -> list[tuple[date, date]]:
    clipped = [
        (max(item.statement_start, period_start), min(item.statement_end, period_end))
        for item in sessions
        if item.status == "closed" and item.statement_start <= period_end and item.statement_end >= period_start
    ]
    if not clipped:
        return []
    clipped.sort()
    merged = [clipped[0]]
    for start, end in clipped[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + timedelta(days=1):
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _coverage_gap_days(merged_ranges: list[tuple[date, date]], *, period_start: date, period_end: date) -> int:
    if not merged_ranges:
        return (period_end - period_start).days + 1
    uncovered = 0
    cursor = period_start
    for start, end in merged_ranges:
        if start > cursor:
            uncovered += (start - cursor).days
        cursor = max(cursor, end + timedelta(days=1))
    if cursor <= period_end:
        uncovered += (period_end - cursor).days + 1
    return uncovered


def reconciliation_coverage_summary(
    session: Session,
    *,
    period_start: date,
    period_end: date,
) -> dict[str, object]:
    required_accounts = reconciliation_required_accounts(session, period_start=period_start, period_end=period_end)
    account_ids = [int(row["account_id"]) for row in required_accounts]
    sessions = list(
        session.scalars(
            select(ReconciliationSession)
            .options(selectinload(ReconciliationSession.account), selectinload(ReconciliationSession.lines).selectinload(ReconciliationLine.matches))
            .where(
                ReconciliationSession.account_id.in_(account_ids),
                ReconciliationSession.status == "closed",
                ReconciliationSession.statement_start <= period_end,
                ReconciliationSession.statement_end >= period_start,
            )
            .order_by(ReconciliationSession.statement_start, ReconciliationSession.statement_end, ReconciliationSession.id)
        )
    )
    sessions_by_account: dict[int, list[ReconciliationSession]] = defaultdict(list)
    for item in sessions:
        sessions_by_account[item.account_id].append(item)

    coverage_rows: list[dict[str, object]] = []
    for account_row in required_accounts:
        account_sessions = sessions_by_account.get(account_row["account_id"], [])
        merged = _merged_reconciliation_ranges(account_sessions, period_start=period_start, period_end=period_end)
        coverage_start = merged[0][0] if merged else None
        coverage_end = merged[-1][1] if merged else None
        gap_days = 0 if not account_row["required_for_close"] else _coverage_gap_days(merged, period_start=period_start, period_end=period_end)
        coverage_rows.append(
            {
                **account_row,
                "covered": (not account_row["required_for_close"]) or gap_days == 0,
                "coverage_start": coverage_start,
                "coverage_end": coverage_end,
                "coverage_gap_days": gap_days,
                "session_ids": [item.id for item in account_sessions],
            }
        )

    session_rows = []
    for item in sessions:
        unresolved = sum(
            1
            for line in item.lines
            if abs(line.amount_cents) != sum(match.applied_amount_cents for match in line.matches if match.reversed_at is None)
        )
        session_rows.append(
            {
                "session_id": item.id,
                "account_code": item.account.code,
                "account_name": item.account.name,
                "statement_start": item.statement_start,
                "statement_end": item.statement_end,
                "statement_starting_balance_cents": item.statement_starting_balance_cents,
                "statement_ending_balance_cents": item.statement_ending_balance_cents,
                "status": item.status,
                "unresolved_statement_lines": unresolved,
            }
        )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "report_basis": "control",
        "sessions": session_rows,
        "coverage_rows": coverage_rows,
    }


def _outstanding_candidates_for_session(session: Session, recon: ReconciliationSession) -> list[dict[str, object]]:
    rows = reconciliation_candidates(session, session_id=recon.id)["rows"]
    return rows


def close_reconciliation(session: Session, *, session_id: int) -> dict[str, object]:
    recon = _load_reconciliation_session(session, session_id)
    ensure_interval_unlocked(session, recon.statement_start, recon.statement_end)
    if recon.status != "open":
        raise ReconciliationError("Reconciliation session is not open")
    conflicting_journal_lines: list[dict[str, object]] = []
    for line in recon.lines:
        for match in line.matches:
            if match.reversed_at is not None:
                continue
            overlap_conflicts = _find_overlap_conflicting_matches(session, journal_line_id=match.journal_line_id, recon=recon)
            if overlap_conflicts:
                conflicting_journal_lines.append(
                    {
                        "journal_line_id": match.journal_line_id,
                        "conflicting_session_ids": [item["session_id"] for item in overlap_conflicts],
                    }
                )
    if conflicting_journal_lines:
        raise ReconciliationError(
            "Cannot close reconciliation while journal lines are shared with overlapping sessions",
            data={"conflicts": conflicting_journal_lines},
        )
    unresolved = [line.id for line in recon.lines if line.status not in {"matched", "ignored"} or _statement_line_residual(session, line) != 0]
    if unresolved:
        raise ReconciliationError("Cannot close reconciliation with unresolved statement lines", data={"line_ids": unresolved})

    statement_activity = sum(line.amount_cents for line in recon.lines)
    expected = recon.statement_starting_balance_cents + statement_activity
    if expected != recon.statement_ending_balance_cents:
        raise ReconciliationError(
            "Statement rows do not bridge the starting balance to the ending balance",
            data={
                "starting_balance_cents": recon.statement_starting_balance_cents,
                "statement_activity_cents": statement_activity,
                "expected_ending_balance_cents": expected,
                "statement_ending_balance_cents": recon.statement_ending_balance_cents,
            },
        )

    recon.status = "closed"
    recon.closed_at = utcnow()
    session.add(ReconciliationSessionEvent(session_id=recon.id, event_type="closed", created_at=utcnow()))
    session.flush()
    outstanding = _outstanding_candidates_for_session(session, recon)
    return {
        "session_id": recon.id,
        "status": recon.status,
        "outstanding_count": len(outstanding),
        "outstanding_residual_cents": sum(item["residual_cents"] for item in outstanding),
    }


def reopen_reconciliation(session: Session, *, session_id: int, reason: str) -> dict[str, object]:
    recon = _load_reconciliation_session(session, session_id)
    ensure_interval_unlocked(session, recon.statement_start, recon.statement_end)
    if recon.status != "closed":
        raise ReconciliationError("Only closed reconciliation sessions can be reopened")
    recon.status = "open"
    recon.closed_at = None
    session.add(ReconciliationSessionEvent(session_id=recon.id, event_type="reopened", reason=reason, created_at=utcnow()))
    session.flush()
    return {"session_id": recon.id, "status": recon.status}


def void_reconciliation(session: Session, *, session_id: int, reason: str) -> dict[str, object]:
    recon = _load_reconciliation_session(session, session_id)
    ensure_interval_unlocked(session, recon.statement_start, recon.statement_end)
    if recon.status == "closed":
        raise ReconciliationError("Closed reconciliation sessions must be reopened before they can be voided")
    if recon.status == "voided":
        raise ReconciliationError("Reconciliation session is already voided")
    for line in recon.lines:
        for match in line.matches:
            if match.reversed_at is not None:
                continue
            match.reversed_at = utcnow()
            match.reversal_reason = reason
        line.status = "voided"
    recon.status = "voided"
    recon.closed_at = None
    session.add(ReconciliationSessionEvent(session_id=recon.id, event_type="voided", reason=reason, created_at=utcnow()))
    session.flush()
    return {"session_id": recon.id, "status": recon.status}


def list_reconciliation_sessions(session: Session) -> list[ReconciliationSession]:
    return list(
        session.scalars(
            select(ReconciliationSession)
            .options(selectinload(ReconciliationSession.account), selectinload(ReconciliationSession.events))
            .order_by(ReconciliationSession.id.desc())
        )
    )


def close_period(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    lock_type: str,
    reason: str | None,
    ledger_dir: Path,
    config: AppConfig,
    acknowledge_review_ids: list[int] | None = None,
) -> dict[str, object]:
    del acknowledge_review_ids

    open_blockers = list(
        session.scalars(
            select(ReviewBlocker).where(
                ReviewBlocker.status == "open",
                ReviewBlocker.blocker_date >= period_start,
                ReviewBlocker.blocker_date <= period_end,
            )
        )
    )
    if open_blockers:
        raise ComplianceError(
            "Cannot close period with open review blockers",
            data={"review_blocker_ids": [item.id for item in open_blockers]},
        )

    coverage = reconciliation_coverage_summary(session, period_start=period_start, period_end=period_end)
    missing = [
        row["account_code"]
        for row in coverage["coverage_rows"]
        if row["required_for_close"] and not row["covered"]
    ]
    if missing:
        raise ReconciliationError(
            "Cannot close period until all financial accounts are reconciled",
            data={"account_codes": missing, "coverage_rows": coverage["coverage_rows"]},
        )

    session.add(
        PeriodLock(
            period_start=period_start,
            period_end=period_end,
            lock_type=lock_type,
            action="close",
            reason=reason,
            created_at=utcnow(),
        )
    )
    session.flush()
    from clawbooks.integrity import persist_close_snapshot

    snapshot = persist_close_snapshot(
        session,
        ledger_dir=ledger_dir,
        config=config,
        period_start=period_start,
        period_end=period_end,
        source="period_close",
        reason=reason,
    )
    return {
        "period_start": period_start,
        "period_end": period_end,
        "status": "closed",
        "close_snapshot_id": snapshot.id,
    }


def reopen_period(session: Session, *, period_start: date, period_end: date, reason: str) -> dict[str, object]:
    session.add(
        PeriodLock(
            period_start=period_start,
            period_end=period_end,
            lock_type="period",
            action="reopen",
            reason=reason,
            created_at=utcnow(),
        )
    )
    session.flush()
    return {"period_start": period_start, "period_end": period_end, "status": "reopened"}


def period_status(session: Session, *, period_start: date, period_end: date) -> dict[str, object]:
    locks = list(
        session.scalars(
            select(PeriodLock)
            .where(PeriodLock.period_start == period_start, PeriodLock.period_end == period_end)
            .order_by(PeriodLock.created_at, PeriodLock.id)
        )
    )
    active = locks[-1].action if locks else "open"
    return {
        "period_start": period_start,
        "period_end": period_end,
        "status": active,
        "events": [
            {
                "action": lock.action,
                "lock_type": lock.lock_type,
                "reason": lock.reason,
                "created_at": lock.created_at,
            }
            for lock in locks
        ],
    }
