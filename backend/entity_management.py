from __future__ import annotations

from typing import Any, TypeVar, cast
from uuid import UUID

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import InstrumentedAttribute, Mapper, raiseload, selectinload
from sqlmodel import Session, SQLModel, col, func, or_, select

from backend.reconciliation import resolve_id
from backend.scd2 import add_scd2_event
from backend.user_management import get_session

from .models import (
    EntitySource,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextContribution,
    TextTitle,
)

ModelT = TypeVar("ModelT", bound=SQLModel)


def _snapshot[ModelT: SQLModel](entity: ModelT) -> dict[str, Any]:
    """Create a JSON-serializable snapshot of the entity's current state."""
    return entity.model_dump(mode="json")


def _entity_identity(entity: SQLModel) -> tuple[str, str]:
    """Create a stable identity tuple for the entity based on its class name and id."""
    data = entity.model_dump(mode="python")
    return (entity.__class__.__name__, str(data.get("id", id(entity))))


def _entity_id(entity: SQLModel) -> str:
    return str(entity.model_dump(mode="python")["id"])


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
    mapper = cast(Mapper[Any], sa_inspect(entity.__class__))
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
    model_cls: type[ModelT], order_by: str | InstrumentedAttribute[Any] | None = None
) -> InstrumentedAttribute[Any] | None:
    """Resolve `order_by` into a SQLModel column/expression.

    Accepts either a SQLAlchemy column/expression or a field name string.
    Falls back to `name`, then `title`, then `id` when field is missing.
    """
    if order_by is not None and not isinstance(order_by, str):
        return order_by
    candidates = ([order_by] if order_by is not None else []) + ["name", "title", "id"]
    for field in candidates:
        value = getattr(model_cls, field, None)
        if value is not None:
            return cast(InstrumentedAttribute[Any], value)
    return None


def list_entities[ModelT: SQLModel](
    model_cls: type[ModelT], order_by: str | InstrumentedAttribute[Any] | None = None
) -> list[ModelT]:
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
    filter_value: Any,  # ruff: ignore[any-type]
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


def entity_page[ModelT: SQLModel](
    model_cls: type[ModelT], search: str, *, limit: int, offset: int
) -> tuple[list[ModelT], int]:
    """Search before pagination, with IDs as deterministic sort tie-breakers."""
    identifier = _resolve_order_by(model_cls, "id")
    label = _resolve_order_by(model_cls)
    if identifier is None or label is None:
        raise ValueError(f"{model_cls.__name__} cannot be listed")
    stmt = select(model_cls)
    entity_id = None
    if search := search.strip():
        try:
            entity_id = UUID(search)
        except ValueError:
            fields = (
                [label, col(Organization.city), col(Organization.country)] if model_cls is Organization else [label]
            )
            filters = [field.icontains(search, autoescape=True) for field in fields]
            if model_cls is Text:
                filters.append(
                    identifier.in_(
                        select(TextTitle.text_id).where(col(TextTitle.title).icontains(search, autoescape=True))
                    )
                )
            stmt = stmt.where(or_(*filters))
    with get_session() as session:
        if entity_id is not None:
            if model_cls is Person:
                entity_id = resolve_id(session, EntityType.PERSON, entity_id)
            stmt = stmt.where(identifier == entity_id)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        return list(session.exec(stmt.order_by(label, identifier).limit(limit).offset(offset)).all()), total


def entities_by_ids[ModelT: SQLModel](model_cls: type[ModelT], ids: set[UUID]) -> list[ModelT]:
    """Load only the rows referenced by the current page."""
    identifier = _resolve_order_by(model_cls, "id")
    if not ids or identifier is None:
        return []
    with get_session() as session:
        return list(session.exec(select(model_cls).where(identifier.in_(ids))).all())


def linked_sources(session: Session, kind: EntityType, entity_id: UUID) -> list[tuple[EntitySource, str | None]]:
    """Load just the entity's source links and names using the entity-source index."""
    stmt = (
        select(EntitySource, Source.name)
        .outerjoin(Source, col(EntitySource.source_id) == col(Source.id))
        .where(EntitySource.entity_type == kind, EntitySource.entity_id == entity_id)
        .order_by(col(EntitySource.sourced_at))
    )
    return [(link, name) for link, name in session.exec(stmt)]


def organization_coverage() -> tuple[int, int, int, list[tuple[str, str, int]]]:
    """Aggregate directory and map coverage without loading organization records."""
    city = func.trim(func.coalesce(Organization.city, ""))
    country = func.trim(func.coalesce(Organization.country, ""))
    mapped = col(Organization.lat).between(-90, 90) & col(Organization.lon).between(-180, 180)
    locations = (
        select(city.label("city"), country.label("country"), func.count().label("entries"))
        .select_from(Organization)
        .where(func.length(city) > 0)
        .group_by(city, country)
        .subquery()
    )
    with get_session() as session:
        total, positioned, places = session.exec(
            select(
                func.count(), func.count().filter(mapped), select(func.count()).select_from(locations).scalar_subquery()
            ).select_from(Organization)
        ).one()
        top = [
            (city_name, country_name, entries)
            for city_name, country_name, entries in session.exec(
                select(locations.c.city, locations.c.country, locations.c.entries)
                .order_by(locations.c.entries.desc(), locations.c.city, locations.c.country)
                .limit(5)
            )
        ]
    return total, positioned, places, top


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
        resolved_id = UUID(entity_id)
        if hasattr(model_cls, "_entity_type"):
            resolved_id = resolve_id(session, model_cls._entity_type, resolved_id)
        entity = session.get(model_cls, resolved_id)
        if entity is None:
            raise ValueError(f"{model_cls.__name__} not found")
        return _serialize_entity_graph(entity, max_depth=max_depth)


def get_people_productions(person_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch distinct productions and credits for people, without recursive graphs."""
    if not person_ids:
        return []
    ids = [UUID(person_id) for person_id in person_ids]
    with get_session() as session:
        matching = select(ProductionMembership.production_id).where(col(ProductionMembership.person_id).in_(ids))
        productions = session.exec(
            select(Production)
            .where(col(Production.id).in_(matching))
            .options(
                selectinload(cast(InstrumentedAttribute, Production.organization)),
                selectinload(cast(InstrumentedAttribute, Production.memberships)).selectinload(
                    cast(InstrumentedAttribute, ProductionMembership.person)
                ),
            )
            .order_by(col(Production.name), col(Production.id))
        ).all()
        return [
            {
                **_snapshot(production),
                "organization": _snapshot(production.organization) if production.organization else None,
                "memberships": [
                    {**_snapshot(membership), "person": _snapshot(membership.person)}
                    for membership in production.memberships
                ],
            }
            for production in productions
        ]


def get_organization_profile(entity_id: str) -> dict[str, Any]:
    """Load theatre display/filter data in batches, without following reverse relationships."""
    with get_session() as session:
        resolved_id = resolve_id(session, EntityType.ORGANIZATION, UUID(entity_id))
        productions = selectinload(cast(InstrumentedAttribute, Organization.productions))
        organization = session.exec(
            select(Organization)
            .where(Organization.id == resolved_id)
            .options(
                raiseload("*"),
                productions.selectinload(cast(InstrumentedAttribute, Production.memberships)).joinedload(
                    cast(InstrumentedAttribute, ProductionMembership.person)
                ),
                productions.joinedload(cast(InstrumentedAttribute, Production.text))
                .selectinload(cast(InstrumentedAttribute, Text.contributions))
                .joinedload(cast(InstrumentedAttribute, TextContribution.person)),
                selectinload(cast(InstrumentedAttribute, Organization.memberships)).joinedload(
                    cast(InstrumentedAttribute, OrganizationMembership.person)
                ),
            )
        ).one_or_none()
        if organization is None:
            raise ValueError("Organization not found")
        entries = []
        for production in organization.productions:
            text = production.text
            entries.append(
                {
                    **_snapshot(production),
                    "memberships": [
                        {**_snapshot(membership), "person": _snapshot(membership.person)}
                        for membership in production.memberships
                    ],
                    "text": {
                        **_snapshot(text),
                        "contributions": [
                            {**_snapshot(contribution), "person": _snapshot(contribution.person)}
                            for contribution in text.contributions
                        ],
                    }
                    if text
                    else None,
                }
            )
        return {
            **_snapshot(organization),
            "productions": entries,
            "memberships": [
                {**_snapshot(membership), "person": _snapshot(membership.person)}
                for membership in organization.memberships
            ],
            "sources": [_snapshot(source) for source in organization.sources],
        }


def browse_entities(
    model_cls: type[Organization | Production | Person],
    query: str = "",
    *,
    limit: int = 12,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch a bounded discovery page with theatre context and a matching count."""
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Invalid discovery page bounds")
    stmt = select(model_cls)
    if model_cls is Production:
        stmt = stmt.outerjoin(Organization)
    query = query.strip()
    if query:
        filters = [col(model_cls.name).icontains(query, autoescape=True)]
        if model_cls is Organization:
            filters.extend(
                field.icontains(query, autoescape=True) for field in (col(Organization.city), col(Organization.country))
            )
        elif model_cls is Production:
            filters.extend(
                field.icontains(query, autoescape=True)
                for field in (col(Organization.name), col(Organization.city), col(Organization.country))
            )
        stmt = stmt.where(or_(*filters))
    with get_session() as session:
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        if model_cls is Production:
            stmt = stmt.options(selectinload(cast(InstrumentedAttribute, Production.organization))).order_by(
                col(Production.premiere).desc().nulls_last()
            )
        rows = session.exec(stmt.order_by(col(model_cls.name), col(model_cls.id)).offset(offset).limit(limit)).all()
        entries = []
        for row in rows:
            entry = _snapshot(row)
            if isinstance(row, Production):
                entry["organization"] = _snapshot(row.organization) if row.organization else None
            entries.append(entry)
        return entries, total


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
                _attach_missing_sources(session, model_cls, _entity_id(existing), source_links)
                session.commit()
                session.refresh(existing)
                return existing

        entity = model_cls.model_validate(payload)
        session.add(entity)
        session.flush()

        entity_id = _entity_id(entity)
        _attach_missing_sources(session, model_cls, entity_id, source_links)

        add_scd2_event(
            session,
            entity_name=model_cls.__name__.lower(),
            entity_id=entity_id,
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
        entity_type = getattr(model_cls, "_entity_type", None)
        if not isinstance(entity_type, EntityType):
            raise TypeError(f"{model_cls.__name__} cannot have sources attached")

        already_exists = session.exec(
            select(EntitySource).where(
                EntitySource.entity_type == entity_type,
                EntitySource.entity_id == entity_id,
                EntitySource.source_id == link["source_id"],
            )
        ).first()

        if already_exists:
            continue

        session.add(
            EntitySource(
                source_id=link["source_id"],
                entity_type=entity_type,
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
        entity = session.get(model_cls, UUID(entity_id))
        if entity is None:
            raise ValueError(f"{model_cls.__name__} not found")

        data = entity.model_dump(mode="python")
        for field in getattr(model_cls, "date_fields", ()):
            if field in payload and f"{field}_precision" not in payload:
                data.pop(f"{field}_precision", None)
        validated = model_cls.model_validate({**data, **payload})
        changed_fields = set(payload)
        changed_fields.update(
            f"{field}_precision" for field in getattr(model_cls, "date_fields", ()) if field in payload
        )
        for key in changed_fields:
            setattr(entity, key, getattr(validated, key))
        session.add(entity)

        add_scd2_event(
            session,
            entity_name=model_cls.__name__.lower(),
            entity_id=_entity_id(entity),
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
            entity_id=_entity_id(entity),
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


def count_entities_many(*model_classes: type[SQLModel]) -> dict[type[SQLModel], int]:
    """Count multiple entity types in one database round trip."""
    if not model_classes:
        return {}
    stmt = select(*(select(func.count()).select_from(model_cls).scalar_subquery() for model_cls in model_classes))
    with get_session() as session:
        values = session.execute(stmt).one()
    return dict(zip(model_classes, values, strict=True))
