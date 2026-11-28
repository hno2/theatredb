"""Offline publisher metadata ingestion using the shared text resolver."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from pydantic import Field
from sqlalchemy import or_
from sqlmodel import Session, col, select
from tqdm import tqdm

from backend.ingestion.staging import HttpURL, NonEmptyText, StagedText
from backend.models import (
    EntitySource,
    EntityType,
    Person,
    ReconciliationCase,
    Source,
    SourceRecord,
    Text,
    TextContribution,
)
from backend.reconciliation import canonical_url, normalize, resolve_id, stable_digest
from backend.texts import CreditPeople, family_roots, fill_text_metadata, resolve_staged_text

PUBLISHERS = {
    "theatertexte.de": "https://www.theatertexte.de/",
    "Rowohlt": "https://www.rowohlt-theaterverlag.de/",
    "S. Fischer": "https://www.fischer-theater.de/",
    "Felix Bloch Erben": "https://www.felix-bloch-erben.de/",
    "Henschel": "https://www.henschel-schauspiel.de/",
    "Verlag der Autoren": "https://www.verlagderautoren.de/",
}
_PUBLISHER_ALIASES = {"sfischer": "S. Fischer", "vda": "Verlag der Autoren"}
_FIELD_ALIASES = {
    "titel": "title",
    "originaltitel": "original_title",
    "originalsprache": "original_language",
    "autor": "authors",
    "autoren": "authors",
    "übersetzung": "translators",
    "ubersetzung": "translators",
    "sprache": "language",
    "beschreibung": "description",
    "inhalt": "description",
    "klappentext": "description",
    "ua": "world_premiere",
    "uraufführung": "world_premiere",
    "urauffuhrung": "world_premiere",
}


class PublisherText(StagedText):
    """One validated publisher observation; UA is retained, not a publication date."""

    url: HttpURL
    external_id: NonEmptyText | None = None
    world_premiere: NonEmptyText | None = None
    source_metadata: dict = Field(default_factory=dict)


def _publisher(name: str) -> str:
    key = normalize(name).replace(" ", "")
    for publisher, url in PUBLISHERS.items():
        if key in {
            normalize(publisher).replace(" ", ""),
            normalize(url.split("//")[1].strip("/").removeprefix("www.")).replace(" ", ""),
        }:
            return publisher
    if key in _PUBLISHER_ALIASES:
        return _PUBLISHER_ALIASES[key]
    raise ValueError(f"Unknown publisher: {name}")


def normalize_metadata(record: Mapping[str, object]) -> PublisherText:
    """Accept normalized keys or common German publisher labels without guessing credits."""
    data: dict = {}
    for key, value in record.items():
        field = _FIELD_ALIASES.get(normalize(key), key)
        if field in data and data[field] != value:
            raise ValueError(f"Conflicting metadata for {field}")
        data[field] = value
    for field in ("authors", "translators", "composers", "lyricists", "adapters", "editors"):
        if isinstance(data.get(field), str):
            data[field] = [data[field]]
    return PublisherText.model_validate(data)


def _source_record(session: Session, source_id: uuid.UUID, record: PublisherText) -> SourceRecord | None:
    url = canonical_url(record.url)
    identities = [col(SourceRecord.record_key) == url, col(SourceRecord.external_url) == url]
    if record.external_id:
        identities.extend(
            [col(SourceRecord.record_key) == record.external_id, col(SourceRecord.external_id) == record.external_id]
        )
    rows = session.exec(
        select(SourceRecord).where(
            SourceRecord.source_id == source_id, SourceRecord.entity_type == EntityType.TEXT, or_(*identities)
        )
    ).all()
    targets = {resolve_id(session, EntityType.TEXT, row.entity_id) for row in rows}
    if len(targets) > 1:
        raise ValueError("Publisher identities point to different texts")
    return rows[0] if rows else None


def save_publisher_observation(session: Session, text: Text, observation: Mapping[str, object]) -> None:
    """Persist an accepted observation with source_id; never reassign another text's source identity."""
    source_id = uuid.UUID(str(observation["source_id"]))
    if session.get(Source, source_id) is None:
        raise ValueError("Publisher source not found")
    record = normalize_metadata({key: value for key, value in observation.items() if key != "source_id"})
    stored = _source_record(session, source_id, record)
    _save_publisher_observation(session, text, source_id, record, stored)


def _save_publisher_observation(
    session: Session,
    text: Text,
    source_id: uuid.UUID,
    record: PublisherText,
    stored: SourceRecord | None,
) -> None:
    if stored is not None and resolve_id(session, EntityType.TEXT, stored.entity_id) != text.id:
        raise ValueError(f"Publisher identity already links another text: {record.external_id or record.url}")
    link = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source_id,
            EntitySource.entity_type == EntityType.TEXT,
            EntitySource.entity_id == text.id,
        )
    ).first()
    url = canonical_url(record.url)
    if link is None:
        link = EntitySource(source_id=source_id, entity_type=EntityType.TEXT, entity_id=text.id)
    link.external_url = link.external_url or url
    link.external_id = link.external_id or record.external_id
    session.add(link)
    if stored is None:
        stored = SourceRecord(
            source_id=source_id, entity_type=EntityType.TEXT, entity_id=text.id, record_key=record.external_id or url
        )
    stored.entity_id = text.id
    stored.external_id, stored.external_url = record.external_id or stored.external_id, url
    payload = record.model_dump(mode="json", exclude_none=True)
    stored.payload = {
        **stored.payload,
        **{key: value for key, value in payload.items() if value not in (None, [], "")},
    }
    session.add(stored)
    session.flush()


def resolve_publisher_observations(session: Session, text: Text, observations: Sequence[Mapping[str, object]]) -> None:
    """Persist publisher observations after an explicit text-link decision; caller commits."""
    for observation in observations:
        save_publisher_observation(session, text, observation)


def _comparable(value: object) -> object:
    if isinstance(value, str):
        return normalize(value)
    if isinstance(value, list):
        return sorted(stable_digest(_comparable(item)) for item in value)
    if isinstance(value, dict):
        return {key: _comparable(item) for key, item in value.items() if item not in (None, [], "")}
    return value


def _known_metadata(session: Session, text: Text, payload: dict) -> dict:
    previous = dict(payload)
    roots = family_roots(session, text.id)
    previous.setdefault("title", text.title)
    if "original_subtitle" not in previous and len(roots) == 1:
        previous["original_subtitle"] = roots[0].subtitle
    previous.setdefault("genre", text.genre)
    previous.setdefault("language", text.languages[0] if text.languages else None)
    previous.setdefault("published", text.published.isoformat() if text.published else None)
    for field in (
        "subtitle",
        "subtitle_en",
        "keywords",
        "genre_labels",
        "women_min",
        "women_max",
        "men_min",
        "men_max",
        "total_cast_min",
        "total_cast_max",
        "cast_note",
        "cast_note_en",
        "suggested_min_age",
        "description",
        "description_en",
        "is_free_for_school_amateur_play",
        "is_novel_or_film_material",
        "released_for_udse",
        "has_role_book",
        "foreign_rights",
        "school_amateur_note",
        "foreign_rights_note",
        "translations_note",
        "translations_note_en",
    ):
        if previous.get(field) in (None, "", []):
            previous[field] = getattr(text, field)
    for role in ("author", "translator", "composer", "lyricist", "adapter", "editor"):
        target = roots[0] if role == "author" and len(roots) == 1 else text
        if not previous.get(f"{role}s"):
            previous[f"{role}s"] = list(
                session.exec(
                    select(Person.name)
                    .join(TextContribution, col(TextContribution.person_id) == col(Person.id))
                    .where(TextContribution.text_id == target.id, TextContribution.role == role)
                ).all()
            )
    return previous


def _replay(
    session: Session, source: Source, stored: SourceRecord, metadata: dict, people: CreditPeople
) -> Text | ReconciliationCase:
    text = session.get(Text, resolve_id(session, EntityType.TEXT, stored.entity_id))
    if text is None:
        raise ValueError("Publisher identity points to a missing text")
    previous = _known_metadata(session, text, stored.payload)
    conflicts = [
        key
        for key, value in metadata.items()
        if value not in (None, [], "")
        and previous.get(key) not in (None, [], "")
        and _comparable(value) != _comparable(previous[key])
    ]
    conflicts.extend(
        key
        for key in ("original_title", "original_language", "based_on")
        if metadata.get(key) and not previous.get(key)
    )
    if conflicts:
        key = "publisher_text:" + stable_digest([str(source.id), stored.record_key, metadata])
        case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
        if case is None:
            case = ReconciliationCase(
                key=key,
                entity_type=EntityType.TEXT,
                operation="link_text",
                source_id=source.id,
                record_key=stored.record_key,
                incoming=metadata,
                context={"candidate_ids": [str(text.id)], "existing": previous},
                reason="Publisher metadata changed: " + ", ".join(sorted(set(conflicts))),
            )
            session.add(case)
            session.flush()
        if case.status != "approved":
            return case
        return text
    _fill_known_text(session, text, metadata, people)
    return text


def _fill_known_text(session: Session, text: Text, metadata: dict, people: CreditPeople) -> None:
    fill_text_metadata(session, text, metadata)
    if metadata.get("original_subtitle"):
        roots = family_roots(session, text.id)
        if len(roots) == 1 and not roots[0].subtitle:
            roots[0].subtitle = metadata["original_subtitle"]
            session.add(roots[0])
    updates = {}
    if not text.languages and metadata.get("language"):
        updates["languages"] = [metadata["language"]]
    if updates:
        validated = Text.model_validate({**text.model_dump(), **updates})
        for key in updates:
            setattr(text, key, getattr(validated, key))
        session.add(text)
    roots = family_roots(session, text.id)
    for role in ("author", "translator", "composer", "lyricist", "adapter", "editor"):
        target = roots[0] if role == "author" and len(roots) == 1 else text
        if (
            session.exec(
                select(TextContribution.id).where(TextContribution.text_id == target.id, TextContribution.role == role)
            ).first()
            is not None
        ):
            continue
        people.add_credits(target, metadata.get(f"{role}s", []), role)


def ingest_text_metadata(
    session: Session, source_name: str, records: Sequence[Mapping[str, object]], *, progress: tqdm | None = None
) -> list[Text | ReconciliationCase]:
    """Validate all records, then import texts and source links; caller owns the transaction."""
    publisher = _publisher(source_name)
    validated = [normalize_metadata(record) for record in records]
    if not validated:
        return []
    source = session.exec(select(Source).where(Source.name == publisher)).first()
    if source is None:
        source = Source(name=publisher, base_url=PUBLISHERS[publisher])
        session.add(source)
        session.flush()
    results = []
    people = CreditPeople(session)
    for record in validated:
        observation = record.model_dump(mode="json", exclude_none=True)
        metadata = record.model_dump(
            mode="json", exclude={"url", "external_id", "world_premiere", "source_metadata"}, exclude_none=True
        )
        stored = _source_record(session, source.id, record)
        result = (
            _replay(session, source, stored, metadata, people)
            if stored is not None
            else resolve_staged_text(session, metadata, people=people)
        )
        results.append(result)
        if isinstance(result, ReconciliationCase):
            result.source_id = result.source_id or source.id
            observations = result.context.get("publisher_observations", [])
            observation = {"source_id": str(source.id), **observation}
            result.context = {
                **result.context,
                "publisher_observations": observations if observation in observations else [*observations, observation],
            }
            session.add(result)
        else:
            _save_publisher_observation(session, result, source.id, record, stored)
        if progress is not None:
            progress.update()
    return results
