"""Accepted Theapolis records and explicit migration of old source proposals."""

import datetime as dt
import uuid

from sqlmodel import Session, SQLModel, select

from backend.config import ENTITY_MODELS
from backend.models import EntitySource, EntityType, Person, Production, ReconciliationCase, Source, SourceRecord, Text
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
from backend.user_management import get_session, init_db

ACTOR = "theapolis"


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
