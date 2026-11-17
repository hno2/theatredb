"""Accepted Theapolis records and explicit migration of old source proposals."""

import datetime as dt
import uuid
from typing import TypedDict

from sqlalchemy import insert
from sqlmodel import Session, SQLModel, col, func, or_, select

from backend.config import ENTITY_MODELS, canonical_role
from backend.models import (
    EntityRedirect,
    EntitySource,
    EntityType,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
)
from backend.reconciliation import (
    EMPTY,
    audit_event,
    canonical_url,
    ensure_credit,
    entity_snapshot,
    resolve_id,
    revalidate,
    save_source_record,
    union_values,
    validate_payload,
)
from backend.scd2 import add_scd2_events
from backend.user_management import get_session, init_db

ACTOR = "theapolis"


class ImportRecord(TypedDict):
    """A normalized, validated observation ready for batch acceptance."""

    record_key: str
    incoming: dict
    context: dict


def resolve_ids(session: Session, kind: EntityType, identities: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Resolve just the batch's redirect chains using indexed set queries."""
    redirects = {}
    pending = identities
    queried = set()
    while pending:
        rows = session.exec(
            select(EntityRedirect).where(EntityRedirect.entity_type == kind, col(EntityRedirect.old_id).in_(pending))
        ).all()
        queried.update(pending)
        redirects.update((row.old_id, row.target_id) for row in rows)
        pending = {row.target_id for row in rows} - queried
    result = {}
    for identity in identities:
        current, seen = identity, set()
        while current in redirects:
            if current in seen:
                raise ValueError("Entity redirect cycle")
            seen.add(current)
            current = redirects[current]
        result[identity] = current
    return result


def _batch_identities(
    session: Session,
    source_id: uuid.UUID,
    kind: EntityType,
    records: list[ImportRecord],
) -> tuple[dict[str, SourceRecord], dict[str, set[uuid.UUID]], dict[uuid.UUID, uuid.UUID]]:
    keys = [record["record_key"] for record in records]
    external_ids = [record["context"]["external_id"] for record in records if record["context"].get("external_id")]
    observations = session.exec(
        select(SourceRecord)
        .where(
            SourceRecord.source_id == source_id,
            SourceRecord.entity_type == kind,
            or_(col(SourceRecord.record_key).in_(keys), col(SourceRecord.external_id).in_(external_ids)),
        )
        .with_for_update()
    ).all()
    external_sources = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source_id,
            EntitySource.entity_type == kind,
            col(EntitySource.external_id).in_(external_ids),
        )
    ).all()
    resolved = resolve_ids(session, kind, {row.entity_id for row in [*observations, *external_sources]})
    by_external: dict[str, set[uuid.UUID]] = {}
    for row in [*observations, *external_sources]:
        if row.external_id:
            by_external.setdefault(row.external_id, set()).add(resolved[row.entity_id])
    return {row.record_key: row for row in observations}, by_external, resolved


def _mapped_identity(
    kind: EntityType,
    incoming: ImportRecord,
    record: SourceRecord | None,
    by_external: dict[str, set[uuid.UUID]],
    resolved: dict[uuid.UUID, uuid.UUID],
) -> uuid.UUID | None:
    candidates = set(by_external.get(incoming["context"].get("external_id") or "", set()))
    if record:
        candidates.add(resolved.get(record.entity_id, record.entity_id))
    if len(candidates) > 1:
        raise ValueError(f"Theapolis source identity collision: {kind}:{incoming['record_key']}")
    return next(iter(candidates), None)


def _observe(
    record: SourceRecord,
    source: EntitySource,
    entity_id: uuid.UUID,
    incoming: ImportRecord,
    captured_at: dt.datetime | None,
) -> None:
    context = incoming["context"]
    record.entity_id = entity_id
    record.external_id = context.get("external_id")
    record.external_url = context.get("external_url")
    payload = {**record.payload, "incoming": incoming["incoming"], "context": context}
    if record.payload != payload:
        record.payload = payload
    record.captured_at = captured_at
    source.reviewed = True
    if captured_at and (source.sourced_at is None or _utc(source.sourced_at) <= captured_at):
        source.sourced_at = captured_at


def _fill_blanks(entity: SQLModel, payload: dict) -> bool:
    changes = _enrich(entity, payload)
    if not any(getattr(entity, field) != value for field, value in changes.items()):
        return False
    validated = revalidate(entity, changes)
    for field in changes:
        setattr(entity, field, getattr(validated, field))
    return True


def _save_observation(
    source_id: uuid.UUID,
    kind: EntityType,
    identity: uuid.UUID,
    incoming: ImportRecord,
    captured_at: dt.datetime | None,
    by_key: dict[str, SourceRecord],
    sources: dict[uuid.UUID, EntitySource],
) -> None:
    key, context = incoming["record_key"], incoming["context"]
    record = by_key.get(key)
    if record and record.captured_at and (captured_at is None or captured_at < _utc(record.captured_at)):
        return
    if record is None:
        record = SourceRecord(source_id=source_id, entity_type=kind, record_key=key, entity_id=identity)
        by_key[key] = record
    source = sources.get(identity)
    if source is None:
        source = EntitySource(
            source_id=source_id,
            entity_type=kind,
            entity_id=identity,
            external_id=context.get("external_id"),
            external_url=context.get("external_url"),
        )
        sources[identity] = source
    _observe(record, source, identity, incoming, captured_at)


def accept_records(
    session: Session,
    source_id: uuid.UUID,
    kind: EntityType,
    records: list[ImportRecord],
    *,
    captured_at: dt.datetime | None,
    actor_id: str,
) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
    """Accept a bounded batch, returning identities and changed observation targets."""
    model = ENTITY_MODELS[kind]
    captured_at = _utc(captured_at) if captured_at else None
    by_key, by_external, resolved = _batch_identities(session, source_id, kind, records)
    ids = set(resolved.values())
    entities = {row.id: row for row in session.exec(select(model).where(col(model.id).in_(ids)).with_for_update())}
    latest = {
        entity_id: _utc(captured)
        for entity_id, captured in session.exec(
            select(SourceRecord.entity_id, func.max(SourceRecord.captured_at))
            .where(
                SourceRecord.source_id == source_id,
                SourceRecord.entity_type == kind,
                col(SourceRecord.entity_id).in_(ids),
                col(SourceRecord.captured_at).is_not(None),
            )
            .group_by(col(SourceRecord.entity_id))
        )
        if captured is not None
    }
    sources = {
        row.entity_id: row
        for row in session.exec(
            select(EntitySource).where(
                EntitySource.source_id == source_id,
                EntitySource.entity_type == kind,
                col(EntitySource.entity_id).in_(ids),
            )
        )
    }
    inserted, updated = {}, {}
    changed = set()

    def accept(incoming: ImportRecord) -> uuid.UUID:
        key, payload = incoming["record_key"], incoming["incoming"]
        record = by_key.get(key)
        identity = _mapped_identity(kind, incoming, record, by_external, resolved)
        entity = entities.get(identity) if identity else None
        if identity and entity is None:
            raise ValueError(f"Theapolis mapped record no longer exists: {kind}:{key}")
        if entity is None:
            entity = model.model_validate(payload)
            entities[entity.id] = entity
            inserted[entity.id] = entity
        identity = entity.id
        older = identity in latest and (captured_at is None or captured_at < latest[identity])
        unchanged = (
            record is not None
            and record.entity_id == identity
            and record.payload.get("incoming") == payload
            and record.payload.get("context") == incoming["context"]
        )
        if not unchanged and not older:
            changed.add(identity)
            if _fill_blanks(entity, payload) and identity not in inserted:
                updated[identity] = entity
        _save_observation(source_id, kind, identity, incoming, captured_at, by_key, sources)
        if captured_at:
            latest[identity] = max(latest.get(identity, captured_at), captured_at)
        if external_id := incoming["context"].get("external_id"):
            by_external.setdefault(external_id, set()).add(identity)
        return identity

    result = [accept(incoming) for incoming in records]
    # Bulk inserts bypass mapper events; entities were validated before this boundary.
    for table, rows in ((model, inserted.values()), (SourceRecord, by_key.values()), (EntitySource, sources.values())):
        values = [row.model_dump() for row in rows if row not in session]
        if values:
            session.execute(insert(table), values)
    session.flush()
    add_scd2_events(session, inserted=list(inserted.values()), updated=list(updated.values()), actor_id=actor_id)
    return result, changed


def add_credits_batch(
    session: Session, kind: EntityType, credits: dict[uuid.UUID, list[dict]], *, actor_id: str
) -> None:
    """Add Theapolis director/author credits with one endpoint query and bulk inserts."""
    if not credits:
        return
    model, endpoint = (
        (ProductionMembership, "production_id") if kind == EntityType.PRODUCTION else (TextContribution, "text_id")
    )
    rows = session.exec(select(model).where(getattr(model, endpoint).in_(credits)).with_for_update()).all()
    existing = {
        (getattr(row, endpoint), row.person_id, canonical_role(row.role), getattr(row, "character", None))
        for row in rows
    }
    inserted: list[SQLModel] = []
    for entity_id, members in credits.items():
        for credit in members:
            person_id, role = uuid.UUID(credit["person_id"]), canonical_role(credit["role"])
            key = (entity_id, person_id, role, None)
            if key in existing:
                continue
            inserted.append(model.model_validate({endpoint: entity_id, "person_id": person_id, "role": role}))
            existing.add(key)
    if inserted:
        session.execute(insert(model), [row.model_dump() for row in inserted])
    session.flush()
    add_scd2_events(session, inserted=inserted, updated=[], actor_id=actor_id)


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _identity(
    session: Session, source_id: uuid.UUID, kind: EntityType, key: str, external_id: str | None
) -> uuid.UUID | None:
    ids = set(
        session.exec(
            select(SourceRecord.entity_id).where(
                SourceRecord.source_id == source_id,
                SourceRecord.entity_type == kind,
                SourceRecord.record_key == key,
            )
        ).all()
    )
    if external_id:
        ids.update(
            session.exec(
                select(EntitySource.entity_id).where(
                    EntitySource.source_id == source_id,
                    EntitySource.entity_type == kind,
                    EntitySource.external_id == external_id,
                )
            ).all()
        )
        ids.update(
            session.exec(
                select(SourceRecord.entity_id).where(
                    SourceRecord.source_id == source_id,
                    SourceRecord.entity_type == kind,
                    SourceRecord.external_id == external_id,
                )
            ).all()
        )
    resolved = {resolve_id(session, kind, identity) for identity in ids}
    if len(resolved) > 1:
        raise ValueError(f"Theapolis source identity collision: {kind}:{key}")
    return next(iter(resolved), None)


def _enrich(entity: SQLModel, payload: dict) -> dict:
    changes = {}
    date_fields = getattr(type(entity), "date_fields", ())
    for field, value in payload.items():
        if field in {f"{name}_precision" for name in date_fields} or value in EMPTY:
            continue
        current = getattr(entity, field)
        if isinstance(value, list):
            changes[field] = union_values(current or [], value)
        elif current in EMPTY:
            changes[field] = value
            if field in date_fields:
                changes[f"{field}_precision"] = payload[f"{field}_precision"]
    return changes


def accept_record(
    session: Session,
    source_id: uuid.UUID,
    kind: EntityType,
    incoming: dict,
    *,
    record_key: str,
    context: dict,
    captured_at: dt.datetime | None,
    actor_id: str = ACTOR,
) -> uuid.UUID:
    """Accept one stable source identity, filling blanks without overwriting existing values."""
    model = ENTITY_MODELS[kind]
    captured_at = _utc(captured_at) if captured_at is not None else None
    if context.get("external_url"):
        context = {**context, "external_url": canonical_url(context["external_url"])}
    payload = validate_payload(kind, incoming, complete=True)
    identity = _identity(session, source_id, kind, record_key, context.get("external_id"))
    entity = session.get(model, identity, with_for_update=True) if identity else None
    if identity and entity is None:
        raise ValueError(f"Theapolis mapped record no longer exists: {kind}:{record_key}")
    if entity is None:
        entity = model.model_validate(payload)
        session.add(entity)
        session.flush()
        audit_event(session, entity, "insert", actor_id)
    else:
        observations = session.exec(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id,
                SourceRecord.entity_type == kind,
                SourceRecord.entity_id == entity.id,
            )
        ).all()
        dates = [_utc(record.captured_at) for record in observations if record.captured_at]
        older = bool(dates) and (captured_at is None or _utc(captured_at) < max(dates))
        before = entity_snapshot(entity)
        changes = {}
        if not older:
            changes = _enrich(entity, payload)
            validated = revalidate(entity, changes)
            for field in changes:
                setattr(entity, field, getattr(validated, field))
        if entity_snapshot(entity) != before:
            session.add(entity)
            audit_event(session, entity, "update", actor_id)
    save_source_record(
        session,
        kind=kind,
        source_id=source_id,
        record_key=record_key,
        entity_id=entity.id,
        incoming=payload,
        context=context,
        captured_at=captured_at,
    )
    session.flush()
    return entity.id


def add_credits(
    session: Session, kind: EntityType, entity_id: uuid.UUID, credits: list[dict], *, actor_id: str = ACTOR
) -> None:
    """Link accepted person identities without removing existing credits."""
    if kind not in {EntityType.PRODUCTION, EntityType.TEXT}:
        if credits:
            raise ValueError("Theapolis credits require a production or text")
        return
    entity = session.get(Production if kind == EntityType.PRODUCTION else Text, entity_id)
    if entity is None:
        raise ValueError("Theapolis credit target no longer exists")
    for credit in credits:
        person_id = resolve_id(session, EntityType.PERSON, credit["person_id"])
        if session.get(Person, person_id) is None:
            raise ValueError("Theapolis credit person no longer exists")
        ensure_credit(session, entity, person_id, credit.get("role"), credit.get("character"), actor_id)


def _case_identity(case: ReconciliationCase) -> tuple[str, dict]:
    context = dict(case.context)
    external_id = context.get("external_id")
    key = case.record_key
    if not key:
        raise ValueError(f"Theapolis case {case.id} has no source record key")
    if case.entity_type == EntityType.PERSON and external_id and ":" not in external_id:
        roles = set(case.incoming.get("type") or [])
        namespace = "directors" if roles == {"director"} else "creators" if roles == {"author"} else None
        if namespace is None:
            raise ValueError(f"Ambiguous legacy Theapolis person case {case.id}; reimport original snapshot")
        external_id = f"{namespace}:{external_id}"
        key = f"id:{external_id}"
        context["external_id"] = external_id
    context["record_key"] = key
    return key, context


def _map_approved_dependency(session: Session, source_id: uuid.UUID, case: ReconciliationCase) -> None:
    if case.source_id != source_id:
        return
    if case.target_id is None:
        raise ValueError(f"Approved Theapolis dependency has no target: {case.id}")
    key, context = _case_identity(case)
    target_id = resolve_id(session, case.entity_type, case.target_id)
    identity = _identity(session, source_id, case.entity_type, key, context.get("external_id"))
    if identity is not None:
        if identity != target_id:
            raise ValueError(f"Theapolis source identity collision for approved case {case.id}")
        return
    save_source_record(
        session,
        kind=case.entity_type,
        source_id=source_id,
        record_key=key,
        entity_id=target_id,
        incoming=case.incoming,
        context=context,
        captured_at=_utc(case.captured_at) if case.captured_at else None,
    )


def _migrate_case(session: Session, source_id: uuid.UUID, case: ReconciliationCase, prepared: dict) -> uuid.UUID | None:
    incoming = dict(case.incoming)
    context = dict(prepared[case.id][1])
    required = [
        *(dep["case_id"] for dep in context.get("dependencies", [])),
        *(credit["case_id"] for credit in context.get("credits", [])),
    ]
    dependencies = {identity: session.get(ReconciliationCase, uuid.UUID(identity)) for identity in required}
    if any(dep is None or (dep.id not in prepared and dep.status != "approved") for dep in dependencies.values()):
        raise ValueError(f"Missing or unapproved Theapolis dependency for case {case.id}")
    if any(dep.status != "approved" for dep in dependencies.values() if dep is not None):
        return None
    for related in dependencies.values():
        if related is not None:
            _map_approved_dependency(session, source_id, related)
    for dep in context.get("dependencies", []):
        related = dependencies[dep["case_id"]]
        expected = {"organization_id": EntityType.ORGANIZATION, "text_id": EntityType.TEXT}.get(dep["field"])
        if related is None or related.entity_type != expected or related.target_id is None:
            raise ValueError(f"Invalid Theapolis dependency for case {case.id}")
        incoming[dep["field"]] = str(resolve_id(session, related.entity_type, related.target_id))
    credits = []
    for credit in context.get("credits", []):
        related = dependencies[credit["case_id"]]
        if related is None or related.entity_type != EntityType.PERSON or related.target_id is None:
            raise ValueError(f"Invalid Theapolis person dependency for case {case.id}")
        credits.append({**credit, "person_id": str(related.target_id)})
    entity_id = accept_record(
        session,
        source_id,
        case.entity_type,
        incoming,
        record_key=prepared[case.id][0],
        context=context,
        captured_at=case.captured_at,
        actor_id="theapolis-migration",
    )
    add_credits(session, case.entity_type, entity_id, credits, actor_id="theapolis-migration")
    entity = session.get(ENTITY_MODELS[case.entity_type], entity_id)
    if entity is None:
        raise ValueError(f"Migrated Theapolis entity missing: {case.id}")
    case.target_id = entity_id
    case.status = "approved"
    case.actor_id = "theapolis-migration"
    case.decided_at = dt.datetime.now(dt.UTC)
    case.decision_note = "Theapolis als akzeptierte Quelle migriert; bestehende Konfliktwerte beibehalten."
    case.context = {
        **case.context,
        "migration": {
            "record_key": prepared[case.id][0],
            "target_id": str(entity_id),
            "after": entity_snapshot(entity),
        },
    }
    session.add(case)
    return entity_id


def migrate_pending_cases() -> dict[str, list[str]]:
    """Explicitly accept stored Theapolis proposals in dependency order, preserving all decisions."""
    init_db()
    result: dict[str, list[str]] = {
        kind.value: [] for kind in (EntityType.ORGANIZATION, EntityType.PERSON, EntityType.TEXT, EntityType.PRODUCTION)
    }
    with get_session() as session:
        source = session.exec(select(Source).where(Source.name == "theapolis.de")).first()
        if source is None:
            return result
        cases = session.exec(
            select(ReconciliationCase)
            .where(
                ReconciliationCase.source_id == source.id,
                ReconciliationCase.operation == "import",
                ReconciliationCase.status == "pending",
            )
            .with_for_update()
        ).all()
        prepared = {case.id: _case_identity(case) for case in cases}
        for case in cases:
            validate_payload(case.entity_type, case.incoming, complete=True)
            if case.entity_type.value not in result:
                raise ValueError(f"Unsupported Theapolis case type: {case.id}")
        pending = sorted(
            cases,
            key=lambda case: (
                list(result).index(case.entity_type.value),
                -_utc(case.captured_at or case.created_at).timestamp(),
                str(case.id),
            ),
        )
        while pending:
            progressed = False
            for case in pending[:]:
                entity_id = _migrate_case(session, source.id, case, prepared)
                if entity_id is None:
                    continue
                if str(entity_id) not in result[case.entity_type.value]:
                    result[case.entity_type.value].append(str(entity_id))
                pending.remove(case)
                progressed = True
            if not progressed:
                raise ValueError(f"Cyclic Theapolis dependencies: {', '.join(str(case.id) for case in pending)}")
        session.commit()
    return result
