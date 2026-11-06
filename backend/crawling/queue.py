"""Procrastinate integration; enqueue/cancel share the application's transaction."""

from functools import lru_cache

import procrastinate
import psycopg
from sqlmodel import Session

from backend.models import CrawlRun
from backend.user_management import DATABASE_URL

TASK_NAME = "theaterdb.crawl"


@lru_cache(maxsize=1)
def app() -> procrastinate.App:
    """Create a lazy queue client; external connections do not need a pool."""
    url = DATABASE_URL.set(drivername="postgresql").render_as_string(hide_password=False)
    return procrastinate.App(connector=procrastinate.SyncPsycopgConnector(conninfo=url))


@lru_cache(maxsize=1)
def worker_app() -> procrastinate.App:
    """Keep the async worker pool separate from synchronous UI transactions."""
    url = DATABASE_URL.set(drivername="postgresql").render_as_string(hide_password=False)
    return procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=url))


def _connection(session: Session) -> psycopg.Connection:
    connection = session.connection().connection.driver_connection
    if not isinstance(connection, psycopg.Connection):
        raise TypeError("Crawl jobs require PostgreSQL with the psycopg driver")
    return connection


def enqueue(session: Session, run: CrawlRun) -> int:
    """Insert a durable job atomically with its run; queue schema must exist."""
    return (
        app()
        .configure_task(
            TASK_NAME,
            connection=_connection(session),
            queueing_lock=f"source:{run.source_id}",
        )
        .defer(run_id=str(run.id))
    )


def cancel(session: Session, job_id: int) -> None:
    """Cancel queued work or interrupt an async running task after commit."""
    app().job_manager.cancel_job_by_id(job_id, abort=True, connection=_connection(session))
