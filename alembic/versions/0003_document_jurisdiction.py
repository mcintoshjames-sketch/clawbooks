"""add jurisdiction metadata to documents"""

from alembic import op
import sqlalchemy as sa


revision = "0003_document_jurisdiction"
down_revision = "0002_integrity_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("jurisdiction", sa.String(length=64), nullable=True))
    op.create_index("ix_documents_jurisdiction", "documents", ["jurisdiction"])


def downgrade() -> None:
    op.drop_index("ix_documents_jurisdiction", table_name="documents")
    op.drop_column("documents", "jurisdiction")
