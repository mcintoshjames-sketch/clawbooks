from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import stripe

from clawbooks.exceptions import ComplianceError
from clawbooks.schemas import StripeEvent


def _to_epoch_bounds(start: date, end: date, *, timezone_name: str) -> tuple[int, int]:
    tz = ZoneInfo(timezone_name)
    start_local = datetime.combine(start, time.min, tzinfo=tz)
    end_local = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    return int(start_local.astimezone(UTC).timestamp()), int(end_local.astimezone(UTC).timestamp())


def _extract_tax_amount(charge: object) -> int | None:
    invoice = getattr(charge, "invoice", None)
    if invoice and getattr(invoice, "total_tax_amounts", None):
        return sum(int(item.amount) for item in invoice.total_tax_amounts)
    if invoice is not None:
        return 0
    return None


def _balance_transaction_to_event(item: object) -> StripeEvent | None:
    occurred_at = datetime.fromtimestamp(item.created, tz=UTC)
    item_type = str(item.type)
    source_id = str(item.source) if getattr(item, "source", None) else item.id
    if item.currency.lower() != "usd":
        return None

    if item_type == "charge":
        charge = None
        tax_cents = None
        try:
            if source_id.startswith("ch_"):
                charge = stripe.Charge.retrieve(source_id, expand=["invoice.total_tax_amounts"])
                tax_cents = _extract_tax_amount(charge)
        except Exception:  # pragma: no cover - live Stripe fallback
            charge = None
        return StripeEvent(
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
    if item_type == "refund":
        return StripeEvent(
            external_id=item.id,
            event_type="refund",
            occurred_at=occurred_at,
            amount_cents=abs(int(item.amount)),
            fee_cents=0,
            tax_cents=None,
            net_cents=abs(int(item.net)),
            description=getattr(item, "description", "") or "",
        )
    if item_type in {"adjustment", "payment_reversal"}:
        return StripeEvent(
            external_id=item.id,
            event_type="dispute",
            occurred_at=occurred_at,
            amount_cents=abs(int(item.amount)),
            tax_cents=None,
            description=getattr(item, "description", "") or "",
        )
    if item_type == "payout":
        return StripeEvent(
            external_id=item.id,
            event_type="payout",
            occurred_at=occurred_at,
            amount_cents=abs(int(item.amount)),
            description=getattr(item, "description", "") or "",
            payout_id=source_id,
        )
    return None


def fetch_stripe_events(api_key: str | None, start: date, end: date, *, timezone_name: str) -> list[StripeEvent]:
    if not api_key:
        raise ComplianceError("Missing Stripe API key. Set CLAWBOOKS_STRIPE_API_KEY to import Stripe activity.")

    stripe.api_key = api_key
    start_ts, end_ts = _to_epoch_bounds(start, end, timezone_name=timezone_name)
    raw_events = stripe.BalanceTransaction.list(
        created={"gte": start_ts, "lt": end_ts},
        limit=100,
    )

    events: list[StripeEvent] = []
    for item in raw_events.auto_paging_iter():
        event = _balance_transaction_to_event(item)
        if event is not None:
            events.append(event)

    return events


def fetch_stripe_event(api_key: str | None, external_id: str) -> StripeEvent:
    if not api_key:
        raise ComplianceError("Missing Stripe API key. Set CLAWBOOKS_STRIPE_API_KEY to import Stripe activity.")

    stripe.api_key = api_key
    item = stripe.BalanceTransaction.retrieve(external_id)
    event = _balance_transaction_to_event(item)
    if event is None:
        raise ComplianceError(f"Stripe event {external_id} is not a supported USD balance transaction.")
    return event
