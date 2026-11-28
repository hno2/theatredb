"""Session-bound persistence shared by import and reconciliation commands."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal, get_origin

from sqlmodel import Session, SQLModel, select

from backend.models import EntityRedirect, EntitySource, EntityType, SourceRecord
from backend.scd2 import add_scd2_event, add_scd2_events


def entity_snapshot(entity: SQLModel) -> dict[str, Any]:
    """Serialize a row without its ORM relationships."""
    return entity.model_dump(mode="json")


def revalidate[Model: SQLModel](entity: Model, changes: dict[str, Any]) -> Model:
    """Validate changes, treating legacy NULL list columns as empty lists."""
    model: type[Model] = type(entity)
    if changes.keys() - model.model_fields.keys():
        raise ValueError("Unknown entity fields")
    data = {**entity_snapshot(entity), **changes}
    for name, field in model.model_fields.items():
        if data.get(name) is None and get_origin(field.annotation) is list:
            data[name] = []
    return model.model_validate(data)


def audit_event(session: Session, entity: SQLModel, operation: str, actor_id: str | None) -> None:
    """Compatibility entry point for a single history event."""
    add_scd2_event(
        session,
        entity_name=type(entity).__name__.lower(),
        entity_id=str(entity.model_dump()["id"]),
        operation=operation,
        snapshot=entity_snapshot(entity),
        actor_id=actor_id,
    )


class EntityWriter:
    """Validate row writes and audit their final state without owning the transaction.

    Domain commands call ``finish`` after relationships and model hooks have run.
    Repeated writes to a row produce one event; updates returning to their initial
    state produce none. Failures propagate to the transaction owner.
    """

    def __init__(self, session: Session, actor_id: str | None) -> None:
        """Bind persistence and history to the transaction owner's session."""
        self.session = session
        self.actor_id = actor_id
        self._events: dict[
            tuple[type[SQLModel], str], tuple[SQLModel, Literal["insert", "update", "delete"], dict | None]
        ] = {}

    def track(self, entity: SQLModel) -> None:
        """Capture a row before a domain helper changes it directly."""
        key = (type(entity), str(entity.model_dump()["id"]))
        self._events.setdefault(key, (entity, "update", entity_snapshot(entity)))

    def record(self, entity: SQLModel, operation: Literal["insert", "update", "delete"]) -> None:
        """Include an already-applied domain mutation in this command's history."""
        key = (type(entity), str(entity.model_dump()["id"]))
        previous = self._events.get(key)
        if previous and previous[1] == "insert":
            if operation == "delete":
                del self._events[key]
            return
        self._events[key] = (entity, operation, previous[2] if previous else None)

    def create[Model: SQLModel](self, model: type[Model], values: dict[str, Any]) -> Model:
        """Validate and insert a row; history waits for the complete command."""
        entity = model.model_validate(values)
        self.session.add(entity)
        self.record(entity, "insert")
        self.session.flush()
        return entity

    def update[Model: SQLModel](self, entity: Model, changes: dict[str, Any]) -> bool:
        """Apply validated fields; report whether their normalized values changed."""
        if "id" in changes:
            raise ValueError("Entity ID cannot be changed")
        before = entity_snapshot(entity)
        validated = revalidate(entity, changes)
        self.track(entity)
        for field in changes:
            setattr(entity, field, getattr(validated, field))
        self.session.add(entity)
        self.session.flush()
        return entity_snapshot(entity) != before

    def finish(self) -> None:
        """Flush model hooks, then write one final event per changed row."""
        self.session.flush()
        inserted, updated, deleted = [], [], []
        for entity, operation, before in self._events.values():
            if operation == "insert":
                inserted.append(entity)
            elif operation == "delete":
                deleted.append(entity)
            elif before is None or entity_snapshot(entity) != before:
                updated.append(entity)
        add_scd2_events(self.session, inserted=inserted, updated=updated, deleted=deleted, actor_id=self.actor_id)
        self._events.clear()


def resolve_id(session: Session, kind: EntityType | str, entity_id: uuid.UUID | str) -> uuid.UUID:
    """Follow persisted redirects, rejecting corrupt cycles."""
    kind, current = EntityType(kind), uuid.UUID(str(entity_id))
    seen = set()
    while current not in seen:
        seen.add(current)
        redirect = session.exec(
            select(EntityRedirect).where(EntityRedirect.entity_type == kind, EntityRedirect.old_id == current)
        ).first()
        if redirect is None:
            return current
        current = redirect.target_id
    raise ValueError("Entity redirect cycle")


@dataclass(frozen=True)
class SourceObservation:
    """Accepted evidence, independent of matching and overwrite policy."""

    kind: EntityType
    source_id: uuid.UUID
    record_key: str
    entity_id: uuid.UUID
    payload: dict
    captured_at: dt.datetime | None
    external_id: str | None = None
    external_url: str | None = None


def record_source(session: Session, observation: SourceObservation, *, reassign: bool = False) -> bool:
    """Persist evidence and a reviewed link; return whether the link is new.

    Reconciliation requires a proven identity. Theatre snapshots may explicitly
    reassign a page to a reviewed production. Neither path can regress timestamps
    or erase archived merge evidence.
    """
    if not observation.record_key:
        raise ValueError("Source record key is required")
    captured_at = observation.captured_at
    if captured_at is not None:
        if captured_at.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        captured_at = captured_at.astimezone(dt.UTC)
    record = session.exec(
        select(SourceRecord).where(
            SourceRecord.source_id == observation.source_id,
            SourceRecord.entity_type == observation.kind,
            SourceRecord.record_key == observation.record_key,
        )
    ).first()
    if record is None:
        record = SourceRecord(
            source_id=observation.source_id,
            entity_type=observation.kind,
            record_key=observation.record_key,
            entity_id=observation.entity_id,
        )
    elif resolve_id(session, observation.kind, record.entity_id) != observation.entity_id and not reassign:
        raise ValueError("Source identity belongs to another entity")
    stale = record.captured_at is not None and (
        captured_at is None or record.captured_at.replace(tzinfo=dt.UTC) > captured_at
    )
    if not stale:
        record.entity_id = observation.entity_id
        record.external_id = observation.external_id or record.external_id
        record.external_url = observation.external_url
        record.payload = {**record.payload, **observation.payload}
        record.captured_at = captured_at
        session.add(record)
    link = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == observation.source_id,
            EntitySource.entity_type == observation.kind,
            EntitySource.entity_id == observation.entity_id,
        )
    ).first()
    created = link is None
    if link is None:
        link = EntitySource(
            source_id=observation.source_id,
            entity_type=observation.kind,
            entity_id=observation.entity_id,
            external_id=observation.external_id,
            external_url=observation.external_url,
        )
    link.reviewed = True
    if not stale:
        link.external_id = link.external_id or observation.external_id
        link.external_url = link.external_url or observation.external_url
        if captured_at and (created or link.sourced_at.replace(tzinfo=dt.UTC) <= captured_at):
            link.sourced_at = captured_at
    session.add(link)
    return created
