"""Shared identity and fill-blank helpers for authority files (GND, Wikidata)."""

from __future__ import annotations

import unicodedata
import uuid

from sqlmodel import Session, col, select

from backend.date_precision import DatePrecision
from backend.models import EntitySource, EntityType, Person, Source, Text, TextTitle
from backend.reconciliation import normalize, resolve_id


def external_source(session: Session, name: str, base_url: str) -> Source:
    """Return the named authority source, creating it on first use."""
    source = session.exec(select(Source).where(col(Source.name).ilike(name))).first()
    if source is None:
        source = Source(name=name, base_url=base_url)
        session.add(source)
        session.flush()
    return source


def linked_entity(session: Session, source: Source, kind: EntityType, external_id: str) -> uuid.UUID | None:
    """The canonical entity that carries this authority identity, if any."""
    links = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source.id,
            EntitySource.entity_type == kind,
            EntitySource.external_id == external_id,
        )
    ).all()
    ids = {resolve_id(session, kind, link.entity_id) for link in links}
    if len(ids) > 1:
        raise ValueError(f"Conflicting {source.name} identity: {external_id}")
    return next(iter(ids), None)


def entity_link(session: Session, source: Source, kind: EntityType, entity_id: uuid.UUID) -> EntitySource | None:
    """The source record already selected for an entity, if any."""
    return session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source.id,
            EntitySource.entity_type == kind,
            EntitySource.entity_id == entity_id,
        )
    ).first()


def identity(session: Session, source_name: str, kind: EntityType, entity_id: uuid.UUID) -> str | None:
    """Read-only lookup of an entity's authority ID; never creates the source row."""
    source = session.exec(select(Source).where(col(Source.name).ilike(source_name))).first()
    link = entity_link(session, source, kind, entity_id) if source else None
    return link.external_id if link else None


def owner(session: Session, source_name: str, kind: EntityType, external_id: str) -> uuid.UUID | None:
    """Read-only: the entity already carrying this authority ID."""
    source = session.exec(select(Source).where(col(Source.name).ilike(source_name))).first()
    return linked_entity(session, source, kind, external_id) if source else None


def attach_identity(
    session: Session, source: Source, kind: EntityType, entity_id: uuid.UUID, external_id: str, url: str
) -> None:
    """Record one reviewed authority identity per entity; an identity never belongs to two entities."""
    current = linked_entity(session, source, kind, external_id)
    if current is not None and current != entity_id:
        raise ValueError(f"{source.name} item {external_id} is already linked to another {kind}")
    if link := entity_link(session, source, kind, entity_id):
        if link.external_id != external_id:
            raise ValueError(f"Entity already has a different {source.name} identity")
        return
    session.add(
        EntitySource(
            source_id=source.id,
            entity_type=kind,
            entity_id=entity_id,
            external_id=external_id,
            external_url=url,
            reviewed=True,
        )
    )
    session.flush()


_ORDER = list(DatePrecision)


def _refines(entity: object, field: str, value: object, precision: DatePrecision | None) -> bool:
    """A finer date agreeing with the stored coarser one (1749 -> 1749-08-28) adds detail, not a conflict."""
    current, stored = getattr(entity, field), getattr(entity, f"{field}_precision", None)
    if precision is None or stored is None or _ORDER.index(precision) <= _ORDER.index(stored):
        return False
    parts = {DatePrecision.YEAR: ("year",), DatePrecision.MONTH: ("year", "month")}.get(stored)
    return parts is not None and all(getattr(current, p) == getattr(value, p) for p in parts)


def fill(entity: object, field: str, value: object, report: dict, precision: DatePrecision | None = None) -> None:
    """Set blank fields (or refine a coarser agreeing date); a different value is reported, never overwritten."""
    if value is None:
        return
    current = getattr(entity, field)
    if current in (None, "", []) or _refines(entity, field, value, precision):
        setattr(entity, field, value)
        if precision is not None:
            setattr(entity, f"{field}_precision", precision)
        report["filled"].append(field)
    elif current != value:
        report["conflicts"].append({"field": field, "current": str(current), "authority": str(value)})


def latin(value: str) -> bool:
    """True if every letter is Latin script."""
    return all(not char.isalpha() or "LATIN" in unicodedata.name(char, "") for char in value)


def fold(value: str) -> str:
    """Casefold and strip diacritics for loose surname comparison."""
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


def same_name_person(session: Session, name: str) -> Person | None:
    """A local person with exactly this normalized name; a match is a review hint, not identity."""
    key = normalize(name)
    return next(
        (p for p in session.exec(select(Person).where(col(Person.name).ilike(name))) if normalize(p.name) == key), None
    )


def same_title_text(session: Session, title: str) -> Text | None:
    """A local text carrying this normalized title; a match is a review hint, not identity."""
    row = session.exec(select(TextTitle).where(TextTitle.normalized == normalize(title))).first()
    return session.get(Text, row.text_id) if row else None
