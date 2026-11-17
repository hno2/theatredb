"""Explicit, source-identified Wikidata imports; search never writes to the database."""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from operator import itemgetter
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlmodel import Session, col, select

from backend.date_precision import DatePrecision
from backend.models import EntitySource, EntityType, Person, Source, Text, TextContribution, TextLineage, TextTitle
from backend.reconciliation import resolve_id
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


def fetch_entity(qid: str) -> dict:
    """Fetch all labels, aliases and claims of a validated item."""
    qid = validate_qid(qid)
    response = _request_json({"action": "wbgetentities", "ids": qid, "props": "labels|aliases|claims"})
    entity = response.get("entities", {}).get(qid)
    if not isinstance(entity, dict) or "missing" in entity or entity.get("id") != qid:
        raise WikidataError(f"Wikidata item not found: {qid}")
    return entity


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
    source = session.exec(select(Source).where(col(Source.name).ilike("wikidata"))).first()
    if source is None:
        source = Source(name="Wikidata", base_url="https://www.wikidata.org")
        session.add(source)
        session.flush()
    return source


def _linked(session: Session, source: Source, kind: EntityType, qid: str) -> uuid.UUID | None:
    links = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source.id,
            EntitySource.entity_type == kind,
            EntitySource.external_id == qid,
        )
    ).all()
    ids = {resolve_id(session, kind, link.entity_id) for link in links}
    if len(ids) > 1:
        raise ValueError(f"Conflicting Wikidata identity: {qid}")
    return next(iter(ids), None)


def _attach(session: Session, source: Source, kind: EntityType, entity_id: uuid.UUID, qid: str) -> None:
    owner = _linked(session, source, kind, qid)
    if owner is not None and owner != entity_id:
        raise ValueError(f"Wikidata item {qid} is already linked to another {kind}")
    link = session.exec(
        select(EntitySource).where(
            EntitySource.source_id == source.id,
            EntitySource.entity_type == kind,
            EntitySource.entity_id == entity_id,
        )
    ).first()
    if link:
        if link.external_id != qid:
            raise ValueError("Entity already has a different Wikidata identity")
        return
    session.add(
        EntitySource(
            source_id=source.id,
            entity_type=kind,
            entity_id=entity_id,
            external_id=qid,
            external_url=f"https://www.wikidata.org/wiki/{qid}",
            reviewed=True,
        )
    )
    session.flush()


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


def _enrich_authors(session: Session, source: Source, text: Text, entity: dict) -> int:
    added = 0
    for author in _claims(entity, "P50"):
        author_qid = validate_qid(author["id"])
        person_id = _linked(session, source, EntityType.PERSON, author_qid)
        if person_id is None:
            author_entity = fetch_entity(author_qid)
            person = Person(name=_label(author_entity), type=["author"])
            session.add(person)
            session.flush()
            person_id = person.id
            _attach(session, source, EntityType.PERSON, person_id, author_qid)
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


def enrich_text(
    session: Session,
    text: Text,
    qid: str | None = None,
    *,
    entity: dict | None = None,
) -> dict:
    """Enrich the identified work only; never copy root credits to translations/adaptations.

    Writes belong to the caller's transaction. Unknown language IDs and unsupported
    dates remain explicit in the report instead of inventing ISO codes or precision.
    """
    derived = session.exec(
        select(TextLineage).where(
            TextLineage.derived_text_id == text.id,
            col(TextLineage.relation).in_([str(relation) for relation in FAMILY_RELATIONS]),
        )
    ).first()
    if derived is not None:
        raise ValueError("Text is a translation/adaptation; select its family root for Wikidata enrichment")
    source = _source(session)
    if qid is None:
        link = session.exec(
            select(EntitySource).where(
                EntitySource.source_id == source.id,
                EntitySource.entity_type == EntityType.TEXT,
                EntitySource.entity_id == text.id,
            )
        ).first()
        if link is None or link.external_id is None:
            raise ValueError("Text has no selected Wikidata identity")
        qid = link.external_id
    entity = _entity(qid, entity)
    _attach(session, source, EntityType.TEXT, text.id, qid)
    report = {
        "titles_added": 0,
        "authors_added": 0,
        "based_on_ids": [],
        "languages": [],
        "unknown_language_qids": [],
        "warnings": [],
    }
    report["titles_added"] = _enrich_titles(session, text, entity)
    report["authors_added"] = _enrich_authors(session, source, text, entity)
    for parent in _claims(entity, "P144"):
        parent_qid = validate_qid(parent["id"])
        parent_text = create_text_stub(session, parent_qid)
        add_lineage(session, parent_text.id, text.id, "based_on", notes="Wikidata P144")
        report["based_on_ids"].append(str(parent_text.id))
    _enrich_languages(entity, report)
    text.languages = list(dict.fromkeys([*(text.languages or []), *report["languages"]]))
    _enrich_publication(text, entity, report["warnings"])
    session.add(text)
    session.flush()
    return report
