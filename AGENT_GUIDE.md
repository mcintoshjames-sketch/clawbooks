# OpenClaw Agent Guide for `clawbooks`

This guide tells an AI agent how to operate `clawbooks` safely as the bookkeeping tool for a single-member Illinois LLC with Stripe subscription revenue.

## Mission

Use `clawbooks` to keep clean books, maintain reconciliation discipline, surface tax-review issues, and prepare period-end and year-end exports for the owner and CPA.

Do not act as a tax lawyer, payroll engine, or filing authority. Use the app to record facts, track liabilities, and produce exports. Escalate legal, nexus, payroll, or filing judgment calls to a human or CPA.

## Non-Negotiable Rules

1. Use the CLI only. Do not write directly to `ledger.db`, edit exported files, or mutate the ledger outside `clawbooks`.
2. Prefer `--json` on every read and every automation-facing command.
3. Use `--dry-run` first on imports and risky writes when facts are incomplete.
4. Never delete or overwrite accounting history. Use `journal reverse` or a compensating entry.
5. Never acknowledge a tax-review warning without explicit human approval.
6. Never close a period unless reconciliation is complete and tax-review warnings are resolved or explicitly approved.
7. Never guess taxability for a transaction stream. Record facts and escalate if Stripe tax detail is missing or ambiguous.
8. Never book owner personal spending as payroll. Use owner contribution or reimbursement logic only.
9. If a command returns a nonzero exit code, stop and resolve it before continuing downstream workflow.

## Environment and Startup

Use one ledger directory per business.

Required startup assumptions:
- Business is a single-member LLC.
- Base currency is `USD`.
- Timezone is `America/Chicago`.
- Stripe is the revenue source in v1.
- Stripe Tax is the expected tax collection engine when enabled.

Recommended startup pattern:

```bash
uv run clawbooks --ledger /absolute/path/to/ledger --json init --business-name "Example LLC"
```

If importing Stripe, set:

```bash
export CLAWBOOKS_STRIPE_API_KEY=sk_live_...
```

## Core Command Pattern

Always prefer this shell shape:

```bash
uv run clawbooks --ledger /absolute/path/to/ledger --json <command> ...
```

Result contract:

```json
{"ok":true,"command":"report pnl","data":{},"warnings":[],"errors":[]}
```

Exit codes:
- `0`: success
- `2`: validation or input error
- `3`: reconciliation mismatch
- `4`: locked period
- `5`: import conflict or duplicate external event problem
- `6`: compliance prerequisite missing, including tax-review blockers

## Default Account Codes

Use these defaults unless the ledger has been customized:

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
- `5100`: Software and SaaS
- `5110`: Hosting
- `5120`: Professional Fees
- `5130`: Bank Fees
- `5140`: Taxes and Licenses
- `5150`: Contractors
- `5160`: Chargebacks
- `5199`: Uncategorized Expense

## Daily Operating Playbooks

### 1. Record a manual expense

For a business-paid expense:

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "AWS" \
  --amount 84.12 \
  --category 5110 \
  --payment-account 1000
```

For an owner-paid expense that should be treated as contributed capital:

```bash
uv run clawbooks --ledger /ledger --json expense record \
  --date 2026-04-21 \
  --vendor "CPA LLC" \
  --amount 125.00 \
  --category 5120 \
  --paid-personally
```

For an owner-paid expense that should be reimbursed later:

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
- Require an explicit category code.
- Require an explicit payment account unless `--paid-personally` is used.
- Use `--receipt-path` when supporting evidence exists.

### 2. Import Stripe activity

Preview first:

```bash
uv run clawbooks --ledger /ledger --json import stripe \
  --from-date 2026-04-01 \
  --to-date 2026-04-30 \
  --dry-run
```

Then commit:

```bash
uv run clawbooks --ledger /ledger --json import stripe \
  --from-date 2026-04-01 \
  --to-date 2026-04-30
```

Interpretation rules:
- `charge` events should post gross cash to Stripe clearing, revenue to `4000`, and tax collected to `2100` when present.
- Stripe fees should post to `5000`.
- Refunds should post to `4010` and reduce Stripe clearing.
- Disputes should post to `5160`.
- Payouts should move value from `1010` to `1000`.

Tax rules:
- If the import returns warnings about missing tax detail, treat them as compliance blockers until human review.
- Do not “fix” missing tax detail by manually inventing a sales-tax amount.

### 3. Import a bank or card CSV

Use a mapping profile JSON file:

```json
{
  "date_column": "date",
  "description_column": "description",
  "amount_column": "amount",
  "external_ref_column": "external_ref",
  "rules": [
    {"match": "AWS", "account_code": "5110", "entry_kind": "expense"},
    {"match": "Stripe Payout", "account_code": "4000", "entry_kind": "income"}
  ]
}
```

Preview first:

```bash
uv run clawbooks --ledger /ledger --json import csv \
  --account-code 1000 \
  --csv-path /path/to/statement.csv \
  --profile-path /path/to/profile.json \
  --statement-ending-balance 1500.25 \
  --dry-run
```

Then commit:

```bash
uv run clawbooks --ledger /ledger --json import csv \
  --account-code 1000 \
  --csv-path /path/to/statement.csv \
  --profile-path /path/to/profile.json \
  --statement-ending-balance 1500.25
```

Rules:
- Explicitly mapped rows post journal entries.
- Unmatched rows become draft reconciliation lines and must be reviewed.
- Do not auto-classify unmatched rows by guesswork.

### 4. Add or reverse a journal entry

Use `journal add` only for explicit adjustments the human or CPA has approved.

```bash
uv run clawbooks --ledger /ledger --json journal add \
  --date 2026-04-30 \
  --description "CPA adjustment" \
  --line 5120:200.00:"Professional fees" \
  --line 2000:-200.00:"Credit card payable"
```

For non-cash entries:

```bash
uv run clawbooks --ledger /ledger --json journal add \
  --date 2026-04-30 \
  --description "Accrual adjustment" \
  --line 1100:100.00:"Accounts receivable" \
  --line 4000:-100.00:"Revenue" \
  --non-cash
```

To reverse:

```bash
uv run clawbooks --ledger /ledger --json journal reverse \
  --entry-id 42 \
  --date 2026-05-01 \
  --reason "Duplicate posting"
```

Rules:
- Entries must balance exactly.
- Prefer reversal over editing history.
- Use non-cash entries sparingly and only with explicit justification.

## Reconciliation Workflow

### Start reconciliation

```bash
uv run clawbooks --ledger /ledger --json reconcile start \
  --account-code 1000 \
  --statement-path /path/to/statement.csv \
  --statement-start 2026-04-01 \
  --statement-end 2026-04-30 \
  --statement-ending-balance 1500.25
```

### Match a draft line to an entry

```bash
uv run clawbooks --ledger /ledger --json reconcile match \
  --session-id 7 \
  --line-id 12 \
  --entry-id 44
```

### Close reconciliation

```bash
uv run clawbooks --ledger /ledger --json reconcile close --session-id 7
```

Rules:
- Every financial account active in the period must be reconciled before period close.
- A reconciliation cannot close with unresolved draft lines.
- If `reconcile close` returns exit code `3`, resolve the mismatch before moving on.

## Month-End Close Workflow

Run this sequence in order:

1. Import Stripe for the full month with a dry run, then a real run.
2. Import bank and card statements with dry runs, then real runs.
3. Review unmatched CSV rows and resolve them.
4. Start and close reconciliations for `1000`, `1010`, and any active card account.
5. Review reports:
   - `report pnl`
   - `report balance-sheet`
   - `report tax-liabilities`
   - `report owner-equity`
6. Review all Stripe or tax warnings from prior commands.
7. If a tax-review warning exists, get human approval before acknowledging it during close.
8. Close the period.
9. Export the period-end package.

Recommended commands:

```bash
uv run clawbooks --ledger /ledger --json report pnl --period-start 2026-04-01 --period-end 2026-04-30
uv run clawbooks --ledger /ledger --json report balance-sheet --as-of 2026-04-30
uv run clawbooks --ledger /ledger --json report tax-liabilities --as-of 2026-04-30
uv run clawbooks --ledger /ledger --json period close --period-start 2026-04-01 --period-end 2026-04-30
uv run clawbooks --ledger /ledger --json export period-end --period-start 2026-04-01 --period-end 2026-04-30
```

If human approval exists for a specific review-required entry:

```bash
uv run clawbooks --ledger /ledger --json period close \
  --period-start 2026-04-01 \
  --period-end 2026-04-30 \
  --acknowledge-review-entry 44
```

Do not use `--acknowledge-review-entry` without explicit human authorization.

## Quarter-End and Year-End

At quarter end:
- Review `tax obligations`.
- Review `tax rollforward`.
- Provide exports to the human or CPA.

Commands:

```bash
uv run clawbooks --ledger /ledger --json tax obligations --as-of 2026-06-30
uv run clawbooks --ledger /ledger --json tax rollforward --period-start 2026-04-01 --period-end 2026-06-30
```

At year end:

```bash
uv run clawbooks --ledger /ledger --json export year-end --year 2026
```

The year-end export bundle is the handoff package. The agent should not file returns directly from it.

## Reporting Recipes

Profit and loss:

```bash
uv run clawbooks --ledger /ledger --json report pnl --period-start 2026-04-01 --period-end 2026-04-30
```

Accrual P&L:

```bash
uv run clawbooks --ledger /ledger --json report pnl --period-start 2026-04-01 --period-end 2026-04-30 --basis accrual
```

Balance sheet:

```bash
uv run clawbooks --ledger /ledger --json report balance-sheet --as-of 2026-04-30
```

Cash flow:

```bash
uv run clawbooks --ledger /ledger --json report cash-flow --period-start 2026-04-01 --period-end 2026-04-30
```

General ledger:

```bash
uv run clawbooks --ledger /ledger --json report general-ledger --period-start 2026-04-01 --period-end 2026-04-30
```

Trial balance:

```bash
uv run clawbooks --ledger /ledger --json report trial-balance --as-of 2026-04-30
```

Owner equity:

```bash
uv run clawbooks --ledger /ledger --json report owner-equity --as-of 2026-04-30
```

Tax liabilities:

```bash
uv run clawbooks --ledger /ledger --json report tax-liabilities --as-of 2026-04-30
```

## Compliance Decision Boundaries

The agent may do these without asking:
- run reports
- run dry-run imports
- post clearly documented operating expenses
- post approved journal adjustments
- perform reconciliation matching when evidence is clear
- export period-end and year-end packages

The agent must ask a human before doing these:
- acknowledging a tax-review warning
- classifying an ambiguous expense or deposit
- posting an owner draw vs business expense when intent is unclear
- adding a new chart-of-accounts account for a policy decision
- posting non-cash adjustments not directly instructed by a human or CPA
- reopening a previously closed period

The agent must escalate to a CPA or human expert for:
- federal or state tax elections
- sales-tax nexus and filing jurisdiction decisions
- payroll setup or payroll tax treatment
- contractor vs employee classification
- legal entity restructuring
- any return filing decision

## Failure Handling

If a command fails:

- Exit `2`: fix inputs, dates, account codes, or unbalanced entries.
- Exit `3`: complete reconciliation work or correct the ledger mismatch.
- Exit `4`: the period is locked. Do not force around it. Ask whether reopen is approved.
- Exit `5`: inspect duplicate or import-conflict conditions. Do not post around them manually.
- Exit `6`: resolve tax-review or compliance prerequisites before proceeding.

Recommended response pattern:

1. Save the exact failing command and JSON response.
2. Explain the blocker in plain language.
3. Offer the smallest safe next step.
4. Do not continue the workflow until the blocker is cleared.

## Safe Operating Checklist

Before each posting/import:
- confirm the ledger path
- confirm the date range
- confirm the account code or category
- prefer `--dry-run` if the source data is external

Before each period close:
- confirm all imports are complete
- confirm all financial accounts are reconciled
- confirm no unresolved warnings remain
- confirm the reports look reasonable
- confirm human approval for any acknowledged review entries

## Recommended Agent Prompt Stub

If OpenClaw needs an operating brief, use something like:

> You are the bookkeeping operator for a single-member Illinois LLC using `clawbooks`. Use the CLI only, prefer `--json`, preserve immutable history, reconcile before close, never acknowledge tax-review warnings without human approval, and escalate taxability, payroll, or filing judgment calls.
