from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from clawbooks.config import ledger_paths
from clawbooks.exceptions import MigrationRequiredError, ValidationError
from clawbooks.legacy_baseline import LEGACY_BASELINE_REVISION, LEGACY_BASELINE_SPEC


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def database_url(ledger_dir: Path) -> str:
    return f"sqlite:///{ledger_paths(ledger_dir)['db']}"


def make_engine(ledger_dir: Path) -> Engine:
    engine = create_engine(database_url(ledger_dir), future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _alembic_config(ledger_dir: Path) -> Config:
    root = _repo_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url(ledger_dir))
    return config


def alembic_head_revision() -> str:
    root = _repo_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def _normalize_declared_type(value: str | None) -> str:
    if not value:
        return ""
    return value.upper().replace(" ", "")


def _sorted_index_columns(columns: list[str]) -> tuple[str, ...]:
    return tuple(columns)


def _sqlite_table_spec(connection: sqlite3.Connection, table_name: str) -> dict[str, Any]:
    column_rows = connection.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    columns = {
        row[1]: {
            "type": _normalize_declared_type(row[2]),
            "nullable": not bool(row[3]),
            "primary_key": bool(row[5]),
        }
        for row in column_rows
    }
    foreign_key_rows = connection.execute(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
    grouped_fks: dict[int, dict[str, Any]] = {}
    for row in foreign_key_rows:
        grouped_fks.setdefault(
            row[0],
            {"local": [], "remote_table": row[2], "remote": []},
        )
        grouped_fks[row[0]]["local"].append(row[3])
        grouped_fks[row[0]]["remote"].append(row[4])
    foreign_keys = sorted(
        (
            tuple(item["local"]),
            item["remote_table"],
            tuple(item["remote"]),
        )
        for item in grouped_fks.values()
    )

    index_rows = connection.execute(f"PRAGMA index_list('{table_name}')").fetchall()
    indexes = set()
    unique_indexes = set()
    for row in index_rows:
        index_name = row[1]
        is_unique = bool(row[2])
        origin = row[3]
        if origin == "pk":
            continue
        columns_tuple = _sorted_index_columns(
            [index_row[2] for index_row in connection.execute(f"PRAGMA index_info('{index_name}')").fetchall()]
        )
        if not columns_tuple:
            continue
        if is_unique:
            unique_indexes.add(columns_tuple)
        else:
            indexes.add(columns_tuple)
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indexes": sorted(indexes),
        "unique_indexes": sorted(unique_indexes),
    }


def _fingerprint_diff(actual: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    missing_tables = sorted(set(expected) - set(actual))
    unexpected_tables = sorted(set(actual) - set(expected))
    table_diffs: dict[str, Any] = {}
    for table_name in sorted(set(expected) & set(actual)):
        actual_spec = actual[table_name]
        expected_spec = expected[table_name]
        diff: dict[str, Any] = {}
        actual_columns = set(actual_spec["columns"])
        expected_columns = set(expected_spec["columns"])
        if actual_columns != expected_columns:
            diff["missing_columns"] = sorted(expected_columns - actual_columns)
            diff["unexpected_columns"] = sorted(actual_columns - expected_columns)
        mismatched_columns = {}
        for column_name in sorted(actual_columns & expected_columns):
            actual_column = actual_spec["columns"][column_name]
            expected_column = expected_spec["columns"][column_name]
            if actual_column != expected_column:
                mismatched_columns[column_name] = {
                    "expected": expected_column,
                    "actual": actual_column,
                }
        if mismatched_columns:
            diff["mismatched_columns"] = mismatched_columns
        if actual_spec["foreign_keys"] != expected_spec["foreign_keys"]:
            diff["foreign_keys"] = {
                "expected": expected_spec["foreign_keys"],
                "actual": actual_spec["foreign_keys"],
            }
        if actual_spec["unique_indexes"] != expected_spec["unique_indexes"]:
            diff["unique_indexes"] = {
                "expected": expected_spec["unique_indexes"],
                "actual": actual_spec["unique_indexes"],
            }
        if actual_spec["indexes"] != expected_spec["indexes"]:
            diff["indexes"] = {
                "expected": expected_spec["indexes"],
                "actual": actual_spec["indexes"],
            }
        if diff:
            table_diffs[table_name] = diff
    return {
        "missing_tables": missing_tables,
        "unexpected_tables": unexpected_tables,
        "table_diffs": table_diffs,
    }


def _legacy_baseline_expected_spec() -> dict[str, dict[str, Any]]:
    return {
        table_name: {
            "columns": {
                column_name: dict(column_spec)
                for column_name, column_spec in table_spec["columns"].items()
            },
            "foreign_keys": list(table_spec["foreign_keys"]),
            "indexes": list(table_spec["indexes"]),
            "unique_indexes": list(table_spec["unique_indexes"]),
        }
        for table_name, table_spec in LEGACY_BASELINE_SPEC.items()
    }


def _legacy_baseline_actual_spec(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        table_name: _sqlite_table_spec(connection, table_name)
        for table_name in sorted(raw_table_set(connection) - {"alembic_version"})
    }


def raw_table_set(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def inspect_ledger_bootstrap(ledger_dir: Path) -> dict[str, Any]:
    paths = ledger_paths(ledger_dir)
    expected_head = alembic_head_revision()
    result: dict[str, Any] = {
        "ledger_dir": str(ledger_dir),
        "db_exists": paths["db"].exists(),
        "config_exists": paths["config"].exists(),
        "raw_tables": [],
        "current_revision": None,
        "expected_head": expected_head,
        "matches_legacy_baseline": False,
        "legacy_baseline_revision": LEGACY_BASELINE_REVISION,
        "legacy_baseline_fingerprint_diff": {"missing_tables": [], "unexpected_tables": [], "table_diffs": {}},
        "full_open_safe": False,
    }
    if not paths["db"].exists():
        return result

    connection = sqlite3.connect(paths["db"])
    try:
        table_set = raw_table_set(connection)
        result["raw_tables"] = sorted(table_set)
        if "alembic_version" in table_set:
            row = connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
            if row:
                result["current_revision"] = row[0]
        if result["current_revision"] == expected_head:
            result["full_open_safe"] = True
            return result

        if result["current_revision"] is None:
            expected = _legacy_baseline_expected_spec()
            actual = _legacy_baseline_actual_spec(connection)
            diff = _fingerprint_diff(actual, expected)
            result["legacy_baseline_fingerprint_diff"] = diff
            matches = not diff["missing_tables"] and not diff["unexpected_tables"] and not diff["table_diffs"]
            result["matches_legacy_baseline"] = matches
        return result
    finally:
        connection.close()


def _enable_wal(ledger_dir: Path) -> None:
    paths = ledger_paths(ledger_dir)
    connection = sqlite3.connect(paths["db"])
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.commit()
    finally:
        connection.close()


def create_schema(ledger_dir: Path) -> None:
    paths = ledger_paths(ledger_dir)
    paths["root"].mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(ledger_dir), "head")
    _enable_wal(ledger_dir)


def migrate_ledger(ledger_dir: Path) -> dict[str, Any]:
    paths = ledger_paths(ledger_dir)
    if not paths["db"].exists():
        raise ValidationError(f"Ledger is not initialized at {ledger_dir}")
    state = inspect_ledger_bootstrap(ledger_dir)
    if state["current_revision"] is None:
        if not state["matches_legacy_baseline"]:
            raise MigrationRequiredError(
                "Ledger does not match the expected legacy baseline and cannot be migrated automatically.",
                data={"migration_state": state},
            )
        command.stamp(_alembic_config(ledger_dir), LEGACY_BASELINE_REVISION)
    command.upgrade(_alembic_config(ledger_dir), "head")
    _enable_wal(ledger_dir)
    return inspect_ledger_bootstrap(ledger_dir)


def require_initialized(ledger_dir: Path) -> None:
    paths = ledger_paths(ledger_dir)
    if not paths["db"].exists():
        raise ValidationError(f"Ledger is not initialized at {ledger_dir}")


@contextmanager
def session_scope(ledger_dir: Path) -> Session:
    require_initialized(ledger_dir)
    state = inspect_ledger_bootstrap(ledger_dir)
    if not state["full_open_safe"]:
        raise MigrationRequiredError(
            "Ledger schema is not at the current Alembic head. Run `clawbooks migrate` before using this ledger.",
            data={"migration_state": state},
        )
    engine = make_engine(ledger_dir)
    session_factory = sessionmaker(engine, expire_on_commit=False, future=True)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
