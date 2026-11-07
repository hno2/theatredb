"""Shared database configuration and sessions used by management and ingestion."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, SQLModel, create_engine

from backend import models  # ruff: ignore[unused-import]
from backend.crawling.schema import ensure_queue_schema
from backend.scd2 import SCD2History  # ruff: ignore[unused-import]

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "example")
DB_NAME = os.getenv("POSTGRES_DB", "postgres")

_database_url = os.getenv("THEATERDB_DATABASE_URL") or os.getenv("DATABASE_URL")
DATABASE_URL = (
    make_url(_database_url)
    if _database_url
    else URL.create(
        "postgresql+psycopg",
        username=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
    )
)
if DATABASE_URL.drivername in {"postgres", "postgresql", "postgresql+psycopg2"}:
    DATABASE_URL = DATABASE_URL.set(drivername="postgresql+psycopg")


@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    return create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    """Create a missing PostgreSQL database and tables without rewriting data."""
    engine = _get_engine()
    try:
        SQLModel.metadata.create_all(engine)
    except OperationalError as connection_error:
        database_name = engine.url.database
        if engine.url.get_backend_name() != "postgresql" or not database_name:
            raise
        with ExitStack() as stack:
            admin_engine = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
            stack.callback(admin_engine.dispose)
            try:
                connection = stack.enter_context(admin_engine.connect())
            except OperationalError:
                raise connection_error from None
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            ).scalar()
            if exists:
                raise
            database = connection.dialect.identifier_preparer.quote_identifier(database_name)
            try:
                connection.exec_driver_sql(f"CREATE DATABASE {database}")
            except ProgrammingError as error:
                if getattr(error.orig, "sqlstate", None) != "42P04":
                    raise
        SQLModel.metadata.create_all(engine)

    ensure_queue_schema(engine)


@contextmanager
def get_session() -> Generator[Session]:
    """Provide one session; callers explicitly commit accepted changes."""
    with Session(_get_engine()) as session:
        yield session
