"""add journal entry review and cash-basis flags"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0005_journal_entry_review_flags"
down_revision = "0004_drop_legacy_attachments"
branch_labels = None
depends_on = None


def _journal_entry_columns() -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in inspect(bind).get_columns("journal_entries")}


def upgrade() -> None:
    existing = _journal_entry_columns()
    if "review_required" not in existing:
        op.add_column(
            "journal_entries",
            sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "review_message" not in existing:
        op.add_column("journal_entries", sa.Column("review_message", sa.Text(), nullable=True))
    if "review_acknowledged_at" not in existing:
        op.add_column(
            "journal_entries",
            sa.Column("review_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "cash_basis_included" not in existing:
        op.add_column(
            "journal_entries",
            sa.Column("cash_basis_included", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    existing = _journal_entry_columns()
    if "cash_basis_included" in existing:
        op.drop_column("journal_entries", "cash_basis_included")
    if "review_acknowledged_at" in existing:
        op.drop_column("journal_entries", "review_acknowledged_at")
    if "review_message" in existing:
        op.drop_column("journal_entries", "review_message")
    if "review_required" in existing:
        op.drop_column("journal_entries", "review_required")
