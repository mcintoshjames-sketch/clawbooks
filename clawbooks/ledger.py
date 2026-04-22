from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from clawbooks.defaults import DEFAULT_ACCOUNTS, default_tax_templates
from clawbooks.exceptions import ComplianceError, ImportConflictError, LockedPeriodError, ReconciliationError, ValidationError
from clawbooks.models import (
    Account,
    Attachment,
    ExternalEvent,
    ImportRun,
    JournalEntry,
    JournalLine,
    PeriodLock,
    ReconciliationLine,
    ReconciliationSession,
    Setting,
    TaxObligation,
)
from clawbooks.schemas import AppConfig, CSVImportProfile, StripeEvent
from clawbooks.stripe_client import fetch_stripe_events
from clawbooks.utils import parse_date, parse_money, read_csv_rows, sha256_for_path, stable_external_id, utcnow

ACCOUNT_KINDS = {"asset", "liability", "equity", "revenue", "expense", "contra_revenue"}
FINANCIAL_SUBTYPES = {"bank", "card", "stripe_clearing"}


@dataclass(slots=True)
class JournalLineInput:
    account_code: str
    amount_cents: int
    memo: str | None = None


def seed_defaults(session: Session, year: int) -> None:
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

    for template in default_tax_templates(year):
        exists = session.scalar(select(TaxObligation.id).where(TaxObligation.code == template.code))
        if exists:
            continue
        liability_account_id = None
        if template.liability_account_code:
            account = session.scalar(select(Account).where(Account.code == template.liability_account_code))
            liability_account_id = account.id if account else None
        session.add(
            TaxObligation(
                code=template.code,
                description=template.description,
                jurisdiction=template.jurisdiction,
                due_date=template.due_date,
                status="pending",
                liability_account_id=liability_account_id,
                notes=template.notes,
            )
        )

    if not session.get(Setting, "ledger_version"):
        session.add(Setting(key="ledger_version", value_json=json.dumps({"version": 1})))


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


def post_journal_entry(
    session: Session,
    *,
    entry_date: date,
    description: str,
    lines: list[JournalLineInput],
    source_type: str,
    source_ref: str | None = None,
    reversal_of_entry_id: int | None = None,
    review_required: bool = False,
    review_message: str | None = None,
    cash_basis_included: bool = True,
    import_run_id: int | None = None,
) -> JournalEntry:
    ensure_unlocked(session, entry_date)
    if not lines:
        raise ValidationError("Journal entry must include at least one line")
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
        review_required=review_required,
        review_message=review_message,
        cash_basis_included=cash_basis_included,
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
    return session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry.id)
    )


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
    attachment_id = None
    if receipt_path:
        attachment = record_attachment(session, receipt_path, description=f"Receipt for {vendor}")
        attachment_id = attachment.id

    return {
        "entry_id": entry.id,
        "vendor": vendor,
        "amount_cents": amount_cents,
        "category_code": category_code,
        "offset_account_code": offset_code,
        "attachment_id": attachment_id,
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

    reversal = post_journal_entry(
        session,
        entry_date=reversal_date,
        description=f"Reversal of entry {entry_id}: {reason}",
        lines=[
            JournalLineInput(line.account.code, -line.amount_cents, line.memo)
            for line in original.lines
        ],
        source_type="reversal",
        source_ref=str(entry_id),
        reversal_of_entry_id=entry_id,
        cash_basis_included=original.cash_basis_included,
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


def import_csv(
    session: Session,
    *,
    account_code: str,
    csv_path: Path,
    profile_path: Path,
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
    if statement_ending_balance is not None:
        recon_session = ReconciliationSession(
            account_id=account.id,
            statement_path=str(csv_path),
            statement_start=min(dates),
            statement_end=max(dates),
            statement_ending_balance_cents=parse_money(statement_ending_balance),
            status="open",
            created_at=utcnow(),
        )
        session.add(recon_session)
        session.flush()

    warnings: list[str] = []
    posted = 0
    duplicates = 0
    drafts = 0
    matched = 0

    for index, row in enumerate(rows, start=1):
        external_id = stable_external_id(csv_path.resolve(), index, json.dumps(row, sort_keys=True))
        existing = session.scalar(
            select(ExternalEvent).where(
                ExternalEvent.provider == f"csv:{account_code}",
                ExternalEvent.external_id == external_id,
            )
        )
        if existing:
            duplicates += 1
            warnings.append(f"Skipped duplicate CSV row {index}")
            continue

        row_date = parse_date(row[profile.date_column])
        description = row[profile.description_column].strip()
        amount_cents = parse_money(row[profile.amount_column])
        external_ref = row.get(profile.external_ref_column) if profile.external_ref_column else None
        matched_rule = next(
            (rule for rule in profile.rules if re.search(rule.match, description, re.IGNORECASE)),
            None,
        )

        external_event = ExternalEvent(
            provider=f"csv:{account_code}",
            external_id=external_id,
            event_type="statement_row",
            occurred_at=utcnow(),
            payload_json=json.dumps(row, sort_keys=True),
            import_run_id=import_run.id,
        )
        session.add(external_event)
        session.flush()

        if not matched_rule:
            drafts += 1
            warnings.append(f"Draft reconciliation row {index}: no profile rule matched '{description}'")
            if recon_session:
                session.add(
                    ReconciliationLine(
                        session_id=recon_session.id,
                        transaction_date=row_date,
                        description=description,
                        amount_cents=amount_cents,
                        external_ref=external_ref,
                        status="draft",
                    )
                )
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

        if recon_session:
            session.add(
                ReconciliationLine(
                    session_id=recon_session.id,
                    transaction_date=row_date,
                    description=description,
                    amount_cents=amount_cents,
                    external_ref=external_ref,
                    matched_entry_id=entry.id,
                    status="matched",
                )
            )
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


def _stripe_review_state(config: AppConfig, event: StripeEvent) -> tuple[bool, str | None]:
    if config.stripe_tax_mode == "handled_by_stripe_tax" and event.event_type in {"charge", "refund"} and event.tax_cents == 0:
        return True, "Stripe event missing tax detail. Confirm taxability or configure manual review."
    return False, None


def _post_stripe_event(
    session: Session,
    *,
    config: AppConfig,
    import_run: ImportRun,
    event: StripeEvent,
) -> list[JournalEntry]:
    review_required, review_message = _stripe_review_state(config, event)
    created: list[JournalEntry] = []
    event_date = event.occurred_at.date()

    if event.event_type == "charge":
        revenue_cents = event.amount_cents - event.tax_cents
        charge_entry = post_journal_entry(
            session,
            entry_date=event_date,
            description=event.description or f"Stripe charge {event.external_id}",
            lines=[
                JournalLineInput("1010", event.amount_cents, "Stripe gross receipt"),
                JournalLineInput("4000", -revenue_cents, "Subscription revenue"),
                *([JournalLineInput("2100", -event.tax_cents, "Sales tax collected")] if event.tax_cents else []),
            ],
            source_type="stripe",
            source_ref=event.external_id,
            review_required=review_required,
            review_message=review_message,
            import_run_id=import_run.id,
        )
        created.append(charge_entry)
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
                    source_ref=f"{event.external_id}:fee",
                    import_run_id=import_run.id,
                )
            )
    elif event.event_type == "refund":
        revenue_reversal = event.amount_cents - event.tax_cents
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe refund {event.external_id}",
                lines=[
                    JournalLineInput("4010", revenue_reversal, "Refund"),
                    *([JournalLineInput("2100", event.tax_cents, "Sales tax refunded")] if event.tax_cents else []),
                    JournalLineInput("1010", -event.amount_cents, "Refund paid"),
                ],
                source_type="stripe",
                source_ref=event.external_id,
                review_required=review_required,
                review_message=review_message,
                import_run_id=import_run.id,
            )
        )
    elif event.event_type == "dispute":
        created.append(
            post_journal_entry(
                session,
                entry_date=event_date,
                description=event.description or f"Stripe dispute {event.external_id}",
                lines=[
                    JournalLineInput("5160", event.amount_cents, "Chargeback"),
                    JournalLineInput("1010", -event.amount_cents, "Stripe dispute hold"),
                ],
                source_type="stripe",
                source_ref=event.external_id,
                import_run_id=import_run.id,
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
                source_ref=event.external_id,
                import_run_id=import_run.id,
            )
        )

    return created


def import_stripe(
    session: Session,
    *,
    config: AppConfig,
    start: date,
    end: date,
    dry_run: bool = False,
    events: list[StripeEvent] | None = None,
) -> tuple[dict[str, object], list[str]]:
    import_run = _start_import_run(session, source="stripe", from_date=start, to_date=end, dry_run=dry_run)
    stripe_events = events or fetch_stripe_events(config.stripe_api_key, start, end)
    warnings: list[str] = []
    posted = 0
    duplicates = 0

    for event in stripe_events:
        existing = session.scalar(
            select(ExternalEvent).where(
                ExternalEvent.provider == "stripe",
                ExternalEvent.external_id == event.external_id,
            )
        )
        if existing:
            duplicates += 1
            warnings.append(f"Skipped duplicate Stripe event {event.external_id}")
            continue

        entries = _post_stripe_event(session, config=config, import_run=import_run, event=event)
        posted += len(entries)
        session.add(
            ExternalEvent(
                provider="stripe",
                external_id=event.external_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload_json=event.model_dump_json(),
                import_run_id=import_run.id,
                journal_entry_id=entries[0].id if entries else None,
            )
        )
        if entries and entries[0].review_required and entries[0].review_message:
            warnings.append(f"{event.external_id}: {entries[0].review_message}")

    summary = {
        "import_run_id": import_run.id,
        "source": "stripe",
        "events_seen": len(stripe_events),
        "entries_posted": posted,
        "duplicates": duplicates,
    }
    _complete_import_run(import_run, warnings=warnings, summary=summary)
    return summary, warnings


def start_reconciliation(
    session: Session,
    *,
    account_code: str,
    statement_path: Path,
    statement_start: date,
    statement_end: date,
    statement_ending_balance: str,
) -> dict[str, object]:
    account = get_account(session, account_code)
    if account.subtype not in FINANCIAL_SUBTYPES:
        raise ValidationError(f"Account {account_code} is not reconcilable")

    rows = read_csv_rows(statement_path)
    recon = ReconciliationSession(
        account_id=account.id,
        statement_path=str(statement_path),
        statement_start=statement_start,
        statement_end=statement_end,
        statement_ending_balance_cents=parse_money(statement_ending_balance),
        status="open",
        created_at=utcnow(),
    )
    session.add(recon)
    session.flush()

    for row in rows:
        session.add(
            ReconciliationLine(
                session_id=recon.id,
                transaction_date=parse_date(row["date"]),
                description=row["description"],
                amount_cents=parse_money(row["amount"]),
                external_ref=row.get("external_ref"),
                status="draft",
            )
        )

    session.flush()
    return {"session_id": recon.id, "line_count": len(rows), "account_code": account_code}


def match_reconciliation(session: Session, *, session_id: int, line_id: int, entry_id: int) -> dict[str, object]:
    line = session.scalar(
        select(ReconciliationLine)
        .join(ReconciliationSession)
        .where(ReconciliationLine.id == line_id, ReconciliationLine.session_id == session_id)
    )
    if not line:
        raise ValidationError(f"Unknown reconciliation line {line_id} in session {session_id}")

    recon = session.get(ReconciliationSession, session_id)
    entry = session.scalar(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines).selectinload(JournalLine.account))
        .where(JournalEntry.id == entry_id)
    )
    if not entry:
        raise ValidationError(f"Unknown journal entry: {entry_id}")

    matching_line = next(
        (
            entry_line
            for entry_line in entry.lines
            if entry_line.account_id == recon.account_id and abs(entry_line.amount_cents) == abs(line.amount_cents)
        ),
        None,
    )
    if not matching_line:
        raise ReconciliationError(
            f"Entry {entry_id} does not contain a matching line for reconciliation line {line_id}",
            data={"amount_cents": line.amount_cents},
        )

    line.matched_entry_id = entry.id
    line.status = "matched"
    session.flush()
    return {"session_id": session_id, "line_id": line_id, "entry_id": entry_id}


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


def close_reconciliation(session: Session, *, session_id: int) -> dict[str, object]:
    recon = session.scalar(
        select(ReconciliationSession)
        .options(selectinload(ReconciliationSession.lines), selectinload(ReconciliationSession.account))
        .where(ReconciliationSession.id == session_id)
    )
    if not recon:
        raise ValidationError(f"Unknown reconciliation session: {session_id}")
    unresolved = [line.id for line in recon.lines if line.status not in {"matched", "ignored"}]
    if unresolved:
        raise ReconciliationError("Cannot close reconciliation with unresolved lines", data={"line_ids": unresolved})

    raw_balance = account_balance_as_of(session, account_id=recon.account_id, as_of=recon.statement_end)
    expected = recon.statement_ending_balance_cents
    actual = display_balance(recon.account, raw_balance)
    if actual != expected:
        raise ReconciliationError(
            "Ledger balance does not match statement ending balance",
            data={"expected_cents": expected, "actual_cents": actual},
        )

    recon.status = "closed"
    recon.closed_at = utcnow()
    session.flush()
    return {"session_id": recon.id, "status": recon.status}


def list_reconciliation_sessions(session: Session) -> list[ReconciliationSession]:
    return list(session.scalars(select(ReconciliationSession).order_by(ReconciliationSession.id.desc())))


def close_period(
    session: Session,
    *,
    period_start: date,
    period_end: date,
    lock_type: str,
    reason: str | None,
    acknowledge_review_ids: list[int] | None = None,
) -> dict[str, object]:
    if acknowledge_review_ids:
        entries = list(
            session.scalars(
                select(JournalEntry).where(JournalEntry.id.in_(acknowledge_review_ids))
            )
        )
        for entry in entries:
            entry.review_acknowledged_at = utcnow()

    unresolved_reviews = list(
        session.scalars(
            select(JournalEntry).where(
                JournalEntry.entry_date >= period_start,
                JournalEntry.entry_date <= period_end,
                JournalEntry.review_required.is_(True),
                JournalEntry.review_acknowledged_at.is_(None),
            )
        )
    )
    if unresolved_reviews:
        raise ComplianceError(
            "Cannot close period with unresolved tax review warnings",
            data={"entry_ids": [entry.id for entry in unresolved_reviews]},
        )

    active_accounts = list(
        session.scalars(
            select(Account)
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                Account.subtype.in_(FINANCIAL_SUBTYPES),
                JournalEntry.entry_date >= period_start,
                JournalEntry.entry_date <= period_end,
            )
            .distinct()
        )
    )
    missing = []
    for account in active_accounts:
        closed_session = session.scalar(
            select(ReconciliationSession.id).where(
                ReconciliationSession.account_id == account.id,
                ReconciliationSession.status == "closed",
                ReconciliationSession.statement_start <= period_start,
                ReconciliationSession.statement_end >= period_end,
            )
        )
        if not closed_session:
            missing.append(account.code)
    if missing:
        raise ReconciliationError(
            "Cannot close period until all financial accounts are reconciled",
            data={"account_codes": missing},
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
    return {"period_start": period_start, "period_end": period_end, "status": "closed"}


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
