from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from itertools import batched
from typing import Any, cast

from sqlalchemy import JSON, Column, insert, tuple_, update
from sqlalchemy.orm import InstrumentedAttribute
from sqlmodel import Field, Session, SQLModel, col, func, select


class SCD2History(SQLModel, table=True):
    """Table for recording SCD2 history of entities."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entity_name: str = Field(index=True)
    entity_id: str = Field(index=True)
    operation: str
    version: int = Field(index=True)
    valid_from: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC), index=True)
    valid_to: dt.datetime | None = Field(default=None, index=True)
    is_current: bool = Field(default=True, index=True)
    actor_id: str | None = Field(default=None, index=True)
    snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))


def add_scd2_events(
    session: Session,
    *,
    inserted: Sequence[SQLModel],
    updated: Sequence[SQLModel],
    actor_id: str | None,
    deleted: Sequence[SQLModel] = (),
) -> None:
    """Audit atomically, bounding history queries to stay below DB parameter limits."""
    now = dt.datetime.now(dt.UTC)
    versions = {}
    keys = [(type(row).__name__.lower(), str(row.model_dump()["id"])) for row in [*updated, *deleted]]
    identity = tuple_(SCD2History.entity_name, SCD2History.entity_id)
    for batch in batched(keys, 1000, strict=False):
        versions.update(
            {
                (name, entity_id): version
                for name, entity_id, version in session.exec(
                    select(SCD2History.entity_name, SCD2History.entity_id, func.max(SCD2History.version))
                    .where(identity.in_(batch))
                    .group_by(SCD2History.entity_name, SCD2History.entity_id)
                )
            }
        )
        session.execute(
            update(SCD2History)
            .where(identity.in_(batch), col(SCD2History.is_current))
            .values(is_current=False, valid_to=now),
            execution_options={"synchronize_session": False},
        )
    events = []
    for operation, rows in (("insert", inserted), ("update", updated), ("delete", deleted)):
        for row in rows:
            name, entity_id = type(row).__name__.lower(), str(row.model_dump()["id"])
            events.append(
                {
                    "entity_name": name,
                    "entity_id": entity_id,
                    "operation": operation,
                    "version": versions.get((name, entity_id), 0) + 1,
                    "valid_from": now,
                    "valid_to": now if operation == "delete" else None,
                    "is_current": operation != "delete",
                    "actor_id": actor_id,
                    "snapshot": row.model_dump(mode="json"),
                }
            )
    if events:
        session.execute(insert(SCD2History), events)


def add_scd2_event(
    session: Session,
    *,
    entity_name: str,
    entity_id: str,
    operation: str,
    snapshot: dict[str, Any],
    actor_id: str | None,
) -> SCD2History:
    """Add an SCD2 event for an entity operation."""
    now = dt.datetime.now(dt.UTC)

    previous = session.exec(
        select(SCD2History).where(
            SCD2History.entity_name == entity_name,
            SCD2History.entity_id == entity_id,
            SCD2History.is_current,
        )
    ).all()
    for row in previous:
        row.is_current = False
        row.valid_to = now
        session.add(row)

    latest = session.exec(
        select(SCD2History)
        .where(SCD2History.entity_name == entity_name, SCD2History.entity_id == entity_id)
        .order_by(cast(InstrumentedAttribute[int], SCD2History.version).desc())
        .limit(1)
    ).first()
    next_version = (latest.version if latest else 0) + 1

    event = SCD2History(
        entity_name=entity_name,
        entity_id=entity_id,
        operation=operation,
        version=next_version,
        valid_from=now,
        valid_to=None if operation != "delete" else now,
        is_current=operation != "delete",
        actor_id=actor_id,
        snapshot=snapshot,
    )
    session.add(event)
    return event
