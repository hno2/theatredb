"""Selective GND authority enrichment via the lobid-gnd API (CC0 metadata).

Only explicit calls write. Values fill blanks and never overwrite editorial data; differences are reported
as conflicts. Identities are GND IDs, never names: same-name local records are reported, not merged.
Text lineage stops after one hop (direct ``precedingWork``), so a work never imports its whole graph.
"""

from __future__ import annotations

import datetime as dt
import json
import operator
import re
import uuid
from collections.abc import Collection
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlmodel import Session, col, select

from backend.authority import (
    attach_identity,
    entity_link,
    external_source,
    fill,
    fold,
    latin,
    linked_entity,
    same_name_person,
    same_title_text,
)
from backend.authority import identity as _identity
from backend.date_precision import DatePrecision, parse_date
from backend.models import (
    EntityType,
    LineageRelation,
    Organization,
    Person,
    Source,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
)
from backend.reconciliation import normalize
from backend.texts import FAMILY_RELATIONS, add_lineage, add_title

API_URL = "https://lobid.org/gnd"
GND_URI = "https://d-nb.info/gnd/"
TIMEOUT = 15
MAX_RESPONSE_BYTES = 4_000_000
# shortcut: fixed cap keeps the first matching GND variants; add ranking if editors miss important spellings.
MAX_VARIANTS = 10
RECORD_TYPES = {EntityType.PERSON: "Person", EntityType.TEXT: "Work", EntityType.ORGANIZATION: "CorporateBody"}
GENDERS = {"male": "m", "female": "f"}  # GND "notKnown" stays NULL (unknown)
# GND formOfWorkAndExpression concepts that determine Text.genre; other forms leave the genre untouched.
WORK_GENRES = {
    "4012899-4": "play",  # Drama
    "4031952-0": "play",  # Komödie
    "4060591-7": "play",  # Tragödie
    "4043582-9": "opera",  # Oper
    "4043602-0": "opera",  # Operette
    "4035588-3": "opera",  # Libretto
    "4040801-2": "musical",  # Musical
    "4050479-7": "novel",  # Roman
    "4017102-4": "film",  # Film
}
CREDIT_FIELDS = {"firstAuthor": "author", "firstComposer": "composer"}
_GND_ID = r"\d+(?:-[\dX]|X)?"
_GND_REF = re.compile(
    rf"\s*(?:https?://(?:d-nb\.info|lobid\.org|explore\.gnd\.network)/gnd/)?({_GND_ID})(?:[./?#][^\s]*)?\s*"
)


class GndError(RuntimeError):
    """A GND request or response could not be used."""


def parse_gnd_id(value: str) -> str:
    """Accept a GND ID or a d-nb.info / lobid / explore.gnd.network record URL, nothing else."""
    match = _GND_REF.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"Invalid GND ID: {value!r}")
    return match.group(1)


def _request_json(url: str) -> dict:
    # URL is always the fixed HTTPS lobid endpoint plus a validated ID or encoded query.
    request = Request(  # ruff: ignore[suspicious-url-open-usage]
        url, headers={"User-Agent": "TheaterDB/0.1 (explicit authority enrichment)", "Accept": "application/json"}
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise GndError("GND response exceeds size limit")
        result = json.loads(body)
    except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError) as exc:
        raise GndError(f"GND request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise GndError("Invalid GND response")
    return result


def fetch_record(gnd_id: str) -> dict:
    """One JSON-LD record; lobid redirects merged IDs, so callers use the returned ``gndIdentifier``."""
    record = _request_json(f"{API_URL}/{parse_gnd_id(gnd_id)}.json")
    if not isinstance(record.get("gndIdentifier"), str):
        raise GndError(f"GND record not found: {gnd_id}")
    return record


def _labels(record: dict, field: str) -> list[str]:
    return [item["label"] for item in record.get(field) or [] if isinstance(item, dict) and item.get("label")]


def refs(record: dict, field: str) -> list[tuple[str, str]]:
    """(GND ID, label) pairs of linked GND records."""
    refs = []
    for item in record.get(field) or []:
        uri = item.get("id", "") if isinstance(item, dict) else ""
        if uri.startswith(GND_URI) and re.fullmatch(_GND_ID, uri.removeprefix(GND_URI)):
            refs.append((uri.removeprefix(GND_URI), item.get("label") or uri))
    return refs


def summary(record: dict) -> str:
    """Disambiguating context: life dates, author and dates, or place of business."""
    life = " - ".join(filter(None, ((record.get(f) or [""])[0] for f in ("dateOfBirth", "dateOfDeath"))))
    parts = [
        life,
        ", ".join(_labels(record, "firstAuthor")),
        (record.get("dateOfPublication") or [""])[0],
        ", ".join(_labels(record, "placeOfBusiness")),
        (record.get("dateOfEstablishment") or [""])[0],
    ]
    return " · ".join(filter(None, parts))


def search(query: str, kind: EntityType, limit: int = 10) -> list[dict]:
    """Return unapproved candidates; callers must let an editor choose. Never writes."""
    if not query.strip():
        return []
    if not 1 <= limit <= 50:
        raise ValueError("GND search limit must be between 1 and 50")
    params = {"q": query.strip(), "filter": f"type:{RECORD_TYPES[kind]}", "size": limit, "format": "json"}
    response = _request_json(f"{API_URL}/search?{urlencode(params)}")
    return [
        {
            "id": record["gndIdentifier"],
            "label": record.get("preferredName", record["gndIdentifier"]),
            "description": summary(record),
            "url": GND_URI + record["gndIdentifier"],
            "record": record,
        }
        for record in response.get("member", [])
        if isinstance(record, dict) and isinstance(record.get("gndIdentifier"), str)
    ]


record_id = operator.itemgetter("gndIdentifier")


def names(record: dict) -> list[str]:
    """Preferred and variant names in natural order; used for exact-name matching, never as identity."""
    person = "Person" in (record.get("type") or [])
    raw = [record.get("preferredName", ""), *(record.get("variantName") or [])]
    return [display_name(n) if person else n for n in raw if n]


def years(record: dict) -> dict[str, int]:
    """Life years of a person as far as GND states them."""
    found = {}
    for field, key in (("dateOfBirth", "born"), ("dateOfDeath", "died")):
        if match := re.match(r"-?(\d{1,4})", (record.get(field) or [""])[0]):
            found[key] = int(match.group(0))
    return found


def author_ids(record: dict) -> set[str]:
    """GND IDs of the credited authors and composers."""
    return {gnd_id for field in CREDIT_FIELDS for gnd_id, _ in refs(record, field)}


def places(record: dict) -> list[str]:
    """Place-of-business labels of a corporate body."""
    return _labels(record, "placeOfBusiness")


def wikidata_ids(record: dict) -> set[str]:
    """Wikidata items GND itself declares as the same entity."""
    prefix = "http://www.wikidata.org/entity/"
    return {
        item["id"].removeprefix(prefix)
        for item in record.get("sameAs") or []
        if isinstance(item, dict) and str(item.get("id", "")).startswith(prefix)
    }


def _source(session: Session) -> Source:
    return external_source(session, "GND", GND_URI)


def identity(session: Session, kind: EntityType, entity_id: uuid.UUID) -> str | None:
    """The selected GND ID of an entity; read-only."""
    return _identity(session, "GND", kind, entity_id)


def _attach(session: Session, kind: EntityType, entity_id: uuid.UUID, gnd_id: str) -> None:
    attach_identity(session, _source(session), kind, entity_id, gnd_id, GND_URI + gnd_id)


def _resolve_record(
    session: Session, kind: EntityType, entity_id: uuid.UUID, gnd_id: str | None, record: dict | None
) -> dict:
    if gnd_id is None and record is None:
        link = entity_link(session, _source(session), kind, entity_id)
        if link is None or link.external_id is None:
            raise ValueError("No GND identity selected")
        gnd_id = link.external_id
    if record is None:
        record = fetch_record(str(gnd_id))
    elif gnd_id is not None and record.get("gndIdentifier") != parse_gnd_id(gnd_id):
        raise ValueError("GND record does not match selected ID")
    if RECORD_TYPES[kind] not in (record.get("type") or []):
        raise ValueError(f"GND record {record.get('gndIdentifier')} is not a {RECORD_TYPES[kind]}")
    _attach(session, kind, entity_id, record["gndIdentifier"])
    return record


def _report(record: dict) -> dict[str, Any]:
    return {
        "gnd_id": record["gndIdentifier"],
        "filled": [],
        "conflicts": [],
        "added_names": [],
        "warnings": [],
    }


def _date(record: dict, field: str, warnings: list[str]) -> tuple[dt.datetime, DatePrecision] | None:
    value = (record.get(field) or [None])[0]
    if not isinstance(value, str):
        return None
    if span := re.fullmatch(r"(\d{4})-(\d{4})", value):
        # shortcut: a creation span keeps its start year; add an end field if spans matter for search.
        warnings.append(f"{field}: Zeitraum {value}, Beginn übernommen")
        return dt.datetime(int(span.group(1)), 1, 1), DatePrecision.YEAR
    try:
        return parse_date(value)
    except ValueError:
        warnings.append(f"{field}: nicht unterstütztes Datum {value!r} nicht übernommen")
        return None


def _fill_date(entity: object, field: str, record: dict, gnd_field: str, report: dict) -> None:
    if parsed := _date(record, gnd_field, report["warnings"]):
        fill(entity, field, parsed[0], report, parsed[1])


def display_name(name: str) -> str:
    """GND inverts personal names ("Goethe, Johann Wolfgang von"); show them in natural order."""
    surname, _, given = name.partition(", ")
    return f"{given} {surname}" if given and "," not in given else name


def _variants(record: dict, *, person: bool) -> list[str]:
    """Selective variants: Latin script, and for people only spellings of the same surname."""
    surname = fold(record.get("preferredName", "").partition(",")[0])
    names = []
    for value in record.get("variantName") or []:
        if not isinstance(value, str) or not latin(value) or "DE-576" in value:
            continue
        if person and fold(value.partition(",")[0]) != surname:
            continue
        names.append(display_name(value) if person else value)
    return names


def _apply_names(person: Person, record: dict, report: dict) -> None:
    known = {normalize(name) for name in [person.name, *(person.alt_names or [])]}
    added = []
    for name in [display_name(record.get("preferredName", "")), *_variants(record, person=True)]:
        if name and normalize(name) not in known and len(added) < MAX_VARIANTS:
            known.add(normalize(name))
            added.append(name)
    if added:
        person.alt_names = [*(person.alt_names or []), *added]
        report["added_names"] = added


def enrich_person(session: Session, person: Person, gnd_id: str | None = None, *, record: dict | None = None) -> dict:
    """Life dates, gender and alternative names; relatives, occupations and biographies stay in GND."""
    record = _resolve_record(session, EntityType.PERSON, person.id, gnd_id, record)
    report = _report(record)
    _fill_date(person, "born", record, "dateOfBirth", report)
    _fill_date(person, "died", record, "dateOfDeath", report)
    genders = {GENDERS.get(item.get("id", "").rpartition("#")[2]) for item in record.get("gender") or []}
    if len(genders) == 1:
        fill(person, "gender", genders.pop(), report)
    _apply_names(person, record, report)
    session.add(person)
    session.flush()
    return report


def enrich_organization(
    session: Session, organization: Organization, gnd_id: str | None = None, *, record: dict | None = None
) -> dict:
    """Founding/closing dates; predecessor and successor bodies are reported as links, not imported."""
    record = _resolve_record(session, EntityType.ORGANIZATION, organization.id, gnd_id, record)
    report = _report(record)
    _fill_date(organization, "founded", record, "dateOfEstablishment", report)
    _fill_date(organization, "closed", record, "dateOfTermination", report)
    report["predecessors"] = [{"id": i, "label": label} for i, label in refs(record, "precedingCorporateBody")]
    report["successors"] = [{"id": i, "label": label} for i, label in refs(record, "succeedingCorporateBody")]
    session.add(organization)
    session.flush()
    return report


def _credit_person(session: Session, gnd_id: str, label: str, role: str, report: dict) -> uuid.UUID | None:
    if person_id := linked_entity(session, _source(session), EntityType.PERSON, gnd_id):
        return person_id
    name = display_name(label)
    if candidate := same_name_person(session, name):
        # Names are not identities: the editor confirms by giving that person this GND ID.
        report["unlinked_people"].append({"gnd_id": gnd_id, "label": label, "candidate_id": str(candidate.id)})
        return None
    person = Person(name=name, type=[role])
    session.add(person)
    session.flush()
    enrich_person(session, person, gnd_id)
    report["people_created"].append(name)
    return person.id


def _apply_credits(session: Session, text: Text, record: dict, report: dict) -> None:
    for field, role in CREDIT_FIELDS.items():
        for gnd_id, label in refs(record, field):
            person_id = _credit_person(session, gnd_id, label, role, report)
            if person_id is None:
                continue
            exists = session.exec(
                select(TextContribution).where(
                    TextContribution.text_id == text.id,
                    TextContribution.person_id == person_id,
                    TextContribution.role == role,
                )
            ).first()
            if exists is None:
                session.add(TextContribution(text_id=text.id, person_id=person_id, role=role))
                session.flush()
                report["credits_added"] += 1


def _predecessor(session: Session, gnd_id: str, label: str, report: dict) -> Text | None:
    if text_id := linked_entity(session, _source(session), EntityType.TEXT, gnd_id):
        return session.get(Text, text_id)
    if candidate := same_title_text(session, label):
        report["unlinked_texts"].append({"gnd_id": gnd_id, "label": label, "candidate_id": str(candidate.id)})
        return None
    stub = Text(title=label)
    session.add(stub)
    session.flush()
    # Stubs get their own facts but no further predecessors: lineage stops after one hop.
    enrich_text(session, stub, gnd_id, predecessor_ids=())
    report["texts_created"].append(label)
    return stub


def enrich_text(
    session: Session,
    text: Text,
    gnd_id: str | None = None,
    *,
    record: dict | None = None,
    predecessor_ids: Collection[str] | None = None,
) -> dict:
    """Creation/publication dates, genre, title variants, credits and selected direct predecessors.

    ``predecessor_ids`` selects which GND ``precedingWork`` entries become ``based_on`` lineage;
    None takes all direct ones, an empty collection none. Writes belong to the caller's transaction.
    """
    derived = session.exec(
        select(TextLineage).where(
            TextLineage.derived_text_id == text.id,
            col(TextLineage.relation).in_([str(relation) for relation in FAMILY_RELATIONS]),
        )
    ).first()
    if derived is not None:
        raise ValueError("Text is a translation/adaptation; select its family root for GND enrichment")
    record = _resolve_record(session, EntityType.TEXT, text.id, gnd_id, record)
    report: dict[str, Any] = {
        **_report(record),
        "credits_added": 0,
        "people_created": [],
        "unlinked_people": [],
        "predecessors": [],
        "texts_created": [],
        "unlinked_texts": [],
    }
    _fill_date(text, "created", record, "dateOfProduction", report)
    _fill_date(text, "published", record, "dateOfPublication", report)
    genres = {WORK_GENRES[i] for i, _ in refs(record, "formOfWorkAndExpression") if i in WORK_GENRES}
    if len(genres) == 1:
        fill(text, "genre", genres.pop(), report)
    known = {row.normalized for row in session.exec(select(TextTitle).where(TextTitle.text_id == text.id))}
    for title in [record.get("preferredName", ""), *_variants(record, person=False)]:
        if title and normalize(title) not in known and len(report["added_names"]) < MAX_VARIANTS:
            add_title(session, text, title)
            known.add(normalize(title))
            report["added_names"].append(title)
    _apply_credits(session, text, record, report)
    for pred_id, label in refs(record, "precedingWork"):
        if predecessor_ids is not None and pred_id not in predecessor_ids:
            continue
        if (parent := _predecessor(session, pred_id, label, report)) is not None:
            add_lineage(session, parent.id, text.id, LineageRelation.BASED_ON, notes="GND precedingWork")
            report["predecessors"].append(label)
    session.add(text)
    session.flush()
    return report


def enrich(
    session: Session,
    kind: EntityType,
    entity_id: uuid.UUID,
    gnd_id: str,
    *,
    record: dict | None = None,
    predecessor_ids: Collection[str] | None = None,
) -> dict:
    """Dispatch to the entity-specific enrichment; ``predecessor_ids`` applies to texts only."""
    if kind == EntityType.PERSON and (person := session.get(Person, entity_id)):
        return enrich_person(session, person, gnd_id, record=record)
    if kind == EntityType.TEXT and (text := session.get(Text, entity_id)):
        return enrich_text(session, text, gnd_id, record=record, predecessor_ids=predecessor_ids)
    if kind == EntityType.ORGANIZATION and (organization := session.get(Organization, entity_id)):
        return enrich_organization(session, organization, gnd_id, record=record)
    raise ValueError(f"No {kind} {entity_id} for GND enrichment")
