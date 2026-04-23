from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResultEnvelope(BaseModel):
    ok: bool
    command: str
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    business_name: str = "My LLC"
    entity_name: str = "My LLC"
    home_state: str = "IL"
    timezone: str = "America/Chicago"
    base_currency: str = "USD"
    default_report_basis: Literal["cash", "accrual"] = "cash"
    stripe_tax_mode: Literal["handled_by_stripe_tax", "manual_review_required"] = "handled_by_stripe_tax"
    stripe_api_key: str | None = None


class CSVRule(BaseModel):
    match: str
    account_code: str
    entry_kind: Literal["expense", "income"]


class CSVImportProfile(BaseModel):
    date_column: str = "date"
    description_column: str = "description"
    amount_column: str = "amount"
    external_ref_column: str | None = None
    rules: list[CSVRule] = Field(default_factory=list)


class StripeEvent(BaseModel):
    external_id: str
    event_type: Literal["charge", "fee", "refund", "dispute", "payout"]
    occurred_at: datetime
    amount_cents: int
    fee_cents: int = 0
    tax_cents: int | None = None
    net_cents: int | None = None
    currency: str = "USD"
    description: str = ""
    invoice_id: str | None = None
    charge_id: str | None = None
    payout_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StripeUnsupportedEvent(BaseModel):
    external_id: str
    occurred_at: datetime
    raw_type: str
    currency: str
    description: str = ""
    reason: str
    source_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class StripeFetchResult(BaseModel):
    supported_events: list[StripeEvent] = Field(default_factory=list)
    unsupported_events: list[StripeUnsupportedEvent] = Field(default_factory=list)


class TaxTemplate(BaseModel):
    code: str
    description: str
    jurisdiction: str
    due_date: date
    liability_account_code: str | None = None
    notes: str | None = None


class ExportManifest(BaseModel):
    name: str
    generated_at: datetime
    files: list[str]
    period_start: date
    period_end: date
    ledger_path: Path


class SalesTaxRegistration(BaseModel):
    jurisdiction: str
    filing_cadence: str
    active: bool = True


class SalesTaxPaymentSlot(BaseModel):
    jurisdiction: str
    period_start: date
    period_end: date
    filing_due_date: date
    payment_expected: Literal["true", "false", "unknown"] = "unknown"
    source: str | None = None
    reason: str | None = None


class PayrollProfile(BaseModel):
    confirmed: bool = False
    enabled: bool | None = None
    provider: str | None = None
    states: list[str] = Field(default_factory=list)


class ContractorProfile(BaseModel):
    confirmed: bool = False
    requires_1099_nec_documents: bool | None = None
    handled_by: str | None = None


class OwnerTrackingProfile(BaseModel):
    estimated_tax_confirmations: bool = False


class ComplianceProfile(BaseModel):
    entity_tax_classification: str = "single_member_llc_disregarded"
    sales_tax_profile_confirmed: bool = False
    sales_tax_registrations: list[SalesTaxRegistration] = Field(default_factory=list)
    sales_tax_payment_slots: list[SalesTaxPaymentSlot] = Field(default_factory=list)
    payroll: PayrollProfile = Field(default_factory=PayrollProfile)
    contractor_profile: ContractorProfile = Field(default_factory=ContractorProfile)
    owner_tracking: OwnerTrackingProfile = Field(default_factory=OwnerTrackingProfile)
