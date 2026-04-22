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
    cash_basis_included: Mapped[bool] = mapped_column(Boolean, default=True)

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


class ReconciliationSession(Base):
    __tablename__ = "reconciliation_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    statement_path: Mapped[str | None] = mapped_column(String(500), default=None)
    statement_start: Mapped[date] = mapped_column(Date)
    statement_end: Mapped[date] = mapped_column(Date)
    statement_ending_balance_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[Account] = relationship()
    lines: Mapped[list["ReconciliationLine"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class ReconciliationLine(Base):
    __tablename__ = "reconciliation_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_sessions.id"), index=True)
    transaction_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    amount_cents: Mapped[int] = mapped_column(Integer)
    external_ref: Mapped[str | None] = mapped_column(String(255), default=None)
    matched_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), default=None)
    status: Mapped[str] = mapped_column(String(32), default="draft")

    session: Mapped[ReconciliationSession] = relationship(back_populates="lines")


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


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
