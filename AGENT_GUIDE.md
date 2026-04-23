# OpenClaw Agent Guide for `clawbooks`

This guide defines how an AI operator should use `clawbooks` safely.

## Mission

Maintain clean books, explicit reconciliation evidence, explicit cash-basis settlement support, and an advisory accountant packet for a single-member LLC with Stripe subscription revenue.

Do not treat `clawbooks` as:
- a tax-law engine
- a filing engine
- a payroll engine
- a nexus determination tool

## Non-Negotiable Rules

1. Use the CLI only for bookkeeping actions. Do not edit `ledger.db` directly.
2. Prefer `--json` for every agent-facing command.
3. Use `--dry-run` on imports and risky writes first.
4. Never mutate history in place. Use reversal commands, compensating entries, settlement reversal, or reconciliation reopen flows.
5. Never guess tax effect for ambiguous Stripe events.
6. Never close a period with open review blockers.
7. Never assume checklist `unknown` means not applicable.
8. Never map a bank-side Stripe payout to `4000`. Stripe payouts should clear `1010`, not create revenue.
9. Balance sheet is accrual-only. Do not describe it as cash-basis.
10. If a ledger is not at the current schema head, stop normal commands and run `migrate` first.

## Core Commands

Always use:

```bash
uv run clawbooks --ledger /absolute/path/to/ledger --json <command> ...
```

Exit codes:
- `0`: success
- `2`: validation/input error
- `3`: reconciliation error
- `4`: locked period
- `5`: import conflict
- `6`: compliance prerequisite missing, including open review blockers

## Startup

```bash
uv run clawbooks --ledger /ledger --json init --business-name "Example LLC"
```

If importing Stripe:

```bash
export CLAWBOOKS_STRIPE_API_KEY=sk_live_...
```

Default assumptions:
- `USD`
- timezone `America/Chicago`
- Stripe subscriptions are the only revenue stream in v1
- checklist and tax outputs are advisory

Integrity check after upgrades:

```bash
uv run clawbooks --ledger /ledger --json doctor
```

If the result says migration is required:

```bash
uv run clawbooks --ledger /ledger --json migrate
```

## Core Accounts

- `1000`: Business Checking
- `1010`: Stripe Clearing
- `1100`: Accounts Receivable
- `2000`: Credit Card Payable
- `2100`: Sales Tax Payable
- `2200`: Payroll Tax Payable
- `2300`: Reimbursement Payable
- `3000`: Owner Contributions
- `3100`: Owner Draws
- `4000`: Subscription Revenue
- `4010`: Refunds and Discounts
- `5000`: Stripe Fees
- `5110`: Hosting
- `5120`: Professional Fees
- `5140`: Taxes and Licenses
- `5150`: Contractors
- `5160`: Chargebacks
- `5199`: Uncategorized Expense

## Manual Expenses

Business-paid:

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "AWS" \
  --amount 84.12 \
  --category 5110 \
  --payment-account 1000
```

Owner-paid, non-reimbursable:

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "CPA LLC" \
  --amount 125.00 \
  --category 5120 \
  --paid-personally
```

Owner-paid, reimbursable later:

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "State Filing Fee" \
  --amount 150.00 \
  --category 5140 \
  --paid-personally \
  --reimbursement
```

Rules:
- always choose an explicit category
- use `--receipt-path` when source evidence exists
- use `3000` only for owner-paid, non-reimbursable expense flow
- use `2300` only when reimbursement is expected later
- an owner reimbursement only auto-links into cash-basis settlement when there is exactly one eligible open reimbursable source line and the reimbursement amount matches it exactly
- otherwise reimbursement clearing stays manual and cash-basis `P&L` continues to warn until explicit settlement is added

## Stripe Import

Preview:

```bash
uv run clawbooks --ledger /ledger --json import stripe \
  --from-date 2026-04-01 \
  --to-date 2026-04-30 \
  --dry-run
```

Commit:

```bash
uv run clawbooks --ledger /ledger --json import stripe \
  --from-date 2026-04-01 \
  --to-date 2026-04-30
```

Posting policy:
- taxable charge: debit `1010`, credit `4000`, credit `2100` for collected tax
- Stripe fee: debit `5000`, credit `1010`
- payout: debit `1000`, credit `1010`
- ambiguous refund/dispute/tax cases: do not post; create a review blocker instead
- unsupported Stripe currencies or unsupported balance-transaction types: do not post; create a review blocker instead

Blocked event workflow:

```bash
uv run clawbooks --ledger /ledger --json review list --status open
uv run clawbooks --ledger /ledger --json review resolve \
  --blocker-id 12 \
  --resolution-type skip \
  --note "Handled offline"
```

Override posting is allowed only with explicit human review:

```bash
uv run clawbooks --ledger /ledger --json review resolve \
  --blocker-id 12 \
  --resolution-type post_with_override \
  --override-tax-cents 0
```

## CSV Import

Profile example:

```json
{
  "date_column": "date",
  "description_column": "description",
  "amount_column": "amount",
  "external_ref_column": "external_ref",
  "rules": [
    {"match": "AWS", "account_code": "5110", "entry_kind": "expense"}
  ]
}
```

Preview:

```bash
uv run clawbooks --ledger /ledger --json import csv \
  --account-code 1000 \
  --csv-path /path/to/statement.csv \
  --profile-path /path/to/profile.json \
  --statement-starting-balance 1000.00 \
  --statement-ending-balance 915.88 \
  --dry-run
```

Commit:

```bash
uv run clawbooks --ledger /ledger --json import csv \
  --account-code 1000 \
  --csv-path /path/to/statement.csv \
  --profile-path /path/to/profile.json \
  --statement-starting-balance 1000.00 \
  --statement-ending-balance 915.88
```

Rules:
- dedupe is transaction-based, not file-path based
- unmatched rows stay unresolved; do not guess
- if you want reconciliation created during import, provide both starting and ending balances

## Settlement Workflow

Cash-basis `P&L` only includes:
- immediate-cash activity posted directly against `bank`, `stripe_clearing`, or `card`
- owner-paid non-reimbursable expenses using `3000`
- accrual activity that is explicitly settled

When a manual accrual entry exists, settle it explicitly:

```bash
uv run clawbooks --ledger /ledger --json settlement apply \
  --source-line-id 10 \
  --settlement-line-id 42 \
  --amount 100.00
```

Inspect current applications:

```bash
uv run clawbooks --ledger /ledger --json settlement list
```

Reverse a bad settlement:

```bash
uv run clawbooks --ledger /ledger --json settlement reverse \
  --settlement-application-id 7 \
  --reason "Wrong source line"
```

Rules:
- `source_line_id` must be revenue, expense, or contra-revenue
- `settlement_line_id` must be a supported cash-equivalent or `3000`
- `settlement_line_id` cannot come from an entry that is already immediate-cash `P&L`
- never over-apply either side
- do not add or reverse settlement applications inside a closed period; reopen the period first
- do not reverse a settled journal entry until the settlement application is reversed

## Reconciliation Workflow

Start:

```bash
uv run clawbooks --ledger /ledger --json reconcile start \
  --account-code 1000 \
  --statement-path /path/to/statement.csv \
  --statement-start 2026-04-01 \
  --statement-end 2026-04-30 \
  --statement-starting-balance 1000.00 \
  --statement-ending-balance 915.88
```

Discover candidate journal lines:

```bash
uv run clawbooks --ledger /ledger --json reconcile candidates --session-id 7
```

Apply matches by amount:

```bash
uv run clawbooks --ledger /ledger --json reconcile match \
  --session-id 7 \
  --line-id 12 \
  --journal-line-id 44 \
  --amount 60.00
```

Close:

```bash
uv run clawbooks --ledger /ledger --json reconcile close --session-id 7
```

Reopen:

```bash
uv run clawbooks --ledger /ledger --json reconcile reopen \
  --session-id 7 \
  --reason "Need to rematch batched deposit"
```

Void a mistaken open session instead of replacing it in place:

```bash
uv run clawbooks --ledger /ledger --json reconcile void \
  --session-id 7 \
  --reason "Wrong statement file"
```

Rules:
- matching is line-level, not entry-level
- partial many-to-one and one-to-many matching is allowed
- closed sessions are immutable until reopened
- overlapping non-voided sessions are rejected for the same account
- once a period is closed, reconciliation start/reopen/void/rematch actions for that statement window are blocked until period reopen
- candidate discovery may show blocked journal lines with rejection reasons; do not force those through manually
- outstanding items carry forward through candidate discovery, not fake copied statement rows

## Close Audit

Audit close readiness and close-snapshot drift explicitly:

```bash
uv run clawbooks --ledger /ledger --json period audit \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

Interpretation:
- `closable_now` reflects enforced close prerequisites
- `blocking_findings` explain what currently blocks close
- `snapshot_drift.accounting_data_drift` means closed-period books changed
- `snapshot_drift.admin_state_drift` means compliance profile or document metadata changed
- `snapshot_drift.advisory_context_drift` only applies to full calendar-year closes
- if a period was reopened after close, snapshot drift is historical context and `historical_only` will be true

## Reporting

Cash-basis `P&L`:

```bash
uv run clawbooks --ledger /ledger --json report pnl \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

Accrual `P&L`:

```bash
uv run clawbooks --ledger /ledger --json report pnl \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --basis accrual
```

Balance sheet:

```bash
uv run clawbooks --ledger /ledger --json report balance-sheet --as-of 2026-04-30
```

General ledger with line ids:

```bash
uv run clawbooks --ledger /ledger --json report general-ledger \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --include-line-ids
```

Interpretation rules:
- if cash-basis `P&L` returns `excluded_lines`, those items were intentionally left out until settlement exists
- if cash-basis `P&L` returns `ignored_invalid_settlement_applications`, the ledger contained legacy invalid settlement structure and the report suppressed it to avoid double counting
- do not “fix” excluded cash-basis lines by guessing

## Period Close

Close only after:
1. imports are complete
2. financial accounts for the period are reconciled with continuous session coverage across the close window
3. open review blockers are resolved

```bash
uv run clawbooks --ledger /ledger --json period close \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

Reopen if a justified correction is required:

```bash
uv run clawbooks --ledger /ledger --json period reopen \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --reason "Late adjustment"
```

Do not treat advisory checklist `unknown` items as close blockers by themselves. Only explicit review blockers should block close.

## Review Retry

Open Stripe blockers can be retried against refreshed upstream facts:

```bash
uv run clawbooks --ledger /ledger --json review retry \
  --blocker-id 12
```

Rules:
- retry refreshes the current Stripe facts by external id; it is not limited to the first-seen payload
- refresh history is preserved for audit trail
- resolved `skip`, `post_with_override`, and `superseded_by_manual_entry` outcomes remain authoritative on rerun

## Compliance Profile

Checklist applicability depends on the compliance profile, not generic account activity.

Show:

```bash
uv run clawbooks --ledger /ledger --json compliance profile show
```

Update from a JSON file:

```bash
uv run clawbooks --ledger /ledger --json compliance profile update --from-file /path/to/profile.json
```

Example profile:

```json
{
  "entity_tax_classification": "single_member_llc_disregarded",
  "sales_tax_profile_confirmed": true,
  "sales_tax_registrations": [
    {"jurisdiction": "illinois", "filing_cadence": "monthly", "active": true}
  ],
  "sales_tax_payment_slots": [
    {
      "jurisdiction": "illinois",
      "period_start": "2026-01-01",
      "period_end": "2026-01-31",
      "filing_due_date": "2026-02-20",
      "payment_expected": "true",
      "source": "idor_notice",
      "reason": "January filing requires remittance"
    }
  ],
  "payroll": {"confirmed": true, "enabled": false, "provider": null, "states": []},
  "contractor_profile": {"confirmed": false, "requires_1099_nec_documents": null, "handled_by": null},
  "owner_tracking": {"estimated_tax_confirmations": true}
}
```

Rules:
- if facts are not explicit, checklist items should remain `unknown`
- do not infer contractor or sales-tax filing duties from raw ledger activity alone
- do not infer sales-tax payment completeness from cadence or `2100`; use explicit filing-slot payment expectations

## Accountant Packet

Add support docs:

```bash
uv run clawbooks --ledger /ledger --json document add \
  --source-path /path/to/stripe-1099-k.pdf \
  --type stripe_1099_k \
  --year 2026 \
  --jurisdiction illinois
```

Inspect checklist:

```bash
uv run clawbooks --ledger /ledger --json document checklist --year 2026
```

Export advisory handoff packet:

```bash
uv run clawbooks --ledger /ledger --json export accountant-packet --year 2026
```

Packet rules:
- the packet is advisory, not filing-ready
- it includes compliance-profile snapshot and unsupported cash-basis warnings
- `year_end_books_package` is only `present` after the year-end export exists
- legacy attachments not registered as documents are not included automatically
- cadence-sensitive checklist items require exact period metadata and, for sales-tax items, matching jurisdiction metadata

## Escalate to a Human or CPA

Escalate when:
- Stripe tax effect is ambiguous
- settlement relationship is unclear
- vendor classification is ambiguous
- a contractor or payroll checklist item is `unknown`
- compliance profile facts are missing
- a review blocker would be resolved with override posting
