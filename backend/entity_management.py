from __future__ import annotations

from typing import Any, TypeVar, cast

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import InstrumentedAttribute
from sqlmodel import Session, SQLModel, func, select

from backend.scd2 import add_scd2_event
from backend.user_management import get_session

from .models import EntitySource

ModelT = TypeVar("ModelT", bound=SQLModel)


def _snapshot[ModelT: SQLModel](entity: ModelT) -> dict[str, Any]:
    """Create a JSON-serializable snapshot of the entity's current state."""
    return entity.model_dump(mode="json")


def _entity_identity(entity: SQLModel) -> tuple[str, str]:
    """Create a stable identity tuple for the entity based on its class name and id."""
    data = entity.model_dump(mode="python")
    return (entity.__class__.__name__, str(data.get("id", id(entity))))


def _serialize_entity_graph(
    entity: SQLModel,
    *,
    seen: set[tuple[str, str]] | None = None,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Serialize entity plus attached relationships into JSON-safe nested dict."""
    seen = seen or set()
    ident = _entity_identity(entity)
    data = entity.model_dump(mode="json")
    if ident in seen or max_depth <= 0:
        return data

    seen.add(ident)
    mapper = sa_inspect(entity.__class__)
    for rel in mapper.relationships:
        rel_value = getattr(entity, rel.key)
        if rel_value is None:
            data[rel.key] = None
            continue
        if rel.uselist:
            data[rel.key] = [
                _serialize_entity_graph(child, seen=seen.copy(), max_depth=max_depth - 1) for child in rel_value
            ]
            continue
        data[rel.key] = _serialize_entity_graph(rel_value, seen=seen.copy(), max_depth=max_depth - 1)

    if hasattr(entity, "sources"):
        data["sources"] = [_snapshot(source) for source in entity.sources]
    return data


def _resolve_order_by[ModelT: SQLModel](
    model_cls: type[ModelT], order_by: str | InstrumentedAttribute | None = None
) -> InstrumentedAttribute | None:
    """Resolve `order_by` into a SQLModel column/expression.

    Accepts either a SQLAlchemy column/expression or a field name string.
    Falls back to `name`, then `title`, then `id` when field is missing.
    """
    if order_by is not None and not isinstance(order_by, str):
        return cast(InstrumentedAttribute[Any], order_by)
    candidates = ([order_by] if order_by is not None else []) + ["name", "title", "id"]
    for field in candidates:
        value = getattr(model_cls, field, None)
        if value is not None:
            return cast(InstrumentedAttribute[Any], value)
    return None


def list_entities[ModelT: SQLModel](model_cls: type[ModelT], order_by: str | None = None) -> list[ModelT]:
    """List all entities of the given type, optionally ordered by a specified field."""
    with get_session() as session:
        stmt = select(model_cls)
        order_expr = _resolve_order_by(model_cls, order_by)
        if order_expr is not None:
            stmt = stmt.order_by(order_expr)
        return list(session.exec(stmt).all())


def list_entities_where[ModelT: SQLModel](
    model_cls: type[ModelT],
    *,
    filter_field: str,
    filter_value: Any,  # noqa: ANN401
    order_by: str | None = "id",
) -> list[ModelT]:
    """List entities of the given type filtered by a field value, optionally ordered."""
    with get_session() as session:
        col = getattr(model_cls, filter_field)
        stmt = select(model_cls).where(col == filter_value)
        order_expr = _resolve_order_by(model_cls, order_by)
        if order_expr is not None:
            stmt = stmt.order_by(order_expr)
        return list(session.exec(stmt).all())


def get_unique_field_values[ModelT: SQLModel](model_cls: type[ModelT], field_name: str) -> list[Any]:
    """Get a list of unique values for a specified field across all entities of the given type."""
    with get_session() as session:
        col = getattr(model_cls, field_name)
        values = session.exec(select(col).where(col.isnot(None))).all()
        unique: set[Any] = set()
    for value in values:
        if isinstance(value, list):
            unique.update(v for v in value if v is not None)
        else:
            unique.add(value)
    return sorted(unique, key=str)


def get_entity_graph[ModelT: SQLModel](
    model_cls: type[ModelT], entity_id: str, *, max_depth: int = 3
) -> dict[str, Any]:
    """Fetch one entity by id with nested relationship data."""
    with get_session() as session:
        entity = session.get(model_cls, entity_id)
        if entity is None:
            raise ValueError(f"{model_cls.__name__} not found")
        return _serialize_entity_graph(entity, max_depth=max_depth)


def create_entity[ModelT: SQLModel](
    model_cls: type[ModelT],
    *,
    payload: dict[str, Any],
    actor_id: str | None,
    source_links: list[dict] | None = None,
    get_if_exists: dict[str, Any] | None = None,
) -> ModelT:
    """Create a new entity, optionally checking for existing one by unique fields first.

    Args:
        model_cls: The SQLModel class of the entity to create.
        entity_name: A human-readable name for the entity type, used in audit logs.
        payload: The data for the new entity.
        actor_id: The ID of the user or system creating the entity, for audit logs.
        source_links: Optional list of source link dicts to attach to the entity.
        get_if_exists: Optional dict of field names and values to check for existing entity before creating a new one.

    Returns:
        The created (or existing) entity instance.
    """
    with get_session() as session:
        if get_if_exists:
            stmt = select(model_cls)
            for key, value in get_if_exists.items():
                stmt = stmt.where(getattr(model_cls, key) == value)
            existing = session.exec(stmt.limit(1)).first()
            if existing is not None:
                _attach_missing_sources(session, model_cls, existing.id, source_links)
                session.commit()
                session.refresh(existing)
                return existing

        entity = model_cls.model_validate(payload)
        session.add(entity)
        session.flush()

        _attach_missing_sources(session, model_cls, entity.id, source_links)

        add_scd2_event(
            session,
            entity_name=model_cls.__name__.lower(),
            entity_id=str(entity.id),
            operation="insert",
            snapshot=_snapshot(entity),
            actor_id=actor_id,
        )
        session.commit()
        session.refresh(entity)
        return entity


def _attach_missing_sources[ModelT: SQLModel](
    session: Session,
    model_cls: type[ModelT],
    entity_id: str,
    source_links: list[dict] | None,
) -> None:
    """Attach source links that don't already exist on the entity."""
    for link in source_links or []:
        already_exists = session.exec(
            select(EntitySource).where(
                EntitySource.entity_type == model_cls._entity_type,
                EntitySource.entity_id == entity_id,
                EntitySource.source_id == link["source_id"],
            )
        ).first()

        if already_exists:
            continue

        session.add(
            EntitySource(
                source_id=link["source_id"],
                entity_type=model_cls._entity_type,
                entity_id=entity_id,
                external_id=link.get("external_id"),
                external_url=link.get("external_url"),
                external_title=link.get("external_title"),
            )
        )


def update_entity[ModelT: SQLModel](
    model_cls: type[ModelT],
    *,
    entity_id: str,
    payload: dict[str, Any],
    actor_id: str | None,
) -> ModelT:
    """Update an existing entity by id, recording the operation in SCD2 history."""
    with get_session() as session:
        entity = session.get(model_cls, entity_id)
        if entity is None:
            raise ValueError(f"{model_cls.__name__} not found")

        for key, value in payload.items():
            setattr(entity, key, value)
        session.add(entity)

        add_scd2_event(
            session,
            entity_name=model_cls.__name__.lower(),
            entity_id=str(entity.id),
            operation="update",
            snapshot=_snapshot(entity),
            actor_id=actor_id,
        )
        session.commit()
        session.refresh(entity)
        return entity


def delete_entity[ModelT: SQLModel](
    model_cls: type[ModelT],
    *,
    entity_id: str,
    actor_id: str | None,
) -> None:
    """Delete an entity by id, recording the operation in SCD2 history."""
    with get_session() as session:
        entity = session.get(model_cls, entity_id)
        if entity is None:
            raise ValueError(f"{model_cls.__name__} not found")

        add_scd2_event(
            session,
            entity_name=model_cls.__name__.lower(),
            entity_id=str(entity.id),
            operation="delete",
            snapshot=_snapshot(entity),
            actor_id=actor_id,
        )
        session.delete(entity)
        session.commit()


def count_entities[ModelT: SQLModel](model_cls: type[ModelT]) -> int:
    """Count the number of entities of a given type."""
    with get_session() as session:
        return session.exec(select(func.count()).select_from(model_cls)).one()
