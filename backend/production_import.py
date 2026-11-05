"""Theatre uploads: enrich productions automatically and queue only real conflicts for review.

Matching is always restricted to the selected theatre. For every uploaded production:

* same title (or same source page) without conflicting values: missing fields, people,
  text and the source are added automatically,
* no similar title in the theatre: the production is created with its people, text and source,
* otherwise a review case is queued; :func:`production_review` compares it side by side with the
  best fitting production and :func:`resolve_production_case` performs the merge in the backend.

People and texts are matched against existing records before anything is created.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter
from difflib import SequenceMatcher
from operator import itemgetter
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlmodel import Session, col, select

from backend.config import CREDIT_ROLES, canonical_role
from backend.config import PRODUCTION_FIELD_LABELS as FIELD_LABELS
from backend.ingestion.staging import validate_records
from backend.models import (
    EntitySource,
    EntityType,
    ImportRun,
    Organization,
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
    decide_case,
    ensure_credit,
    entity_snapshot,
    normalize,
    resolve_id,
    revalidate,
    stable_digest,
    union_values,
    validate_payload,
)
from backend.user_management import get_session

OPERATION = "production_upload"
# Titles at or above this score are treated as "possibly the same production" and reviewed.
TITLE_THRESHOLD = 0.8
LIST_FIELDS = frozenset({"tags", "critics"})
# Every source has its own page; a different URL is stored as source link, never a conflict.
FILL_ONLY_FIELDS = frozenset({"url"})
PERSON_FILL_FIELDS = ("description", "born", "died", "nationality")


def title_similarity(left: str, right: str) -> float:
    """Score title similarity from 0 to 1; a title fully contained in the other counts as similar."""
    left, right = normalize(left), normalize(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    matcher = SequenceMatcher(None, left, right)
    score = matcher.ratio() if matcher.real_quick_ratio() >= TITLE_THRESHOLD else 0.0
    shorter, longer = sorted((left.split(), right.split()), key=len)
    if set(shorter) <= set(longer) and len("".join(shorter)) >= 4:
        score = max(score, 0.85)
    return round(score, 4)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Expected text, got {type(value).__name__}")
    return value.strip() or None


def _credit(member: dict) -> dict:
    raw_role = _clean(member.get("role"))
    raw_types = member.get("type") or raw_role or "actor"
    types = [
        kind
        for kind in dict.fromkeys(
            canonical_role(kind) for kind in (raw_types if isinstance(raw_types, list) else [raw_types])
        )
        if kind
    ]
    role = canonical_role(raw_role) or types[0]
    if role not in CREDIT_ROLES:
        role = types[0]
    if role == "theatre_educator" and types == ["actor"]:
        types = [role]
    return {
        "name": member["name"].strip(),
        "role": role,
        "character": (_clean(member.get("character")) or raw_role) if role == "actor" else None,
        "types": types,
        "urls": list(dict.fromkeys([*([member["url"]] if member.get("url") else []), *member.get("urls", [])])),
        **{field: member.get(field) for field in PERSON_FILL_FIELDS},
    }


def prepare_record(record: dict) -> dict:
    """Turn one validated staging record into production fields, credits and authors."""
    members = list(record.get("memberships", []))
    author = _clean(record.get("author"))
    if author and not any(normalize(member["name"]) == normalize(author) for member in members):
        members.append({"name": author, "type": "author", "role": "author"})
    credits = [_credit(member) for member in members]
    premiere = _clean(record.get("premiere"))
    incoming = validate_payload(
        EntityType.PRODUCTION,
        {
            "name": record["title"].strip(),
            "description": _clean(record.get("description")),
            "premiere": dt.datetime.fromisoformat(premiere).isoformat() if premiere else None,
            **({"premiere_year": record["premiere_year"]} if record.get("premiere_year") is not None else {}),
            "duration": record.get("duration"),
            "age_rating": record.get("age_rating"),
            "url": record["url"],
            "tags": record.get("tags", []),
            "critics": record.get("critics", []),
        },
        complete=True,
    )
    return {
        "incoming": incoming,
        "credits": credits,
        "authors": list(dict.fromkeys(c["name"] for c in credits if c["role"] == "author")),
        "record_key": canonical_url(record["url"]),
    }


# --- comparison -----------------------------------------------------------------------------------------------


def _same(field: str, left: object, right: object) -> bool:
    if field == "premiere":
        return str(left)[:10] == str(right)[:10]
    if isinstance(left, str) and isinstance(right, str):
        return normalize(left) == normalize(right) if field == "name" else left.split() == right.split()
    return left == right


def _field_row(field: str, current: object, incoming: object) -> dict:
    """Classify one field: same, new (fills a gap), extended (list), kept (only existing) or conflict."""
    if field in LIST_FIELDS:
        current = current if isinstance(current, list) else []
        incoming = incoming if isinstance(incoming, list) else []
        result = union_values(current, incoming)
        if not current and not incoming:
            status = "empty"
        elif not incoming:
            status = "kept"
        elif not current:
            status = "new"
        else:
            status = "same" if result == current else "extended"
    elif current in EMPTY and incoming in EMPTY:
        status, result = "empty", None
    elif incoming in EMPTY:
        status, result = "kept", current
    elif current in EMPTY:
        status, result = "new", incoming
    elif _same(field, current, incoming):
        status, result = "same", current
    else:
        status, result = ("kept" if field in FILL_ONLY_FIELDS else "conflict"), current
    return {
        "field": field,
        "label": FIELD_LABELS[field],
        "existing": current,
        "incoming": incoming,
        "result": result,
        "status": status,
    }


def _members(session: Session, production: Production | None) -> list[tuple[ProductionMembership, Person]]:
    if production is None:
        return []
    return list(
        session.exec(
            select(ProductionMembership, Person)
            .join(Person, col(Person.id) == col(ProductionMembership.person_id))
            .where(ProductionMembership.production_id == production.id)
            .order_by(col(Person.name))
        ).all()
    )


def _credit_matches(credit: dict, membership: ProductionMembership, person: Person) -> bool:
    if normalize(person.name) != normalize(credit["name"]):
        return False
    role = canonical_role(membership.role)
    return role == credit["role"] or (
        credit["role"] == "actor" and bool(credit["character"]) and membership.role == credit["character"]
    )


def _directors_conflict(members: list[tuple[ProductionMembership, Person]], credits: list[dict]) -> bool:
    existing = {normalize(p.name) for m, p in members if canonical_role(m.role) == "director"}
    incoming = {normalize(c["name"]) for c in credits if c["role"] == "director"}
    return bool(existing and incoming and not existing & incoming)


def _find_person(
    session: Session,
    credit: dict,
    *,
    production: Production | None,
    organization_id: uuid.UUID,
    source_id: uuid.UUID | None,
) -> Person | None:
    """Find an existing person: source identity, production cast, then unique (theatre-preferred) namesake."""
    name = normalize(credit["name"])
    if source_id:
        for url in credit["urls"]:
            key = canonical_url(url)
            identity = (
                session.exec(
                    select(SourceRecord.entity_id).where(
                        SourceRecord.source_id == source_id,
                        SourceRecord.entity_type == EntityType.PERSON,
                        SourceRecord.record_key == key,
                    )
                ).first()
                or session.exec(
                    select(EntitySource.entity_id).where(
                        EntitySource.source_id == source_id,
                        EntitySource.entity_type == EntityType.PERSON,
                        col(EntitySource.external_url).in_([url, key]),
                    )
                ).first()
            )
            person = session.get(Person, resolve_id(session, EntityType.PERSON, identity)) if identity else None
            if person:
                return person
    for _, person in _members(session, production):
        if normalize(person.name) == name:
            return person
    token = max(name.split(), key=len, default="")
    if not token:
        return None
    rows = [
        row
        for row in session.exec(
            select(Person).where(col(Person.name).icontains(token, autoescape=True)).order_by(col(Person.id)).limit(500)
        ).all()
        if normalize(row.name) == name
    ]
    if len(rows) <= 1:
        return rows[0] if rows else None
    # Namesakes: prefer the person who already works at this theatre, then the most credited one.
    local = dict(
        session.exec(
            select(ProductionMembership.person_id, func.count())
            .join(Production, col(Production.id) == col(ProductionMembership.production_id))
            .where(
                Production.organization_id == organization_id,
                col(ProductionMembership.person_id).in_([row.id for row in rows]),
            )
            .group_by(col(ProductionMembership.person_id))
        ).all()
    )
    return max(rows, key=lambda row: (local.get(row.id, 0), len(row.production_memberships)))


def _people_rows(
    session: Session,
    production: Production | None,
    credits: list[dict],
    organization_id: uuid.UUID,
    source_id: uuid.UUID | None,
) -> list[dict]:
    members = _members(session, production)
    matched: set[uuid.UUID] = set()
    rows = []
    for credit in credits:
        match = next((m for m, p in members if m.id not in matched and _credit_matches(credit, m, p)), None)
        if match:
            matched.add(match.id)
            status = "same"
        else:
            known = _find_person(
                session, credit, production=production, organization_id=organization_id, source_id=source_id
            )
            status = "new_known" if known else "new_person"
        rows.append(
            {"name": credit["name"], "role": credit["role"], "character": credit["character"], "status": status}
        )
    rows.extend(
        {
            "name": person.name,
            "role": canonical_role(membership.role),
            "character": membership.character,
            "status": "kept",
        }
        for membership, person in members
        if membership.id not in matched
    )
    return rows


def _text_authors(session: Session, text_id: uuid.UUID) -> list[Person]:
    rows = session.exec(
        select(Person, TextContribution.role)
        .join(TextContribution, col(TextContribution.person_id) == col(Person.id))
        .where(TextContribution.text_id == text_id)
        .order_by(col(Person.name))
    ).all()
    return list({person.id: person for person, role in rows if canonical_role(role) == "author"}.values())


def _text_label(title: str, authors: list[str]) -> str:
    return f"{title} ({', '.join(authors)})" if authors else title


def _text_row(session: Session, production: Production | None, item: dict) -> dict:
    incoming_authors = item["authors"]
    incoming = _text_label(item["incoming"]["name"], incoming_authors)
    text = session.get(Text, production.text_id) if production and production.text_id else None
    if text is None:
        return {
            "field": "text",
            "label": "Werk / Autor:in",
            "existing": None,
            "incoming": incoming,
            "result": incoming,
            "status": "new",
        }
    authors = [person.name for person in _text_authors(session, text.id)]
    existing = _text_label(text.title, authors)
    known, new = {normalize(name) for name in authors}, {normalize(name) for name in incoming_authors}
    if not new or new <= known:
        status = "same" if new else "kept"
    elif known and not known & new:
        status = "conflict"
    else:
        status = "extended"
    result = (
        existing
        if status in {"same", "kept", "conflict"}
        else _text_label(text.title, list(dict.fromkeys([*authors, *incoming_authors])))
    )
    return {
        "field": "text",
        "label": "Werk / Autor:in",
        "existing": existing,
        "incoming": incoming,
        "result": result,
        "status": status,
    }


def _compare(
    session: Session,
    production: Production | None,
    item: dict,
    organization_id: uuid.UUID,
    source_id: uuid.UUID | None,
) -> dict:
    current = entity_snapshot(production) if production else {}
    fields = [_field_row(field, current.get(field), item["incoming"].get(field)) for field in FIELD_LABELS]
    fields.append(_text_row(session, production, item))
    people = _people_rows(session, production, item["credits"], organization_id, source_id)
    conflicts = [row["field"] for row in fields if row["status"] == "conflict"]
    if production and _directors_conflict(_members(session, production), item["credits"]):
        conflicts.append("director")
    return {"fields": fields, "people": people, "conflicts": conflicts}


# --- applying -------------------------------------------------------------------------------------------------


def _link_source(
    session: Session,
    *,
    source_id: uuid.UUID,
    kind: EntityType,
    entity_id: uuid.UUID,
    record_key: str,
    external_url: str | None,
    payload: dict,
    captured_at: dt.datetime | None,
    reassign: bool,
) -> bool:
    """Record the source identity and source link; return whether the entity gained a new source."""
    record = session.exec(
        select(SourceRecord).where(
            SourceRecord.source_id == source_id,
            SourceRecord.entity_type == kind,
            SourceRecord.record_key == record_key,
        )
    ).first()
    if record is None:
        record = SourceRecord(source_id=source_id, entity_type=kind, record_key=record_key, entity_id=entity_id)
    if record.entity_id == entity_id or reassign:
        record.entity_id = entity_id
        record.external_url = external_url
        record.payload = payload
        if captured_at or record.captured_at is None:
            record.captured_at = captured_at
        session.add(record)
    link = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source_id,
            EntitySource.entity_type == kind,
            EntitySource.entity_id == entity_id,
        )
    ).first()
    created = link is None
    if link is None:
        link = EntitySource(source_id=source_id, entity_type=kind, entity_id=entity_id, external_url=external_url)
    link.external_url = link.external_url or external_url
    link.reviewed = True
    if captured_at:
        link.sourced_at = captured_at
    session.add(link)
    return created


def _resolve_person(
    session: Session,
    credit: dict,
    *,
    production: Production,
    organization_id: uuid.UUID,
    source_id: uuid.UUID,
    captured_at: dt.datetime | None,
    actor_id: str | None,
) -> Person:
    person = _find_person(session, credit, production=production, organization_id=organization_id, source_id=source_id)
    if person is None:
        person = Person.model_validate(
            {
                "name": credit["name"],
                "type": credit["types"],
                "urls": credit["urls"],
                **{field: credit.get(field) for field in PERSON_FILL_FIELDS},
            }
        )
        session.add(person)
        session.flush()
        audit_event(session, person, "insert", actor_id)
    else:
        before = entity_snapshot(person)
        updates = {
            "type": union_values(person.type or [], credit["types"]),
            "urls": union_values(person.urls or [], credit["urls"]),
            **{
                field: credit[field]
                for field in PERSON_FILL_FIELDS
                if credit.get(field) not in EMPTY and before.get(field) in EMPTY
            },
        }
        validated = revalidate(person, updates)
        for field in updates:
            setattr(person, field, getattr(validated, field))
        if entity_snapshot(person) != before:
            session.add(person)
            audit_event(session, person, "update", actor_id)
    if credit["urls"]:
        _link_source(
            session,
            source_id=source_id,
            kind=EntityType.PERSON,
            entity_id=person.id,
            record_key=canonical_url(credit["urls"][0]),
            external_url=canonical_url(credit["urls"][0]),
            payload={"credit": credit},
            captured_at=captured_at,
            reassign=False,
        )
    return person


def _find_text(session: Session, title: str, author_ids: set[uuid.UUID]) -> Text | None:
    name = normalize(title)
    token = max(name.split(), key=len, default="")
    rows = [
        row
        for row in session.exec(
            select(Text).where(col(Text.title).icontains(token, autoescape=True)).order_by(col(Text.id)).limit(500)
        ).all()
        if normalize(row.title) == name
    ]
    authored = {row.id: {person.id for person in _text_authors(session, row.id)} for row in rows}
    if author_ids:
        match = next((row for row in rows if authored[row.id] & author_ids), None)
        unattributed = [row for row in rows if not authored[row.id]]
        return match or (unattributed[0] if len(unattributed) == 1 else None)
    return rows[0] if len(rows) == 1 else None


def _apply_text(
    session: Session, production: Production, item: dict, authors: list[Person], *, replace: bool, actor_id: str | None
) -> bool:
    author_ids = {person.id for person in authors}
    text = None if replace or not production.text_id else session.get(Text, production.text_id)
    changed = False
    if text is None:
        text = _find_text(session, item["incoming"]["name"], author_ids)
        if text is None:
            text = Text(title=item["incoming"]["name"])
            session.add(text)
            session.flush()
            audit_event(session, text, "insert", actor_id)
        production.text_id = text.id
        session.add(production)
        changed = True
    existing = {person.id for person in _text_authors(session, text.id)}
    # Never attach an author to a text that is attributed to someone else.
    if not existing or existing & author_ids:
        for person in authors:
            changed |= ensure_credit(session, text, person.id, "author", None, actor_id)
    return changed


def _apply(
    session: Session,
    production: Production | None,
    item: dict,
    *,
    organization_id: uuid.UUID,
    source_id: uuid.UUID,
    captured_at: dt.datetime | None,
    choices: dict[str, str],
    actor_id: str | None,
) -> tuple[Production, bool]:
    """Merge the upload into a production (or create it); conflicts keep existing values unless chosen."""
    if production is None:
        production = Production.model_validate({**item["incoming"], "organization_id": organization_id})
        session.add(production)
        session.flush()
        audit_event(session, production, "insert", actor_id)
        changed, text_conflict, director_choice = True, False, "incoming"
    else:
        before = entity_snapshot(production)
        comparison = _compare(session, production, item, organization_id, source_id)
        updates = {
            row["field"]: row["incoming"] if row["status"] == "conflict" else row["result"]
            for row in comparison["fields"]
            if row["field"] in FIELD_LABELS
            and (
                row["status"] in {"new", "extended"}
                or (row["status"] == "conflict" and choices.get(row["field"]) == "incoming")
            )
        }
        validated = revalidate(production, updates)
        for field in updates:
            setattr(production, field, getattr(validated, field))
        changed = entity_snapshot(production) != before
        if changed:
            session.add(production)
            audit_event(session, production, "update", actor_id)
        text_conflict = "text" in comparison["conflicts"]
        director_choice = choices.get("director", "existing") if "director" in comparison["conflicts"] else "incoming"
    if choices.get("director") == "incoming" and director_choice == "incoming":
        # Explicitly chosen: the uploaded direction replaces the existing one.
        for membership, _ in _members(session, production):
            if canonical_role(membership.role) == "director":
                audit_event(session, membership, "delete", actor_id)
                session.delete(membership)
                changed = True
        session.flush()
    persons: dict[str, Person] = {}
    for credit in item["credits"]:
        if credit["role"] == "director" and director_choice != "incoming":
            continue
        person = _resolve_person(
            session,
            credit,
            production=production,
            organization_id=organization_id,
            source_id=source_id,
            captured_at=captured_at,
            actor_id=actor_id,
        )
        persons[normalize(credit["name"])] = person
        changed |= ensure_credit(session, production, person.id, credit["role"], credit["character"], actor_id)
        session.flush()
    authors = [persons[normalize(name)] for name in item["authors"] if normalize(name) in persons]
    if not text_conflict or choices.get("text") == "incoming":
        changed |= _apply_text(session, production, item, authors, replace=text_conflict, actor_id=actor_id)
    changed |= _link_source(
        session,
        source_id=source_id,
        kind=EntityType.PRODUCTION,
        entity_id=production.id,
        record_key=item["record_key"],
        external_url=item["record_key"],
        payload={"incoming": item["incoming"], "credits": item["credits"]},
        captured_at=captured_at,
        reassign=True,
    )
    session.flush()
    return production, changed


# --- upload ---------------------------------------------------------------------------------------------------


def _source_production(
    session: Session, source_id: uuid.UUID, record_key: str, organization_id: uuid.UUID
) -> tuple[Production | None, dt.datetime | None]:
    record = session.exec(
        select(SourceRecord).where(
            SourceRecord.source_id == source_id,
            SourceRecord.entity_type == EntityType.PRODUCTION,
            SourceRecord.record_key == record_key,
        )
    ).first()
    if record is None:
        return None, None
    production = session.get(Production, resolve_id(session, EntityType.PRODUCTION, record.entity_id))
    if production is None or production.organization_id != organization_id:
        return None, None
    return production, record.captured_at


def _queue(
    session: Session,
    item: dict,
    *,
    organization: Organization,
    source_id: uuid.UUID,
    captured_at: dt.datetime | None,
    suggested: Production,
    reason: str,
    actor_id: str | None,
) -> ReconciliationCase:
    key = stable_digest([OPERATION, str(source_id), str(organization.id), item, captured_at])
    case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
    if case:
        return case
    # A newer upload of the same source page supersedes older open cases.
    for older in session.exec(
        select(ReconciliationCase).where(
            ReconciliationCase.operation == OPERATION,
            ReconciliationCase.status == "pending",
            ReconciliationCase.source_id == source_id,
            ReconciliationCase.record_key == item["record_key"],
        )
    ).all():
        older.status, older.decision_note = "rejected", "Durch neueren Upload ersetzt"
        older.decided_at = dt.datetime.now(dt.UTC)
        session.add(older)
    case = ReconciliationCase(
        key=key,
        entity_type=EntityType.PRODUCTION,
        operation=OPERATION,
        source_id=source_id,
        record_key=item["record_key"],
        incoming=item["incoming"],
        context={
            "organization_id": str(organization.id),
            "credits": item["credits"],
            "authors": item["authors"],
            "external_url": item["record_key"],
            "record_key": item["record_key"],
            "suggested_target_id": str(suggested.id),
        },
        reason=reason,
        captured_at=captured_at,
        actor_id=actor_id,
    )
    session.add(case)
    session.flush()
    return case


def _conflict_reason(conflicts: list[str]) -> str:
    labels = {**FIELD_LABELS, "text": "Werk / Autor:in", "director": "Regie"}
    return "Abweichend: " + ", ".join(labels[field] for field in conflicts)


def _ingest_one(
    session: Session,
    item: dict,
    *,
    organization: Organization,
    productions: list[Production],
    source_id: uuid.UUID,
    captured_at: dt.datetime | None,
    actor_id: str | None,
) -> str:
    def apply(production: Production | None) -> tuple[Production, bool]:
        return _apply(
            session,
            production,
            item,
            organization_id=organization.id,
            source_id=source_id,
            captured_at=captured_at,
            choices={},
            actor_id=actor_id,
        )

    linked, linked_at = _source_production(session, source_id, item["record_key"], organization.id)
    if (
        linked
        and linked_at
        and (captured_at is None or captured_at.replace(tzinfo=dt.UTC) < linked_at.replace(tzinfo=dt.UTC))
    ):
        # Older snapshot of an already imported page: only fill gaps, never override newer source data.
        return "merged" if apply(linked)[1] else "unchanged"
    name = normalize(item["incoming"]["name"])
    exact = [linked] if linked else [row for row in productions if normalize(row.name) == name]
    compared = [(row, _compare(session, row, item, organization.id, source_id)["conflicts"]) for row in exact]
    clean = [row for row, conflicts in compared if not conflicts]
    if len(clean) == 1:
        return "merged" if apply(clean[0])[1] else "unchanged"
    if compared:
        suggested, reason = (
            compared[0][0],
            _conflict_reason(compared[0][1]) if compared[0][1] else "Mehrere gleichnamige Produktionen",
        )
    else:
        scored = [(title_similarity(item["incoming"]["name"], row.name), row) for row in productions]
        best_score, best = max(scored, key=itemgetter(0), default=(0.0, None))
        if best is None or best_score < TITLE_THRESHOLD:
            productions.append(apply(None)[0])
            return "created"
        suggested, reason = best, f"Ähnlicher Titel ({best_score:.0%})"
    case = _queue(
        session,
        item,
        organization=organization,
        suggested=suggested,
        reason=reason,
        source_id=source_id,
        captured_at=captured_at,
        actor_id=actor_id,
    )
    return "review" if case.status == "pending" else "unchanged"


def ingest_records(
    records: list[dict],
    *,
    organization_id: uuid.UUID | str,
    source_name: str,
    actor_id: str | None,
    captured_at: dt.datetime | None = None,
    source_base_url: str | None = None,
    file_name: str | None = None,
) -> dict[str, list[str]]:
    """Import an uploaded theatre snapshot atomically; return production titles per outcome.

    Outcomes: ``merged`` (existing production enriched), ``unchanged``, ``created`` and
    ``review`` (conflict or similar title queued for :func:`production_review`).
    Every call is logged as an :class:`ImportRun` for the scraping overview.
    """
    if not source_name.strip():
        raise ValueError("A source name is required")
    if captured_at and captured_at.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    captured_at = captured_at.astimezone(dt.UTC) if captured_at else None
    records = validate_records(records)
    items = [prepare_record(record) for record in records]
    result: dict[str, list[str]] = {"merged": [], "unchanged": [], "created": [], "review": []}
    with get_session() as session:
        organization = session.get(Organization, uuid.UUID(str(organization_id)))
        if organization is None:
            raise ValueError("Theatre not found")
        source = session.exec(select(Source).where(Source.name == source_name.strip())).first()
        if source is None:
            base_url = (
                canonical_url(source_base_url)
                if source_base_url
                else f"https://{urlsplit(items[0]['record_key']).netloc}/"
            )
            source = Source(name=source_name.strip(), base_url=base_url)
            session.add(source)
            session.flush()
        productions = list(session.exec(select(Production).where(Production.organization_id == organization.id)).all())
        for item in items:
            outcome = _ingest_one(
                session,
                item,
                organization=organization,
                productions=productions,
                source_id=source.id,
                captured_at=captured_at,
                actor_id=actor_id,
            )
            result[outcome].append(item["incoming"]["name"])
        hosts = Counter(url_host(item["record_key"]) for item in items)
        session.add(
            ImportRun(
                organization_id=organization.id,
                source_id=source.id,
                file_name=file_name,
                scope=scrape_scope(file_name),
                website=hosts.most_common(1)[0][0] if hosts else None,
                captured_at=captured_at,
                actor_id=actor_id,
                total=len(items),
                merged=len(result["merged"]),
                created=len(result["created"]),
                review=len(result["review"]),
                unchanged=len(result["unchanged"]),
            )
        )
        session.commit()
    return result


def url_host(url: str | None) -> str | None:
    """Lower-case host without ``www.`` for comparing a URL with a theatre's website."""
    host = urlsplit(url or "").hostname
    return host.removeprefix("www.") if host else None


def scrape_scope(file_name: str | None) -> str | None:
    """Read the scrape scope (``all`` or ``limit-<n>``) from a snapshot filename written by the scraper."""
    match = re.search(r"_(all|limit-\d+)-(?:no-)?people-[0-9a-f]+_productions\.json$", file_name or "")
    return match[1] if match else None


# --- conflict review ------------------------------------------------------------------------------------------


def production_label(production: Production | dict, theatre: str | None) -> str:
    """Human label for a production: title, premiere date and theatre (never the UUID)."""
    data = production if isinstance(production, dict) else entity_snapshot(production)
    parts = [data["name"]]
    if data.get("premiere"):
        parts.append("Premiere " + dt.date.fromisoformat(str(data["premiere"])[:10]).strftime("%d.%m.%Y"))
    if theatre:
        parts.append(theatre)
    return " · ".join(parts)


def list_production_cases(
    *, status: str = "pending", organization_id: str | None = None, query: str = ""
) -> list[dict]:
    """List upload review cases with theatre names, oldest first."""
    with get_session() as session:
        cases = session.exec(
            select(ReconciliationCase)
            .where(ReconciliationCase.operation == OPERATION, ReconciliationCase.status == status)
            .order_by(col(ReconciliationCase.created_at), col(ReconciliationCase.id))
            .limit(1000)
        ).all()
        theatres = {str(org.id): org.name for org in session.exec(select(Organization)).all()}
        needle = normalize(query) if query.strip() else ""
        return [
            {
                "id": str(case.id),
                "name": case.incoming["name"],
                "theatre": theatres.get(case.context["organization_id"], "Unbekanntes Theater"),
                "organization_id": case.context["organization_id"],
                "reason": case.reason,
            }
            for case in cases
            if (organization_id is None or case.context["organization_id"] == str(organization_id))
            and needle in normalize(case.incoming["name"])
        ]


def _load_case(session: Session, case_id: uuid.UUID | str, *, lock: bool = False) -> ReconciliationCase:
    case = session.get(ReconciliationCase, uuid.UUID(str(case_id)), with_for_update=lock)
    if case is None or case.operation != OPERATION:
        raise ValueError("Review case not found")
    return case


def _item(case: ReconciliationCase) -> dict:
    return {
        "incoming": case.incoming,
        "credits": case.context["credits"],
        "authors": case.context["authors"],
        "record_key": case.record_key,
    }


def _review(session: Session, case: ReconciliationCase, target_id: uuid.UUID | str | None, create: bool) -> dict:
    organization_id = uuid.UUID(case.context["organization_id"])
    organization = session.get(Organization, organization_id)
    theatre = organization.name if organization else "Unbekanntes Theater"
    item = _item(case)
    productions = session.exec(select(Production).where(Production.organization_id == organization_id)).all()
    candidates = sorted(
        (
            {
                "id": str(row.id),
                "label": production_label(row, theatre),
                "similarity": title_similarity(item["incoming"]["name"], row.name),
            }
            for row in productions
        ),
        key=lambda row: (-row["similarity"], row["label"]),
    )
    target = None
    if not create:
        wanted = target_id or case.context.get("suggested_target_id") or (candidates[0]["id"] if candidates else None)
        if wanted:
            target = session.get(Production, resolve_id(session, EntityType.PRODUCTION, wanted))
            if target is not None and target.organization_id != organization_id:
                raise ValueError("Only productions of the same theatre can be merged")
    comparison = _compare(session, target, item, organization_id, case.source_id)
    source = session.get(Source, case.source_id) if case.source_id else None
    target_sources = []
    if target:
        for link in session.exec(
            select(EntitySource).where(
                EntitySource.entity_type == EntityType.PRODUCTION, EntitySource.entity_id == target.id
            )
        ).all():
            linked_source = session.get(Source, link.source_id)
            target_sources.append({"source": linked_source.name if linked_source else "?", "url": link.external_url})
    revision = stable_digest(
        [
            str(case.id),
            case.status,
            entity_snapshot(target) if target else None,
            comparison,
            target_sources,
        ]
    )
    return {
        "case_id": str(case.id),
        "status": case.status,
        "reason": case.reason,
        "theatre": theatre,
        "organization_id": str(organization_id),
        "source": {"name": source.name if source else "?", "url": case.context.get("external_url")},
        "captured_at": case.captured_at.date().isoformat() if case.captured_at else None,
        "incoming_label": production_label(case.incoming, theatre),
        "candidates": candidates,
        "target": (
            {"id": str(target.id), "label": production_label(target, theatre), "sources": target_sources}
            if target
            else None
        ),
        **comparison,
        "revision": revision,
    }


def production_review(
    case_id: uuid.UUID | str, *, target_id: uuid.UUID | str | None = None, create: bool = False
) -> dict:
    """Side-by-side comparison of an upload with a production of the same theatre.

    Without ``target_id`` the suggested (best fitting) production is used; ``create`` compares
    against nothing, i.e. previews a new production.
    """
    with get_session() as session:
        return _review(session, _load_case(session, case_id), target_id, create)


def resolve_production_case(
    case_id: uuid.UUID | str,
    *,
    revision: str,
    actor_id: str | None,
    target_id: uuid.UUID | str | None = None,
    create: bool = False,
    choices: dict[str, str] | None = None,
    note: str = "",
) -> uuid.UUID:
    """Merge the upload into ``target_id`` (or create it) including people, text and source.

    ``choices`` maps conflicting fields (and ``director``/``text``) to ``"existing"`` or ``"incoming"``;
    conflicts default to the existing value. Gaps are filled and lists are combined automatically.
    """
    choices = choices or {}
    if set(choices.values()) - {"existing", "incoming"}:
        raise ValueError("Choices must be 'existing' or 'incoming'")
    with get_session() as session:
        case = _load_case(session, case_id, lock=True)
        if case.status != "pending":
            raise ValueError("Case is already decided")
        review = _review(session, case, target_id, create)
        if review["revision"] != revision:
            raise ValueError("Data changed since preview. Refresh and review again.")
        target = session.get(Production, uuid.UUID(review["target"]["id"])) if review["target"] else None
        before = entity_snapshot(target) if target else None
        if case.source_id is None:
            raise ValueError("Upload case without source")
        production, _ = _apply(
            session,
            target,
            _item(case),
            organization_id=uuid.UUID(review["organization_id"]),
            source_id=case.source_id,
            captured_at=case.captured_at,
            choices={field: choices[field] for field in review["conflicts"] if field in choices},
            actor_id=actor_id,
        )
        case.context = {
            **case.context,
            "decision": {"before": before, "after": entity_snapshot(production), "choices": choices},
        }
        case.target_id = production.id
        case.status, case.actor_id, case.decision_note = "approved", actor_id, note
        case.decided_at = dt.datetime.now(dt.UTC)
        session.add(case)
        session.commit()
        return production.id


def reject_production_case(case_id: uuid.UUID | str, *, actor_id: str, note: str = "") -> None:
    """Discard an upload record without changing any production."""
    decide_case(case_id, status="rejected", actor_id=actor_id, note=note.strip() or "Import verworfen")
