"""initial schema"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subtype", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_accounts_code", "accounts", ["code"])
    op.create_index("ix_accounts_kind", "accounts", ["kind"])

    op.create_table(
        "imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("from_date", sa.Date()),
        sa.Column("to_date", sa.Date()),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("source_path", sa.String(length=500)),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
    )

    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversal_of_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
        sa.Column("import_run_id", sa.Integer(), sa.ForeignKey("imports.id")),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("review_message", sa.Text()),
        sa.Column("review_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("cash_basis_included", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_journal_entries_entry_date", "journal_entries", ["entry_date"])
    op.create_index("ix_journal_entries_source_type", "journal_entries", ["source_type"])

    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("memo", sa.String(length=500)),
    )
    op.create_index("ix_journal_lines_entry_id", "journal_lines", ["entry_id"])
    op.create_index("ix_journal_lines_account_id", "journal_lines", ["account_id"])

    op.create_table(
        "external_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("import_run_id", sa.Integer(), sa.ForeignKey("imports.id")),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
        sa.UniqueConstraint("provider", "external_id", name="uq_external_events_provider_id"),
    )
    op.create_index("ix_external_events_provider", "external_events", ["provider"])

    op.create_table(
        "reconciliation_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("statement_path", sa.String(length=500)),
        sa.Column("statement_start", sa.Date(), nullable=False),
        sa.Column("statement_end", sa.Date(), nullable=False),
        sa.Column("statement_ending_balance_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_reconciliation_sessions_account_id", "reconciliation_sessions", ["account_id"])

    op.create_table(
        "reconciliation_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("reconciliation_sessions.id"), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("external_ref", sa.String(length=255)),
        sa.Column("matched_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
        sa.Column("status", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_reconciliation_lines_session_id", "reconciliation_lines", ["session_id"])

    op.create_table(
        "tax_obligations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("jurisdiction", sa.String(length=64), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.Date()),
        sa.Column("period_end", sa.Date()),
        sa.Column("liability_account_id", sa.Integer(), sa.ForeignKey("accounts.id")),
        sa.Column("amount_cents", sa.Integer()),
        sa.Column("export_path", sa.String(length=500)),
        sa.Column("notes", sa.Text()),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_tax_obligations_code", "tax_obligations", ["code"])
    op.create_index("ix_tax_obligations_due_date", "tax_obligations", ["due_date"])
    op.create_index("ix_tax_obligations_jurisdiction", "tax_obligations", ["jurisdiction"])

    op.create_table(
        "period_locks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("lock_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_period_locks_period_start", "period_locks", ["period_start"])
    op.create_index("ix_period_locks_period_end", "period_locks", ["period_end"])

    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("attachments")
    op.drop_index("ix_period_locks_period_end", table_name="period_locks")
    op.drop_index("ix_period_locks_period_start", table_name="period_locks")
    op.drop_table("period_locks")
    op.drop_index("ix_tax_obligations_jurisdiction", table_name="tax_obligations")
    op.drop_index("ix_tax_obligations_due_date", table_name="tax_obligations")
    op.drop_index("ix_tax_obligations_code", table_name="tax_obligations")
    op.drop_table("tax_obligations")
    op.drop_index("ix_reconciliation_lines_session_id", table_name="reconciliation_lines")
    op.drop_table("reconciliation_lines")
    op.drop_index("ix_reconciliation_sessions_account_id", table_name="reconciliation_sessions")
    op.drop_table("reconciliation_sessions")
    op.drop_index("ix_external_events_provider", table_name="external_events")
    op.drop_table("external_events")
    op.drop_index("ix_journal_lines_account_id", table_name="journal_lines")
    op.drop_index("ix_journal_lines_entry_id", table_name="journal_lines")
    op.drop_table("journal_lines")
    op.drop_index("ix_journal_entries_source_type", table_name="journal_entries")
    op.drop_index("ix_journal_entries_entry_date", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_table("imports")
    op.drop_index("ix_accounts_kind", table_name="accounts")
    op.drop_index("ix_accounts_code", table_name="accounts")
    op.drop_table("accounts")
