"""add fixed asset register and depreciation support"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0006_fixed_assets"
down_revision = "0005_journal_entry_review_flags"
branch_labels = None
depends_on = None


DEFAULT_ASSET_ACCOUNTS = (
    ("1500", "Computer Equipment", "asset", "fixed_asset"),
    ("1590", "Accumulated Depreciation - Computer Equipment", "asset", "accumulated_depreciation"),
    ("5170", "Depreciation Expense", "expense", "expense"),
)


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing_tables = _tables()
    if "fixed_assets" not in existing_tables:
        op.create_table(
            "fixed_assets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=False),
            sa.Column("vendor", sa.String(length=255), nullable=True),
            sa.Column("purchase_date", sa.Date(), nullable=False),
            sa.Column("placed_in_service_date", sa.Date(), nullable=False),
            sa.Column("cost_cents", sa.Integer(), nullable=False),
            sa.Column("asset_account_id", sa.Integer(), nullable=False),
            sa.Column("accumulated_depreciation_account_id", sa.Integer(), nullable=False),
            sa.Column("depreciation_expense_account_id", sa.Integer(), nullable=False),
            sa.Column("useful_life_months", sa.Integer(), nullable=False),
            sa.Column("salvage_value_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("business_use_percent", sa.Integer(), nullable=False, server_default="10000"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("source_journal_entry_id", sa.Integer(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["accumulated_depreciation_account_id"], ["accounts.id"]),
            sa.ForeignKeyConstraint(["asset_account_id"], ["accounts.id"]),
            sa.ForeignKeyConstraint(["depreciation_expense_account_id"], ["accounts.id"]),
            sa.ForeignKeyConstraint(["source_journal_entry_id"], ["journal_entries.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fixed_assets_purchase_date", "fixed_assets", ["purchase_date"])
        op.create_index("ix_fixed_assets_placed_in_service_date", "fixed_assets", ["placed_in_service_date"])
        op.create_index("ix_fixed_assets_source_journal_entry_id", "fixed_assets", ["source_journal_entry_id"])
        op.create_index("ix_fixed_assets_status", "fixed_assets", ["status"])

    if "asset_book_depreciation_postings" not in existing_tables:
        op.create_table(
            "asset_book_depreciation_postings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("period_start", sa.Date(), nullable=False),
            sa.Column("period_end", sa.Date(), nullable=False),
            sa.Column("journal_entry_id", sa.Integer(), nullable=False),
            sa.Column("amount_cents", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["fixed_assets.id"]),
            sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entries.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("asset_id", "period_start", "period_end", name="uq_asset_book_depr_period"),
        )
        op.create_index("ix_asset_book_depreciation_postings_asset_id", "asset_book_depreciation_postings", ["asset_id"])
        op.create_index("ix_asset_book_depreciation_postings_journal_entry_id", "asset_book_depreciation_postings", ["journal_entry_id"])
        op.create_index("ix_asset_book_depreciation_postings_period_end", "asset_book_depreciation_postings", ["period_end"])
        op.create_index("ix_asset_book_depreciation_postings_period_start", "asset_book_depreciation_postings", ["period_start"])

    if "asset_tax_depreciation" not in existing_tables:
        op.create_table(
            "asset_tax_depreciation",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("asset_id", sa.Integer(), nullable=False),
            sa.Column("tax_year", sa.Integer(), nullable=False),
            sa.Column("deduction_type", sa.String(length=32), nullable=False),
            sa.Column("amount_cents", sa.Integer(), nullable=False),
            sa.Column("business_use_percent", sa.Integer(), nullable=False, server_default="10000"),
            sa.Column("tax_basis_before_cents", sa.Integer(), nullable=False),
            sa.Column("tax_basis_after_cents", sa.Integer(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["fixed_assets.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("asset_id", "tax_year", "deduction_type", name="uq_asset_tax_depr_type_year"),
        )
        op.create_index("ix_asset_tax_depreciation_asset_id", "asset_tax_depreciation", ["asset_id"])
        op.create_index("ix_asset_tax_depreciation_tax_year", "asset_tax_depreciation", ["tax_year"])

    for code, name, kind, subtype in DEFAULT_ASSET_ACCOUNTS:
        op.execute(
            sa.text(
                """
                INSERT INTO accounts (code, name, kind, subtype, currency, is_active, created_at)
                SELECT :code, :name, :kind, :subtype, 'USD', 1, CURRENT_TIMESTAMP
                WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE code = :code)
                """
            ).bindparams(code=code, name=name, kind=kind, subtype=subtype)
        )


def downgrade() -> None:
    existing_tables = _tables()
    if "asset_tax_depreciation" in existing_tables:
        op.drop_index("ix_asset_tax_depreciation_tax_year", table_name="asset_tax_depreciation")
        op.drop_index("ix_asset_tax_depreciation_asset_id", table_name="asset_tax_depreciation")
        op.drop_table("asset_tax_depreciation")
    if "asset_book_depreciation_postings" in existing_tables:
        op.drop_index("ix_asset_book_depreciation_postings_period_start", table_name="asset_book_depreciation_postings")
        op.drop_index("ix_asset_book_depreciation_postings_period_end", table_name="asset_book_depreciation_postings")
        op.drop_index("ix_asset_book_depreciation_postings_journal_entry_id", table_name="asset_book_depreciation_postings")
        op.drop_index("ix_asset_book_depreciation_postings_asset_id", table_name="asset_book_depreciation_postings")
        op.drop_table("asset_book_depreciation_postings")
    if "fixed_assets" in existing_tables:
        op.drop_index("ix_fixed_assets_status", table_name="fixed_assets")
        op.drop_index("ix_fixed_assets_source_journal_entry_id", table_name="fixed_assets")
        op.drop_index("ix_fixed_assets_placed_in_service_date", table_name="fixed_assets")
        op.drop_index("ix_fixed_assets_purchase_date", table_name="fixed_assets")
        op.drop_table("fixed_assets")
