from __future__ import annotations

from datetime import date

from clawbooks.schemas import TaxTemplate


DEFAULT_ACCOUNTS: list[dict[str, str]] = [
    {"code": "1000", "name": "Business Checking", "kind": "asset", "subtype": "bank"},
    {"code": "1010", "name": "Stripe Clearing", "kind": "asset", "subtype": "stripe_clearing"},
    {"code": "1100", "name": "Accounts Receivable", "kind": "asset", "subtype": "receivable"},
    {"code": "2000", "name": "Credit Card Payable", "kind": "liability", "subtype": "card"},
    {"code": "2100", "name": "Sales Tax Payable", "kind": "liability", "subtype": "tax_liability"},
    {"code": "2200", "name": "Payroll Tax Payable", "kind": "liability", "subtype": "tax_liability"},
    {"code": "2300", "name": "Reimbursement Payable", "kind": "liability", "subtype": "reimbursement"},
    {"code": "3000", "name": "Owner Contributions", "kind": "equity", "subtype": "equity"},
    {"code": "3100", "name": "Owner Draws", "kind": "equity", "subtype": "equity"},
    {"code": "4000", "name": "Subscription Revenue", "kind": "revenue", "subtype": "operating"},
    {"code": "4010", "name": "Refunds and Discounts", "kind": "contra_revenue", "subtype": "contra_revenue"},
    {"code": "4020", "name": "Miscellaneous Income", "kind": "revenue", "subtype": "other_income"},
    {"code": "5000", "name": "Stripe Fees", "kind": "expense", "subtype": "expense"},
    {"code": "5100", "name": "Software and SaaS", "kind": "expense", "subtype": "expense"},
    {"code": "5110", "name": "Hosting", "kind": "expense", "subtype": "expense"},
    {"code": "5120", "name": "Professional Fees", "kind": "expense", "subtype": "expense"},
    {"code": "5130", "name": "Bank Fees", "kind": "expense", "subtype": "expense"},
    {"code": "5140", "name": "Taxes and Licenses", "kind": "expense", "subtype": "expense"},
    {"code": "5150", "name": "Contractors", "kind": "expense", "subtype": "expense"},
    {"code": "5160", "name": "Chargebacks", "kind": "expense", "subtype": "expense"},
    {"code": "5199", "name": "Uncategorized Expense", "kind": "expense", "subtype": "expense"},
]


def default_tax_templates(year: int) -> list[TaxTemplate]:
    return [
        TaxTemplate(
            code=f"fed-est-q1-{year}",
            description=f"Federal estimated tax reminder Q1 {year}",
            jurisdiction="federal",
            due_date=date(year, 4, 15),
            notes="Reminder only. Confirm estimate and filing with your CPA.",
        ),
        TaxTemplate(
            code=f"fed-est-q2-{year}",
            description=f"Federal estimated tax reminder Q2 {year}",
            jurisdiction="federal",
            due_date=date(year, 6, 15),
            notes="Reminder only. Confirm estimate and filing with your CPA.",
        ),
        TaxTemplate(
            code=f"fed-est-q3-{year}",
            description=f"Federal estimated tax reminder Q3 {year}",
            jurisdiction="federal",
            due_date=date(year, 9, 15),
            notes="Reminder only. Confirm estimate and filing with your CPA.",
        ),
        TaxTemplate(
            code=f"fed-est-q4-{year}",
            description=f"Federal estimated tax reminder Q4 {year}",
            jurisdiction="federal",
            due_date=date(year + 1, 1, 15),
            notes="Reminder only. Confirm estimate and filing with your CPA.",
        ),
        TaxTemplate(
            code=f"il-sales-tax-{year}",
            description=f"Illinois sales tax review {year}",
            jurisdiction="illinois",
            due_date=date(year, 12, 20),
            liability_account_code="2100",
            notes="Default reminder. Confirm actual filing cadence and form requirements with IDOR.",
        ),
        TaxTemplate(
            code=f"il-registration-review-{year}",
            description=f"Illinois registration and employer review {year}",
            jurisdiction="illinois",
            due_date=date(year, 1, 31),
            notes="Review account registrations, withholding, and unemployment requirements before business changes.",
        ),
    ]
