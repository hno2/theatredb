"""Review-first reconciliation. Proposals never create or change domain entities."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Collection
from difflib import SequenceMatcher
from operator import itemgetter
from typing import Any, get_origin
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import String, cast, or_
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, SQLModel, col, select

from backend.models import (
    Award,
    AwardNomination,
    EntityRedirect,
    EntitySource,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
    TextLineage,
)
from backend.scd2 import add_scd2_event
from backend.user_management import get_session

ENTITY_MODELS = {
    EntityType.ORGANIZATION: Organization,
    EntityType.PERSON: Person,
    EntityType.PRODUCTION: Production,
    EntityType.TEXT: Text,
    EntityType.AWARD: Award,
}
type Entity = Organization | Person | Production | Text | Award
EMPTY = (None, "", [], {})
ROLE_ALIASES = {
    "regie": "director",
    "inszenierung": "director",
    "director": "director",
    "dramaturgie": "dramaturg",
    "bühne": "set_designer",
    "bühnenbild": "set_designer",
    "kostüm": "costume_designer",
    "kostüme": "costume_designer",
    "licht": "light_designer",
    "lighting_designer": "light_designer",
    "choreografie": "choreographer",
    "choreographie": "choreographer",
    "coreographer": "choreographer",
    "schauspiel": "actor",
    "schauspieler": "actor",
    "autor": "author",
    "creator": "author",
    "text": "author",
    "buch": "author",
    "übersetzung": "translator",
    "bearbeitung": "adapter",
    "fassung": "adapter",
    "musik": "composer",
    "komposition": "composer",
    "theaterpädagoge": "theatre_educator",
    "Vermittlung": "theatre_educator",
}


def normalize(value: str) -> str:
    """Normalize names without deleting non-Latin letters or significant words."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


def canonical_role(value: str | None) -> str | None:
    """Canonicalize known credit labels, preserving unknown roles."""
    if not value or not value.strip():
        return None
    value = value.strip().casefold()
    return ROLE_ALIASES.get(value, value)


def canonical_url(value: str) -> str:
    """Remove fragments and normalize scheme/host; preserve meaningful URL queries."""
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Invalid source URL: {value!r}")
    return urlunsplit(("https", parts.netloc.casefold(), parts.path.rstrip("/"), parts.query, ""))


def stable_digest(value: object) -> str:
    """Hash JSON-compatible values independent of key order."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def union_values(left: list, right: list) -> list:
    """Combine lists in order without duplicates, including unhashable items."""
    return list({stable_digest(value): value for value in [*left, *right]}.values())


def entity_snapshot(entity: SQLModel) -> dict[str, Any]:
    """Serialize an entity to JSON-compatible values."""
    return entity.model_dump(mode="json")


def revalidate[Model: SQLModel](entity: Model, changes: dict[str, Any]) -> Model:
    """Validate an existing row with changes; legacy NULL list columns count as empty lists."""
    model: type[Model] = type(entity)
    data = {**entity_snapshot(entity), **changes}
    for name, field in model.model_fields.items():
        if data.get(name) is None and get_origin(field.annotation) is list:
            data[name] = []
    return model.model_validate(data)


def _kind(value: EntityType | str) -> EntityType:
    return EntityType(value)


def resolve_id(session: Session, kind: EntityType | str, entity_id: uuid.UUID | str) -> uuid.UUID:
    """Follow persisted redirects, rejecting corrupt cycles."""
    kind, current = _kind(kind), uuid.UUID(str(entity_id))
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


def _get(session: Session, kind: EntityType, entity_id: uuid.UUID | str) -> Entity:
    entity = session.get(ENTITY_MODELS[kind], resolve_id(session, kind, entity_id), with_for_update=True)
    if entity is None:
        raise ValueError(f"{kind}: record no longer exists")
    return entity


def search_entities(kind: EntityType | str, query: str = "", *, limit: int = 50, offset: int = 0) -> list[dict]:
    """Search by name/title or UUID, returning IDs to disambiguate namesakes."""
    kind = _kind(kind)
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    with get_session() as session:
        stmt = select(model)
        if query.strip():
            try:
                entity_id = uuid.UUID(query.strip())
            except ValueError:
                stmt = stmt.where(label.icontains(query.strip(), autoescape=True))
            else:
                stmt = stmt.where(model.id == resolve_id(session, kind, entity_id))
        return [
            entity_snapshot(row)
            for row in session.exec(stmt.order_by(label, col(model.id)).offset(offset).limit(limit)).all()
        ]


def search_people_for_merge(query: str = "", *, limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch a people page and batch its production/theatre context without per-person queries."""
    people = search_entities(EntityType.PERSON, query, limit=limit, offset=offset)
    if not people:
        return []
    by_id = {uuid.UUID(person["id"]): person for person in people}
    for person in people:
        person["productions"] = []
    with get_session() as session:
        involvement = (
            select(ProductionMembership.person_id, ProductionMembership.production_id)
            .where(col(ProductionMembership.person_id).in_(by_id))
            .union(
                select(TextContribution.person_id, Production.id)
                .join(Production, col(Production.text_id) == col(TextContribution.text_id))
                .where(col(TextContribution.person_id).in_(by_id))
            )
            .subquery()
        )
        credits = session.exec(
            select(involvement.c.person_id, Production.name, Organization.name, Organization.city)
            .join(Production, col(Production.id) == involvement.c.production_id)
            .outerjoin(Organization, col(Organization.id) == col(Production.organization_id))
            .distinct()
            .order_by(Production.name, Organization.name, col(Organization.city))
        ).all()
        for person_id, production, theatre, city in credits:
            location = f"{theatre} ({city})" if theatre and city else theatre or "Theater nicht hinterlegt"
            by_id[person_id]["productions"].append(f"{production} · {location}")
    return people


def _source_matches(session: Session, kind: EntityType, source_id: uuid.UUID, context: dict) -> set[uuid.UUID]:
    keys = []
    if context.get("external_id"):
        keys.append(EntitySource.external_id == context["external_id"])
    if context.get("external_url"):
        url = context["external_url"]
        keys.append(col(EntitySource.external_url).in_([url, url + "/"]))
    matches = set()
    if keys:
        matches.update(
            session.exec(
                select(EntitySource.entity_id).where(
                    EntitySource.source_id == source_id, EntitySource.entity_type == kind, or_(*keys)
                )
            ).all()
        )
    record_filters = []
    if context.get("record_key"):
        record_filters.append(SourceRecord.record_key == context["record_key"])
    if context.get("external_id"):
        record_filters.append(SourceRecord.external_id == context["external_id"])
    if context.get("external_url"):
        record_filters.append(SourceRecord.external_url == context["external_url"])
    if record_filters:
        matches.update(
            session.exec(
                select(SourceRecord.entity_id).where(
                    SourceRecord.source_id == source_id,
                    SourceRecord.entity_type == kind,
                    or_(*record_filters),
                )
            ).all()
        )
    return {resolve_id(session, kind, value) for value in matches}


def _person_names(session: Session, production_id: uuid.UUID, role: str | None) -> set[str]:
    rows = session.exec(
        select(Person.name, ProductionMembership.role)
        .join(ProductionMembership, col(ProductionMembership.person_id) == col(Person.id))
        .where(ProductionMembership.production_id == production_id)
    ).all()
    return {normalize(name) for name, raw_role in rows if canonical_role(raw_role) == role}


def _authors(session: Session, text_id: uuid.UUID) -> set[str]:
    rows = session.exec(
        select(Person.name, TextContribution.role)
        .join(TextContribution, col(TextContribution.person_id) == col(Person.id))
        .where(TextContribution.text_id == text_id)
    ).all()
    return {normalize(name) for name, role in rows if canonical_role(role) == "author"}


def _candidate_evidence(
    session: Session, row: Entity, incoming: dict, context: dict
) -> tuple[list[str], list[str], bool]:
    evidence, conflicts = [], []
    strong = False
    title = incoming.get("title" if isinstance(row, Text) else "name", "")
    exact = normalize(title) == normalize(getattr(row, "title" if isinstance(row, Text) else "name"))
    if exact:
        evidence.append("Exakter normalisierter Titel/Name")
    if isinstance(row, Production):
        extra, conflicts, strong = _production_evidence(session, row, incoming, context, exact)
        evidence.extend(extra)
    elif isinstance(row, Text):
        authors = {normalize(name) for name in context.get("authors", [])}
        existing_authors = _authors(session, row.id)
        if authors and existing_authors:
            if authors == existing_authors:
                evidence.append("Gleiche Autoren")
                strong = exact
            else:
                conflicts.append("Abweichende Autoren")
    elif isinstance(row, Person):
        conflicts.extend(
            f"Abweichend: {field}"
            for field in ("born", "died")
            if incoming.get(field) and getattr(row, field) and str(incoming[field]) != str(getattr(row, field))
        )
    elif isinstance(row, Organization):
        conflicts.extend(
            f"Abweichend: {field}"
            for field in ("city", "country")
            if incoming.get(field)
            and getattr(row, field)
            and normalize(incoming[field]) != normalize(getattr(row, field))
        )
        if (
            incoming.get("url")
            and row.url
            and urlsplit(incoming["url"]).netloc.casefold() == urlsplit(row.url).netloc.casefold()
        ):
            evidence.append("Gleiche Website")
            strong = exact
    return evidence, conflicts, strong


def _production_evidence(
    session: Session,
    row: Production,
    incoming: dict,
    context: dict,
    exact: bool,
) -> tuple[list[str], list[str], bool]:
    evidence, conflicts = [], []
    organization_id = incoming.get("organization_id")
    same_org = bool(organization_id and str(row.organization_id) == str(organization_id))
    if same_org:
        evidence.append("Gleiche Organisation")
    elif organization_id and row.organization_id:
        conflicts.append("Andere Organisation")
    directors = {normalize(n) for n in context.get("directors", [])}
    existing_directors = _person_names(session, row.id, "director")
    same_directors = bool(directors and existing_directors and directors == existing_directors)
    if same_directors:
        evidence.append("Gleiche Regie")
    elif directors and existing_directors:
        conflicts.append("Abweichende Regie")
    for role, names in context.get("personnel", {}).items():
        overlap = {normalize(n) for n in names} & _person_names(session, row.id, canonical_role(role))
        if overlap:
            evidence.append(f"{role}: {', '.join(sorted(overlap))}")
    _premiere_evidence(row, incoming, evidence, conflicts)
    return evidence, conflicts, exact and same_org and same_directors


def _premiere_evidence(row: Production, incoming: dict, evidence: list[str], conflicts: list[str]) -> None:
    premiere = incoming.get("premiere")
    if premiere and row.premiere:
        if str(premiere)[:10] == row.premiere.date().isoformat():
            evidence.append("Gleiche Premiere")
        else:
            conflicts.append("Abweichende Premiere")
    elif (premiere or incoming.get("premiere_year")) and (row.premiere or row.premiere_year):
        incoming_year = int(str(premiere)[:4]) if premiere else incoming["premiere_year"]
        current_year = row.premiere.year if row.premiere else row.premiere_year
        if incoming_year == current_year:
            evidence.append("Gleiches Premierenjahr")
        else:
            conflicts.append("Abweichende Premiere")


def _candidates(
    session: Session, kind: EntityType, incoming: dict, context: dict, source_id: uuid.UUID | None
) -> list[dict]:
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    title = incoming.get("title" if kind == EntityType.TEXT else "name", "")
    if not title:
        raise ValueError("A name/title is required for matching")
    linked = _source_matches(session, kind, source_id, context) if source_id else set()
    stmt = select(model)
    # Query a bounded candidate pool. Missing organization must never enable automatic production matching.
    organization_id = incoming.get("organization_id")
    if kind == EntityType.PRODUCTION and organization_id:
        stmt = stmt.where(Production.organization_id == uuid.UUID(str(organization_id)))
    tokens = sorted(normalize(title).split(), key=len, reverse=True)[:3]
    filters = [label.icontains(token, autoescape=True) for token in tokens]
    if linked:
        filters.append(col(model.id).in_(linked))
    if filters:
        stmt = stmt.where(or_(*filters))
    rows = list(session.exec(stmt.order_by(label, col(model.id)).limit(200)).all())
    # Source identities must not disappear behind the name-search limit or organization filter.
    known_ids = {row.id for row in rows}
    rows.extend(_get(session, kind, identity) for identity in linked - known_ids)
    results = []
    for row in rows:
        name = getattr(row, "title" if kind == EntityType.TEXT else "name")
        similarity = SequenceMatcher(None, normalize(title), normalize(name)).ratio()
        if similarity < 0.55 and row.id not in linked:
            continue
        evidence, conflicts, strong = _candidate_evidence(session, row, incoming, context)
        if row.id in linked:
            evidence.append("Bereits verknüpfte Quellidentität")
        results.append(
            {
                "id": str(row.id),
                "label": name,
                "similarity": round(similarity, 4),
                "evidence": evidence,
                "conflicts": conflicts,
                "recommendation": "linked"
                if row.id in linked and not conflicts
                else "strong"
                if strong and not conflicts
                else "review",
                "record": entity_snapshot(row),
            }
        )
    results.sort(
        key=lambda c: (c["recommendation"] == "linked", c["recommendation"] == "strong", c["similarity"]), reverse=True
    )
    return results


def find_candidates(
    kind: EntityType | str, incoming: dict, *, context: dict | None = None, source_id: uuid.UUID | None = None
) -> list[dict]:
    """Explain possible matches; even strong recommendations require approval."""
    with get_session() as session:
        return _candidates(session, _kind(kind), incoming, context or {}, source_id)


def validate_payload(kind: EntityType, payload: dict, *, complete: bool = False) -> dict:
    """Validate entity fields and return their JSON-compatible representation."""
    model = ENTITY_MODELS[kind]
    unknown = set(payload) - (set(model.model_fields) - {"id"})
    if unknown:
        raise ValueError(f"Unknown or protected fields: {', '.join(sorted(unknown))}")
    label = "title" if kind == EntityType.TEXT else "name"
    if (complete or label in payload) and not str(payload.get(label) or "").strip():
        raise ValueError("A non-empty name/title is required")
    # Validation must also run on updates; SQLAlchemy attribute assignment does not validate values.
    data = model.model_validate(
        payload if complete else {"title" if kind == EntityType.TEXT else "name": "validation", **payload}
    )
    if isinstance(data, Production) and data.premiere and data.premiere.tzinfo is None:
        data.premiere = data.premiere.replace(tzinfo=dt.UTC)
    clean = data.model_dump(mode="json")
    return {field: clean[field] for field in payload}


def propose_record(
    kind: EntityType | str,
    incoming: dict,
    *,
    source_id: uuid.UUID,
    external_id: str | None = None,
    external_url: str | None = None,
    record_key: str | None = None,
    context: dict | None = None,
    captured_at: dt.datetime | None = None,
    actor_id: str | None = None,
    reason: str = "Quellimport",
) -> ReconciliationCase:
    """Queue an idempotent source proposal without creating people, texts or productions.

    Context may contain directors, personnel, authors, dependencies and credits.
    Dependencies reference other approved cases, never implicitly create entities.
    """
    kind = _kind(kind)
    incoming = validate_payload(kind, incoming, complete=True)
    external_url = canonical_url(external_url) if external_url else None
    record_key = record_key or (f"id:{external_id}" if external_id else external_url)
    if not record_key:
        raise ValueError("A stable source ID, URL or record_key is required")
    if captured_at and captured_at.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    if captured_at:
        captured_at = captured_at.astimezone(dt.UTC)
    context = {**(context or {}), "external_id": external_id, "external_url": external_url, "record_key": record_key}
    key = stable_digest([kind, str(source_id), record_key, incoming, context, captured_at])
    with get_session() as session:
        if session.get(Source, source_id) is None:
            raise ValueError("Source not found")
        existing = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
        if existing:
            return existing
        case = ReconciliationCase(
            key=key,
            entity_type=kind,
            operation="import",
            source_id=source_id,
            record_key=record_key,
            incoming=incoming,
            context=context,
            reason=reason,
            captured_at=captured_at,
            actor_id=actor_id,
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        return case


def queue_duplicate(
    kind: EntityType | str,
    target_id: uuid.UUID | str,
    duplicate_id: uuid.UUID | str,
    *,
    reason: str,
    actor_id: str | None = None,
) -> ReconciliationCase:
    """Submit an existing duplicate pair manually or programmatically for review."""
    kind = _kind(kind)
    if not reason.strip():
        raise ValueError("A review reason is required")
    with get_session() as session:
        target, duplicate = _get(session, kind, target_id), _get(session, kind, duplicate_id)
        if target.id == duplicate.id:
            raise ValueError("Select two different records")
        key = stable_digest(["duplicate", kind, sorted([str(target.id), str(duplicate.id)])])
        existing = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
        if existing:
            return existing
        case = ReconciliationCase(
            key=key,
            entity_type=kind,
            operation="merge",
            target_id=target.id,
            duplicate_id=duplicate.id,
            reason=reason.strip(),
            actor_id=actor_id,
            incoming={
                "title" if kind == EntityType.TEXT else "name": getattr(
                    duplicate, "title" if kind == EntityType.TEXT else "name"
                )
            },
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        return case


def discover_duplicates(kind: EntityType | str, query: str = "", *, limit: int = 50) -> list[dict]:
    """Return bounded, explained duplicate candidates without submitting or merging them."""
    kind = _kind(kind)
    results, seen = [], set()
    for record in search_entities(kind, query, limit=limit):
        context = {}
        with get_session() as session:
            if kind == EntityType.PRODUCTION:
                context["directors"] = sorted(_person_names(session, uuid.UUID(record["id"]), "director"))
            elif kind == EntityType.TEXT:
                context["authors"] = sorted(_authors(session, uuid.UUID(record["id"])))
        for candidate in find_candidates(kind, record, context=context):
            if candidate["id"] == record["id"]:
                continue
            pair = tuple(sorted([record["id"], candidate["id"]]))
            if pair in seen:
                continue
            seen.add(pair)
            results.append({"left": record, "right": candidate})
            if len(results) >= limit:
                return results
    return results


def list_cases(
    *,
    status: str = "pending",
    kind: EntityType | str | None = None,
    query: str = "",
    limit: int = 200,
    offset: int = 0,
    exclude_operations: Collection[str] = (),
) -> list[dict]:
    """List persistent review cases, including decisions for audit and follow-up."""
    with get_session() as session:
        stmt = select(ReconciliationCase).where(ReconciliationCase.status == status)
        if exclude_operations:
            stmt = stmt.where(col(ReconciliationCase.operation).not_in(list(exclude_operations)))
        if kind:
            stmt = stmt.where(ReconciliationCase.entity_type == _kind(kind))
        if query.strip():
            try:
                identity = uuid.UUID(query.strip())
            except ValueError:
                stmt = stmt.where(
                    or_(
                        col(ReconciliationCase.reason).icontains(query.strip(), autoescape=True),
                        cast(col(ReconciliationCase.incoming), String).icontains(query.strip(), autoescape=True),
                    )
                )
            else:
                stmt = stmt.where(ReconciliationCase.id == identity)
        return [
            entity_snapshot(case)
            for case in session.exec(
                stmt.order_by(col(ReconciliationCase.created_at), col(ReconciliationCase.id))
                .offset(offset)
                .limit(limit)
            ).all()
        ]


def _dependencies(session: Session, case: ReconciliationCase) -> tuple[dict, list[str]]:
    incoming, blocked = dict(case.incoming), []
    for dep in case.context.get("dependencies", []):
        related = session.get(ReconciliationCase, uuid.UUID(dep["case_id"]))
        if related is None or related.status != "approved" or related.target_id is None:
            blocked.append(dep["case_id"])
            continue
        if dep.get("field"):
            field = dep["field"]
            mapper = sa_inspect(ENTITY_MODELS[case.entity_type], raiseerr=True)
            foreign_key = next(iter(mapper.columns[field].foreign_keys), None)
            if foreign_key is None or foreign_key.column.table.name != ENTITY_MODELS[related.entity_type].__tablename__:
                raise ValueError("Dependency type does not match the target field")
            incoming[field] = str(resolve_id(session, related.entity_type, related.target_id))
    for credit in case.context.get("credits", []):
        related = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        if related is None or related.entity_type != EntityType.PERSON or related.status != "approved":
            blocked.append(credit["case_id"])
    return incoming, sorted(set(blocked))


def _references(session: Session, kind: EntityType, entity_id: uuid.UUID) -> list[tuple[type, str, list]]:
    target_table = sa_inspect(ENTITY_MODELS[kind], raiseerr=True).local_table
    refs = []
    # Inspect all registered domain FKs, including nominations, memberships and text lineage.
    for mapper in sa_inspect(Organization, raiseerr=True).registry.mappers:
        model = mapper.class_
        for column in mapper.columns:
            if any(fk.column.table is target_table for fk in column.foreign_keys):
                rows = session.exec(select(model).where(getattr(model, column.key) == entity_id)).all()
                refs.append((model, column.key, list(rows)))
    return refs


def _state(session: Session, kind: EntityType, entity: Entity) -> dict:
    refs = {
        f"{model.__tablename__}.{field}": sorted((entity_snapshot(row) for row in rows), key=itemgetter("id"))
        for model, field, rows in _references(session, kind, entity.id)
    }
    sources = session.exec(
        select(EntitySource).where(EntitySource.entity_type == kind, EntitySource.entity_id == entity.id)
    ).all()
    mappings = session.exec(
        select(SourceRecord).where(SourceRecord.entity_type == kind, SourceRecord.entity_id == entity.id)
    ).all()
    return {
        "record": entity_snapshot(entity),
        "references": refs,
        "sources": sorted((entity_snapshot(row) for row in sources), key=itemgetter("id")),
        "source_records": sorted((entity_snapshot(row) for row in mappings), key=itemgetter("id")),
    }


def _matching_context(session: Session, case: ReconciliationCase) -> dict:
    context = dict(case.context)
    personnel = {}
    for credit in context.get("credits", []):
        related = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        if related and related.status == "approved" and related.target_id:
            person = _get(session, EntityType.PERSON, related.target_id)
            if not isinstance(person, Person):
                raise TypeError("Credit must resolve to a person")
            personnel.setdefault(canonical_role(credit.get("role")), []).append(person.name)
    if personnel:
        context["personnel"] = personnel
        if "director" in personnel:
            context["directors"] = personnel["director"]
        if "author" in personnel:
            context["authors"] = personnel["author"]
    return context


def _preview_pair(
    session: Session,
    case: ReconciliationCase,
    target_id: uuid.UUID | str | None,
) -> tuple[Entity | None, Entity | None]:
    target_id = target_id or case.target_id
    target = _get(session, case.entity_type, target_id) if target_id else None
    duplicate_id = case.duplicate_id
    if case.operation == "merge" and target:
        if case.target_id is None or case.duplicate_id is None:
            raise ValueError("A merge requires two existing records")
        pair = {resolve_id(session, case.entity_type, identity) for identity in (case.target_id, case.duplicate_id)}
        if target.id not in pair:
            raise ValueError("Merge target must be one of the submitted pair")
        if len(pair) != 2:
            raise ValueError("These records have already been merged")
        duplicate_id = next(identity for identity in pair if identity != target.id)
    duplicate = _get(session, case.entity_type, duplicate_id) if duplicate_id else None
    if duplicate and target and duplicate.id == target.id:
        raise ValueError("These records have already been merged")
    return target, duplicate


def _source_conflicts(session: Session, case: ReconciliationCase, target: Entity | None) -> list[str]:
    if not case.source_id:
        return []
    conflicts = []
    linked = _source_matches(session, case.entity_type, case.source_id, case.context)
    if linked and (target is None or linked != {target.id}):
        conflicts.append("Quellidentität gehört zu einem anderen Datensatz; zuerst Duplikate prüfen.")
    if target:
        observations = session.exec(
            select(SourceRecord).where(
                SourceRecord.source_id == case.source_id,
                SourceRecord.entity_type == case.entity_type,
                SourceRecord.entity_id == target.id,
            )
        ).all()
        dates = [record.captured_at.replace(tzinfo=dt.UTC) for record in observations if record.captured_at]
        if dates and (not case.captured_at or case.captured_at.replace(tzinfo=dt.UTC) < max(dates)):
            conflicts.append("Älterer oder undatierter Quellstand; neueren Import nicht überschreiben.")
    return conflicts


def _credit_comparison(session: Session, case: ReconciliationCase, target: Entity | None) -> list[dict]:
    result = []
    for credit in case.context.get("credits", []):
        related = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        name = related.incoming.get("name", "") if related else ""
        role, character = canonical_role(credit.get("role")), credit.get("character")
        status = "Person zuerst prüfen"
        if related and related.status == "approved" and related.target_id:
            person = _get(session, EntityType.PERSON, related.target_id)
            if not isinstance(person, Person):
                raise TypeError("Credit must resolve to a person")
            name, status = person.name, "Neu hinzufügen"
            if isinstance(target, (Production, Text)):
                model, endpoint = (
                    (ProductionMembership, "production_id")
                    if isinstance(target, Production)
                    else (TextContribution, "text_id")
                )
                rows = session.exec(
                    select(model).where(
                        getattr(model, endpoint) == target.id,
                        model.person_id == person.id,
                    )
                ).all()
                if any(
                    canonical_role(row.role) == role
                    and (not isinstance(row, ProductionMembership) or row.character == character)
                    for row in rows
                ):
                    status = "Bereits vorhanden"
        result.append({"Person": name, "Funktion": role, "Figur": character, "Aktion": status})
    return result


def _preview(session: Session, case: ReconciliationCase, target_id: uuid.UUID | str | None) -> dict:
    incoming, blocked = _dependencies(session, case)
    target, duplicate = _preview_pair(session, case, target_id)
    current = entity_snapshot(target) if target else {}
    if duplicate:
        incoming = entity_snapshot(duplicate)
        incoming.pop("id")
    fields = []
    for field, value in incoming.items():
        if value in EMPTY or current.get(field) == value:
            continue
        old = current.get(field)
        merged = union_values(old or [], value) if isinstance(value, list) else value
        if merged == old:
            continue
        fields.append(
            {
                "field": field,
                "current": old,
                "incoming": value,
                "value": merged,
                "selected": old in EMPTY or isinstance(value, list),
            }
        )
    target_state = _state(session, case.entity_type, target) if target else None
    duplicate_state = _state(session, case.entity_type, duplicate) if duplicate else None
    candidates = (
        _candidates(session, case.entity_type, incoming, _matching_context(session, case), case.source_id)
        if not duplicate
        else []
    )
    blocked.extend(_source_conflicts(session, case, target))
    credit_targets = []
    for credit in case.context.get("credits", []):
        related = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        if related and related.target_id:
            credit_targets.append(str(resolve_id(session, EntityType.PERSON, related.target_id)))
    state = [case.id, case.status, case.context, incoming, blocked, credit_targets, target_state, duplicate_state]
    return {
        "case": entity_snapshot(case),
        "current": current,
        "incoming": incoming,
        "fields": fields,
        "blocked": blocked,
        "candidates": candidates,
        "credits": _credit_comparison(session, case, target),
        "revision": stable_digest(state),
        "target_id": str(target.id) if target else None,
        "target_state": target_state,
        "duplicate_state": duplicate_state,
    }


def preview_case(case_id: uuid.UUID | str, *, target_id: uuid.UUID | str | None = None) -> dict:
    """Preview field choices, dependencies and relationship changes with a stale-write token."""
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(str(case_id)))
        if case is None:
            raise ValueError("Review case not found")
        return _preview(session, case, target_id)


def audit_event(session: Session, entity: SQLModel, operation: str, actor_id: str | None) -> None:
    """Record an SCD2 history event for an entity change."""
    add_scd2_event(
        session,
        entity_name=type(entity).__name__.lower(),
        entity_id=str(entity.model_dump(mode="python")["id"]),
        operation=operation,
        snapshot=entity_snapshot(entity),
        actor_id=actor_id,
    )


def _save_source(session: Session, case: ReconciliationCase, entity_id: uuid.UUID) -> None:
    if case.source_id is None:
        return
    if case.record_key is None:
        raise ValueError("Source record key is required")
    record = session.exec(
        select(SourceRecord).where(
            SourceRecord.source_id == case.source_id,
            SourceRecord.entity_type == case.entity_type,
            SourceRecord.record_key == case.record_key,
        )
    ).first()
    if record is None:
        record = SourceRecord(
            source_id=case.source_id, entity_type=case.entity_type, record_key=case.record_key, entity_id=entity_id
        )
    record.entity_id = entity_id
    record.external_id = case.context.get("external_id")
    record.external_url = case.context.get("external_url")
    record.payload = {"incoming": case.incoming, "context": case.context}
    record.captured_at = case.captured_at
    session.add(record)
    source = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == case.source_id,
            EntitySource.entity_type == case.entity_type,
            EntitySource.entity_id == entity_id,
        )
    ).first()
    if source is None:
        source = EntitySource(
            source_id=case.source_id,
            entity_type=case.entity_type,
            entity_id=entity_id,
            external_id=record.external_id,
            external_url=record.external_url,
        )
    source.reviewed = True
    if case.captured_at:
        source.sourced_at = case.captured_at
    session.add(source)


def _archive_source(
    session: Session,
    source: EntitySource,
    target_id: uuid.UUID,
    duplicate_id: uuid.UUID,
    approved_ids: Collection[uuid.UUID] = (),
) -> None:
    identities = _source_matches(
        session,
        source.entity_type,
        source.source_id,
        {
            "external_id": source.external_id,
            "external_url": source.external_url,
        },
    )
    allowed = {source.entity_id, target_id, duplicate_id, *approved_ids}
    if identities - allowed:
        raise ValueError("Source identity collision with a third entity; resolve it first")
    keys = []
    if source.external_id:
        keys.append(f"id:{source.external_id}")
    if source.external_url:
        keys.append(canonical_url(source.external_url))
    if not keys:
        keys.append(f"legacy:{source.id}")
    for key in keys:
        record = session.exec(
            select(SourceRecord).where(
                SourceRecord.source_id == source.source_id,
                SourceRecord.entity_type == source.entity_type,
                SourceRecord.record_key == key,
            )
        ).first()
        if record is None:
            record = SourceRecord(
                source_id=source.source_id,
                entity_type=source.entity_type,
                record_key=key,
                entity_id=target_id,
                external_id=source.external_id,
                external_url=source.external_url,
                payload={"legacy_source": entity_snapshot(source)},
                captured_at=None,
            )
        elif record.entity_id not in allowed:
            raise ValueError("Source identity collision with a third entity; resolve it first")
        archived = record.payload.get("merged_sources", [])
        snapshot = entity_snapshot(source)
        if snapshot not in archived:
            record.payload = {**record.payload, "merged_sources": [*archived, snapshot]}
        record.entity_id = target_id
        session.add(record)


def _deduplicate_links(session: Session, actor_id: str | None, touched: set[tuple[type, uuid.UUID]]) -> None:
    specs = {
        ProductionMembership: ("production_id", "person_id", "role", "character"),
        TextContribution: ("text_id", "person_id", "role"),
        OrganizationMembership: ("organization_id", "person_id", "role", "start_date", "end_date"),
        TextLineage: ("source_text_id", "derived_text_id", "relation", "notes"),
        AwardNomination: ("award_id", "production_id", "person_id", "category", "year", "result"),
    }
    for model, fields in specs.items():
        for touched_model, row_id in touched:
            if touched_model != model:
                continue
            row = session.get(model, row_id)
            if row is None:
                continue
            # Only inspect the affected relationship's endpoint pair, not the entire table.
            stmt = select(model)
            for field in fields[:2]:
                stmt = stmt.where(getattr(model, field) == getattr(row, field))
            matches = session.exec(stmt.order_by(col(model.id))).all()
            seen = set()
            for match in matches:
                key = tuple(canonical_role(getattr(match, f)) if f == "role" else getattr(match, f) for f in fields)
                if key in seen or (isinstance(match, TextLineage) and match.source_text_id == match.derived_text_id):
                    audit_event(session, match, "delete", actor_id)
                    session.delete(match)
                else:
                    seen.add(key)
    session.flush()


def _merge(
    session: Session,
    case: ReconciliationCase,
    target: Entity,
    duplicate_id: str,
    actor_id: str | None,
    *,
    approved_ids: Collection[uuid.UUID] = (),
) -> None:
    duplicate = _get(session, case.entity_type, duplicate_id)
    touched = set()
    for model, field, rows in _references(session, case.entity_type, duplicate.id):
        for row in rows:
            setattr(row, field, target.id)
            session.add(row)
            touched.add((model, row.id))
    session.flush()
    for model, row_id in touched:
        row = session.get(model, row_id)
        if row is not None:
            audit_event(session, row, "update", actor_id)
    _deduplicate_links(session, actor_id, touched)
    links = session.exec(
        select(EntitySource).where(
            EntitySource.entity_type == case.entity_type,
            col(EntitySource.entity_id).in_([target.id, duplicate.id]),
        )
    ).all()
    by_source = {}
    for link in sorted(links, key=lambda row: row.entity_id != target.id):
        _archive_source(session, link, target.id, duplicate.id, approved_ids)
        if link.source_id in by_source:
            session.delete(link)
        else:
            link.entity_id = target.id
            session.add(link)
            by_source[link.source_id] = link
    for record in session.exec(
        select(SourceRecord).where(
            SourceRecord.entity_type == case.entity_type,
            SourceRecord.entity_id == duplicate.id,
        )
    ).all():
        record.entity_id = target.id
        session.add(record)
    for redirect in session.exec(
        select(EntityRedirect).where(
            EntityRedirect.entity_type == case.entity_type,
            EntityRedirect.target_id == duplicate.id,
        )
    ).all():
        redirect.target_id = target.id
        session.add(redirect)
    session.add(EntityRedirect(entity_type=case.entity_type, old_id=duplicate.id, target_id=target.id))
    audit_event(session, duplicate, "delete", actor_id)
    session.delete(duplicate)
    session.flush()


def _people_merge_preview(
    session: Session, person_ids: Collection[uuid.UUID | str], target_id: uuid.UUID | str
) -> dict:
    ids = [uuid.UUID(str(value)) for value in person_ids]
    target_id = uuid.UUID(str(target_id))
    if len(ids) < 2 or len(set(ids)) != len(ids) or target_id not in ids:
        raise ValueError("Select at least two different people and a survivor from the selection")
    people = session.exec(
        select(Person).where(col(Person.id).in_(ids)).order_by(col(Person.id)).with_for_update()
    ).all()
    if len(people) != len(ids):
        raise ValueError("Selected people no longer exist. Refresh and review again.")
    ordered = sorted(people, key=lambda person: (person.id != target_id, person.id))
    states = [_state(session, EntityType.PERSON, person) for person in ordered]
    result = dict(states[0]["record"])
    fields = []
    for field in Person.model_fields:
        if field == "id":
            continue
        options = {}
        for state in states:
            value = state["record"][field]
            if value not in EMPTY:
                options.setdefault(stable_digest(value), {"person_id": state["record"]["id"], "value": value})
        values = list(options.values())
        if not values:
            continue
        value = values[0]["value"]
        if isinstance(value, list):
            value = []
            for option in values:
                value = union_values(value, option["value"])
        result[field] = value
        fields.append(
            {
                "field": field,
                "value": value,
                "options": values,
                "conflict": len(values) > 1 and not isinstance(value, list),
            }
        )
    return {
        "target_id": str(target_id),
        "people": states,
        "fields": fields,
        "result": result,
        "revision": stable_digest([str(target_id), states]),
    }


def preview_people_merge(person_ids: Collection[uuid.UUID | str], *, target_id: uuid.UUID | str) -> dict:
    """Review a whole selection without writes, including all references in the revision."""
    with get_session() as session:
        return _people_merge_preview(session, person_ids, target_id)


def merge_people(
    person_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str,
    revision: str,
    actor_id: str,
    choices: dict[str, str] | None = None,
) -> uuid.UUID:
    """Approve a reviewed people selection atomically, retaining source identities and history."""
    with get_session() as session:
        preview = _people_merge_preview(session, person_ids, target_id)
        if preview["revision"] != revision:
            raise ValueError("Data changed since preview. Refresh and review again.")
        result = dict(preview["result"])
        conflicts = {field["field"]: field for field in preview["fields"] if field["conflict"]}
        for field, person_id in (choices or {}).items():
            options = {option["person_id"]: option["value"] for option in conflicts.get(field, {}).get("options", [])}
            if person_id not in options:
                raise ValueError("Selected fields are not in the preview")
            result[field] = options[person_id]
        target = session.get(Person, uuid.UUID(preview["target_id"]))
        if target is None:
            raise ValueError("Merge target no longer exists")
        before = entity_snapshot(target)
        validated = revalidate(target, result)
        approved_ids = {uuid.UUID(state["record"]["id"]) for state in preview["people"]}
        aliases = session.exec(
            select(EntityRedirect.old_id).where(
                EntityRedirect.entity_type == EntityType.PERSON,
                col(EntityRedirect.target_id).in_(approved_ids),
            )
        ).all()
        review_ids = approved_ids | set(aliases)
        related_cases = session.exec(
            select(ReconciliationCase).where(
                ReconciliationCase.entity_type == EntityType.PERSON,
                ReconciliationCase.operation == "merge",
                ReconciliationCase.status == "pending",
                col(ReconciliationCase.target_id).in_(review_ids),
                col(ReconciliationCase.duplicate_id).in_(review_ids),
            )
        ).all()
        for field in result.keys() - {"id"}:
            setattr(target, field, getattr(validated, field))
        if entity_snapshot(target) != before:
            session.add(target)
            audit_event(session, target, "update", actor_id)
        for duplicate in preview["people"][1:]:
            duplicate_id = duplicate["record"]["id"]
            key = stable_digest(["duplicate", EntityType.PERSON, sorted([str(target.id), duplicate_id])])
            case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
            case = case or ReconciliationCase(
                key=key,
                entity_type=EntityType.PERSON,
                operation="merge",
                reason="Personenauswahl geprüft",
                incoming={"name": duplicate["record"]["name"]},
            )
            case.target_id = target.id
            case.duplicate_id = uuid.UUID(duplicate_id)
            case.status, case.actor_id = "approved", actor_id
            case.decided_at = dt.datetime.now(dt.UTC)
            case.decision_note = "Identität und Feldwerte in der Personenauswahl bestätigt."
            case.context = {
                **case.context,
                "decision": {"before": before, "after": entity_snapshot(target), "duplicate": duplicate},
            }
            _merge(
                session,
                case,
                target,
                duplicate_id,
                actor_id,
                approved_ids=approved_ids,
            )
            session.add(case)
        for case in related_cases:
            if case.status == "pending":
                case.status, case.actor_id = "approved", actor_id
                case.target_id = target.id
                case.decided_at = dt.datetime.now(dt.UTC)
                case.decision_note = "Durch bestätigte Personenauswahl zusammengeführt."
                case.context = {
                    **case.context,
                    "bulk_merge": {
                        "target_id": str(target.id),
                        "person_ids": sorted(str(value) for value in approved_ids),
                        "revision": revision,
                    },
                }
                session.add(case)
        session.commit()
        return target.id


def ensure_credit(
    session: Session,
    entity: Production | Text,
    person_id: uuid.UUID,
    role: str | None,
    character: str | None,
    actor_id: str | None,
) -> bool:
    """Add a production membership or text contribution unless an equivalent credit exists."""
    role = canonical_role(role)
    model, endpoint = (
        (ProductionMembership, "production_id") if isinstance(entity, Production) else (TextContribution, "text_id")
    )
    rows = session.exec(
        select(model).where(
            getattr(model, endpoint) == entity.id,
            model.person_id == person_id,
        )
    ).all()
    if role == "actor" and character:
        legacy = next(
            (
                row
                for row in rows
                if isinstance(row, ProductionMembership) and row.role == character and not row.character
            ),
            None,
        )
        if legacy:
            legacy.role, legacy.character = "actor", character
            session.add(legacy)
            audit_event(session, legacy, "update", actor_id)
            return True
    if any(
        canonical_role(row.role) == role and (not isinstance(row, ProductionMembership) or row.character == character)
        for row in rows
    ):
        return False
    payload = {endpoint: entity.id, "person_id": person_id, "role": role}
    if model == ProductionMembership:
        payload["character"] = character
    row = model.model_validate(payload)
    session.add(row)
    audit_event(session, row, "insert", actor_id)
    return True


def _apply_credits(session: Session, case: ReconciliationCase, entity: Entity, actor_id: str | None) -> None:
    if not isinstance(entity, (Production, Text)):
        if case.context.get("credits"):
            raise ValueError("Credits are supported for productions and texts only")
        return
    for credit in case.context.get("credits", []):
        person_case = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        if person_case is None or person_case.target_id is None:
            raise ValueError("Approved person case is required")
        person_id = resolve_id(session, EntityType.PERSON, person_case.target_id)
        ensure_credit(session, entity, person_id, credit.get("role"), credit.get("character"), actor_id)


def approve_case(
    case_id: uuid.UUID | str,
    *,
    revision: str,
    actor_id: str,
    target_id: uuid.UUID | str | None = None,
    fields: list[str] | None = None,
    allow_create: bool = False,
    note: str = "",
) -> uuid.UUID:
    """Apply exactly reviewed fields atomically; creation always requires explicit opt-in.

    Obtain revision from preview_case with the same target_id. A stale preview,
    unresolved dependency or conflicting source identity aborts the whole operation.
    """
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(str(case_id)), with_for_update=True)
        if case is None or case.status != "pending":
            raise ValueError("Case is missing or already decided")
        preview = _preview(session, case, target_id)
        if revision != preview["revision"]:
            raise ValueError("Data changed since preview. Refresh and review again.")
        if preview["blocked"]:
            raise ValueError(f"Unresolved dependencies/conflicts: {preview['blocked']}")
        target_id = preview["target_id"]
        if not target_id and not allow_create:
            raise ValueError("Creation requires explicit approval")
        options = {row["field"]: row for row in preview["fields"]}
        selected = fields if fields is not None else [f for f, row in options.items() if row["selected"]]
        if set(selected) - options.keys():
            raise ValueError("Selected fields are not in the preview")
        model = ENTITY_MODELS[case.entity_type]
        if target_id:
            entity = _get(session, case.entity_type, target_id)
            before = entity_snapshot(entity)
            changes = {field: options[field]["value"] for field in selected}
            validated = revalidate(entity, changes)
            for field in changes:
                setattr(entity, field, getattr(validated, field))
            if entity_snapshot(entity) != before:
                session.add(entity)
                audit_event(session, entity, "update", actor_id)
        else:
            entity = model.model_validate(preview["incoming"])
            session.add(entity)
            audit_event(session, entity, "insert", actor_id)
        session.flush()
        if case.operation == "merge":
            _merge(session, case, entity, preview["duplicate_state"]["record"]["id"], actor_id)
            case.duplicate_id = uuid.UUID(preview["duplicate_state"]["record"]["id"])
        else:
            _save_source(session, case, entity.id)
            _apply_credits(session, case, entity, actor_id)
        case.context = {
            **case.context,
            "decision": {
                "before": preview["current"],
                "after": entity_snapshot(entity),
                "fields": selected,
                "duplicate": preview["duplicate_state"],
            },
        }
        case.target_id = entity.id
        case.status, case.actor_id, case.decision_note = "approved", actor_id, note
        case.decided_at = dt.datetime.now(dt.UTC)
        session.add(case)
        session.commit()
        return entity.id


def decide_case(case_id: uuid.UUID | str, *, status: str, actor_id: str, note: str) -> None:
    """Reject a proposal or reopen a rejected case, with an explicit audit note."""
    if status not in {"rejected", "pending"} or not note.strip():
        raise ValueError("Use rejected/pending and provide a reason")
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(str(case_id)), with_for_update=True)
        if case is None or case.status == "approved":
            raise ValueError("Missing or approved case cannot be changed")
        case.status, case.actor_id, case.decision_note = status, actor_id, note.strip()
        case.decided_at = dt.datetime.now(dt.UTC)
        session.add(case)
        session.commit()
