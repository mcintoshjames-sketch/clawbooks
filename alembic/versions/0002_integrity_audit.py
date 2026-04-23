"""integrity and close audit tables"""

from alembic import op
import sqlalchemy as sa


revision = "0002_integrity_audit"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "close_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_schema_version", sa.Integer(), nullable=False),
        sa.Column("report_generator_version", sa.String(length=64), nullable=False),
        sa.Column("report_basis_json", sa.Text(), nullable=False),
        sa.Column("report_hashes_json", sa.Text(), nullable=False),
        sa.Column("canonical_summaries_json", sa.Text(), nullable=False),
        sa.Column("normalized_reports_json", sa.Text(), nullable=False),
        sa.Column("heavy_artifact_hashes_json", sa.Text(), nullable=False),
        sa.Column("heavy_artifact_summaries_json", sa.Text(), nullable=False),
        sa.Column("open_review_blocker_count", sa.Integer(), nullable=False),
        sa.Column("cash_basis_warnings_json", sa.Text(), nullable=False),
        sa.Column("compliance_profile_json", sa.Text(), nullable=False),
        sa.Column("advisory_context_json", sa.Text()),
    )
    op.create_index("ix_close_snapshots_period_end", "close_snapshots", ["period_end"])
    op.create_index("ix_close_snapshots_period_start", "close_snapshots", ["period_start"])
    op.create_index("ix_close_snapshots_snapshot_at", "close_snapshots", ["snapshot_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_ref", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("before_json", sa.Text()),
        sa.Column("after_json", sa.Text()),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_entity_ref", "audit_events", ["entity_ref"])
    op.create_index("ix_audit_events_entity_type", "audit_events", ["entity_type"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_entity_type", table_name="audit_events")
    op.drop_index("ix_audit_events_entity_ref", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_close_snapshots_snapshot_at", table_name="close_snapshots")
    op.drop_index("ix_close_snapshots_period_start", table_name="close_snapshots")
    op.drop_index("ix_close_snapshots_period_end", table_name="close_snapshots")
    op.drop_table("close_snapshots")
