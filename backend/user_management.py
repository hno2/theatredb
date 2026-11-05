from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.base import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from backend.models import User
from backend.scd2 import SCD2History  # noqa: F401

try:
    from backend.models import ModelChangeLog  # type: ignore
except ImportError:
    ModelChangeLog = None

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


def _set_session_actor(session: Session, user_id: str | None) -> None:
    if user_id is None:
        session.info.pop("actor_id", None)
        return
    session.info["actor_id"] = str(user_id)


def init_db() -> None:
    SQLModel.metadata.create_all(_get_engine())


@contextmanager
def get_session():
    with Session(_get_engine()) as session:
        yield session


def ensure_default_admin() -> None:
    with get_session() as session:
        existing = session.exec(select(User).where(User.username == "admin")).first()
        if existing:
            return
        admin = User.model_validate(
            {
                "username": "admin",
                "display_name": "Administrator",
                "email": "admin@local",
                "password_hash": "external-auth",
                "is_active": True,
                "is_admin": True,
            }
        )
        session.add(admin)
        session.commit()


def upsert_identity_user(*, username: str, email: str | None, display_name: str | None, is_admin: bool = False) -> User:
    with get_session() as session:
        clean_username = username.strip()
        clean_email = (email or "").strip() or None
        clean_display = (display_name or "").strip() or None

        user = session.exec(select(User).where(User.username == clean_username)).first()
        if user is None and clean_email:
            user = session.exec(select(User).where(User.email == clean_email)).first()

        if user is None:
            user = User.model_validate(
                {
                    "username": clean_username,
                    "display_name": clean_display,
                    "email": clean_email,
                    "password_hash": "external-auth",
                    "is_active": True,
                    "is_admin": bool(is_admin),
                }
            )
            session.add(user)
        else:
            user.username = clean_username or user.username
            user.display_name = clean_display
            user.email = clean_email
            user.is_admin = bool(user.is_admin or is_admin)
            session.add(user)

        session.commit()
        session.refresh(user)
        return user


def list_users() -> list[User]:
    with get_session() as session:
        return list(session.exec(select(User).order_by(User.username)).all())


def list_changes(limit: int = 200) -> list[Any]:
    if ModelChangeLog is None:
        return []
    with get_session() as session:
        statement = select(ModelChangeLog).order_by(ModelChangeLog.changed_at.desc()).limit(limit)
        return list(session.exec(statement).all())


def create_user(
    *,
    username: str,
    display_name: str | None,
    email: str | None,
    is_admin: bool,
    actor_id: str | None,
) -> User:
    with get_session() as session:
        _set_session_actor(session, actor_id)

        existing = session.exec(select(User).where(User.username == username)).first()
        if existing:
            raise ValueError("Username already exists")

        user = User.model_validate(
            {
                "username": username.strip(),
                "password_hash": "external-auth",
                "display_name": (display_name or "").strip() or None,
                "email": (email or "").strip() or None,
                "is_active": True,
                "is_admin": is_admin,
            }
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def update_user(
    *,
    user_id: str,
    display_name: str | None,
    email: str | None,
    is_active: bool,
    is_admin: bool,
    actor_id: str | None,
) -> User:
    with get_session() as session:
        _set_session_actor(session, actor_id)

        user = session.get(User, user_id)
        if user is None:
            raise ValueError("User not found")

        user.display_name = (display_name or "").strip() or None
        user.email = (email or "").strip() or None
        user.is_active = is_active
        user.is_admin = is_admin

        session.add(user)
        session.commit()
        session.refresh(user)
        return user
