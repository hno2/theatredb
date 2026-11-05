from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.base import Engine
from sqlmodel import Session, SQLModel, create_engine

from backend import models as _models  # ruff: ignore[unused-import]
from backend.scd2 import SCD2History  # ruff: ignore[unused-import]

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

_database_url = os.getenv("THEATERDB_DATABASE_URL") or os.getenv("DATABASE_URL")
DATABASE_URL = (
    make_url(_database_url)
    if _database_url
    else URL.create(
        "postgresql+psycopg",
        username=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", "example"),
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "postgres"),
    )
)
if DATABASE_URL.drivername in {"postgres", "postgresql", "postgresql+psycopg2"}:
    DATABASE_URL = DATABASE_URL.set(drivername="postgresql+psycopg")


@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    return create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    """Create database tables defined by the registered SQLModel classes."""
    SQLModel.metadata.create_all(_get_engine())


@contextmanager
def get_session() -> Generator[Session]:
    """Yield a database session and close it when the context exits."""
    with Session(_get_engine()) as session:
        yield session
