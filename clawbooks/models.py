from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    subtype: Mapped[str] = mapped_column(String(64), default="other")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    journal_lines: Mapped[list["JournalLine"]] = relationship(back_populates="account")


class ImportRun(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    from_date: Mapped[date | None] = mapped_column(Date, default=None)
    to_date: Mapped[date | None] = mapped_column(Date, default=None)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    source_path: Mapped[str | None] = mapped_column(String(500), default=None)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reversal_of_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), default=None)
    import_run_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"), default=None)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    review_message: Mapped[str | None] = mapped_column(Text, default=None)
    review_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cash_basis_included: Mapped[bool] = mapped_column(Boolean, default=False)

    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    memo: Mapped[str | None] = mapped_column(String(500), default=None)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(back_populates="journal_lines")
    source_settlements: Mapped[list["SettlementApplication"]] = relationship(
        back_populates="source_line",
        foreign_keys="SettlementApplication.source_line_id",
    )
    settlement_applications: Mapped[list["SettlementApplication"]] = relationship(
        back_populates="settlement_line",
        foreign_keys="SettlementApplication.settlement_line_id",
    )
    reconciliation_matches: Mapped[list["ReconciliationMatch"]] = relationship(back_populates="journal_line")


class SettlementApplication(Base):
    __tablename__ = "settlement_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_line_id: Mapped[int] = mapped_column(ForeignKey("journal_lines.id"), index=True)
    settlement_line_id: Mapped[int] = mapped_column(ForeignKey("journal_lines.id"), index=True)
    applied_amount_cents: Mapped[int] = mapped_column(Integer)
    applied_date: Mapped[date] = mapped_column(Date, index=True)
    application_type: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    reversal_reason: Mapped[str | None] = mapped_column(Text, default=None)

    source_line: Mapped[JournalLine] = relationship(
        back_populates="source_settlements",
        foreign_keys=[source_line_id],
    )
    settlement_line: Mapped[JournalLine] = relationship(
        back_populates="settlement_applications",
        foreign_keys=[settlement_line_id],
    )


class ExternalEvent(Base):
    __tablename__ = "external_events"
    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_external_events_provider_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)
    import_run_id: Mapped[int | None] = mapped_column(ForeignKey("imports.id"), default=None)
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), default=None)
    refresh_history: Mapped[list["ExternalEventRefreshHistory"]] = relationship(
        back_populates="external_event",
        cascade="all, delete-orphan",
    )


class ExternalEventRefreshHistory(Base):
    __tablename__ = "external_event_refresh_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_event_id: Mapped[int] = mapped_column(ForeignKey("external_events.id"), index=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_json: Mapped[str] = mapped_column(Text)
    refresh_source: Mapped[str] = mapped_column(String(64))
    change_note: Mapped[str | None] = mapped_column(Text, default=None)

    external_event: Mapped[ExternalEvent] = relationship(back_populates="refresh_history")


class ReviewBlocker(Base):
    __tablename__ = "review_blockers"
    __table_args__ = (UniqueConstraint("provider", "external_id", "blocker_type", name="uq_review_blockers_provider_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blocker_type: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    blocker_date: Mapped[date] = mapped_column(Date, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolution_type: Mapped[str | None] = mapped_column(String(64), default=None)
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolution_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), default=None)
    external_event_id: Mapped[int | None] = mapped_column(ForeignKey("external_events.id"), default=None)


class ReconciliationSession(Base):
    __tablename__ = "reconciliation_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    statement_path: Mapped[str | None] = mapped_column(String(500), default=None)
    statement_start: Mapped[date] = mapped_column(Date)
    statement_end: Mapped[date] = mapped_column(Date)
    statement_starting_balance_cents: Mapped[int] = mapped_column(Integer)
    statement_ending_balance_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[Account] = relationship()
    lines: Mapped[list["ReconciliationLine"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    events: Mapped[list["ReconciliationSessionEvent"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )


class ReconciliationLine(Base):
    __tablename__ = "reconciliation_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"), index=True)
    transaction_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    amount_cents: Mapped[int] = mapped_column(Integer)
    external_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(32), default="open")

    session: Mapped[ReconciliationSession] = relationship(back_populates="lines")
    matches: Mapped[list["ReconciliationMatch"]] = relationship(back_populates="reconciliation_line", cascade="all, delete-orphan")


class ReconciliationMatch(Base):
    __tablename__ = "reconciliation_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reconciliation_line_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_lines.id"), index=True)
    journal_line_id: Mapped[int] = mapped_column(ForeignKey("journal_lines.id"), index=True)
    applied_amount_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    reversal_reason: Mapped[str | None] = mapped_column(Text, default=None)

    reconciliation_line: Mapped[ReconciliationLine] = relationship(back_populates="matches")
    journal_line: Mapped[JournalLine] = relationship(back_populates="reconciliation_matches")


class ReconciliationSessionEvent(Base):
    __tablename__ = "reconciliation_session_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    session: Mapped[ReconciliationSession] = relationship(back_populates="events")


class TaxObligation(Base):
    __tablename__ = "tax_obligations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500))
    jurisdiction: Mapped[str] = mapped_column(String(64), index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    period_start: Mapped[date | None] = mapped_column(Date, default=None)
    period_end: Mapped[date | None] = mapped_column(Date, default=None)
    liability_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), default=None)
    amount_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    export_path: Mapped[str | None] = mapped_column(String(500), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    liability_account: Mapped[Account | None] = relationship()


class PeriodLock(Base):
    __tablename__ = "period_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    lock_type: Mapped[str] = mapped_column(String(32), default="month")
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_type: Mapped[str] = mapped_column(String(64), index=True)
    tax_year: Mapped[int] = mapped_column(Integer, index=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    period_start: Mapped[date | None] = mapped_column(Date, default=None)
    period_end: Mapped[date | None] = mapped_column(Date, default=None)
    scope: Mapped[str] = mapped_column(String(32), index=True, default="business")
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_via: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    links: Mapped[list["DocumentLink"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentLink(Base):
    __tablename__ = "document_links"
    __table_args__ = (UniqueConstraint("document_id", "target_type", "target_id", name="uq_document_links"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)

    document: Mapped[Document] = relationship(back_populates="links")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)


class CloseSnapshot(Base):
    __tablename__ = "close_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    snapshot_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    report_generator_version: Mapped[str] = mapped_column(String(64))
    report_basis_json: Mapped[str] = mapped_column(Text)
    report_hashes_json: Mapped[str] = mapped_column(Text)
    canonical_summaries_json: Mapped[str] = mapped_column(Text)
    normalized_reports_json: Mapped[str] = mapped_column(Text)
    heavy_artifact_hashes_json: Mapped[str] = mapped_column(Text)
    heavy_artifact_summaries_json: Mapped[str] = mapped_column(Text)
    open_review_blocker_count: Mapped[int] = mapped_column(Integer, default=0)
    cash_basis_warnings_json: Mapped[str] = mapped_column(Text)
    compliance_profile_json: Mapped[str] = mapped_column(Text)
    advisory_context_json: Mapped[str | None] = mapped_column(Text, default=None)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_ref: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    before_json: Mapped[str | None] = mapped_column(Text, default=None)
    after_json: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
