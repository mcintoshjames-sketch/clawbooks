from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from clawbooks.config import ledger_paths
from clawbooks.exceptions import ValidationError
from clawbooks.models import Base


def database_url(ledger_dir: Path) -> str:
    return f"sqlite:///{ledger_paths(ledger_dir)['db']}"


def make_engine(ledger_dir: Path) -> Engine:
    engine = create_engine(database_url(ledger_dir), future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - pragma hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_schema(ledger_dir: Path) -> None:
    paths = ledger_paths(ledger_dir)
    engine = make_engine(ledger_dir)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA journal_mode=WAL"))
    paths["db"].touch(exist_ok=True)


def require_initialized(ledger_dir: Path) -> None:
    paths = ledger_paths(ledger_dir)
    if not paths["db"].exists():
        raise ValidationError(f"Ledger is not initialized at {ledger_dir}")


@contextmanager
def session_scope(ledger_dir: Path) -> Session:
    require_initialized(ledger_dir)
    engine = make_engine(ledger_dir)
    session_factory = sessionmaker(engine, expire_on_commit=False, future=True)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
