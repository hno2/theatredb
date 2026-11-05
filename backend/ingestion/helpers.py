import json
from pathlib import Path

from pydantic import ValidationError

from backend.entity_management import create_entity
from backend.models import Organization, OrganizationMembership, Person, Source

ACTOR_ID = "Simon"


def _read_latest(data_dir: Path) -> list[dict]:
    """Find latest JSON file in data dir (named as YYYYMMDD.json) and return its content as list of dicts."""
    latest_file = max(data_dir.glob("*.json"), default=None)
    if latest_file is None:
        return []
    try:
        payload: list[dict] = json.loads(latest_file.read_text(encoding="utf-8"))
        return payload
    except json.JSONDecodeError:
        return []


def add_org_and_org_members(json_path: Path):
    source = create_entity(
        Source,
        payload={
            "name": "Theaterkompass",
            "base_url": "https://theaterkompass.de/",
            "url_pattern": "https://theaterkompass.de/?type={id}",
            "description": "Das Nachrichtenportal für Theaterbesucher und Theatermacher. Seit 2004 schreiben wir über News, Trends und Kritiken rund um das Thema Theater.",
        },
        get_if_exists={"name": "Theaterkompass"},
        actor_id=ACTOR_ID,
    )
    memberships = []
    for orga in _read_latest(json_path):
        if orga.get("memberships") and len(orga["memberships"]) > 0:
            for p in orga["memberships"]:
                try:
                    person = create_entity(  # TODO: Maybe add sources here
                        model_cls=Person,
                        payload={
                            "name": next(iter(p.keys())),
                            "type": ["management"],
                        },
                        actor_id=ACTOR_ID,
                        get_if_exists={"name": next(iter(p.keys()))},
                    )
                    memberships.append(
                        {
                            "person_id": person.id,
                            "role": next(iter(p.values())),
                        }
                    )
                except ValidationError as e:
                    print(f"Validation error for person {next(iter(p.keys()))}: {e}")
        try:
            created_orga = create_entity(
                Organization,
                payload=orga,
                actor_id=ACTOR_ID,
                source_links=[
                    {
                        "source_id": source.id,  # already in db
                        "external_url": f"https://theaterkompass.de/theaterverzeichnisse/organisation/{orga['uid']}",
                    }
                ],
            )
        except ValidationError as e:
            print(f"Validation error for {orga['name']}: {e}")
        for m in memberships:
            create_entity(
                OrganizationMembership,
                payload={
                    "person_id": m["person_id"],
                    "organization_id": created_orga.id,
                    "role": m["role"],
                },
                actor_id=ACTOR_ID,
            )
