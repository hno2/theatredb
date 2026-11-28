"""Review-first reconciliation. Proposals never create or change domain entities."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Collection, Sequence
from contextlib import nullcontext
from difflib import SequenceMatcher
from operator import itemgetter
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Boolean, Integer, String, Uuid, and_, cast, func, literal, or_, union_all, values
from sqlalchemy import column as sql_column
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE
from sqlmodel import Session, SQLModel, col, select
from sqlmodel.sql.expression import Select, SelectOfScalar

from backend.config import ENTITY_MODELS, LINK_IDENTITY_FIELDS, canonical_role, credit_role
from backend.data_quality import QUALITY_FIELDS
from backend.date_precision import DatePrecision, date_interval, intervals_overlap, parse_date
from backend.models import (
    Award,
    EntityRedirect,
    EntitySource,
    EntityType,
    LibraryEntry,
    Organization,
    OrganizationSeasonCheck,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.taxonomy import prepare_production_merge
from backend.user_management import get_session
from backend.writes import (  # ruff: ignore[unused-import] -- compatibility exports
    EntityWriter,
    SourceObservation,
    audit_event,
    entity_snapshot,
    record_source,
    resolve_id,
    revalidate,
)

type Entity = Organization | Person | Production | Text | Award
EMPTY = (None, "", [], {})
LOG = logging.getLogger(__name__)


def normalize(value: str) -> str:
    """Normalize names without deleting non-Latin letters or significant words."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


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


def _kind(value: EntityType | str) -> EntityType:
    return EntityType(value)


def _get(session: Session, kind: EntityType, entity_id: uuid.UUID | str) -> Entity:
    entity = session.get(ENTITY_MODELS[kind], resolve_id(session, kind, entity_id), with_for_update=True)
    if entity is None:
        raise ValueError(f"{kind}: record no longer exists")
    return entity


def _entity_search_statement(
    session: Session,
    kind: EntityType,
    query: str,
    *,
    limit: int | None,
    offset: int,
    filters: Sequence[ColumnElement[bool]] = (),
) -> SelectOfScalar[Entity]:
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    stmt = select(model).where(*filters)
    if query.strip():
        try:
            entity_id = uuid.UUID(query.strip())
        except ValueError:
            stmt = stmt.where(label.icontains(query.strip(), autoescape=True))
        else:
            stmt = stmt.where(model.id == resolve_id(session, kind, entity_id))
    return stmt.order_by(label, col(model.id)).offset(offset).limit(limit)


def search_entities(kind: EntityType | str, query: str = "", *, limit: int = 50, offset: int = 0) -> list[dict]:
    """Search by name/title or UUID, returning IDs to disambiguate namesakes."""
    with get_session() as session:
        stmt = _entity_search_statement(session, _kind(kind), query, limit=limit, offset=offset)
        return [entity_snapshot(row) for row in session.exec(stmt).all()]


def count_people_for_merge(query: str, filters: Sequence[ColumnElement[bool]] = ()) -> int:
    """Count all matching people using the same filter as the paged table."""
    with get_session() as session:
        stmt = _entity_search_statement(session, EntityType.PERSON, query, limit=None, offset=0, filters=filters)
        return session.exec(select(func.count()).select_from(stmt.order_by(None).offset(None).subquery())).one()


def search_people_for_merge(
    query: str = "", *, limit: int | None = 100, offset: int = 0, filters: Sequence[ColumnElement[bool]] = ()
) -> list[dict]:
    """Fetch people and production/theatre context in two queries; None loads all people."""
    with get_session() as session:
        stmt = _entity_search_statement(session, EntityType.PERSON, query, limit=limit, offset=offset, filters=filters)
        people = [entity_snapshot(row) for row in session.exec(stmt).all()]
        if not people:
            return []
        by_id = {uuid.UUID(person["id"]): person for person in people}
        for person in people:
            person["productions"] = []
        page = stmt.with_only_columns(col(Person.id)).cte("people_page")
        involvement = (
            select(ProductionMembership.person_id, ProductionMembership.production_id)
            .join(page, page.c.id == col(ProductionMembership.person_id))
            .union(
                select(TextContribution.person_id, Production.id)
                .join(Production, col(Production.text_id) == col(TextContribution.text_id))
                .join(page, page.c.id == col(TextContribution.person_id))
            )
            .subquery()
        )
        credits = session.exec(
            select(involvement.c.person_id, Production.name, Organization.name, Organization.address_locality)
            .join(Production, col(Production.id) == involvement.c.production_id)
            .outerjoin(Organization, col(Organization.id) == col(Production.organization_id))
            .distinct()
            .order_by(Production.name, Organization.name, col(Organization.address_locality))
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
        select(Person.name, ProductionMembership.role_name)
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
    session: Session,
    row: Entity,
    incoming: dict,
    context: dict,
    related_names: dict[uuid.UUID, dict[str | None, set[str]]] | None = None,
) -> tuple[list[str], list[str], bool]:
    evidence, conflicts = [], []
    strong = False
    title = incoming.get("title" if isinstance(row, Text) else "name", "")
    exact = normalize(title) == normalize(getattr(row, "title" if isinstance(row, Text) else "name"))
    if exact:
        evidence.append("Exakter normalisierter Titel/Name")
    if isinstance(row, Production):
        extra, conflicts, strong = _production_evidence(session, row, incoming, context, exact, related_names)
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
            if incoming.get(field)
            and getattr(row, field)
            and not _dates_overlap(
                incoming[field],
                incoming.get(f"{field}_precision"),
                getattr(row, field),
                getattr(row, f"{field}_precision"),
            )
        )
    elif isinstance(row, Organization):
        conflicts.extend(
            f"Abweichend: {field}"
            for field in ("address_locality", "address_country")
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
    related_names: dict[uuid.UUID, dict[str | None, set[str]]] | None = None,
) -> tuple[list[str], list[str], bool]:
    evidence, conflicts = [], []
    organization_id = incoming.get("organization_id")
    same_org = bool(organization_id and str(row.organization_id) == str(organization_id))
    if same_org:
        evidence.append("Gleiche Organisation")
    elif organization_id and row.organization_id:
        conflicts.append("Andere Organisation")
    directors = {normalize(n) for n in context.get("directors", [])}
    existing_directors = (
        _person_names(session, row.id, "director")
        if related_names is None
        else related_names.get(row.id, {}).get("director", set())
    )
    same_directors = bool(directors and existing_directors and directors == existing_directors)
    if same_directors:
        evidence.append("Gleiche Regie")
    elif directors and existing_directors:
        conflicts.append("Abweichende Regie")
    for role, names in context.get("personnel", {}).items():
        canonical = canonical_role(role)
        existing = (
            _person_names(session, row.id, canonical)
            if related_names is None
            else related_names.get(row.id, {}).get(canonical, set())
        )
        overlap = {normalize(n) for n in names} & existing
        if overlap:
            evidence.append(f"{role}: {', '.join(sorted(overlap))}")
    _premiere_evidence(row, incoming, evidence, conflicts)
    return evidence, conflicts, exact and same_org and same_directors


def _premiere_evidence(row: Production, incoming: dict, evidence: list[str], conflicts: list[str]) -> None:
    premiere = incoming.get("premiere")
    if premiere and row.premiere:
        if _dates_overlap(premiere, incoming.get("premiere_precision"), row.premiere, row.premiere_precision):
            evidence.append("Gleiche Premiere")
        else:
            conflicts.append("Abweichende Premiere")


def _dates_overlap(left: object, left_precision: object, right: object, right_precision: object) -> bool:
    left_date, left_inferred = parse_date(left)
    right_date, right_inferred = parse_date(right)
    return intervals_overlap(
        date_interval(left_date, DatePrecision(left_precision or left_inferred)),
        date_interval(right_date, DatePrecision(right_precision or right_inferred)),
    )


def _candidates(
    session: Session,
    kind: EntityType,
    incoming: dict,
    context: dict,
    source_id: uuid.UUID | None,
    *,
    candidate_rows: list[Entity] | None = None,
    related_names: dict[uuid.UUID, dict[str | None, set[str]]] | None = None,
) -> list[dict]:
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    title = incoming.get("title" if kind == EntityType.TEXT else "name", "")
    if not title:
        raise ValueError("A name/title is required for matching")
    linked = _source_matches(session, kind, source_id, context) if source_id else set()
    if candidate_rows is None:
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
    else:
        rows = candidate_rows
    results = []
    for row in rows:
        name = getattr(row, "title" if kind == EntityType.TEXT else "name")
        similarity = SequenceMatcher(None, normalize(title), normalize(name)).ratio()
        if similarity < 0.55 and row.id not in linked:
            continue
        evidence, conflicts, strong = _candidate_evidence(session, row, incoming, context, related_names)
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
    protected = QUALITY_FIELDS if kind == EntityType.ORGANIZATION else set()
    unknown = set(payload) - (set(model.model_fields) - {"id"} - protected)
    if unknown:
        raise ValueError(f"Unknown or protected fields: {', '.join(sorted(unknown))}")
    label = "title" if kind == EntityType.TEXT else "name"
    if (complete or label in payload) and not str(payload.get(label) or "").strip():
        raise ValueError("A non-empty name/title is required")
    # Validation must also run on updates; SQLAlchemy attribute assignment does not validate values.
    data = model.model_validate(
        payload if complete else {"title" if kind == EntityType.TEXT else "name": "validation", **payload}
    )
    clean = data.model_dump(mode="json")
    fields = set(payload)
    for field in getattr(model, "date_fields", ()):
        if field in fields:
            fields.add(f"{field}_precision")
    return {field: clean[field] for field in fields}


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
    session: Session | None = None,
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
    owns_session = session is None
    with get_session() if owns_session else nullcontext(session) as session:
        if session is None:
            raise TypeError("An active reconciliation session is required")
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
        if owns_session:
            session.commit()
        else:
            session.flush()
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


def _discovery_inputs(kind: EntityType, records: list[dict]) -> CTE:
    columns = [
        sql_column("record_index", Integer),
        sql_column("has_tokens", Boolean),
        sql_column("token_1", String),
        sql_column("token_2", String),
        sql_column("token_3", String),
        sql_column("organization_id", Uuid(as_uuid=True)),
    ]
    rows = []
    for index, record in enumerate(records):
        title = record.get("title" if kind == EntityType.TEXT else "name", "")
        if not title:
            raise ValueError("A name/title is required for matching")
        tokens = sorted(normalize(title).split(), key=len, reverse=True)[:3]
        tokens = [token.replace("/", "//").replace("%", "/%").replace("_", "/_") for token in tokens]
        organization_id = record.get("organization_id") if kind == EntityType.PRODUCTION else None
        row = (
            index,
            bool(tokens),
            *(tokens + [None] * (3 - len(tokens))),
            uuid.UUID(str(organization_id)) if organization_id else None,
        )
        rows.append(row)
    return values(*columns, name="discovery_inputs").data(rows).cte("discovery_inputs")


def _discovery_related_names(
    session: Session, kind: EntityType, entity_ids: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str | None, set[str]]] | None:
    if not entity_ids or kind != EntityType.PRODUCTION:
        return None
    names: dict[uuid.UUID, dict[str | None, set[str]]] = {}
    rows = session.exec(
        select(ProductionMembership.production_id, Person.name, ProductionMembership.role_name)
        .join(Person, col(Person.id) == col(ProductionMembership.person_id))
        .where(col(ProductionMembership.production_id).in_(entity_ids))
    ).all()
    for production_id, name, role in rows:
        names.setdefault(production_id, {}).setdefault(canonical_role(role), set()).add(normalize(name))
    return names


def _postgres_candidate_statement(kind: EntityType, records: list[dict]) -> Select[Entity, int]:
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    branches = []
    for index, record in enumerate(records):
        title = record.get("title" if kind == EntityType.TEXT else "name", "")
        if not title:
            raise ValueError("A name/title is required for matching")
        stmt = select(literal(index).label("record_index"), col(model.id).label("candidate_id"))
        organization_id = record.get("organization_id")
        if kind == EntityType.PRODUCTION and organization_id:
            stmt = stmt.where(col(Production.organization_id) == uuid.UUID(str(organization_id)))
        tokens = sorted(normalize(title).split(), key=len, reverse=True)[:3]
        filters = [label.icontains(token, autoescape=True) for token in tokens]
        if filters:
            stmt = stmt.where(or_(*filters))
        branches.append(stmt.order_by(label, col(model.id)).limit(200))
    candidates = union_all(*branches).subquery("candidate_pools")
    return (
        select(model, candidates.c.record_index)
        .join(candidates, col(model.id) == candidates.c.candidate_id)
        .order_by(candidates.c.record_index, label, col(model.id))
    )


def _sqlite_candidate_statement(kind: EntityType, records: list[dict]) -> Select[Entity, int]:
    model = ENTITY_MODELS[kind]
    label = getattr(model, "title" if kind == EntityType.TEXT else "name")
    record_inputs = _discovery_inputs(kind, records)
    matches = or_(
        ~record_inputs.c.has_tokens,
        *(label.icontains(record_inputs.c[f"token_{index}"], escape="/") for index in range(1, 4)),
    )
    if kind == EntityType.PRODUCTION:
        matches = and_(
            matches,
            or_(
                record_inputs.c.organization_id.is_(None),
                col(Production.organization_id) == record_inputs.c.organization_id,
            ),
        )
    ranked = (
        select(
            record_inputs.c.record_index,
            col(model.id).label("candidate_id"),
            func.row_number()
            .over(partition_by=record_inputs.c.record_index, order_by=(label, col(model.id)))
            .label("candidate_rank"),
        )
        .select_from(record_inputs.join(model, matches))
        .subquery()
    )
    return (
        select(model, ranked.c.record_index)
        .join(ranked, col(model.id) == ranked.c.candidate_id)
        .where(ranked.c.candidate_rank <= 200)
        .order_by(ranked.c.record_index, ranked.c.candidate_rank)
    )


def _batch_duplicate_candidates(
    session: Session, kind: EntityType, records: list[dict]
) -> tuple[dict[int, list[Entity]], dict[uuid.UUID, dict[str | None, set[str]]] | None]:
    stmt = (
        _postgres_candidate_statement(kind, records)
        if session.get_bind().dialect.name == "postgresql"
        else _sqlite_candidate_statement(kind, records)
    )
    candidate_rows = {index: [] for index in range(len(records))}
    candidate_entities = session.exec(stmt).all()
    for candidate, index in candidate_entities:
        candidate_rows[index].append(candidate)
    name_field = "title" if kind == EntityType.TEXT else "name"
    for index, record in enumerate(records):
        title = record.get(name_field, "")
        candidate_rows[index] = [
            candidate
            for candidate in candidate_rows[index]
            if SequenceMatcher(None, normalize(title), normalize(getattr(candidate, name_field))).ratio() >= 0.55
        ]
    candidate_ids = {candidate.id for rows in candidate_rows.values() for candidate in rows}
    related_names = _discovery_related_names(
        session, kind, candidate_ids | {uuid.UUID(record["id"]) for record in records}
    )
    return candidate_rows, related_names


def discover_duplicates(kind: EntityType | str, query: str = "", *, limit: int = 50) -> list[dict]:
    """Return bounded, explained duplicate candidates without submitting or merging them."""
    kind = _kind(kind)
    results, seen = [], set()
    with get_session() as session:
        records = [
            entity_snapshot(row)
            for row in session.exec(_entity_search_statement(session, kind, query, limit=limit, offset=0)).all()
        ]
        # Broad title/name pools did not benefit from batching on PostgreSQL.
        batch_size = 5 if kind == EntityType.PRODUCTION else 1
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            if kind == EntityType.PRODUCTION:
                candidate_rows, related_names = _batch_duplicate_candidates(session, kind, batch)
            else:
                candidate_rows, related_names = {}, None
            for index, record in enumerate(batch):
                context = {}
                record_id = uuid.UUID(record["id"])
                if kind == EntityType.PRODUCTION:
                    context["directors"] = sorted((related_names or {}).get(record_id, {}).get("director", set()))
                elif kind == EntityType.TEXT:
                    context["authors"] = sorted(_authors(session, record_id))
                for candidate in _candidates(
                    session,
                    kind,
                    record,
                    context,
                    None,
                    candidate_rows=candidate_rows.get(index),
                    related_names=related_names,
                ):
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
    exclude_source_ids: Collection[uuid.UUID] = (),
) -> list[dict]:
    """List persistent review cases, including decisions for audit and follow-up."""
    with get_session() as session:
        stmt = select(ReconciliationCase).where(ReconciliationCase.status == status)
        if exclude_operations:
            stmt = stmt.where(col(ReconciliationCase.operation).not_in(list(exclude_operations)))
        if exclude_source_ids:
            stmt = stmt.where(
                or_(
                    col(ReconciliationCase.source_id).is_(None),
                    col(ReconciliationCase.source_id).not_in(list(exclude_source_ids)),
                )
            )
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
        if model is LibraryEntry:  # private; moved by backend.library.merge_target, never snapshotted
            continue
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
        role, character = credit_role(credit.get("role")), credit.get("character")
        status = "Person zuerst prüfen"
        if related and related.status == "approved" and related.target_id:
            person = _get(session, EntityType.PERSON, related.target_id)
            if not isinstance(person, Person):
                raise TypeError("Credit must resolve to a person")
            name, status = person.name, "Neu hinzufügen"
            if isinstance(target, (Production, Text)) and any(
                _same_credit(row, role, character) for row in _credit_rows(session, target, person.id)
            ):
                status = "Bereits vorhanden"
        result.append({"Person": name, "Funktion": role, "Figur": character, "Aktion": status})
    return result


def _review_incoming(case: ReconciliationCase, incoming: dict, duplicate: Entity | None) -> dict:
    if duplicate:
        incoming = entity_snapshot(duplicate)
        incoming.pop("id")
    elif case.entity_type == EntityType.ORGANIZATION:
        incoming = {field: value for field, value in incoming.items() if field not in QUALITY_FIELDS}
    return incoming


def _preview(session: Session, case: ReconciliationCase, target_id: uuid.UUID | str | None) -> dict:
    incoming, blocked = _dependencies(session, case)
    target, duplicate = _preview_pair(session, case, target_id)
    current = entity_snapshot(target) if target else {}
    incoming = _review_incoming(case, incoming, duplicate)
    fields = []
    date_fields = getattr(ENTITY_MODELS[case.entity_type], "date_fields", ())
    for field, value in incoming.items():
        if case.entity_type == EntityType.ORGANIZATION and field in QUALITY_FIELDS:
            continue
        if field.endswith("_precision") and field.removesuffix("_precision") in date_fields:
            continue
        precision_name = f"{field}_precision" if field in date_fields else None
        same_precision = not precision_name or incoming.get(precision_name) == current.get(precision_name)
        if value in EMPTY or (current.get(field) == value and same_precision):
            continue
        old = current.get(field)
        merged = union_values(old or [], value) if isinstance(value, list) else value
        if merged == old and same_precision:
            continue
        fields.append(
            {
                "field": field,
                "current": old,
                "incoming": value,
                "value": merged,
                "selected": old in EMPTY or isinstance(value, list),
                **(
                    {"precision": incoming.get(precision_name), "current_precision": current.get(precision_name)}
                    if precision_name
                    else {}
                ),
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
    assessment_conflicts = []
    assessment_state = None
    assessment_result = None
    if isinstance(target, Organization) and isinstance(duplicate, Organization):
        assessment_conflicts, assessment_result, assessment_state = _organization_quality_preview(
            session, target, duplicate, target_state, duplicate_state
        )
    state = [
        case.id,
        case.status,
        case.context,
        incoming,
        blocked,
        credit_targets,
        target_state,
        duplicate_state,
        assessment_state,
    ]
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
        "assessment_conflicts": assessment_conflicts,
        "assessment_result": assessment_result,
    }


def preview_case(case_id: uuid.UUID | str, *, target_id: uuid.UUID | str | None = None) -> dict:
    """Preview field choices, dependencies and relationship changes with a stale-write token."""
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(str(case_id)))
        if case is None:
            raise ValueError("Review case not found")
        return _preview(session, case, target_id)


def _save_source(session: Session, case: ReconciliationCase, entity_id: uuid.UUID) -> None:
    if case.source_id is None:
        return
    if case.record_key is None:
        raise ValueError("Source record key is required")
    save_source_record(
        session,
        kind=case.entity_type,
        source_id=case.source_id,
        record_key=case.record_key,
        entity_id=entity_id,
        incoming=case.incoming,
        context=case.context,
        captured_at=case.captured_at,
    )


def save_source_record(
    session: Session,
    *,
    kind: EntityType,
    source_id: uuid.UUID,
    record_key: str,
    entity_id: uuid.UUID,
    incoming: dict,
    context: dict,
    captured_at: dt.datetime | None,
) -> None:
    """Persist an accepted source observation without requiring a review case."""
    record_source(
        session,
        SourceObservation(
            kind=kind,
            source_id=source_id,
            record_key=record_key,
            entity_id=entity_id,
            payload={"incoming": incoming, "context": context},
            captured_at=captured_at,
            external_id=context.get("external_id"),
            external_url=context.get("external_url"),
        ),
    )


def _archive_source(
    session: Session,
    source: EntitySource,
    target_id: uuid.UUID,
    duplicate_id: uuid.UUID,
    approved_ids: Collection[uuid.UUID] = (),
) -> None:
    provider = session.get(Source, source.source_id)
    scoped_person = (
        provider is not None
        and provider.name == "theapolis.de"
        and source.entity_type == EntityType.PERSON
        and bool(source.external_id)
        and source.external_id.startswith(("directors:", "creators:"))
    )
    identities = _source_matches(
        session,
        source.entity_type,
        source.source_id,
        {
            "external_id": source.external_id,
            "external_url": None if scoped_person else source.external_url,
        },
    )
    allowed = {source.entity_id, target_id, duplicate_id, *approved_ids}
    if identities - allowed:
        raise ValueError("Source identity collision with a third entity; resolve it first")
    keys = []
    if source.external_id:
        keys.append(f"id:{source.external_id}")
    if source.external_url and not scoped_person:
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


_ORGANIZATION_ASSESSMENTS = {
    "general": (
        "general_verified_at",
        "general_verified_by",
        "general_verification_evidence",
    ),
    "history": (
        "history_from_season",
        "history_through_season",
        "history_verified_at",
        "history_verified_by",
        "history_verification_evidence",
    ),
    "update": ("update_scope", "update_reason", "update_flagged_at", "update_flagged_by"),
}


def _assessment_values(record: SQLModel, fields: tuple[str, ...]) -> tuple:
    return tuple(getattr(record, field) for field in fields)


def _check_values(check: OrganizationSeasonCheck) -> tuple:
    return (check.season_start_year, check.checked_at, check.checked_by, check.evidence)


def _organization_quality_conflicts(session: Session, target: Organization, duplicate: Organization) -> list[dict]:
    conflicts = []
    for block, fields in _ORGANIZATION_ASSESSMENTS.items():
        target_values = _assessment_values(target, fields)
        duplicate_values = _assessment_values(duplicate, fields)
        if (
            all(value is not None for value in target_values)
            and all(value is not None for value in duplicate_values)
            and target_values != duplicate_values
        ):
            conflicts.append(
                {
                    "key": block,
                    "field": f"assessment:{block}",
                    "sources": [str(target.id), str(duplicate.id)],
                }
            )
    target_checks = {
        row.season_start_year: row
        for row in session.exec(
            select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == target.id)
        ).all()
    }
    duplicate_checks = {
        row.season_start_year: row
        for row in session.exec(
            select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == duplicate.id)
        ).all()
    }
    for year in target_checks.keys() & duplicate_checks.keys():
        left, right = target_checks[year], duplicate_checks[year]
        if _check_values(left) != _check_values(right):
            key = f"season:{year}"
            conflicts.append({"key": key, "field": f"assessment:{key}", "sources": [str(target.id), str(duplicate.id)]})
    return conflicts


def _organization_assessment_values(
    session: Session, target: Organization, duplicate: Organization
) -> tuple[list[dict], dict]:
    conflicts = _organization_quality_conflicts(session, target, duplicate)
    conflict_keys = {item["key"] for item in conflicts}
    sources = (target, duplicate)

    def option(source: Organization, block: str) -> dict:
        record = entity_snapshot(source)
        if block.startswith("season:"):
            year = int(block.split(":", 1)[1])
            check = session.exec(
                select(OrganizationSeasonCheck).where(
                    OrganizationSeasonCheck.organization_id == source.id,
                    OrganizationSeasonCheck.season_start_year == year,
                )
            ).one()
            value = {
                "season_start_year": check.season_start_year,
                "checked_at": check.checked_at.isoformat(),
                "checked_by": check.checked_by,
                "evidence": check.evidence,
            }
        else:
            value = {field: record[field] for field in _ORGANIZATION_ASSESSMENTS[block]}
        return {"entity_id": str(source.id), "value": value}

    assessment_options = {
        conflict["key"]: [option(target, conflict["key"]), option(duplicate, conflict["key"])] for conflict in conflicts
    }
    for conflict in conflicts:
        conflict["options"] = assessment_options[conflict["key"]]
    results = {}
    for block, fields in _ORGANIZATION_ASSESSMENTS.items():
        target_values = _assessment_values(target, fields)
        duplicate_values = _assessment_values(duplicate, fields)
        results[block] = (
            {"conflict": True, "options": assessment_options[block]}
            if block in conflict_keys
            else dict(
                zip(
                    fields,
                    target_values if any(value is not None for value in target_values) else duplicate_values,
                    strict=True,
                )
            )
        )
    checks_by_year: dict[str, dict[str, object]] = {}
    for source in sources:
        checks = session.exec(
            select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == source.id)
        ).all()
        for check in checks:
            key = f"season:{check.season_start_year}"
            if key in conflict_keys:
                checks_by_year[key] = {"conflict": True, "options": assessment_options[key]}
            else:
                checks_by_year.setdefault(
                    key,
                    {
                        "season_start_year": check.season_start_year,
                        "checked_at": check.checked_at.isoformat(),
                        "checked_by": check.checked_by,
                        "evidence": check.evidence,
                    },
                )
    return conflicts, {"blocks": results, "season_checks": checks_by_year}


def _organization_quality_preview(
    session: Session,
    target: Organization,
    duplicate: Organization,
    target_state: dict | None,
    duplicate_state: dict | None,
) -> tuple[list[dict], dict, list]:
    conflicts, result = _organization_assessment_values(session, target, duplicate)
    for state, organization in ((target_state, target), (duplicate_state, duplicate)):
        if state is None:
            continue
        record = state["record"]
        for block, assessment_fields in _ORGANIZATION_ASSESSMENTS.items():
            record[f"assessment:{block}"] = (
                {field: record[field] for field in assessment_fields}
                if all(record[field] is not None for field in assessment_fields)
                else None
            )
        for check in session.exec(
            select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == organization.id)
        ).all():
            record[f"assessment:season:{check.season_start_year}"] = {
                "season_start_year": check.season_start_year,
                "checked_at": check.checked_at.isoformat(),
                "checked_by": check.checked_by,
                "evidence": check.evidence,
            }
    state = [
        {field: getattr(target, field) for fields in _ORGANIZATION_ASSESSMENTS.values() for field in fields},
        {field: getattr(duplicate, field) for fields in _ORGANIZATION_ASSESSMENTS.values() for field in fields},
        [
            entity_snapshot(row)
            for row in session.exec(
                select(OrganizationSeasonCheck)
                .where(col(OrganizationSeasonCheck.organization_id).in_([target.id, duplicate.id]))
                .order_by(
                    col(OrganizationSeasonCheck.organization_id),
                    col(OrganizationSeasonCheck.season_start_year),
                )
            ).all()
        ],
    ]
    return conflicts, result, state


def _validate_assessment_choices(
    conflicts: list[dict], choices: dict[str, str] | None
) -> tuple[set[str], dict[str, str]]:
    conflict_keys = {item["key"] for item in conflicts}
    choices = choices or {}
    if conflict_keys - choices.keys():
        raise ValueError(
            f"Choose an assessment for every conflict: {', '.join(sorted(conflict_keys - choices.keys()))}"
        )
    if choices.keys() - conflict_keys:
        raise ValueError(f"Assessment choices are not conflicting: {', '.join(sorted(choices.keys() - conflict_keys))}")
    return conflict_keys, choices


def _carry_organization_blocks(
    target: Organization,
    duplicate: Organization,
    conflicts: set[str],
    choices: dict[str, str],
) -> bool:
    changed = False
    for block, fields in _ORGANIZATION_ASSESSMENTS.items():
        target_values = _assessment_values(target, fields)
        duplicate_values = _assessment_values(duplicate, fields)
        if not any(value is not None for value in target_values) and any(
            value is not None for value in duplicate_values
        ):
            for field, value in zip(fields, duplicate_values, strict=True):
                setattr(target, field, value)
            changed = True
        if block in conflicts:
            selected = str(choices[block])
            if selected not in {str(target.id), str(duplicate.id)}:
                raise ValueError(f"Invalid source for assessment conflict {block}")
            if selected == str(duplicate.id):
                for field, value in zip(fields, duplicate_values, strict=True):
                    setattr(target, field, value)
                changed = True
    return changed


def _merge_organization_season_checks(
    writes: EntityWriter,
    target: Organization,
    duplicate: Organization,
    conflicts: set[str],
    choices: dict[str, str],
) -> None:
    session = writes.session
    checks = session.exec(
        select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == duplicate.id)
    ).all()
    target_check_map = {
        row.season_start_year: row
        for row in session.exec(
            select(OrganizationSeasonCheck).where(OrganizationSeasonCheck.organization_id == target.id)
        ).all()
    }
    for check in checks:
        existing = target_check_map.get(check.season_start_year)
        if existing is None:
            check.organization_id = target.id
            session.add(check)
            session.flush()
            writes.record(check, "update")
            continue
        key = f"season:{check.season_start_year}"
        selected = str(choices.get(key, target.id))
        if selected not in {str(target.id), str(duplicate.id)}:
            raise ValueError(f"Invalid source for assessment conflict {key}")
        if selected == str(duplicate.id) and key in conflicts:
            writes.record(existing, "delete")
            session.delete(existing)
            session.flush()
            check.organization_id = target.id
            session.add(check)
            session.flush()
            writes.record(check, "update")
        else:
            writes.record(check, "delete")
            session.delete(check)


def _apply_organization_assessments(
    writes: EntityWriter,
    target: Organization,
    duplicate: Organization,
    choices: dict[str, str] | None,
) -> bool:
    conflict_keys, selected = _validate_assessment_choices(
        _organization_quality_conflicts(writes.session, target, duplicate), choices
    )
    writes.track(target)
    changed = _carry_organization_blocks(target, duplicate, conflict_keys, selected)
    _merge_organization_season_checks(writes, target, duplicate, conflict_keys, selected)
    return changed


def _deduplicate_links(writes: EntityWriter, touched: set[tuple[type, uuid.UUID]]) -> None:
    session = writes.session
    for model, fields in LINK_IDENTITY_FIELDS.items():
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
                key = tuple(
                    credit_role(getattr(match, f)) if f in {"role", "role_name"} else getattr(match, f) for f in fields
                )
                if key in seen or (isinstance(match, TextLineage) and match.source_text_id == match.derived_text_id):
                    writes.record(match, "delete")
                    session.delete(match)
                else:
                    seen.add(key)
    session.flush()


def merge_entity(
    writes: EntityWriter,
    kind: EntityType,
    target: Entity,
    duplicate_id: str,
    *,
    approved_ids: Collection[uuid.UUID] = (),
    assessment_choices: dict[str, str] | None = None,
) -> None:
    """Move an approved duplicate's graph and provenance into its survivor."""
    session = writes.session
    duplicate = _get(session, kind, duplicate_id)
    if duplicate.id == target.id:
        raise ValueError("Cannot merge an entity into itself")
    if isinstance(target, Organization) and isinstance(duplicate, Organization):
        _apply_organization_assessments(writes, target, duplicate, assessment_choices)
    _prepare_link_merge(writes, kind, target.id, duplicate.id)
    # Local import: backend.library imports texts, which imports this module.
    from backend.library import merge_target  # ruff: ignore[import-outside-top-level]

    merge_target(session, kind.value, duplicate.id, target.id)
    touched = set()
    for model, field, rows in _references(session, kind, duplicate.id):
        for row in rows:
            setattr(row, field, target.id)
            session.add(row)
            touched.add((model, row.id))
    session.flush()
    for model, row_id in touched:
        row = session.get(model, row_id)
        if row is not None:
            writes.record(row, "update")
    _deduplicate_links(writes, touched)
    _merge_provenance(session, kind, target.id, duplicate.id, approved_ids)
    writes.record(duplicate, "delete")
    session.delete(duplicate)
    session.flush()
    if kind == EntityType.PERSON:
        _merge_author_texts(writes, target.id)


def _merge_provenance(
    session: Session,
    kind: EntityType,
    target_id: uuid.UUID,
    duplicate_id: uuid.UUID,
    approved_ids: Collection[uuid.UUID],
) -> None:
    links = session.exec(
        select(EntitySource).where(
            EntitySource.entity_type == kind,
            col(EntitySource.entity_id).in_([target_id, duplicate_id]),
        )
    ).all()
    by_source = {}
    for link in sorted(links, key=lambda row: row.entity_id != target_id):
        _archive_source(session, link, target_id, duplicate_id, approved_ids)
        if link.source_id in by_source:
            session.delete(link)
        else:
            link.entity_id = target_id
            session.add(link)
            by_source[link.source_id] = link
    for record in session.exec(
        select(SourceRecord).where(
            SourceRecord.entity_type == kind,
            SourceRecord.entity_id == duplicate_id,
        )
    ).all():
        record.entity_id = target_id
        session.add(record)
    for redirect in session.exec(
        select(EntityRedirect).where(
            EntityRedirect.entity_type == kind,
            EntityRedirect.target_id == duplicate_id,
        )
    ).all():
        redirect.target_id = target_id
        session.add(redirect)
    session.add(EntityRedirect(entity_type=kind, old_id=duplicate_id, target_id=target_id))


def _merge_author_texts(writes: EntityWriter, person_id: uuid.UUID) -> None:
    """Consolidate compatible texts whose author identities now coincide."""
    session = writes.session
    credits = session.exec(
        select(TextContribution).where(
            col(TextContribution.text_id).in_(
                select(TextContribution.text_id).where(TextContribution.person_id == person_id)
            )
        )
    ).all()
    authors: dict[uuid.UUID, set[uuid.UUID]] = {}
    for credit in credits:
        if canonical_role(credit.role) == "author":
            authors.setdefault(credit.text_id, set()).add(credit.person_id)
    groups: dict[tuple[str, frozenset[uuid.UUID]], list[uuid.UUID]] = {}
    for text in session.exec(select(Text).where(col(Text.id).in_(authors))):
        if person_id in authors[text.id]:
            groups.setdefault((normalize(text.title), frozenset(authors[text.id])), []).append(text.id)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        preview = _entity_merge_preview(session, EntityType.TEXT, ids, None)
        conflicts = {field["field"] for field in preview["fields"] if field["conflict"]}
        if conflicts - {"title"}:
            LOG.warning(
                "Texts %s still have conflicting fields after person merge %s; merge them manually: %s",
                sorted(map(str, ids)),
                person_id,
                sorted(conflicts - {"title"}),
            )
            continue
        choices = (
            {
                "title": next(
                    option["entity_id"]
                    for field in preview["fields"]
                    if field["field"] == "title"
                    for option in field["options"]
                    if option["value"] == preview["result"]["title"]
                )
            }
            if "title" in conflicts
            else {}
        )
        _apply_entity_merge(writes, preview, choices=choices, require_choices=True, automatic=True)


def _prepare_link_merge(writes: EntityWriter, kind: EntityType, target_id: uuid.UUID, duplicate_id: uuid.UUID) -> None:
    """Resolve uniquely keyed child rows before foreign keys move to the target."""
    if kind == EntityType.TEXT:
        _prepare_text_merge(writes, target_id, duplicate_id)
    elif kind == EntityType.PRODUCTION:
        prepare_production_merge(writes.session, target_id, duplicate_id)


def _prepare_text_merge(writes: EntityWriter, target_id: uuid.UUID, duplicate_id: uuid.UUID) -> None:
    """Delete future unique-key collisions before FK updates; retain notes and titles."""
    session = writes.session
    for model, fields in (
        (TextTitle, ("text_id", "normalized")),
        (TextLineage, ("source_text_id", "derived_text_id", "relation")),
    ):
        seen: dict[tuple, TextTitle | TextLineage] = {}
        rows = list(session.exec(select(model)).all())
        rows.sort(key=lambda row: duplicate_id in [getattr(row, field) for field in fields])
        for row in rows:
            values = tuple(
                target_id if getattr(row, field) == duplicate_id else getattr(row, field) for field in fields
            )
            if isinstance(row, TextLineage) and values[0] == values[1]:
                writes.record(row, "delete")
                session.delete(row)
            elif values in seen:
                survivor = seen[values]
                writes.track(survivor)
                if isinstance(row, TextLineage) and isinstance(survivor, TextLineage) and row.notes:
                    survivor.notes = "\n".join(dict.fromkeys(filter(None, (survivor.notes, row.notes))))
                if isinstance(row, TextTitle) and isinstance(survivor, TextTitle) and survivor.language is None:
                    survivor.language = row.language
                writes.record(row, "delete")
                session.delete(row)
            else:
                seen[values] = row
        session.flush()


def _merge_options(kind: EntityType, states: list[dict], field: str, *, is_date: bool) -> list[dict]:
    options = {}
    for state in states:
        value = state["record"][field]
        if value in EMPTY:
            continue
        option = {"entity_id": state["record"]["id"], "value": value}
        if kind == EntityType.PERSON:
            option["person_id"] = option["entity_id"]
        if is_date:
            option["precision"] = state["record"][f"{field}_precision"]
        options.setdefault(stable_digest([value, option.get("precision")]), option)
    values = list(options.values())
    if is_date and values:
        intervals = [date_interval(parse_date(option["value"])[0], option["precision"]) for option in values]
        if all(intervals_overlap(left, right) for left in intervals for right in intervals):
            values = [max(values, key=lambda option: list(DatePrecision).index(DatePrecision(option["precision"])))]
    return values


def _text_title_quality(title: str) -> tuple[bool, bool, bool, str, str]:
    cleaned = title.strip()
    return (
        cleaned == title,
        cleaned.istitle(),
        bool(cleaned) and cleaned[0].isupper() and not cleaned.isupper(),
        cleaned.casefold(),
        cleaned,
    )


def merge_entity_fields(kind: EntityType, states: list[dict]) -> tuple[dict, list[dict]]:
    """Combine person/text fields with shared list, date precision, and conflict rules."""
    model = Person if kind == EntityType.PERSON else Text
    result = dict(states[0]["record"])
    fields = []
    for field in model.model_fields:
        if field == "id" or field in {f"{name}_precision" for name in model.date_fields}:
            continue
        values = _merge_options(kind, states, field, is_date=field in model.date_fields)
        if not values:
            continue
        value = values[0]["value"]
        if kind == EntityType.TEXT and field == "title":
            value = max((option["value"] for option in values), key=_text_title_quality)
        if isinstance(value, list):
            value = []
            for option in values:
                value = union_values(value, option["value"])
        result[field] = value
        if field in model.date_fields:
            result[f"{field}_precision"] = values[0]["precision"]
        fields.append(
            {
                "field": field,
                "value": value,
                "options": values,
                "conflict": len(values) > 1 and not isinstance(value, list),
            }
        )
    if kind == EntityType.PERSON:
        result["alt_names"] = [
            name
            for name in union_values(result.get("alt_names", []), [state["record"]["name"] for state in states])
            if name != result["name"]
        ]
    return result, fields


def _entity_merge_preview(
    session: Session, kind: EntityType, entity_ids: Collection[uuid.UUID | str], target_id: uuid.UUID | str | None
) -> dict:
    if kind not in {EntityType.PERSON, EntityType.TEXT}:
        raise ValueError("Table merging supports only people and texts")
    model = Person if kind == EntityType.PERSON else Text
    ids = [uuid.UUID(str(value)) for value in entity_ids]
    target_id = uuid.UUID(str(target_id)) if target_id is not None else min(ids, default=None)
    if len(ids) < 2 or len(set(ids)) != len(ids) or target_id not in ids:
        raise ValueError("Select at least two different records and a survivor from the selection")
    records = session.exec(select(model).where(col(model.id).in_(ids)).order_by(col(model.id)).with_for_update()).all()
    if len(records) != len(ids):
        raise ValueError("Selected records no longer exist. Refresh and review again.")
    ordered = sorted(records, key=lambda record: (record.id != target_id, record.id))
    states = [_state(session, kind, record) for record in ordered]
    result, fields = merge_entity_fields(kind, states)
    return {
        "kind": kind.value,
        "target_id": str(target_id),
        "records": states,
        "fields": fields,
        "result": result,
        "revision": stable_digest([kind, str(target_id), states]),
    }


def preview_entity_merge(
    kind: EntityType | str,
    entity_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str | None = None,
) -> dict:
    """Preview a people/text selection, choosing a deterministic survivor when omitted."""
    with get_session() as session:
        return _entity_merge_preview(session, _kind(kind), entity_ids, target_id)


def preview_people_merge(person_ids: Collection[uuid.UUID | str], *, target_id: uuid.UUID | str) -> dict:
    """Review a whole selection without writes, including all references in the revision."""
    preview = preview_entity_merge(EntityType.PERSON, person_ids, target_id=target_id)
    return {**preview, "people": preview["records"]}


def _merge_choices(preview: dict, choices: dict[str, str] | None, *, require_choices: bool) -> dict:
    model = Person if _kind(preview["kind"]) == EntityType.PERSON else Text
    result = dict(preview["result"])
    conflicts = {field["field"]: field for field in preview["fields"] if field["conflict"]}
    if require_choices and conflicts.keys() - (choices or {}).keys():
        raise ValueError("Choose a value for every conflicting field")
    for field, entity_id in (choices or {}).items():
        options = {option["entity_id"]: option for option in conflicts.get(field, {}).get("options", [])}
        if entity_id not in options:
            raise ValueError("Selected fields are not in the preview")
        result[field] = options[entity_id]["value"]
        if field in model.date_fields:
            result[f"{field}_precision"] = options[entity_id]["precision"]
    if model is Person:
        result["alt_names"] = [
            name
            for name in union_values(result["alt_names"], [state["record"]["name"] for state in preview["records"]])
            if name != result["name"]
        ]
    return result


def _apply_entity_merge(
    writes: EntityWriter,
    preview: dict,
    *,
    choices: dict[str, str] | None,
    require_choices: bool,
    automatic: bool = False,
) -> uuid.UUID:
    session, actor_id = writes.session, writes.actor_id
    kind = _kind(preview["kind"])
    result = _merge_choices(preview, choices, require_choices=require_choices)
    target = session.get(ENTITY_MODELS[kind], uuid.UUID(preview["target_id"]))
    if target is None:
        raise ValueError("Merge target no longer exists")
    before = entity_snapshot(target)
    approved_ids = {uuid.UUID(state["record"]["id"]) for state in preview["records"]}
    aliases = session.exec(
        select(EntityRedirect.old_id).where(
            EntityRedirect.entity_type == kind,
            col(EntityRedirect.target_id).in_(approved_ids),
        )
    ).all()
    review_ids = approved_ids | set(aliases)
    related_cases = session.exec(
        select(ReconciliationCase).where(
            ReconciliationCase.entity_type == kind,
            ReconciliationCase.operation == "merge",
            ReconciliationCase.status == "pending",
            col(ReconciliationCase.target_id).in_(review_ids),
            col(ReconciliationCase.duplicate_id).in_(review_ids),
        )
    ).all()
    writes.update(target, {field: result[field] for field in result.keys() - {"id"}})
    for duplicate in preview["records"][1:]:
        duplicate_id = duplicate["record"]["id"]
        key = stable_digest(["duplicate", kind, sorted([str(target.id), duplicate_id])])
        case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
        case = case or ReconciliationCase(
            key=key,
            entity_type=kind,
            operation="merge",
            reason="Gleicher Titel und gleiche Autoren nach Personenmerge" if automatic else "Datensatzauswahl geprüft",
            incoming={field: duplicate["record"][field] for field in ("name", "title") if field in duplicate["record"]},
        )
        case.target_id = target.id
        case.duplicate_id = uuid.UUID(duplicate_id)
        case.status, case.actor_id = "approved", actor_id
        case.decided_at = dt.datetime.now(dt.UTC)
        case.decision_note = (
            "Nach Personenmerge automatisch anhand von Titel und Autoren zusammengeführt."
            if automatic
            else "Identität und Feldwerte in der Datensatzauswahl bestätigt."
        )
        case.context = {
            **case.context,
            "decision": {"before": before, "after": entity_snapshot(target), "duplicate": duplicate},
        }
        merge_entity(
            writes,
            kind,
            target,
            duplicate_id,
            approved_ids=approved_ids,
        )
        session.add(case)
    for case in related_cases:
        if case.status == "pending":
            case.status, case.actor_id = "approved", actor_id
            case.target_id = target.id
            case.decided_at = dt.datetime.now(dt.UTC)
            case.decision_note = "Durch bestätigte Datensatzauswahl zusammengeführt."
            case.context = {
                **case.context,
                "bulk_merge": {
                    "target_id": str(target.id),
                    "entity_ids": sorted(str(value) for value in approved_ids),
                    "revision": preview["revision"],
                },
            }
            session.add(case)
    return target.id


def _execute_entity_merge(
    kind: EntityType,
    entity_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str,
    revision: str,
    actor_id: str,
    choices: dict[str, str] | None,
    require_choices: bool,
) -> uuid.UUID:
    with get_session() as session:
        target_id = merge_entities_in_session(
            session,
            kind,
            entity_ids,
            target_id=target_id,
            revision=revision,
            actor_id=actor_id,
            choices=choices,
            require_choices=require_choices,
        )
        session.commit()
        return target_id


def merge_entities_in_session(
    session: Session,
    kind: EntityType | str,
    entity_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str,
    revision: str,
    actor_id: str,
    choices: dict[str, str] | None = None,
    require_choices: bool = True,
) -> uuid.UUID:
    """Apply a reviewed selection with history, leaving commit to the caller."""
    preview = _entity_merge_preview(session, _kind(kind), entity_ids, target_id)
    if preview["revision"] != revision:
        raise ValueError("Data changed since preview. Refresh and review again.")
    writes = EntityWriter(session, actor_id)
    target_id = _apply_entity_merge(writes, preview, choices=choices, require_choices=require_choices)
    writes.finish()
    return target_id


def merge_entities(
    kind: EntityType | str,
    entity_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str,
    revision: str,
    actor_id: str,
    choices: dict[str, str] | None = None,
) -> uuid.UUID:
    """Merge a reviewed selection atomically, requiring explicit choices for conflicts."""
    return _execute_entity_merge(
        _kind(kind),
        entity_ids,
        target_id=target_id,
        revision=revision,
        actor_id=actor_id,
        choices=choices,
        require_choices=True,
    )


def merge_people(
    person_ids: Collection[uuid.UUID | str],
    *,
    target_id: uuid.UUID | str,
    revision: str,
    actor_id: str,
    choices: dict[str, str] | None = None,
) -> uuid.UUID:
    """Keep the existing people API's reviewed survivor defaults for unselected conflicts."""
    return _execute_entity_merge(
        EntityType.PERSON,
        person_ids,
        target_id=target_id,
        revision=revision,
        actor_id=actor_id,
        choices=choices,
        require_choices=False,
    )


def _credit_rows(
    session: Session, entity: Production | Text, person_id: uuid.UUID
) -> list[ProductionMembership] | list[TextContribution]:
    if isinstance(entity, Production):
        return list(
            session.exec(
                select(ProductionMembership).where(
                    ProductionMembership.production_id == entity.id, ProductionMembership.person_id == person_id
                )
            ).all()
        )
    return list(
        session.exec(
            select(TextContribution).where(
                TextContribution.text_id == entity.id, TextContribution.person_id == person_id
            )
        ).all()
    )


def _same_credit(row: ProductionMembership | TextContribution, role: str | None, character: str | None) -> bool:
    if isinstance(row, ProductionMembership):
        return credit_role(row.role_name) == role and row.character_name == character
    return credit_role(row.role) == role


def ensure_credit(
    session: Session,
    entity: Production | Text,
    person_id: uuid.UUID,
    role: str | None,
    character: str | None,
    actor_id: str | None,
) -> bool:
    """Add a production membership or text contribution unless an equivalent credit exists."""
    writes = EntityWriter(session, actor_id)
    changed = apply_credit(writes, entity, person_id, role, character)
    writes.finish()
    return changed


def apply_credit(
    writes: EntityWriter,
    entity: Production | Text,
    person_id: uuid.UUID,
    role: str | None,
    character: str | None,
) -> bool:
    """Apply a credit as part of a compound write command."""
    session = writes.session
    role = credit_role(role)
    rows = _credit_rows(session, entity, person_id)
    if role == "actor" and character:
        legacy = next(
            (
                row
                for row in rows
                if isinstance(row, ProductionMembership) and row.role_name == character and not row.character_name
            ),
            None,
        )
        if legacy:
            writes.track(legacy)
            legacy.role_name, legacy.character_name = "actor", character
            session.add(legacy)
            return True
    if any(_same_credit(row, role, character) for row in rows):
        return False
    row = (
        ProductionMembership(production_id=entity.id, person_id=person_id, role_name=role, character_name=character)
        if isinstance(entity, Production)
        else TextContribution(text_id=entity.id, person_id=person_id, role=role)
    )
    session.add(row)
    writes.record(row, "insert")
    return True


def _apply_credits(writes: EntityWriter, case: ReconciliationCase, entity: Entity) -> None:
    session = writes.session
    if not isinstance(entity, (Production, Text)):
        if case.context.get("credits"):
            raise ValueError("Credits are supported for productions and texts only")
        return
    for credit in case.context.get("credits", []):
        person_case = session.get(ReconciliationCase, uuid.UUID(credit["case_id"]))
        if person_case is None or person_case.target_id is None:
            raise ValueError("Approved person case is required")
        person_id = resolve_id(session, EntityType.PERSON, person_case.target_id)
        apply_credit(writes, entity, person_id, credit.get("role"), credit.get("character"))


def _selected_changes(entity: Entity, options: dict, selected: list[str]) -> dict:
    changes = {field: options[field]["value"] for field in selected}
    for field in selected:
        if field in getattr(type(entity), "date_fields", ()):
            changes[f"{field}_precision"] = options[field]["precision"]
    return changes


def _require_general_case(case: ReconciliationCase) -> None:
    if case.operation in {"link_text", "text_lineage"}:
        raise ValueError("Use the text lineage inbox to resolve this case")


def approve_case(
    case_id: uuid.UUID | str,
    *,
    revision: str,
    actor_id: str,
    target_id: uuid.UUID | str | None = None,
    fields: list[str] | None = None,
    allow_create: bool = False,
    note: str = "",
    assessment_choices: dict[str, str] | None = None,
) -> uuid.UUID:
    """Apply exactly reviewed fields atomically; creation always requires explicit opt-in.

    Obtain revision from preview_case with the same target_id. A stale preview,
    unresolved dependency or conflicting source identity aborts the whole operation.
    """
    with get_session() as session:
        entity_id = approve_case_in_session(
            session,
            case_id,
            revision=revision,
            actor_id=actor_id,
            target_id=target_id,
            fields=fields,
            allow_create=allow_create,
            note=note,
            assessment_choices=assessment_choices,
        )
        session.commit()
        return entity_id


def approve_case_in_session(
    session: Session,
    case_id: uuid.UUID | str,
    *,
    revision: str,
    actor_id: str,
    target_id: uuid.UUID | str | None = None,
    fields: list[str] | None = None,
    allow_create: bool = False,
    note: str = "",
    assessment_choices: dict[str, str] | None = None,
) -> uuid.UUID:
    """Apply a reviewed proposal with history, never committing the caller's session."""
    case = session.get(ReconciliationCase, uuid.UUID(str(case_id)), with_for_update=True)
    if case is None or case.status != "pending":
        raise ValueError("Case is missing or already decided")
    _require_general_case(case)
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
    writes = EntityWriter(session, actor_id)
    if target_id:
        entity = _get(session, case.entity_type, target_id)
        writes.update(entity, _selected_changes(entity, options, selected))
    else:
        entity = writes.create(ENTITY_MODELS[case.entity_type], preview["incoming"])
    if case.operation == "merge":
        merge_entity(
            writes,
            case.entity_type,
            entity,
            preview["duplicate_state"]["record"]["id"],
            assessment_choices=assessment_choices,
        )
        case.duplicate_id = uuid.UUID(preview["duplicate_state"]["record"]["id"])
    else:
        _save_source(session, case, entity.id)
        _apply_credits(writes, case, entity)
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
    writes.finish()
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
