# clawbooks

`clawbooks` is a local CLI and Textual TUI for bookkeeping and accountant handoff support for a small LLC. It keeps an accrual ledger, supports cash-basis `P&L` through explicit settlement rules, and treats tax/checklist output as advisory support rather than filing authority.

## Agent Guide

For the operator manual with guardrails, settlement/reconciliation workflows, review-blocker handling, and accountant-packet rules, see [AGENT_GUIDE.md](/Users/jamesmcintosh/projects/LLC/AGENT_GUIDE.md).

## Quick Start

```bash
uv run clawbooks --ledger ./demo init --business-name "Example LLC"
uv run clawbooks --ledger ./demo coa show
uv run clawbooks --ledger ./demo expense record \
  --date 2026-04-21 \
  --vendor "AWS" \
  --amount 84.12 \
  --category 5110 \
  --payment-account 1000
uv run clawbooks --ledger ./demo report pnl --period-start 2026-04-01 --period-end 2026-04-30 --json
```

## TUI

Launch the read-heavy Textual interface with:

```bash
uv run clawbooks tui --ledger ./demo
```

If you omit `--ledger`, the app opens a directory picker and only accepts folders containing both `ledger.db` and `config.toml`.

The TUI is intentionally limited:
- it can review reports, status, compliance profile, review blockers, and packet checklist state
- it includes an `Audit` pane for integrity findings, period close-readiness, and close-snapshot drift
- it can add documents and generate export bundles
- bookkeeping, imports, settlement, reconciliation, and blocker resolution stay CLI-first
- if a ledger is not at the current Alembic head, the TUI shows a migration-required screen instead of opening normally

## Integrity and Migration

Check whether a ledger is structurally clean:

```bash
uv run clawbooks --ledger ./demo doctor
```

Inspect close readiness and close-snapshot drift for a specific window:

```bash
uv run clawbooks --ledger ./demo period audit \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

If a ledger was created before Alembic-enforced schema management, migrate it explicitly:

```bash
uv run clawbooks --ledger ./demo migrate
```

Normal commands now require the ledger DB to be at the current Alembic head; schema drift is no longer repaired implicitly on open.

## Key Policies

- `report balance-sheet` is always accrual.
- Cash-basis `P&L` only includes immediate-cash activity or explicitly settled accrual activity.
- Immediate-cash entries cannot also be reused as settlement cash for prior accruals.
- Unsupported or invalid cash-basis cases are excluded and surfaced in warnings.
- Stripe tax ambiguity creates review blockers instead of silent postings.
- Unsupported Stripe currencies and unsupported balance-transaction types create review blockers instead of being dropped.
- Open Stripe blockers can be retried against refreshed Stripe facts without losing prior payload history.
- Reconciliation uses statement-line to journal-line amount applications, not entry-level matching, and mistaken sessions are retired with `reconcile void`.
- Period close accepts continuous coverage from chained closed sessions; it does not require one oversized spanning reconciliation.
- Period close freezes settlement and reconciliation mutations for the closed window until `period reopen` is recorded.
- Exact one-source owner reimbursements auto-link into cash-basis settlement; ambiguous reimbursement clearing stays manual.
- `report owner-equity` is now a compatibility alias to the full `equity-rollforward` report.
- Fixed assets are capitalized to the accrual ledger, with book depreciation posted automatically during `period close`.
- Tax depreciation is operator-entered advisory support only and never posts to GAAP/book P&L.
- Accountant packets are advisory handoff bundles, not filing-ready returns.

## Fixed Assets and Depreciation

Add a computer as a capital asset:

```bash
uv run clawbooks --ledger ./demo asset add \
  --description "Development computer" \
  --vendor Apple \
  --purchase-date 2026-05-03 \
  --placed-in-service-date 2026-05-03 \
  --cost 2400.00 \
  --useful-life-months 36 \
  --payment-account 1000
```

Record a CPA-directed 100% tax deduction without changing book P&L:

```bash
uv run clawbooks --ledger ./demo asset tax set \
  --asset-id 1 \
  --year 2026 \
  --deduction-type section_179 \
  --amount 2400.00 \
  --notes "Operator-entered per CPA direction"
```

Book depreciation is straight-line by month and is posted automatically on `period close` as `Dr 5170 Depreciation Expense`, `Cr 1590 Accumulated Depreciation - Computer Equipment`. Tax depreciation is not posted to the ledger; it appears in advisory reports and accountant packets for preparer review.

Useful reports:

```bash
uv run clawbooks --ledger ./demo report fixed-assets --as-of 2026-12-31
uv run clawbooks --ledger ./demo report book-depreciation --period-start 2026-01-01 --period-end 2026-12-31
uv run clawbooks --ledger ./demo report tax-depreciation --year 2026
uv run clawbooks --ledger ./demo report depreciation-difference --year 2026
```

## Accountant Packet

Add support documents:

```bash
uv run clawbooks --ledger ./demo document add \
  --source-path /path/to/stripe-1099-k.pdf \
  --type stripe_1099_k \
  --year 2026 \
  --jurisdiction illinois
```

Inspect advisory packet status:

```bash
uv run clawbooks --ledger ./demo document checklist --year 2026
```

Export the advisory handoff bundle:

```bash
uv run clawbooks --ledger ./demo export accountant-packet --year 2026
```

The export writes `exports/accountant-packet_YYYY/` plus a sibling `.zip`, including:
- the books export
- copied source documents
- compliance-profile snapshot
- advisory checklist and missing/unknown items
- unsupported cash-basis warning snapshot

For cadence-sensitive tax support:
- estimated-tax confirmations must carry exact filing-period metadata
- sales-tax returns/payments are matched by jurisdiction plus exact filing slot
- sales-tax payment completeness stays `unknown` until explicit slot expectations are recorded with `compliance sales-tax-slot ...`
