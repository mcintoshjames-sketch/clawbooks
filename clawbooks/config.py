from __future__ import annotations

import os
import tomllib
from pathlib import Path

from clawbooks.exceptions import ValidationError
from clawbooks.schemas import AppConfig


def ledger_paths(ledger_dir: Path) -> dict[str, Path]:
    return {
        "root": ledger_dir,
        "db": ledger_dir / "ledger.db",
        "config": ledger_dir / "config.toml",
        "imports": ledger_dir / "imports",
        "exports": ledger_dir / "exports",
        "attachments": ledger_dir / "attachments",
    }


def is_ledger_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    paths = ledger_paths(path)
    return paths["db"].exists() and paths["config"].exists()


def validate_ledger_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise ValidationError(f"Ledger directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValidationError(f"Ledger path is not a directory: {resolved}")
    paths = ledger_paths(resolved)
    missing = [name for name in ("db", "config") if not paths[name].exists()]
    if missing:
        raise ValidationError(
            f"Directory is not a clawbooks ledger: {resolved}",
            data={"missing": missing, "ledger_dir": str(resolved)},
        )
    return resolved


def write_default_config(path: Path, business_name: str) -> None:
    content = f"""business_name = "{business_name}"
entity_name = "{business_name}"
home_state = "IL"
timezone = "America/Chicago"
base_currency = "USD"
default_report_basis = "cash"
stripe_tax_mode = "handled_by_stripe_tax"
"""
    path.write_text(content, encoding="utf-8")


def load_config(ledger_dir: Path) -> AppConfig:
    paths = ledger_paths(ledger_dir)
    raw: dict[str, object] = {}
    if paths["config"].exists():
        raw = tomllib.loads(paths["config"].read_text(encoding="utf-8"))
    config = AppConfig.model_validate(raw)
    stripe_api_key = os.getenv("CLAWBOOKS_STRIPE_API_KEY")
    if stripe_api_key:
        config = config.model_copy(update={"stripe_api_key": stripe_api_key})
    return config
