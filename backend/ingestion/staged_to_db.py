"""Import staged theatre JSON into an existing theatre; only real conflicts are queued for review."""

import argparse
import datetime as dt
import json
import re
import uuid
from pathlib import Path

from sqlmodel import select

from backend.models import Organization
from backend.production_import import ingest_records
from backend.reconciliation import normalize
from backend.user_management import get_session, init_db

DEFAULT_ACTOR_ID = "ingestion"


def find_organization(*, organization_id: str | None = None, organization_name: str | None = None) -> uuid.UUID:
    """Resolve the existing theatre by UUID or unique (normalized) name."""
    with get_session() as session:
        if organization_id:
            organization = session.get(Organization, uuid.UUID(organization_id))
            if organization is None:
                raise ValueError("Theatre not found")
            return organization.id
        if not organization_name or not organization_name.strip():
            raise ValueError("--organization-name or --organization-id is required")
        matches = [
            row.id
            for row in session.exec(select(Organization)).all()
            if normalize(row.name) == normalize(organization_name)
        ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one theatre named {organization_name!r}, found {len(matches)}")
    return matches[0]


def import_staged_productions(
    *,
    json_path: Path,
    actor_id: str,
    source_name: str | None = None,
    source_base_url: str | None = None,
    organization_name: str | None = None,
    organization_id: str | None = None,
) -> dict[str, list[str]]:
    """Import a dated JSON snapshot; the date comes from a ``YYYYMMDD_`` filename prefix."""
    records = json.loads(json_path.read_text(encoding="utf-8"))
    organization = find_organization(organization_id=organization_id, organization_name=organization_name)
    match = re.match(r"(\d{8})_", json_path.name)
    captured_at = dt.datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=dt.UTC) if match else None
    result = ingest_records(
        records,
        organization_id=organization,
        source_name=source_name or json_path.parent.name,
        actor_id=actor_id,
        source_base_url=source_base_url,
        captured_at=captured_at,
        file_name=json_path.name,
    )
    print({key: len(value) for key, value in result.items()})
    return result


def parse_args() -> argparse.Namespace:
    """Parse import options."""
    parser = argparse.ArgumentParser(description="Import theatre JSON into an existing theatre")
    parser.add_argument("--json", required=True, help="Path to staged JSON file")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID)
    parser.add_argument("--source-name", default=None)
    parser.add_argument("--source-base-url", default=None)
    theatre = parser.add_mutually_exclusive_group(required=True)
    theatre.add_argument("--organization-name")
    theatre.add_argument("--organization-id")
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
        organization_id=args.organization_id,
    )
