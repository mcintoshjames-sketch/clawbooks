from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from clawbooks.exceptions import ValidationError


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid date: {value}") from exc


def parse_money(value: str | int | float | Decimal) -> int:
    if isinstance(value, int):
        return value
    try:
        quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:  # pragma: no cover - Decimal covers broad exception types
        raise ValidationError(f"Invalid money amount: {value}") from exc
    return int(quantized * 100)


def cents_to_decimal(amount_cents: int) -> str:
    return f"{Decimal(amount_cents) / Decimal(100):.2f}"


def format_money(amount_cents: int) -> str:
    prefix = "-" if amount_cents < 0 else ""
    return f"{prefix}${cents_to_decimal(abs(amount_cents))}"


def json_dumps(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def stable_external_id(*parts: object) -> str:
    joined = "::".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def sha256_for_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8192):
            digest.update(chunk)
    return digest.hexdigest()


def month_bounds(value: date) -> tuple[date, date]:
    start = value.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    return start, end


def year_bounds(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


@dataclass(slots=True)
class StatementRow:
    transaction_date: date
    description: str
    amount_cents: int
    external_ref: str | None = None


def daterange(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)
