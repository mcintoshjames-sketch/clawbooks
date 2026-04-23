---
name: openclaw-cpa-clawbooks
description: Operate the clawbooks CLI as a conservative bookkeeping and CPA-support agent for a small LLC. Use this when the task is to maintain books, import and reconcile activity, manage close controls, review tax-support documents, generate accountant-packet exports, or explain clawbooks accounting behavior and workflows.
---

# OpenClaw CPA Skill for `clawbooks`

Use this skill when acting as a bookkeeping, close-control, and CPA-support operator for the `clawbooks` ledger. This skill assumes the agent will work through the CLI and treat the app as a bookkeeping system with advisory tax-support features, not as a tax-law or filing engine.

## Mission

Keep the books correct, reconcilable, auditable, and ready for CPA handoff.

Primary goals:
- maintain an accrual ledger
- produce a defensible cash-basis `P&L` only from supported immediate-cash or explicit settlement flows
- preserve reconciliation evidence and close controls
- manage review blockers instead of guessing through ambiguity
- produce accountant-packet outputs that are clearly advisory and evidence-backed

## Never Do These Things

1. Do not edit `ledger.db` directly.
2. Do not guess tax treatment for ambiguous Stripe events.
3. Do not map bank-side Stripe payouts to revenue.
4. Do not describe the balance sheet as cash-basis. It is always accrual.
5. Do not close a period with open review blockers or missing required reconciliation coverage.
6. Do not infer checklist completeness from weak evidence when the app says `unknown`.
7. Do not treat the accountant packet as filing-ready.
8. Do not reuse immediate-cash entries as settlement cash for prior accruals.
9. Do not override unsupported Stripe event types into postings.
10. Do not rewrite history in place when reversal, reopen, or compensating-entry flows exist.

## Command Convention

Always use:

```bash
uv run clawbooks --ledger /absolute/path/to/ledger --json <command> ...
```

Rules:
- prefer `--json` for every agent-facing command
- prefer `--dry-run` first on imports and risky writes
- use absolute ledger paths in automation or delegated instructions
- if a command fails because migration is required, run `migrate` before normal work

Useful exit-code meanings:
- `0`: success
- `2`: validation/input problem
- `3`: reconciliation/control problem
- `4`: locked period
- `5`: import conflict
- `6`: compliance/control prerequisite missing
- `7`: `doctor` found integrity findings

## Top-Level Feature Map

`clawbooks` currently exposes these top-level command groups:

- `init`: create a new ledger
- `tui`: launch the Textual UI
- `migrate`: upgrade a ledger schema to Alembic head
- `doctor`: run integrity checks
- `coa`: chart-of-accounts management
  - `show`
  - `add`
  - `deactivate`
- `account`: account lifecycle helpers
  - `list`
  - `open`
  - `deactivate`
- `expense`: guided expense entry
  - `record`
- `journal`: direct journal workflows
  - `add`
  - `reverse`
- `import`: external activity import
  - `stripe`
  - `csv`
- `reconcile`: statement reconciliation lifecycle
  - `start`
  - `match`
  - `candidates`
  - `close`
  - `reopen`
  - `void`
  - `list`
- `report`: books and control reporting
  - `pnl`
  - `balance-sheet`
  - `cash-flow`
  - `general-ledger`
  - `trial-balance`
  - `tax-liabilities`
  - `equity-rollforward`
  - `owner-equity`
- `tax`: tax support and liability rollforward
  - `obligations`
  - `rollforward`
- `period`: close control lifecycle
  - `close`
  - `reopen`
  - `status`
  - `audit`
- `export`: package and report export
  - `period-end`
  - `year-end`
  - `accountant-packet`
- `document`: source-document registry and checklist support
  - `add`
  - `list`
  - `update`
  - `checklist`
- `settlement`: explicit cash-basis settlement control
  - `list`
  - `apply`
  - `reverse`
- `review`: blocker lifecycle
  - `list`
  - `resolve`
  - `retry`
- `compliance`: advisory compliance facts
  - `profile`
  - `sales-tax-slot`

## Core Accounting Policies

### Accrual vs Cash

`clawbooks` is accrual-native.

- `report balance-sheet` is always accrual.
- `report pnl --basis accrual` is normal accrual `P&L`.
- cash-basis `P&L` is limited to supported flows:
  - immediate-cash postings directly involving `bank`, `stripe_clearing`, or `card`
  - owner-paid non-reimbursable expenses through `3000`
  - explicit settlement applications between accrual source lines and supported cash-settling lines

Unsupported cash-basis cases are excluded and surfaced in warnings. Do not “fix” them by inference.

### Settlement Rules

Use settlement only when the cash-basis result should come from actual settlement of a prior accrual.

Rules:
- `source_line_id` must be a revenue, expense, or contra-revenue line
- `settlement_line_id` must be a supported settling line
- immediate-cash entries cannot serve as settlement cash for prior accruals
- settlement mutations are lock-aware and cannot change closed periods without `period reopen`
- if a settlement is wrong, reverse it rather than editing rows in place

### Reconciliation Rules

Reconciliation is statement-line to journal-line amount matching.

Rules:
- use statement sessions, not entry-level “mark reconciled” shortcuts
- overlapping active sessions are not allowed
- mistaken sessions are retired with `reconcile void`
- closed sessions are immutable unless reopened
- period close requires coverage for financial accounts that have nonzero opening balance, nonzero closing balance, or in-period activity
- dormant but nonzero bank/card/Stripe-clearing balances still require reconciliation coverage

### Review Blockers

A blocker means “stop guessing.”

Blockers are created for:
- Stripe tax ambiguity
- unsupported Stripe balance-transaction types
- non-USD Stripe activity
- other explicit review-required situations

Use blockers as the safe path. Do not force postings around them unless the supported override flow explicitly allows it.

### Advisory Tax Support

Tax features are evidence and checklist support only.

They are not:
- nexus determination
- payroll compliance automation
- return preparation
- filing submission

Checklist statuses:
- `present`
- `missing`
- `optional`
- `not_applicable`
- `unknown`

Treat `unknown` as “insufficient facts,” not “fine to ignore.”

## Startup and Safety Workflow

### Create a Ledger

```bash
uv run clawbooks --ledger /ledger --json init --business-name "Example LLC"
```

### Before Normal Work

Run:

```bash
uv run clawbooks --ledger /ledger --json doctor
```

If migration is required:

```bash
uv run clawbooks --ledger /ledger --json migrate
```

If `doctor` returns findings:
- treat `critical` and `high` findings as blocking bookkeeping trust issues
- inspect `period audit` for affected windows before closing periods or exporting support packages

## Chart of Accounts and Accounts

Use:
- `coa show` to inspect the chart
- `coa add` to add new accounts
- `coa deactivate` to retire chart accounts
- `account list` / `account open` / `account deactivate` for account lifecycle operations

Core default accounts commonly used:
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

## Guided Expense Entry

Use `expense record` whenever the expense fits the guided workflow.

### Business-paid

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "AWS" \
  --amount 84.12 \
  --category 5110 \
  --payment-account 1000
```

### Owner-paid, non-reimbursable

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "CPA LLC" \
  --amount 125.00 \
  --category 5120 \
  --paid-personally
```

This uses the supported owner-contribution flow and is cash-basis supported automatically.

### Owner-paid, reimbursable later

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "State Filing Fee" \
  --amount 150.00 \
  --category 5140 \
  --paid-personally \
  --reimbursement
```

This creates a reimbursable liability through `2300`.

Reimbursement auto-linking is intentionally narrow:
- the reimbursement-clearing entry must contain exactly one `2300` line and one cash-equivalent line
- no `P&L` lines in the reimbursement entry
- exactly one eligible open reimbursable source line
- exact amount match

If those conditions are not met, cash-basis treatment stays excluded until manual settlement is applied.

## Direct Journals

Use `journal add` when guided expense/import flows do not fit.

Important behaviors:
- journals must balance
- `--source-type` exists for explicit classification
  - `manual`
  - `owner_contribution`
  - `owner_draw`
- explicit owner source types are used by the equity rollforward

Use `journal reverse` instead of mutating an old entry in place.

## Stripe Import

Preview first:

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

Supported policy:
- taxable charge: `1010` debit, `4000` credit, `2100` credit for collected tax
- Stripe fee: `5000` debit, `1010` credit
- payout: `1000` debit, `1010` credit

Unsupported or ambiguous policy:
- ambiguous refund/dispute/tax effect: create blocker, do not post
- unsupported balance-transaction type: create blocker, do not post
- non-USD activity: create blocker, do not post

Import results distinguish:
- normal imported events
- blocked events
- unsupported events

## CSV Import

Use `import csv` for bank/card/statement imports.

Typical flow:
1. Prepare a CSV import profile.
2. Run `--dry-run`.
3. Re-run without `--dry-run`.
4. If statement balances are provided, the import can create reconciliation support.

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

Rules:
- dedupe is transaction-based, not file-path based
- unmatched rows stay unresolved
- do not guess coding when rules do not support it

## Settlement Workflow

Use for manual accruals that become cash later.

Apply:

```bash
uv run clawbooks --ledger /ledger --json settlement apply \
  --source-line-id 10 \
  --settlement-line-id 42 \
  --amount 100.00
```

Inspect:

```bash
uv run clawbooks --ledger /ledger --json settlement list
```

Reverse:

```bash
uv run clawbooks --ledger /ledger --json settlement reverse \
  --settlement-application-id 7 \
  --reason "Wrong source line"
```

Use settlement for:
- accrual revenue later collected
- accrual expenses later paid
- manual reimbursable flows that did not qualify for exact-safe auto-linking

Do not use settlement to reuse a fresh immediate-cash `bank/revenue` style entry.

## Reconciliation Workflow

### Start

```bash
uv run clawbooks --ledger /ledger --json reconcile start \
  --account-code 1000 \
  --statement-path /path/to/statement.csv \
  --statement-start 2026-04-01 \
  --statement-end 2026-04-30 \
  --statement-starting-balance 1000.00 \
  --statement-ending-balance 915.88
```

### Inspect candidates

```bash
uv run clawbooks --ledger /ledger --json reconcile candidates --session-id 1
```

### Match

```bash
uv run clawbooks --ledger /ledger --json reconcile match \
  --session-id 1 \
  --line-id 10 \
  --journal-line-id 42 \
  --amount 84.12
```

### Close

```bash
uv run clawbooks --ledger /ledger --json reconcile close --session-id 1
```

### Reopen or void

```bash
uv run clawbooks --ledger /ledger --json reconcile reopen --session-id 1 --reason "Need correction"
uv run clawbooks --ledger /ledger --json reconcile void --session-id 1 --reason "Wrong statement file"
```

Rules:
- use `void` for mistaken sessions
- use `reopen` only when working the same session again
- statement support documents should be linked to reconciliation sessions and match the statement window

## Review Blocker Workflow

List:

```bash
uv run clawbooks --ledger /ledger --json review list --status open
```

Resolve by skip:

```bash
uv run clawbooks --ledger /ledger --json review resolve \
  --blocker-id 12 \
  --resolution-type skip \
  --note "Handled outside clawbooks"
```

Resolve by manual supersession:

```bash
uv run clawbooks --ledger /ledger --json review resolve \
  --blocker-id 12 \
  --resolution-type superseded_by_manual_entry \
  --manual-entry-id 55
```

Retry against refreshed facts:

```bash
uv run clawbooks --ledger /ledger --json review retry --blocker-id 12
```

Rules:
- `post_with_override` is for explicitly reviewable tax blockers, not unsupported Stripe event types
- unsupported Stripe event blockers should resolve by `skip` or `superseded_by_manual_entry`

## Tax Support Features

### Obligations

Use `tax obligations` for the ledger’s tracked tax-obligation objects.

### Rollforward

Use `tax rollforward` or `report tax-liabilities` to review liability movement.

Important: liability balances are accounting facts; filing completeness is advisory and document-driven.

## Period Close Workflow

Inspect status:

```bash
uv run clawbooks --ledger /ledger --json period status \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

Audit first:

```bash
uv run clawbooks --ledger /ledger --json period audit \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

Close:

```bash
uv run clawbooks --ledger /ledger --json period close \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --lock-type month \
  --reason "April close"
```

Reopen:

```bash
uv run clawbooks --ledger /ledger --json period reopen \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --reason "Correction required"
```

Close requires:
- no open review blockers in the period
- required reconciliation coverage for financial accounts
- no lock-sensitive mutations after close unless reopened

## Compliance Profile and Sales-Tax Slots

The compliance profile is the authoritative source for advisory checklist applicability.

### Show the profile

```bash
uv run clawbooks --ledger /ledger --json compliance profile show
```

### Update the profile

```bash
uv run clawbooks --ledger /ledger --json compliance profile update --from-file /path/to/profile.json
```

Use this for:
- entity tax classification
- sales-tax registrations
- payroll facts
- owner tracking settings
- sales-tax payment slot expectations

### Sales-tax slot expectations

List:

```bash
uv run clawbooks --ledger /ledger --json compliance sales-tax-slot list --year 2026
```

Set expectation:

```bash
uv run clawbooks --ledger /ledger --json compliance sales-tax-slot set-payment-expectation \
  --jurisdiction illinois \
  --period-start 2026-01-01 \
  --period-end 2026-01-31 \
  --filing-due-date 2026-02-20 \
  --payment-expected true \
  --source operator \
  --reason "Monthly filing requires payment"
```

Rules:
- sales-tax payment completeness is assessable only from explicit slot metadata
- do not infer payment expectations from cadence alone, `2100`, return docs, or generic obligations

## Documents and Checklisting

### Add a document

```bash
uv run clawbooks --ledger /ledger --json document add \
  --source-path /path/to/file.pdf \
  --type illinois_sales_tax_return \
  --year 2026 \
  --jurisdiction illinois \
  --period-start 2026-01-01 \
  --period-end 2026-01-31
```

### List documents

```bash
uv run clawbooks --ledger /ledger --json document list --year 2026
```

### Update a document

```bash
uv run clawbooks --ledger /ledger --json document update \
  --document-id 12 \
  --jurisdiction illinois \
  --period-start 2026-01-01 \
  --period-end 2026-01-31
```

### Checklist

```bash
uv run clawbooks --ledger /ledger --json document checklist --year 2026
```

Checklist rules:
- year-end books package depends on a real export artifact
- estimated-tax confirmations are slot-based
- sales-tax returns are slot-based by jurisdiction and exact period
- sales-tax payments depend on explicit slot expectations plus matching payment evidence
- statement support should be backed by reconciliation-linked, period-matched statement evidence

## Reports

Use `report` for books output:

- `pnl`
- `balance-sheet`
- `cash-flow`
- `general-ledger`
- `trial-balance`
- `tax-liabilities`
- `equity-rollforward`
- `owner-equity`

Important semantics:
- `owner-equity` is a compatibility alias to the real `equity-rollforward`
- `equity-rollforward` shows:
  - opening equity
  - owner contributions
  - owner draws
  - current-period earnings
  - other equity adjustments
  - ending equity
- manual equity postings that are not explicit owner-flow source types belong in `other_equity_adjustments`

## Exports

### Period-end books export

```bash
uv run clawbooks --ledger /ledger --json export period-end \
  --period-start 2026-04-01 \
  --period-end 2026-04-30
```

### Year-end books export

```bash
uv run clawbooks --ledger /ledger --json export year-end --year 2026
```

### Accountant packet

```bash
uv run clawbooks --ledger /ledger --json export accountant-packet --year 2026
```

The accountant packet is advisory and should include:
- books export
- copied documents
- compliance-profile snapshot
- advisory checklist
- missing/unknown items
- cash-basis warning snapshot

## Integrity Features

### Doctor

```bash
uv run clawbooks --ledger /ledger --json doctor --year 2026
```

`doctor` checks:
- schema/migration health
- settlement integrity
- reconciliation lifecycle/control integrity
- closed-period control regressions
- stale blockers
- year-scoped document/export/checklist support
- snapshot drift

### Period audit

Use `period audit` whenever deciding whether to close or trust a previously closed window.

### Close snapshots

Closed periods store snapshots for later drift comparison. A reopened period is historical context, not automatically an integrity failure.

## TUI

`tui` exists, but it is read-heavy and CLI-secondary.

Use it for:
- reviewing reports
- status
- audit findings
- packet/export status
- adding simple documents

Do not rely on the TUI as the primary bookkeeping surface. CLI remains the authoritative workflow surface.

## Recommended Agent Workflow

For normal monthly operation:

1. Run `doctor`.
2. Review `review list --status open`.
3. Import Stripe with `--dry-run`, then commit.
4. Import CSV statements with `--dry-run`, then commit.
5. Reconcile all required financial accounts.
6. Review `report pnl`, `report balance-sheet`, `report cash-flow`, `report trial-balance`, and `report equity-rollforward`.
7. Review `document checklist --year YYYY`.
8. Run `period audit`.
9. Close the period only when blockers and reconciliation gaps are gone.
10. Generate `export accountant-packet --year YYYY` when preparing CPA handoff.

## When to Stop and Escalate

Stop and ask for human direction when:
- Stripe facts are ambiguous and require business judgment
- the compliance profile is incomplete enough that checklist interpretation is mostly `unknown`
- reconciliation evidence is missing for a required account
- a period must be reopened after a prior close
- a manual equity posting needs classification as owner flow vs adjustment
- the operator is asking for legal or filing conclusions beyond advisory support

## Minimal Decision Rules

- If the workflow fits a guided command, use the guided command.
- If facts are ambiguous, create or preserve blockers instead of guessing.
- If cash-basis recognition depends on settlement, make the settlement explicit.
- If statement support is not linked to the relevant reconciliation evidence, do not call it complete.
- If the app says `unknown`, gather more structured facts instead of reinterpreting it away.
