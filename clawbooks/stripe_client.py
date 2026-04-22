from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import stripe

from clawbooks.exceptions import ComplianceError
from clawbooks.schemas import StripeEvent


def _to_epoch_bounds(start: date, end: date) -> tuple[int, int]:
    start_dt = datetime.combine(start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return int(start_dt.timestamp()), int(end_dt.timestamp())


def _extract_tax_amount(charge: object) -> int:
    invoice = getattr(charge, "invoice", None)
    if invoice and getattr(invoice, "total_tax_amounts", None):
        return sum(int(item.amount) for item in invoice.total_tax_amounts)
    return 0


def fetch_stripe_events(api_key: str | None, start: date, end: date) -> list[StripeEvent]:
    if not api_key:
        raise ComplianceError("Missing Stripe API key. Set CLAWBOOKS_STRIPE_API_KEY to import Stripe activity.")

    stripe.api_key = api_key
    start_ts, end_ts = _to_epoch_bounds(start, end)
    raw_events = stripe.BalanceTransaction.list(
        created={"gte": start_ts, "lt": end_ts},
        limit=100,
    )

    events: list[StripeEvent] = []
    for item in raw_events.auto_paging_iter():
        occurred_at = datetime.fromtimestamp(item.created, tz=UTC)
        item_type = str(item.type)
        source_id = str(item.source) if getattr(item, "source", None) else item.id
        if item.currency.lower() != "usd":
            continue

        if item_type == "charge":
            charge = None
            tax_cents = 0
            try:
                if source_id.startswith("ch_"):
                    charge = stripe.Charge.retrieve(source_id, expand=["invoice.total_tax_amounts"])
                    tax_cents = _extract_tax_amount(charge)
            except Exception:  # pragma: no cover - live Stripe fallback
                charge = None
            events.append(
                StripeEvent(
                    external_id=item.id,
                    event_type="charge",
                    occurred_at=occurred_at,
                    amount_cents=int(item.amount),
                    fee_cents=int(item.fee or 0),
                    tax_cents=tax_cents,
                    net_cents=int(item.net),
                    description=getattr(item, "description", "") or "",
                    charge_id=source_id if source_id.startswith("ch_") else None,
                    invoice_id=getattr(charge, "invoice", None).id if charge and getattr(charge, "invoice", None) else None,
                )
            )
        elif item_type == "refund":
            events.append(
                StripeEvent(
                    external_id=item.id,
                    event_type="refund",
                    occurred_at=occurred_at,
                    amount_cents=abs(int(item.amount)),
                    fee_cents=0,
                    tax_cents=0,
                    net_cents=abs(int(item.net)),
                    description=getattr(item, "description", "") or "",
                )
            )
        elif item_type in {"adjustment", "payment_reversal"}:
            events.append(
                StripeEvent(
                    external_id=item.id,
                    event_type="dispute",
                    occurred_at=occurred_at,
                    amount_cents=abs(int(item.amount)),
                    description=getattr(item, "description", "") or "",
                )
            )
        elif item_type == "payout":
            events.append(
                StripeEvent(
                    external_id=item.id,
                    event_type="payout",
                    occurred_at=occurred_at,
                    amount_cents=abs(int(item.amount)),
                    description=getattr(item, "description", "") or "",
                    payout_id=source_id,
                )
            )

    return events
