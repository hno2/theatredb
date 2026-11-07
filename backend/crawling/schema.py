"""Initialize a missing queue schema without overwriting existing queue data."""

import logging

import psycopg
from procrastinate.schema import SchemaManager
from sqlalchemy import text
from sqlalchemy.engine import Engine

LOG = logging.getLogger(__name__)


def ensure_queue_schema(engine: Engine) -> None:
    """Create fresh PostgreSQL queue objects atomically; reject partial installations."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext('theaterdb.queue-schema'))")
        )
        jobs, workers, prune = connection.execute(
            text(
                "SELECT to_regclass('procrastinate_jobs'), to_regclass('procrastinate_workers'), "
                "to_regprocedure('procrastinate_prune_stalled_workers_v1(double precision)')"
            )
        ).one()
        if jobs and workers and prune:
            return
        existing = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relnamespace = current_schema()::regnamespace "
                "AND starts_with(relname, 'procrastinate_')) "
                "OR EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = current_schema()::regnamespace "
                "AND starts_with(proname, 'procrastinate_')) "
                "OR EXISTS (SELECT 1 FROM pg_type WHERE typnamespace = current_schema()::regnamespace "
                "AND starts_with(typname, 'procrastinate_'))"
            )
        ).scalar_one()
        if existing:
            raise RuntimeError(
                "Procrastinate queue schema is incomplete or incompatible with the installed package. "
                "Apply the package's documented schema migrations; existing queue data was not modified."
            )
        driver = connection.connection.driver_connection
        if not isinstance(driver, psycopg.Connection):
            raise TypeError("Queue schema initialization requires PostgreSQL with the psycopg driver")
        LOG.info("Initializing missing Procrastinate queue schema")
        with driver.cursor() as cursor:
            cursor.execute(SchemaManager.get_schema())
