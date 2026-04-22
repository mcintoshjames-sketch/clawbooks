# clawbooks

Compliance-first CLI bookkeeping for a single-member Illinois LLC.

## Agent Guide

For an AI-operator manual with workflows, guardrails, and exact command patterns, see [AGENT_GUIDE.md](AGENT_GUIDE.md).

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
