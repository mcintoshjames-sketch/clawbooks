"""drop obsolete legacy attachments table"""

from alembic import op


revision = "0004_drop_legacy_attachments"
down_revision = "0003_document_jurisdiction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("attachments")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE attachments (
            id INTEGER NOT NULL,
            path VARCHAR(500) NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            description VARCHAR(500),
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )
