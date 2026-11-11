"""Shared database configuration and sessions used by management and ingestion."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.base import Connection, Engine
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


BASELINE_REVISION = "0001"


@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    return create_engine(DATABASE_URL, echo=False)


def alembic_config(connection: Connection | None = None) -> Config:
    """Alembic config for the bundled migrations, optionally bound to an open connection."""
    config = Config(toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml")
    config.attributes["connection"] = connection
    return config


def migrate(engine: Engine) -> None:
    """Upgrade PostgreSQL to the latest migration; unversioned pre-Alembic schemas are stamped as baseline."""
    if engine.dialect.name != "postgresql":
        # shortcut: migrations are PostgreSQL-only, SQLite (tests) gets the current models directly.
        SQLModel.metadata.create_all(engine)
        return
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext('theaterdb.migrations'))")
        )
        config = alembic_config(connection)
        unversioned = MigrationContext.configure(connection).get_current_revision() is None
        if unversioned and set(inspect(connection).get_table_names()) & set(SQLModel.metadata.tables):
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")


def init_db() -> None:
    """Create a missing PostgreSQL database, apply pending migrations and ensure the queue schema."""
    engine = _get_engine()
    try:
        migrate(engine)
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
        migrate(engine)

    ensure_queue_schema(engine)


@contextmanager
def get_session() -> Generator[Session]:
    """Provide one session; callers explicitly commit accepted changes."""
    with Session(_get_engine()) as session:
        yield session
