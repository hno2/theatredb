"""Open Repertoire Interchange Format (ORIF v2) export and import.

Format: https://buehnenverein.github.io/open-repertoire/ — the field mapping and
known gaps are documented in specs/open-repertoire.md. Export works on the JSON
dicts already produced by ``backend.entity_management`` loaders; import turns an
ORIF document into staged production records for ``backend.production_import``.
"""

import datetime as dt
from collections.abc import Iterable
from itertools import starmap
from typing import Any
from zoneinfo import ZoneInfo

from backend.config import PERFORMER_ROLES, canonical_role, role_label

ORIF_CONTEXT = "https://schema.org"
ORIF_VERSION = "v2"
ORIF_MEDIA_TYPE = "application/ld+json"
ORIF_GENRES = frozenset(
    {
        "audiowalk", "ballett", "comedy", "digitaltheater", "figurentheater", "game-theater", "improtheater",
        "inklusives-theater", "installation", "kabarett", "kammerkonzert", "konzert", "lecture-performance", "lesung",
        "live-hoerspiel", "musical", "musiktheater", "objekttheater", "oper", "operette", "performance",
        "physical-theatre", "postmigrantisches-theater", "puppentheater", "sinfoniekonzert", "sprechtheater",
        "szenische-lesung", "szenisches-konzert", "tanz", "theater-im-oeffentlichen-raum", "workshop",
        "zeitgenoessischer-tanz",
    }
)  # fmt: skip
TEXT_GENRE_TO_ORIF = {"play": "sprechtheater", "musical": "musical", "opera": "oper"}
MUSIC_GENRES = frozenset({"musical", "opera"})
# Theapolis premiere pills become production tags; the first tag per type is used on import.
PRODUCTION_TYPE_TAGS = {
    "ua": "WorldPremiere",
    "dea": "LanguagePremiere",
    "dse": "LanguagePremiere",
    "öea": "CountryPremiere",
    "sea": "CountryPremiere",
    "ea": "FirstPerformance",
}
TAG_FOR_PRODUCTION_TYPE = {kind: tag.upper() for tag, kind in reversed(PRODUCTION_TYPE_TAGS.items())}
# shortcut: premieres are stored as source wall time and all current sources are DACH (same offsets as Berlin);
# store a timezone per organization once theatres outside CET/CEST are imported.
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional values; ORIF forbids nulls in typed fields."""
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def _person(person: dict) -> dict:
    return _compact({"@type": "Person", "identifier": str(person.get("id") or ""), "name": person["name"]})


def _address(organization: dict) -> dict | None:
    if not (organization.get("postal_code") and organization.get("address_locality")):
        return None  # ORIF requires both postalCode and addressLocality.
    return _compact(
        {
            "@type": "PostalAddress",
            "streetAddress": organization.get("street_address"),
            "postalCode": organization["postal_code"],
            "addressLocality": organization["address_locality"],
            "addressCountry": organization.get("address_country"),
        }
    )


def organization_to_orif(organization: dict) -> dict:
    """Map an organization dict to an ORIF Organization."""
    return _compact(
        {
            "@type": "Organization",
            "identifier": str(organization["id"]),
            "name": organization["name"],
            "address": _address(organization),
        }
    )


def _people_by_role(contributions: Iterable[dict]) -> dict[str | None, list[dict]]:
    people: dict[str | None, list[dict]] = {}
    for contribution in contributions:
        if (person := contribution.get("person") or {}).get("name"):
            people.setdefault(canonical_role(contribution.get("role")), []).append(_person(person))
    return people


def _work(text: dict | None) -> dict | None:
    if not text or not text.get("title"):
        return None
    people = _people_by_role(text.get("contributions") or [])
    if text.get("genre") in MUSIC_GENRES:
        return _compact(
            {
                "@type": "MusicComposition",
                "name": text["title"],
                "composer": people.get("composer"),
                "lyricist": people.get("lyricist", []) + people.get("author", []),
            }
        )
    return _compact(
        {
            "@type": "Play" if text.get("genre") == "play" else "CreativeWork",
            "name": text["title"],
            "author": people.get("author"),
            "translator": people.get("translator"),
        }
    )


def _credits(production: dict) -> tuple[list[dict], list[dict]]:
    """Split memberships into ORIF creator roles and on-stage performer roles."""
    creators, performers = [], []
    for membership in production.get("memberships") or []:
        if not (person := membership.get("person") or {}).get("name"):
            continue
        role_name, character = membership.get("role_name"), membership.get("character_name")
        role = canonical_role(role_name)
        if role in PERFORMER_ROLES or character:
            performer = {"characterName": character, "roleName": None if role == "actor" else role_label(role_name)}
            performers.append(_compact({"@type": "PerformanceRole", "performer": [_person(person)], **performer}))
        else:
            creators.append(
                _compact({"@type": "Role", "creator": [_person(person)], "roleName": role_label(role_name)})
            )
    return creators, performers


def _premiere_event(production: dict, organization: dict, performers: list[dict]) -> dict | None:
    """ORIF events need a date-time, so month/year premieres cannot be exported."""
    if not production.get("premiere") or production.get("premiere_precision") not in ("day", "minute"):
        return None
    start = dt.datetime.fromisoformat(production["premiere"]).replace(tzinfo=LOCAL_TIMEZONE)
    address = _address(organization)
    place = {
        "@type": "Place",
        "name": organization.get("name"),
        "address": address,
        "latitude": organization.get("latitude"),
        "longitude": organization.get("longitude"),
    }
    return _compact(
        {
            "@type": "Event",
            "identifier": f"{production['id']}:premiere",
            "startDate": start.isoformat(),
            "duration": production.get("duration"),
            "url": production.get("url"),
            "location": [_compact(place)] if address else None,
            "performer": performers,
            "eventType": ["Premiere"],
        }
    )


def production_to_orif(production: dict, organization: dict | None = None) -> dict:
    """Map a production dict (memberships with person, optional text with contributions) to ORIF."""
    organization = organization or production.get("organization") or {}
    text = production.get("text") or {}
    creators, performers = _credits(production)
    event = _premiere_event(production, organization, performers)
    if event is None:
        # Without an event, cast can only be listed as production creators (character names are lost).
        creators += [
            {"@type": "Role", "creator": row["performer"], "roleName": row.get("roleName") or role_label("actor")}
            for row in performers
        ]
    tag_list = production.get("tags") or []
    tags = {tag.casefold() for tag in tag_list}
    genres = sorted(
        tags & ORIF_GENRES | ({TEXT_GENRE_TO_ORIF[text["genre"]]} if text.get("genre") in TEXT_GENRE_TO_ORIF else set())
    )
    age = production.get("suggested_min_age")
    return _compact(
        {
            "@type": "Play" if text.get("genre") == "play" else "CreativeWork",
            "identifier": str(production["id"]),
            "name": production["name"],
            "description": production.get("description"),
            "isBasedOn": [work] if (work := _work(text)) else None,
            "genre": genres,
            "productionType": next(
                (PRODUCTION_TYPE_TAGS[tag.casefold()] for tag in tag_list if tag.casefold() in PRODUCTION_TYPE_TAGS),
                None,
            ),
            "inLanguage": [language for language in text.get("languages") or [] if language != "mul"],
            "audience": {"@type": "PeopleAudience", "suggestedMinAge": age} if age is not None else None,
            "creator": creators,
        }
    ) | {"events": [event] if event else []}


def orif_document(organization: dict, productions: Iterable[dict]) -> dict:
    """Build one ORIF document for a theatre and its productions."""
    return {
        "@context": ORIF_CONTEXT,
        "version": ORIF_VERSION,
        "organization": organization_to_orif(organization),
        "productions": [production_to_orif(production, organization) for production in productions],
    }


def orif_documents(productions: Iterable[dict]) -> list[dict]:
    """Group productions by theatre into ORIF documents; productions without a theatre cannot be exported."""
    grouped: dict[str, tuple[dict, list[dict]]] = {}
    for production in productions:
        if organization := production.get("organization"):
            grouped.setdefault(str(organization["id"]), (organization, []))[1].append(production)
    return list(starmap(orif_document, grouped.values()))


def _plain(value: object) -> str | None:
    """Read ORIF StringType or InternationalizedString, preferring German."""
    if isinstance(value, dict):
        return next((value[language] for language in ("de", "en", "fr") if value.get(language)), None)
    return value if isinstance(value, str) else None


def _wall_time(value: str) -> str:
    """Store ORIF's offset date-times as local wall time, matching the export."""
    start = dt.datetime.fromisoformat(value)
    if start.tzinfo is not None:
        start = start.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
    return start.isoformat(timespec="minutes")


def _names(agents: object) -> list[str]:
    if not isinstance(agents, list):
        return []
    return [agent["name"] for agent in agents if isinstance(agent, dict) and agent.get("name")]


def _member(agent: dict, role: str | None, character: str | None = None) -> dict:
    return _compact(
        {"name": agent["name"], "external_id": agent.get("identifier"), "role_name": role, "character_name": character}
    )


def _staged_text(works: list, languages: list[str]) -> dict | None:
    # shortcut: staged productions hold one text; further isBasedOn/workPerformed works are dropped.
    work = works[0] if works else None
    if not isinstance(work, dict) or not _plain(work.get("name")):
        return None
    language = languages[0].split("-")[0].lower() if len(languages) == 1 else None
    return _compact(
        {
            "title": _plain(work["name"]),
            "authors": _names(work.get("author")),
            "translators": _names(work.get("translator")),
            "composers": _names(work.get("composer")),
            "lyricists": _names(work.get("lyricist")),
            "genre": "play" if work.get("@type") == "Play" else None,
            "language": language if language and len(language) == 2 else None,
        }
    )


def _staged_production(production: dict) -> dict:
    events = [event for event in production.get("events") or [] if isinstance(event, dict)]
    premiere = next((event for event in events if "Premiere" in (event.get("eventType") or [])), None)
    url = next((event["url"] for event in ([premiere] if premiere else []) + events if event.get("url")), None)
    if not url:
        raise ValueError(f"ORIF production {production.get('identifier')!r} needs an event url as its source link")
    # shortcut: Organization agents (ensembles, bands) are skipped; staged credits are people only.
    people = [
        _member(agent, role.get("roleName"))
        for role in production.get("creator") or []
        for agent in role.get("creator") or []
        if agent.get("@type") == "Person" and agent.get("name")
    ] + [
        _member(agent, role.get("roleName") or "actor", role.get("characterName"))
        for event in events
        for role in event.get("performer") or []
        for agent in role.get("performer") or []
        if agent.get("@type") == "Person" and agent.get("name")
    ]
    audience = production.get("audience") or {}
    first = premiere or (events[0] if events else {})
    return _compact(
        {
            "title": _plain(production.get("name")),
            "url": url,
            "description": _plain(production.get("description")),
            "premiere": _wall_time(premiere["startDate"]) if premiere else None,
            "duration": first.get("duration"),
            "suggested_min_age": audience.get("suggestedMinAge"),
            "tags": [
                *(production.get("genre") or []),
                *filter(None, [TAG_FOR_PRODUCTION_TYPE.get(production.get("productionType") or "")]),
            ],
            "text": _staged_text(
                production.get("isBasedOn") or production.get("workPerformed") or [], production.get("inLanguage") or []
            ),
            # Performers repeat per event; keep each credit once, in source order.
            "memberships": list({repr(sorted(member.items())): member for member in people}.values()),
        }
    )


def as_staged(data: object) -> object:
    """Convert an ORIF document to staged production records; any other payload is returned unchanged."""
    if not isinstance(data, dict) or data.get("@context") != ORIF_CONTEXT or "productions" not in data:
        return data
    if data.get("version") != ORIF_VERSION:
        raise ValueError(f"Unsupported ORIF version {data.get('version')!r}; expected {ORIF_VERSION!r}")
    try:
        return [_staged_production(production) for production in data["productions"]]
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError(f"Invalid ORIF document: {error!r}") from error
