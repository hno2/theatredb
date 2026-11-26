"""Explicit, source-identified Wikidata imports; search never writes to the database."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Collection
from operator import itemgetter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlmodel import Session, col, select

from backend.authority import (
    attach_identity,
    external_source,
    fill,
    fold,
    latin,
    linked_entity,
    same_name_person,
    same_title_text,
)
from backend.authority import identity as _identity
from backend.date_precision import DatePrecision
from backend.models import EntityType, Organization, Person, Source, Text, TextContribution, TextLineage, TextTitle
from backend.reconciliation import normalize
from backend.texts import FAMILY_RELATIONS, add_lineage, add_title

API_URL = "https://www.wikidata.org/w/api.php"
TIMEOUT = 15
MAX_RESPONSE_BYTES = 8_000_000
LANGUAGE_CODES = {
    "Q1860": "en",
    "Q188": "de",
    "Q150": "fr",
    "Q1321": "es",
    "Q652": "it",
    "Q7737": "ru",
    "Q5287": "ja",
    "Q7850": "zh",
    "Q9610": "bn",
    "Q256": "tr",
    "Q7411": "nl",
    "Q9027": "sv",
    "Q809": "pl",
    "Q5146": "pt",
    "Q13955": "ar",
    "Q8798": "uk",
    "Q9067": "hu",
    "Q9056": "cs",
    "Q9035": "da",
    "Q9129": "el",
    "Q1412": "fi",
    "Q9240": "id",
    "Q8785": "hy",
    "Q9288": "he",
    "Q9168": "fa",
}


class WikidataError(RuntimeError):
    """A Wikidata request or response could not be used."""


def validate_qid(qid: str) -> str:
    """Accept an item ID, never arbitrary URLs or query fragments."""
    if not isinstance(qid, str) or re.fullmatch(r"Q[1-9]\d*", qid) is None:
        raise ValueError(f"Invalid Wikidata QID: {qid!r}")
    return qid


def _request_json(params: dict) -> dict:
    # URL is the fixed HTTPS Wikidata endpoint.
    request = Request(  # ruff: ignore[suspicious-url-open-usage]
        API_URL + "?" + urlencode({"format": "json", **params}),
        headers={"User-Agent": "TheaterDB/0.1 (explicit work reconciliation)", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise WikidataError("Wikidata response exceeds size limit")
        result = json.loads(body)
    except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError) as exc:
        raise WikidataError(f"Wikidata request failed: {exc}") from exc
    if not isinstance(result, dict) or result.get("error"):
        raise WikidataError(f"Invalid Wikidata response: {result!r}")
    return result


def search_texts(query: str, language: str = "de", limit: int = 10) -> list[dict]:
    """Return unapproved item dictionaries; callers must explicitly select an item."""
    if not query.strip():
        return []
    if not 1 <= limit <= 50:
        raise ValueError("Wikidata search limit must be between 1 and 50")
    response = _request_json(
        {
            "action": "wbsearchentities",
            "search": query.strip(),
            "language": language,
            "uselang": language,
            "type": "item",
            "limit": limit,
        }
    )
    return [
        {
            "id": validate_qid(item["id"]),
            "label": item.get("label", item["id"]),
            "description": item.get("description", ""),
            "aliases": item.get("aliases", []),
            "url": f"https://www.wikidata.org/wiki/{item['id']}",
        }
        for item in response.get("search", [])
    ]


WIKI_URL = "https://www.wikidata.org/wiki/"
ENTITY_URI = "http://www.wikidata.org/entity/"
HUMAN = "Q5"
GENDERS = {"Q6581097": "m", "Q6581072": "f", "Q48270": "div"}  # male, female, non-binary; others stay unknown
_QID_REF = re.compile(r"\s*(?:https?://(?:www\.)?wikidata\.org/(?:wiki|entity)/)?(Q[1-9]\d*)/?\s*")
# shortcut: fixed cap keeps the first matching aliases; add ranking if editors miss important spellings.
MAX_VARIANTS = 10


def parse_qid(value: str) -> str:
    """Accept a QID or a Wikidata item URL."""
    match = _QID_REF.fullmatch(value or "")
    if match is None:
        raise ValueError(f"Invalid Wikidata QID: {value!r}")
    return match.group(1)


def fetch_entities(qids: list[str]) -> dict[str, dict]:
    """Labels, descriptions, aliases and claims of up to 50 validated items in one request."""
    ids = [validate_qid(qid) for qid in qids]
    if not ids:
        return {}
    if len(ids) > 50:
        raise ValueError("At most 50 Wikidata items per request")
    response = _request_json(
        {"action": "wbgetentities", "ids": "|".join(ids), "props": "labels|descriptions|aliases|claims"}
    )
    entities = response.get("entities", {})
    return {
        qid: entities[qid]
        for qid in ids
        if isinstance(entities.get(qid), dict) and "missing" not in entities[qid] and entities[qid].get("id") == qid
    }


def fetch_entity(qid: str) -> dict:
    """Fetch labels, descriptions, aliases and claims of a validated item."""
    entity = fetch_entities([qid]).get(validate_qid(qid))
    if entity is None:
        raise WikidataError(f"Wikidata item not found: {qid}")
    return entity


def search(query: str, kind: EntityType, limit: int = 10) -> list[dict]:
    """Candidates with full entities; people are limited to humans. Never writes."""
    hits = search_texts(query, limit=limit)
    entities = fetch_entities([hit["id"] for hit in hits])
    results = []
    for hit in hits:
        entity = entities.get(hit["id"])
        if entity is None:
            continue
        # shortcut: only people are type-filtered; works and theatres have too many classes to whitelist.
        if kind == EntityType.PERSON and HUMAN not in {v.get("id") for v in _claims(entity, "P31")}:
            continue
        results.append({**hit, "description": summary(entity), "record": entity})
    return results


record_id = itemgetter("id")


def summary(entity: dict) -> str:
    """Description plus life or creation years for disambiguation."""
    description = next(
        (
            entity.get("descriptions", {}).get(lang, {}).get("value")
            for lang in ("de", "en")
            if entity.get("descriptions", {}).get(lang)
        ),
        "",
    )
    span = " - ".join(str(y) for y in years(entity).values())
    return " · ".join(filter(None, [description, span]))


def names(entity: dict) -> list[str]:
    """All labels and aliases; used for exact-name matching, never as identity."""
    found = [label["value"] for label in entity.get("labels", {}).values() if label.get("value")]
    for aliases in entity.get("aliases", {}).values():
        found += [alias["value"] for alias in aliases if alias.get("value")]
    return found


def _year(value: object) -> int | None:
    match = re.match(r"\+(\d{4,})-", value.get("time", "")) if isinstance(value, dict) else None
    return int(match.group(1)) if match else None


def years(entity: dict) -> dict[str, int]:
    """Life years of a person, first stated value each."""
    found = {}
    for prop, key in (("P569", "born"), ("P570", "died")):
        if (year := next(filter(None, map(_year, _claims(entity, prop))), None)) is not None:
            found[key] = year
    return found


def author_ids(entity: dict) -> set[str]:
    """QIDs of authors (P50) and composers (P86)."""
    return {value["id"] for prop in ("P50", "P86") for value in _claims(entity, prop) if isinstance(value, dict)}


def places(_entity: dict) -> list[str]:
    """Wikidata places are item IDs; resolving labels would cost a request per hit, so none are offered."""
    return []


def gnd_ids(entity: dict) -> set[str]:
    """GND IDs Wikidata states for this item (P227)."""
    return {value for value in _claims(entity, "P227") if isinstance(value, str)}


def refs(entity: dict, prop: str) -> list[tuple[str, str]]:
    """(QID, label) of linked items, labels fetched in one batch request."""
    qids = [v["id"] for v in _claims(entity, prop) if isinstance(v, dict) and v.get("id")][:50]
    linked = fetch_entities(qids)
    return [(qid, _label(linked[qid]) if qid in linked else qid) for qid in qids]


def identity(session: Session, kind: EntityType, entity_id: uuid.UUID) -> str | None:
    """The selected QID of an entity; read-only."""
    return _identity(session, "Wikidata", kind, entity_id)


def _entity(qid: str, entity: dict | None) -> dict:
    qid = validate_qid(qid)
    value = fetch_entity(qid) if entity is None else entity
    if value.get("id") != qid or "missing" in value:
        raise ValueError("Wikidata entity does not match selected QID")
    return value


def _label(entity: dict, language: str = "de") -> str:
    labels = entity.get("labels", {})
    for key in (language, "en", *labels):
        if value := labels.get(key, {}).get("value"):
            return value
    return entity["id"]


def _title_language(code: str) -> str | None:
    """Keep plain ISO 639-1 codes; variants like en-gb, zh-hans or simple stay unlabelled."""
    return code if re.fullmatch(r"[a-z]{2}", code) else None


def _source(session: Session) -> Source:
    return external_source(session, "Wikidata", "https://www.wikidata.org")


def _linked(session: Session, source: Source, kind: EntityType, qid: str) -> uuid.UUID | None:
    return linked_entity(session, source, kind, qid)


def _attach(session: Session, source: Source, kind: EntityType, entity_id: uuid.UUID, qid: str) -> None:
    attach_identity(session, source, kind, entity_id, qid, f"https://www.wikidata.org/wiki/{qid}")


def create_text_stub(
    session: Session,
    qid: str,
    *,
    entity: dict | None = None,
    language: str = "de",
) -> Text:
    """Create only an explicitly selected work, preserving QID rather than matching by title."""
    qid = validate_qid(qid)
    source = _source(session)
    existing_id = _linked(session, source, EntityType.TEXT, qid)
    if existing_id is not None:
        existing = session.get(Text, existing_id)
        if existing is None:
            raise ValueError("Wikidata identity points to a missing text")
        return existing
    entity = _entity(qid, entity)
    text = Text(title=_label(entity, language))
    session.add(text)
    session.flush()
    _attach(session, source, EntityType.TEXT, text.id, qid)
    add_title(
        session,
        text,
        text.title,
        next(
            (
                _title_language(lang)
                for lang, label in entity.get("labels", {}).items()
                if label.get("value") == text.title and _title_language(lang)
            ),
            None,
        ),
    )
    return text


def _claims(entity: dict, prop: str) -> list:
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claim in entity.get("claims", {}).get(prop, [])
        if claim.get("rank") != "deprecated"
        and claim.get("mainsnak", {}).get("snaktype") == "value"
        and "datavalue" in claim.get("mainsnak", {})
    ]


def parse_publication(value: dict) -> tuple[dt.datetime, DatePrecision]:
    """Retain Wikidata year/month/day precision; reject unsupported calendars and dates."""
    if value.get("calendarmodel", "http://www.wikidata.org/entity/Q1985727").rsplit("/", 1)[-1] != "Q1985727":
        raise ValueError("Unsupported Wikidata calendar")
    precisions: dict[int | None, DatePrecision] = {
        9: DatePrecision.YEAR,
        10: DatePrecision.MONTH,
        11: DatePrecision.DAY,
    }
    precision = precisions.get(value.get("precision"))
    match = re.fullmatch(r"\+(\d{4,})-(\d{2})-(\d{2})T00:00:00Z", value.get("time", ""))
    if precision is None or match is None:
        raise ValueError("Unsupported Wikidata date precision or date")
    year, month, day = map(int, match.groups())
    return dt.datetime(
        year,
        month if precision != DatePrecision.YEAR else 1,
        day if precision == DatePrecision.DAY else 1,
    ), precision


def _enrich_titles(session: Session, text: Text, entity: dict) -> int:
    before = len(session.exec(select(TextTitle).where(TextTitle.text_id == text.id)).all())
    for language, label in entity.get("labels", {}).items():
        if label.get("value"):
            add_title(session, text, label["value"], _title_language(language))
    for language, aliases in entity.get("aliases", {}).items():
        for alias in aliases:
            if alias.get("value"):
                add_title(session, text, alias["value"], _title_language(language))
    session.flush()
    return len(session.exec(select(TextTitle).where(TextTitle.text_id == text.id)).all()) - before


def _author_id(session: Session, source: Source, author_qid: str, report: dict) -> uuid.UUID | None:
    if person_id := _linked(session, source, EntityType.PERSON, author_qid):
        return person_id
    author_entity = fetch_entity(author_qid)
    name = _label(author_entity)
    if candidate := same_name_person(session, name):
        # Names are not identities: the editor confirms by giving that person this QID.
        report["unlinked_people"].append({"qid": author_qid, "label": name, "candidate_id": str(candidate.id)})
        return None
    person = Person(name=name, type=["author"])
    session.add(person)
    session.flush()
    enrich_person(session, person, author_qid, entity=author_entity)
    return person.id


def _enrich_authors(session: Session, source: Source, text: Text, entity: dict, report: dict) -> int:
    added = 0
    for author in _claims(entity, "P50"):
        person_id = _author_id(session, source, validate_qid(author["id"]), report)
        if person_id is None:
            continue
        if (
            session.exec(
                select(TextContribution).where(
                    TextContribution.text_id == text.id,
                    TextContribution.person_id == person_id,
                    TextContribution.role == "author",
                )
            ).first()
            is None
        ):
            session.add(TextContribution(text_id=text.id, person_id=person_id, role="author"))
            session.flush()
            added += 1
    return added


def _enrich_languages(entity: dict, report: dict) -> None:
    for language in _claims(entity, "P407"):
        language_qid = validate_qid(language["id"])
        code = LANGUAGE_CODES.get(language_qid)
        if code is None:
            language_entity = fetch_entity(language_qid)
            codes = _claims(language_entity, "P218")
            code = next((value for value in codes if isinstance(value, str) and re.fullmatch(r"[a-z]{2}", value)), None)
        if code:
            report["languages"].append(code)
        else:
            report["unknown_language_qids"].append(language_qid)
            report["warnings"].append(f"No supported ISO 639-1 language mapping for {language_qid}")


def _enrich_publication(text: Text, entity: dict, warnings: list[str]) -> None:
    if text.published is not None:
        return
    dates = []
    for value in _claims(entity, "P577"):
        try:
            dates.append(parse_publication(value))
        except (ValueError, TypeError, OverflowError) as exc:
            warnings.append(str(exc))
    if dates:
        text.published, text.published_precision = min(dates, key=itemgetter(0))


def _date(entity: dict, prop: str, warnings: list[str]) -> tuple[dt.datetime, DatePrecision] | None:
    for value in _claims(entity, prop):
        try:
            return parse_publication(value)
        except (ValueError, TypeError, OverflowError) as exc:
            warnings.append(f"{prop}: {exc}")
    return None


def _fill_date(target: object, field: str, entity: dict, prop: str, report: dict) -> None:
    if parsed := _date(entity, prop, report["warnings"]):
        fill(target, field, parsed[0], report, parsed[1])


def _report(qid: str) -> dict:
    return {"qid": qid, "filled": [], "conflicts": [], "added_names": [], "warnings": []}


def _resolve(session: Session, kind: EntityType, entity_id: uuid.UUID, qid: str | None, entity: dict | None) -> dict:
    source = _source(session)
    if qid is None:
        qid = identity(session, kind, entity_id)
        if qid is None:
            raise ValueError(f"{kind} has no selected Wikidata identity")
    entity = _entity(qid, entity)
    _attach(session, source, kind, entity_id, qid)
    return entity


def _person_names(person: Person, entity: dict, report: dict) -> None:
    """German/English label and aliases in Latin script sharing the surname of the label."""
    label = _label(entity)
    surname = fold(label.rsplit(" ", 1)[-1])
    known = {normalize(name) for name in [person.name, *(person.alt_names or [])]}
    candidates = [label]
    for lang in ("de", "en"):
        candidates += [a["value"] for a in entity.get("aliases", {}).get(lang, []) if a.get("value")]
    added = []
    for name in candidates:
        if (
            latin(name)
            and fold(name.rsplit(" ", 1)[-1]) == surname
            and normalize(name) not in known
            and len(added) < MAX_VARIANTS
        ):
            known.add(normalize(name))
            added.append(name)
    if added:
        person.alt_names = [*(person.alt_names or []), *added]
        report["added_names"] = added


def enrich_person(session: Session, person: Person, qid: str | None = None, *, entity: dict | None = None) -> dict:
    """Life dates, gender and alternative names; fills blanks only."""
    entity = _resolve(session, EntityType.PERSON, person.id, qid, entity)
    report = _report(entity["id"])
    _fill_date(person, "born", entity, "P569", report)
    _fill_date(person, "died", entity, "P570", report)
    genders = {GENDERS.get(str(v.get("id"))) for v in _claims(entity, "P21") if isinstance(v, dict)}
    if len(genders) == 1:
        fill(person, "gender", genders.pop(), report)
    _person_names(person, entity, report)
    session.add(person)
    session.flush()
    return report


def enrich_organization(
    session: Session, organization: Organization, qid: str | None = None, *, entity: dict | None = None
) -> dict:
    """Inception (P571) and dissolution (P576) dates; fills blanks only."""
    entity = _resolve(session, EntityType.ORGANIZATION, organization.id, qid, entity)
    report = _report(entity["id"])
    _fill_date(organization, "founded", entity, "P571", report)
    _fill_date(organization, "closed", entity, "P576", report)
    session.add(organization)
    session.flush()
    return report


def _parent(session: Session, parent_qid: str, report: dict) -> Text | None:
    source = _source(session)
    if text_id := _linked(session, source, EntityType.TEXT, parent_qid):
        return session.get(Text, text_id)
    parent_entity = fetch_entity(parent_qid)
    if candidate := same_title_text(session, _label(parent_entity)):
        report["unlinked_texts"].append(
            {"qid": parent_qid, "label": _label(parent_entity), "candidate_id": str(candidate.id)}
        )
        return None
    return create_text_stub(session, parent_qid, entity=parent_entity)


def enrich_text(
    session: Session,
    text: Text,
    qid: str | None = None,
    *,
    entity: dict | None = None,
    parent_qids: Collection[str] | None = None,
) -> dict:
    """Enrich the identified work only; never copy root credits to translations/adaptations.

    ``parent_qids`` selects which direct P144 items become ``based_on`` lineage; None takes all,
    an empty collection none. Writes belong to the caller's transaction. Unknown language IDs and
    unsupported dates remain explicit in the report instead of inventing ISO codes or precision.
    """
    derived = session.exec(
        select(TextLineage).where(
            TextLineage.derived_text_id == text.id,
            col(TextLineage.relation).in_([str(relation) for relation in FAMILY_RELATIONS]),
        )
    ).first()
    if derived is not None:
        raise ValueError("Text is a translation/adaptation; select its family root for Wikidata enrichment")
    entity = _resolve(session, EntityType.TEXT, text.id, qid, entity)
    report: dict[str, Any] = {
        **_report(entity["id"]),
        "titles_added": 0,
        "authors_added": 0,
        "unlinked_people": [],
        "based_on_ids": [],
        "unlinked_texts": [],
        "languages": [],
        "unknown_language_qids": [],
    }
    report["titles_added"] = _enrich_titles(session, text, entity)
    report["authors_added"] = _enrich_authors(session, _source(session), text, entity, report)
    for parent in _claims(entity, "P144"):
        parent_qid = validate_qid(parent["id"])
        if parent_qids is not None and parent_qid not in parent_qids:
            continue
        if (parent_text := _parent(session, parent_qid, report)) is not None:
            add_lineage(session, parent_text.id, text.id, "based_on", notes="Wikidata P144")
            report["based_on_ids"].append(str(parent_text.id))
    _fill_date(text, "created", entity, "P571", report)
    _enrich_languages(entity, report)
    text.languages = list(dict.fromkeys([*(text.languages or []), *report["languages"]]))
    _enrich_publication(text, entity, report["warnings"])
    session.add(text)
    session.flush()
    return report


def enrich(
    session: Session,
    kind: EntityType,
    entity_id: uuid.UUID,
    qid: str,
    *,
    record: dict | None = None,
    predecessor_ids: Collection[str] | None = None,
) -> dict:
    """Dispatch to the entity-specific enrichment; ``predecessor_ids`` applies to texts only."""
    if kind == EntityType.PERSON and (person := session.get(Person, entity_id)):
        return enrich_person(session, person, qid, entity=record)
    if kind == EntityType.TEXT and (text := session.get(Text, entity_id)):
        return enrich_text(session, text, qid, entity=record, parent_qids=predecessor_ids)
    if kind == EntityType.ORGANIZATION and (organization := session.get(Organization, entity_id)):
        return enrich_organization(session, organization, qid, entity=record)
    raise ValueError(f"No {kind} {entity_id} for Wikidata enrichment")
