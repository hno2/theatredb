"""Accepted Theapolis records and explicit migration of old source proposals."""

import datetime as dt
import logging
import uuid
from typing import TypedDict

from sqlalchemy import delete, insert, update
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, SQLModel, col, func, or_, select

from backend.config import ENTITY_MODELS, LINK_IDENTITY_FIELDS, credit_role
from backend.locations import split_country
from backend.models import (
    EntityRedirect,
    EntitySource,
    EntityType,
    LibraryEntry,
    Organization,
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
from backend.reconciliation import (
    EMPTY,
    audit_event,
    canonical_url,
    ensure_credit,
    entity_snapshot,
    merge_entity_fields,
    resolve_id,
    revalidate,
    save_source_record,
    stable_digest,
    union_values,
    validate_payload,
)
from backend.scd2 import add_scd2_events
from backend.user_management import get_session, init_db

ACTOR = "theapolis"
LOG = logging.getLogger(__name__)


class ImportRecord(TypedDict):
    """A normalized, validated observation ready for batch acceptance."""

    record_key: str
    incoming: dict
    context: dict


MERGE_REASONS = {
    EntityType.PERSON: "Theapolis: gleicher Name",
    EntityType.TEXT: "Theapolis: gleicher Titel und gleiche Autor:innen",
}


def _check_merge_identities(
    session: Session, source_id: uuid.UUID, kind: EntityType, rows: list, redirects: dict
) -> None:
    external_ids = {row.external_id for row in rows if row.entity_id in redirects and row.external_id}
    owners: dict[str, set[uuid.UUID]] = {}
    for model in (SourceRecord, EntitySource):
        matches = session.exec(
            select(model.external_id, model.entity_id).where(
                model.source_id == source_id,
                model.entity_type == kind,
                col(model.external_id).in_(external_ids),
            )
        ).all()
        for external_id, identity in matches:
            if external_id is not None:
                owners.setdefault(external_id, set()).add(identity)
    resolved = resolve_ids(session, kind, set().union(*owners.values()))
    if any(
        len({redirects.get(resolved[identity], resolved[identity]) for identity in group}) > 1
        for group in owners.values()
    ):
        raise ValueError("Theapolis source identity collision with a third entity; resolve it first")


def _merge_states(
    session: Session, model: type[SQLModel], entities: dict, links: list, mappings: list, selected: set
) -> tuple[dict, list]:
    states = {
        identity: {
            "record": entity_snapshot(entities[identity]),
            "references": {},
            "sources": [],
            "source_records": [],
        }
        for identity in selected
    }
    references = []
    target_table = sa_inspect(model).local_table
    for mapper in sa_inspect(model).registry.mappers:
        for column in mapper.columns:
            if any(fk.column.table is target_table for fk in column.foreign_keys):
                referencing = mapper.class_
                rows = session.exec(
                    select(referencing).where(getattr(referencing, column.key).in_(selected)).with_for_update()
                ).all()
                references.append((referencing, column.key, rows))
                for identity in selected:
                    states[identity]["references"][f"{referencing.__tablename__}.{column.key}"] = []
                for row in rows:
                    states[getattr(row, column.key)]["references"][f"{referencing.__tablename__}.{column.key}"].append(
                        entity_snapshot(row)
                    )
    for rows, key in ((links, "sources"), (mappings, "source_records")):
        for row in rows:
            if row.entity_id in selected:
                states[row.entity_id][key].append(entity_snapshot(row))

    return states, references


def _merge_conflicts(kind: EntityType, members: list, entities: dict) -> bool:
    """Best-formatted title wins for texts; any other differing single value needs manual review."""
    _, fields = merge_entity_fields(kind, [{"record": entity_snapshot(entities[identity])} for identity in members])
    return any(field["conflict"] and field["field"] != "title" for field in fields)


def _merge_decisions(
    kind: EntityType, entities: dict, groups: dict, targets: dict, states: dict, actor_id: str
) -> tuple[list, list, list]:
    updated, deleted, decisions = [], [], []
    for name, members in groups.items():
        target_id = targets[name]
        ordered = [states[identity] for identity in sorted(members, key=lambda value: (value != target_id, value))]
        result, _ = merge_entity_fields(kind, ordered)
        target = entities[target_id]
        validated = revalidate(target, result)
        before = entity_snapshot(target)
        for field in result.keys() - {"id"}:
            setattr(target, field, getattr(validated, field))
        if entity_snapshot(target) != before:
            updated.append(target)
        for identity in sorted(members - {target_id}):
            duplicate = entities[identity]
            deleted.append(duplicate)
            decisions.append(
                ReconciliationCase(
                    key=stable_digest(["duplicate", kind, sorted([str(target_id), str(identity)])]),
                    entity_type=kind,
                    operation="merge",
                    reason=MERGE_REASONS[kind],
                    incoming={
                        field: getattr(duplicate, field) for field in ("name", "title") if hasattr(duplicate, field)
                    },
                    target_id=target_id,
                    duplicate_id=identity,
                    status="approved",
                    actor_id=actor_id,
                    decided_at=dt.datetime.now(dt.UTC),
                    decision_note="Konfliktfreie Theapolis-Quellidentitäten zusammengeführt.",
                    context={
                        "decision": {"before": before, "after": entity_snapshot(target), "duplicate": states[identity]}
                    },
                )
            )

    return updated, deleted, decisions


def _deduplicate_merged_links(session: Session, model: type[SQLModel], rows: list, touched: dict) -> list:
    fields = LINK_IDENTITY_FIELDS.get(model)
    if not fields:
        return []
    affected = {tuple(getattr(row, name) for name in fields[:2]) for row in rows if (model, row.id) in touched}
    seen, deleted = set(), []
    for row in sorted(rows, key=lambda row: row.id):
        if tuple(getattr(row, name) for name in fields[:2]) not in affected:
            continue
        key = tuple(credit_role(getattr(row, f)) if f in ("role", "role_name") else getattr(row, f) for f in fields)
        if key in seen:
            deleted.append(row)
            touched.pop((model, row.id), None)
            session.delete(row)
        else:
            seen.add(key)
    return deleted


def _move_references(session: Session, references: list, redirects: dict) -> tuple[list, list]:
    touched = {}
    deleted = []
    for model, field, rows in references:
        for row in rows:
            current = getattr(row, field)
            if redirects[current] != current:
                setattr(row, field, redirects[current])
                touched[model, row.id] = row
        deleted.extend(_deduplicate_merged_links(session, model, rows, touched))
    return list(touched.values()), deleted


def _move_scoped_sources(session: Session, links: list, mappings: list, by_key: dict, redirects: dict) -> None:
    surviving_links = set()
    payloads: dict[uuid.UUID, dict] = {}
    # Texts have no external IDs; archive their link snapshot on any observation of the same entity.
    by_entity = {(row.source_id, row.entity_id): row for row in sorted(mappings, key=lambda row: row.record_key)}
    for row in sorted(links, key=lambda row: (row.entity_id != redirects.get(row.entity_id), row.id)):
        if row.entity_id not in redirects:
            continue
        record = by_key.get((row.source_id, f"id:{row.external_id}")) or by_entity[row.source_id, row.entity_id]
        payload = payloads.get(record.id, record.payload)
        archived = payload.get("merged_sources", [])
        snapshot = entity_snapshot(row)
        if snapshot not in archived:
            payloads[record.id] = {**payload, "merged_sources": [*archived, snapshot]}
        target_id = redirects[row.entity_id]
        if target_id in surviving_links:
            session.delete(row)
        else:
            row.entity_id = target_id
            surviving_links.add(target_id)
    with session.no_autoflush:
        session.execute(
            update(SourceRecord),
            [
                {"id": row.id, "entity_id": redirects[row.entity_id], "payload": payloads.get(row.id, row.payload)}
                for row in mappings
                if row.entity_id in redirects
            ],
        )


def _record_merges(session: Session, kind: EntityType, redirects: dict, decisions: list, actor_id: str) -> None:
    aliases = session.exec(
        select(EntityRedirect).where(EntityRedirect.entity_type == kind, col(EntityRedirect.target_id).in_(redirects))
    ).all()
    review_ids = redirects.keys() | {row.old_id for row in aliases}
    for row in aliases:
        row.target_id = redirects[row.target_id]
    alias_targets = {row.old_id: redirects.get(row.target_id, row.target_id) for row in aliases}
    for case in session.exec(
        select(ReconciliationCase).where(
            ReconciliationCase.entity_type == kind,
            ReconciliationCase.operation == "merge",
            ReconciliationCase.status == "pending",
            col(ReconciliationCase.target_id).in_(review_ids),
            col(ReconciliationCase.duplicate_id).in_(review_ids),
        )
    ):
        if case.target_id is None or case.duplicate_id is None:
            raise ValueError("Pending merge requires two identities")
        target = redirects.get(case.target_id, alias_targets.get(case.target_id))
        duplicate = redirects.get(case.duplicate_id, alias_targets.get(case.duplicate_id))
        if target is not None and target == duplicate:
            case.status, case.actor_id, case.target_id = "approved", actor_id, target
            case.decided_at = dt.datetime.now(dt.UTC)
            case.decision_note = "Durch konfliktfreien Theapolis-Abgleich zusammengeführt."
    existing_cases = {
        row.key: row
        for row in session.exec(
            select(ReconciliationCase).where(col(ReconciliationCase.key).in_([c.key for c in decisions]))
        )
    }
    inserted = []
    for case in decisions:
        existing = existing_cases.get(case.key)
        if existing:
            for field in ("target_id", "duplicate_id", "status", "actor_id", "decided_at", "decision_note"):
                setattr(existing, field, getattr(case, field))
            existing.context = {**existing.context, **case.context}
        else:
            inserted.append(case.model_dump())
    if inserted:
        session.execute(insert(ReconciliationCase), inserted)
    redirect_rows = [
        EntityRedirect(entity_type=kind, old_id=identity, target_id=target).model_dump()
        for identity, target in redirects.items()
        if identity != target
    ]
    if redirect_rows:
        session.execute(insert(EntityRedirect), redirect_rows)


def _scoped_unsupported(
    session: Session, source_id: uuid.UUID, kind: EntityType, ids: set, rows: list, by_key: dict
) -> set[uuid.UUID]:
    """Entities with other sources or rows the bulk path cannot move go to the reviewed merge."""
    if kind == EntityType.PERSON:
        return {
            row.entity_id
            for row in rows
            if row.source_id != source_id
            or not (row.external_id or "").startswith(("directors:", "creators:", "detail:profile:"))
            or (row.source_id, f"id:{row.external_id}") not in by_key
        }
    unsupported = {
        row.entity_id
        for row in rows
        if row.source_id != source_id
        or (isinstance(row, SourceRecord) and not row.record_key.startswith("work:production:"))
    }
    for model, fields in (
        (TextTitle, ("text_id",)),
        (TextLineage, ("source_text_id", "derived_text_id")),
        (LibraryEntry, ("text_id",)),
    ):
        for field in fields:
            unsupported.update(
                session.exec(select(getattr(model, field)).where(getattr(model, field).in_(ids)).distinct())
            )
    return unsupported


def merge_scoped_entities(
    session: Session,
    source_id: uuid.UUID,
    kind: EntityType,
    groups: dict[str, set[uuid.UUID]],
    *,
    actor_id: str,
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[str, set[uuid.UUID]], list[str]]:
    """Batch-merge Theapolis-only people or texts.

    Returns redirects, groups deferred to the reviewed merge (other sources or dependent rows)
    and names of groups skipped because their field values conflict.
    """
    model = ENTITY_MODELS[kind]
    ids = set().union(*groups.values())
    if not ids:
        return {}, {}, []
    entities = {
        row.id: row
        for row in session.exec(select(model).where(col(model.id).in_(ids)).order_by(col(model.id)).with_for_update())
    }
    if entities.keys() != ids:
        raise ValueError("Selected Theapolis records no longer exist; rerun the import")
    mappings = list(
        session.exec(
            select(SourceRecord)
            .where(SourceRecord.entity_type == kind, col(SourceRecord.entity_id).in_(ids))
            .with_for_update()
        )
    )
    links = list(
        session.exec(
            select(EntitySource)
            .where(EntitySource.entity_type == kind, col(EntitySource.entity_id).in_(ids))
            .with_for_update()
        )
    )
    by_key = {(row.source_id, row.record_key): row for row in mappings}
    unsupported = _scoped_unsupported(session, source_id, kind, ids, [*mappings, *links], by_key)
    deferred = {name: members for name, members in groups.items() if members & unsupported}
    conflicting = [
        name
        for name, members in groups.items()
        if name not in deferred and _merge_conflicts(kind, sorted(members), entities)
    ]
    groups = {name: members for name, members in groups.items() if name not in deferred and name not in conflicting}
    if not groups:
        return {}, deferred, conflicting
    # Prefer the record carrying the Theapolis profile link so its public URL stays visible.
    profiled = {row.entity_id for row in links if (row.external_id or "").startswith("detail:")}
    linked = {row.entity_id for row in links if row.external_url}
    targets = {
        name: min(members, key=lambda value: (value not in profiled, value not in linked, value))
        for name, members in groups.items()
    }
    redirects = {identity: targets[name] for name, members in groups.items() for identity in members}
    _check_merge_identities(session, source_id, kind, [*mappings, *links], redirects)
    states, references = _merge_states(session, model, entities, links, mappings, set(redirects))
    updated, deleted, decisions = _merge_decisions(kind, entities, groups, targets, states, actor_id)
    removed = {row.id for row in deleted}
    changed_links, deleted_links = _move_references(session, references, redirects)
    _move_scoped_sources(session, links, mappings, by_key, redirects)
    _record_merges(session, kind, redirects, decisions, actor_id)
    session.flush()
    add_scd2_events(
        session, inserted=[], updated=[*updated, *changed_links], deleted=[*deleted, *deleted_links], actor_id=actor_id
    )
    session.execute(delete(model).where(col(model.id).in_(removed)))
    return redirects, deferred, conflicting


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
    external_ids.extend(
        alias
        for record in records
        for field in ("legacy_ids", "matched_legacy_ids")
        for alias in record["context"].get(field, [])
    )
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
    context = incoming["context"]
    candidates = set().union(
        *(by_external.get(key or "", set()) for key in [context.get("external_id"), *context.get("legacy_ids", [])])
    )
    if record:
        candidates.add(resolved.get(record.entity_id, record.entity_id))
    if len(candidates) > 1:
        raise ValueError(f"Theapolis source identity collision: {kind}:{incoming['record_key']}")
    return next(iter(candidates), None)


def _detail_person_records(kind: EntityType, records: list[ImportRecord]) -> list[ImportRecord]:
    if kind != EntityType.PERSON:
        return records
    fields = {"legacy_ids", "matched_legacy_ids"}
    return [
        {
            **record,
            "incoming": {field: value for field, value in record["incoming"].items() if field not in fields},
            "context": {**record["context"], **{field: record["incoming"][field] for field in aliases}},
        }
        if record["record_key"].startswith("id:detail:") and (aliases := fields & record["incoming"].keys())
        else record
        for record in records
    ]


def _accept_profile_matches(
    incoming: ImportRecord,
    by_key: dict[str, SourceRecord],
    by_external: dict[str, set[uuid.UUID]],
    detail_owners: dict[uuid.UUID, set[str]],
) -> ImportRecord:
    if "matched_legacy_ids" not in incoming["context"]:
        return incoming
    context = dict(incoming["context"])
    aliases = context.pop("matched_legacy_ids")
    key = incoming["record_key"]
    owners = set().union(*(by_external.get(external_id, set()) for external_id in [context["external_id"], *aliases]))
    if (
        len(owners) > 1
        or any(detail_owners.get(owner, set()) - {key} for owner in owners)
        or any(
            (record := by_key.get(f"alias:{alias}")) is not None and record.payload.get("detail_record_key") != key
            for alias in aliases
        )
    ):
        LOG.warning(
            "Theapolis profile overlap for %s conflicts with existing mappings %s; review manually", key, aliases
        )
    else:
        context["legacy_ids"] = aliases
    return {**incoming, "context": context}


def _register_person_aliases(
    source_id: uuid.UUID,
    incoming: ImportRecord,
    identity: uuid.UUID,
    by_key: dict[str, SourceRecord],
    by_external: dict[str, set[uuid.UUID]],
    detail_owners: dict[uuid.UUID, set[str]],
    *,
    older: bool,
) -> None:
    detail_key = incoming["record_key"]
    aliases = incoming["context"].get("legacy_ids", [])
    if detail_key.startswith("id:detail:"):
        known = detail_owners.setdefault(identity, set())
        if aliases and known and detail_key not in known:
            raise ValueError(f"Conflicting Theapolis detail IDs for person {identity}")
        known.add(detail_key)
    for alias in aliases:
        key = f"alias:{alias}"
        record = by_key.get(key)
        if record is not None and record.payload.get("detail_record_key") != detail_key:
            raise ValueError(f"Conflicting Theapolis detail IDs for legacy alias {alias}")
        if record is None and not older:
            by_key[key] = SourceRecord(
                source_id=source_id,
                entity_type=EntityType.PERSON,
                record_key=key,
                external_id=alias,
                entity_id=identity,
                payload={"detail_record_key": detail_key},
            )
            by_external.setdefault(alias, set()).add(identity)


def _detail_owners(
    session: Session, source_id: uuid.UUID, records: list[ImportRecord], ids: set[uuid.UUID]
) -> dict[uuid.UUID, set[str]]:
    owners: dict[uuid.UUID, set[str]] = {}
    if ids and any(
        record["context"].get("legacy_ids") or record["context"].get("matched_legacy_ids") for record in records
    ):
        for identity, key in session.exec(
            select(SourceRecord.entity_id, SourceRecord.record_key).where(
                SourceRecord.source_id == source_id,
                SourceRecord.entity_type == EntityType.PERSON,
                col(SourceRecord.entity_id).in_(ids),
                col(SourceRecord.record_key).startswith("id:detail:"),
            )
        ):
            owners.setdefault(identity, set()).add(key)
    return owners


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


def _fill_blanks(entity: SQLModel, payload: dict, previous: SourceRecord | None) -> bool:
    changes = _enrich(entity, payload, previous)
    if isinstance(entity, Organization):
        country, legacy_region = split_country(entity.address_country)
        if entity.address_country and country != payload.get("address_country"):
            changes.pop("region", None)
        elif (
            legacy_region
            and previous is not None
            and previous.entity_id == entity.id
            and previous.payload.get("incoming", {}).get("address_country") == entity.address_country
        ):
            # Repair only a region still stored verbatim from the previous Theapolis observation.
            changes["address_country"] = country
            if entity.region in EMPTY:
                changes["region"] = payload.get("region") or legacy_region
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
    records = _detail_person_records(kind, records)
    model = ENTITY_MODELS[kind]
    captured_at = _utc(captured_at) if captured_at else None
    by_key, by_external, resolved = _batch_identities(session, source_id, kind, records)
    previous_names = {
        (row.entity_id, row.payload.get("incoming", {}).get("name")): row
        for row in by_key.values()
        if kind == EntityType.PERSON
    }
    ids = set(resolved.values())
    entities = {row.id: row for row in session.exec(select(model).where(col(model.id).in_(ids)).with_for_update())}
    detail_owners = _detail_owners(session, source_id, records, ids)
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
        incoming = _accept_profile_matches(incoming, by_key, by_external, detail_owners)
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
            previous = record or previous_names.get((identity, getattr(entity, "name", None)))
            if _fill_blanks(entity, payload, previous) and identity not in inserted:
                updated[identity] = entity
        _save_observation(source_id, kind, identity, incoming, captured_at, by_key, sources)
        _register_person_aliases(source_id, incoming, identity, by_key, by_external, detail_owners, older=older)
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
    """Add Theapolis credits with one endpoint query and bulk inserts."""
    if not credits:
        return
    model, endpoint, role_field = (
        (ProductionMembership, "production_id", "role_name")
        if kind == EntityType.PRODUCTION
        else (TextContribution, "text_id", "role")
    )
    rows = session.exec(select(model).where(getattr(model, endpoint).in_(credits)).with_for_update()).all()
    existing = {
        (
            getattr(row, endpoint),
            row.person_id,
            credit_role(getattr(row, role_field)),
            normalize_character(getattr(row, "character_name", None)),
        )
        for row in rows
    }
    inserted: list[SQLModel] = []
    for entity_id, members in credits.items():
        for credit in members:
            person_id, role = uuid.UUID(credit["person_id"]), credit_role(credit["role"])
            character = normalize_character(credit.get("character")) if kind == EntityType.PRODUCTION else None
            key = (entity_id, person_id, role, character)
            if key in existing:
                continue
            payload = {endpoint: entity_id, "person_id": person_id, role_field: role}
            if character is not None:
                payload["character_name"] = character
            inserted.append(model.model_validate(payload))
            existing.add(key)
    if inserted:
        session.execute(insert(model), [row.model_dump() for row in inserted])
    session.flush()
    add_scd2_events(session, inserted=inserted, updated=[], actor_id=actor_id)


def normalize_character(value: str | None) -> str | None:
    """Trim surrounding space without changing character spelling, case, or internal spacing."""
    return (value or "").strip() or None


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


def _enrich(entity: SQLModel, payload: dict, previous: SourceRecord | None = None) -> dict:
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
    if isinstance(entity, Person) and payload.get("name"):
        main_name = entity.name
        if previous is not None and previous.payload.get("incoming", {}).get("name") == main_name:
            main_name = changes["name"] = payload["name"]
        aliases = union_values(entity.alt_names, [entity.name, payload["name"], *payload.get("alt_names", [])])
        changes["alt_names"] = [name for name in aliases if name != main_name]
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
            previous = next((record for record in observations if record.record_key == record_key), None)
            changes = _enrich(entity, payload, previous)
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
