"""Stage theatre JSON for review; domain records are only written after approval."""

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from sqlmodel import select

from backend.ingestion.staging import validate_records
from backend.models import EntityType, Source
from backend.reconciliation import canonical_role, canonical_url, propose_record
from backend.user_management import get_session, init_db

DEFAULT_ACTOR_ID = "ingestion"


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Expected text, got {type(value).__name__}")
    return value.strip() or None


def _premiere(value: object) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    return dt.datetime.fromisoformat(value).isoformat()


def _stage_person(member: dict, record: dict, organization_name: str, common: dict) -> tuple[str, str, dict]:
    name, raw_role = member["name"].strip(), _clean(member.get("role"))
    raw_types = member.get("type") or raw_role or "actor"
    person_types = list(
        dict.fromkeys(canonical_role(kind) for kind in (raw_types if isinstance(raw_types, list) else [raw_types]))
    )
    person_type = person_types[0]
    if person_type is None:
        raise ValueError("A person type is required")
    role = canonical_role(raw_role) or person_type
    if role not in {
        "actor",
        "director",
        "dramaturg",
        "set_designer",
        "costume_designer",
        "light_designer",
        "choreographer",
        "composer",
        "video_designer",
        "author",
        "translator",
        "adapter",
    }:
        role = person_type
    person_urls = list(dict.fromkeys([*([member["url"]] if member.get("url") else []), *member.get("urls", [])]))
    person_url = person_urls[0] if person_urls else None
    person = propose_record(
        EntityType.PERSON,
        {
            "name": name,
            "type": person_types,
            "urls": person_urls,
            "description": _clean(member.get("description")),
            **{field: member.get(field) for field in ("born", "died", "nationality")},
        },
        external_url=person_url,
        # A source assertion, not a globally unique human.
        record_key=None if person_url else f"credit:{canonical_url(record['url'])}:{name}",
        context={"organization_name": organization_name},
        **common,
    )
    return (
        name,
        person_type,
        {
            "case_id": str(person.id),
            "role": role,
            "character": (_clean(member.get("character")) or raw_role) if role == "actor" else None,
            "source_role": raw_role,
        },
    )


def stage_records(
    records: list[dict],
    *,
    source_name: str,
    organization_name: str,
    actor_id: str,
    source_base_url: str | None = None,
    captured_at: dt.datetime | None = None,
) -> dict[str, list[str]]:
    """Queue organization, people, works, credits and productions with explicit dependencies.

    The source is metadata, not a domain entity. All domain creations and updates
    require review. Reimporting the same snapshot reuses its existing cases.
    """
    if not source_name.strip() or not organization_name.strip():
        raise ValueError("Source and organization names are required")
    records = validate_records(records)
    if source_base_url:
        source_base_url = canonical_url(source_base_url)
    else:
        parsed = urlsplit(records[0]["url"])
        source_base_url = f"https://{parsed.netloc}/"
    with get_session() as session:
        source = session.exec(select(Source).where(Source.name == source_name.strip())).first()
        if source is None:
            source = Source(name=source_name.strip(), base_url=source_base_url)
            session.add(source)
            session.commit()
            session.refresh(source)
        source_id = source.id
    common = {"source_id": source_id, "captured_at": captured_at, "actor_id": actor_id}
    result = {
        kind.value: [] for kind in (EntityType.ORGANIZATION, EntityType.PERSON, EntityType.TEXT, EntityType.PRODUCTION)
    }

    def remember(kind: EntityType, case_id: object) -> str:
        value = str(case_id)
        if value not in result[kind.value]:
            result[kind.value].append(value)
        return value

    org = propose_record(
        EntityType.ORGANIZATION,
        {"name": organization_name.strip(), "url": source_base_url},
        external_url=source_base_url,
        **common,
    )
    org_case = remember(EntityType.ORGANIZATION, org.id)
    for record in records:
        members = list(record.get("memberships", []))
        author = _clean(record.get("author"))
        if author and not any(member["name"].strip() == author for member in members):
            members.append({"name": author, "type": "author", "role": "author"})
        credits, authors, personnel = [], [], {}
        for member in members:
            name, _, credit = _stage_person(member, record, organization_name, common)
            remember(EntityType.PERSON, credit["case_id"])
            credits.append(credit)
            personnel.setdefault(credit["role"], []).append(name)
            if credit["role"] == "author":
                authors.append(name)
        text = propose_record(
            EntityType.TEXT,
            {"title": record["title"].strip()},
            record_key=f"work:{canonical_url(record['url'])}",
            context={
                "authors": sorted(set(authors)),
                "credits": [
                    {"case_id": credit["case_id"], "role": credit["role"]}
                    for credit in credits
                    if credit["role"] in {"author", "translator", "adapter"}
                ],
            },
            **common,
        )
        text_case = remember(EntityType.TEXT, text.id)
        production = propose_record(
            EntityType.PRODUCTION,
            {
                "name": record["title"].strip(),
                "description": _clean(record.get("description")),
                "premiere": _premiere(record.get("premiere")),
                "duration": record.get("duration"),
                "age_rating": record.get("age_rating"),
                "url": record["url"],
                "tags": record["tags"],
                "critics": record["critics"],
            },
            external_url=record["url"],
            context={
                "directors": sorted(set(personnel.get("director", []))),
                "personnel": personnel,
                "credits": credits,
                "dependencies": [
                    {"case_id": org_case, "field": "organization_id"},
                    {"case_id": text_case, "field": "text_id"},
                ],
            },
            **common,
        )
        remember(EntityType.PRODUCTION, production.id)
    return result


def import_staged_productions(
    *,
    json_path: Path,
    actor_id: str,
    source_name: str | None = None,
    source_base_url: str | None = None,
    organization_name: str | None = None,
) -> dict[str, list[str]]:
    """Submit a dated JSON snapshot to reconciliation instead of blindly importing."""
    records = json.loads(json_path.read_text(encoding="utf-8"))
    if not organization_name:
        raise ValueError("--organization-name is required for safe production matching")
    match = re.match(r"(\d{8})_", json_path.name)
    captured_at = dt.datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=dt.UTC) if match else None
    result = stage_records(
        records,
        source_name=source_name or json_path.parent.name,
        organization_name=organization_name,
        actor_id=actor_id,
        source_base_url=source_base_url,
        captured_at=captured_at,
    )
    print(f"Review cases (including existing cases): { {key: len(value) for key, value in result.items()} }")
    return result


def parse_args() -> argparse.Namespace:
    """Parse review-first staging options."""
    parser = argparse.ArgumentParser(description="Submit theatre JSON to the reconciliation review queue")
    parser.add_argument("--json", required=True, help="Path to staged JSON file")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID)
    parser.add_argument("--source-name", default=None)
    parser.add_argument("--source-base-url", default=None)
    parser.add_argument("--organization-name", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    init_db()
    import_staged_productions(
        json_path=Path(args.json),
        actor_id=args.actor_id,
        source_name=args.source_name,
        source_base_url=args.source_base_url,
        organization_name=args.organization_name,
    )
