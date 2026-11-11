"""Alembic environment bound to the application's database and SQLModel metadata."""

from alembic import context
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel

from backend import user_management

QUEUE_PREFIX = "procrastinate_"


def include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """Leave Procrastinate queue objects to its own schema manager."""
    return not (name or "").startswith(QUEUE_PREFIX)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=user_management.DATABASE_URL,
        target_metadata=SQLModel.metadata,
        include_object=include_object,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
elif (connection := context.config.attributes.get("connection")) is not None:
    _run(connection)
else:
    with user_management._get_engine().connect() as connection:
        _run(connection)
        connection.commit()
