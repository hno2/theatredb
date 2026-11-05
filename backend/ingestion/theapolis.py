"""backend/ingestion/theapolis.py

Import theapolis.de data into database:
- Organizations from production list
- Productions with source links
- Directors and creators as persons, linked via ProductionMembership

Run after download():
    python -m backend.ingestion.theapolis import_all
"""

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import aiohttp
from tqdm.asyncio import tqdm

from backend.entity_management import create_entity
from backend.models import (
    EntityType,
    Organization,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextContribution,
)
from backend.user_management import init_db

BASE_URL = "https://www.theapolis.de/api/de/productions?page={page}&items=500&status[0]=2&status[1]=4&search="
OUTPUT_FILE = "productions_new.json"

CONCURRENCY = 10
MAX_RETRIES = 5
RETRY_BACKOFF = 1.5
ACTOR_ID = "theapolis-ingestion"

SEM = asyncio.Semaphore(CONCURRENCY)


async def fetch_page(session: aiohttp.ClientSession, page: int) -> dict:
    url = BASE_URL.format(page=page)
    delay = RETRY_BACKOFF
    for attempt in range(MAX_RETRIES):
        async with SEM:
            try:
                async with session.get(url) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", delay))
                        await asyncio.sleep(retry_after)
                        delay *= 2
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError, asyncio.TimeoutError:
                if attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Failed to fetch page {page} after {MAX_RETRIES} retries")


async def download():
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    ) as session:
        first = await fetch_page(session, 1)
        data = first["data"]
        total_count = data["totalCount"]
        per_page = data["numItemsPerPage"]
        total_pages = math.ceil(total_count / per_page)

        print(f"Total items: {total_count:,} | Per page: {per_page} | Pages: {total_pages:,}")

        items_written = 0

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write('{"success":true,"items":[\n')

            # Write first page immediately
            for i, item in enumerate(data["items"]):
                f.write(json.dumps(item, ensure_ascii=False))
                f.write(",\n")
            items_written += len(data["items"])

            # Stream remaining pages as they arrive
            tasks = [fetch_page(session, p) for p in range(2, total_pages + 1)]
            pbar = tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Fetching pages", unit="page")

            for coro in pbar:
                result = await coro
                items = result["data"]["items"]
                for item in items:
                    f.write(json.dumps(item, ensure_ascii=False))
                    f.write(",\n")
                items_written += len(items)
                pbar.set_postfix(items=f"{items_written:,}")
                f.flush()

            # Rewind and fix the trailing comma on the last item
            f.seek(f.tell() - 2)
            f.write(f'\n],"count":{items_written}}}\n')

    print(f"Saved {items_written:,} items to {OUTPUT_FILE}")


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    out = value.strip()
    return out or None


def _parse_geolocation(geoloc_str: str | None) -> tuple[float, float] | None:
    """Parse '52.5127766,13.3084693' → (52.5127766, 13.3084693)."""
    if not geoloc_str:
        return None
    try:
        parts = geoloc_str.split(",")
        return (float(parts[0]), float(parts[1]))
    except ValueError, IndexError:
        return None


def import_all(json_path: Path = Path(OUTPUT_FILE)) -> None:
    """Import organizations, productions, and persons from theapolis JSON."""
    init_db()

    with open(json_path) as f:
        data = json.load(f)
    items = data.get("items", [])

    if not items:
        print("No items found.")
        return

    # Create source
    source = create_entity(
        Source,
        payload={
            "name": "theapolis.de",
            "base_url": "https://www.theapolis.de/de/",
            "url_pattern": "https://www2.theapolis.de/de/produktionen/{id}",
            "description": "Theater productions, organizations, and persons from theapolis.de",
        },
        get_if_exists={"name": "theapolis.de"},
        actor_id=ACTOR_ID,
    )
    print(f"Source: {source.name} ({source.id})")

    # Extract unique orgas
    orga_map = {}
    for prod in items:
        org = prod.get("organization")
        if org and org.get("id"):
            org_id = org["id"]
            if org_id not in orga_map:
                orga_map[org_id] = org

    print(f"Importing {len(orga_map)} organizations...")
    org_db_map = {}
    for org in orga_map.values():
        org_id = org.get("id")
        name = org.get("name")
        slug = org.get("slug")
        location = org.get("location", {})
        city = location.get("city")
        country = location.get("country")
        geoloc = location.get("geolocation")

        lat, lon = None, None
        if geoloc_tuple := _parse_geolocation(geoloc):
            lat, lon = geoloc_tuple

        org_db = create_entity(
            Organization,
            payload={
                "name": name,
                "city": city,
                "country": country,
                "lat": lat,
                "lon": lon,
            },
            get_if_exists={"name": name},
            actor_id=ACTOR_ID,
            source_links=[
                {
                    "source_id": source.id,
                    "external_id": str(org_id),
                    "external_url": f"https://www.theapolis.de/de/organization/{slug}",
                    "title": name,
                }
            ],
        )
        org_db_map[org_id] = org_db

    print(f"Imported {len(org_db_map)} organizations.")

    # Import persons for directors and creators first
    person_db_map = {}

    print(f"Importing productions and persons...")
    for prod in tqdm(items, desc="Importing", unit="prod"):
        prod_id = prod.get("id")
        prod_name = prod.get("name")
        slug = prod.get("slug")
        premiere_date = _clean_str(prod.get("premierDate"))

        if not prod_name:
            continue

        org_id = prod.get("organization", {}).get("id")
        org_db = org_db_map.get(org_id)

        # Create or fetch Text (for creators as authors)
        text_db = None
        creators = prod.get("creators", [])
        if creators:
            text_db = create_entity(
                Text,
                payload={"title": prod_name},
                get_if_exists={"title": prod_name},
                actor_id=ACTOR_ID,
                source_links=[
                    {
                        "source_id": source.id,
                        "external_id": str(prod_id),
                        "external_url": f"https://www2.theapolis.de/de/produktionen/{prod_id}",
                        "title": prod_name,
                    }
                ],
            )

        # Create production
        get_if_exists = {"name": prod_name}
        if org_db:
            get_if_exists["organization_id"] = org_db.id

        prod_db = create_entity(
            Production,
            payload={
                "name": prod_name,
                "premiere": None,  # parse if needed
                "organization_id": org_db.id if org_db else None,
                "text_id": text_db.id if text_db else None,
                "url": f"https://www2.theapolis.de/de/produktionen/{prod_id}",
            },
            get_if_exists=get_if_exists,
            actor_id=ACTOR_ID,
            source_links=[
                {
                    "source_id": source.id,
                    "external_id": str(prod_id),
                    "external_url": f"https://www2.theapolis.de/de/produktionen/{prod_id}",
                    "title": prod_name,
                }
            ],
        )

        # Link directors
        for director in prod.get("directors", []):
            dir_name = director.get("name")
            dir_id = director.get("id")
            if not dir_name:
                continue

            person_key = (dir_name, "director")
            if person_key not in person_db_map:
                person_db = create_entity(
                    Person,
                    payload={
                        "name": dir_name,
                        "type": ["director"],
                    },
                    get_if_exists={"name": dir_name},
                    actor_id=ACTOR_ID,
                    source_links=[
                        {
                            "source_id": source.id,
                            "external_id": str(dir_id),
                            "title": dir_name,
                        }
                    ],
                )
                person_db_map[person_key] = person_db
            else:
                person_db = person_db_map[person_key]

            create_entity(
                ProductionMembership,
                payload={
                    "production_id": prod_db.id,
                    "person_id": person_db.id,
                    "role": "director",
                },
                get_if_exists={
                    "production_id": prod_db.id,
                    "person_id": person_db.id,
                    "role": "director",
                },
                actor_id=ACTOR_ID,
            )

        # Link creators as authors to Text and Production
        for creator in creators:
            creator_name = creator.get("name")
            creator_id = creator.get("id")
            if not creator_name:
                continue

            person_key = (creator_name, "author")
            if person_key not in person_db_map:
                person_db = create_entity(
                    Person,
                    payload={
                        "name": creator_name,
                        "type": ["author"],
                    },
                    get_if_exists={"name": creator_name},
                    actor_id=ACTOR_ID,
                    source_links=[
                        {
                            "source_id": source.id,
                            "external_id": str(creator_id),
                            "title": creator_name,
                        }
                    ],
                )
                person_db_map[person_key] = person_db
            else:
                person_db = person_db_map[person_key]

            # Link to Text as author
            if text_db:
                create_entity(
                    TextContribution,
                    payload={
                        "text_id": text_db.id,
                        "person_id": person_db.id,
                        "role": "author",
                    },
                    get_if_exists={
                        "text_id": text_db.id,
                        "person_id": person_db.id,
                        "role": "author",
                    },
                    actor_id=ACTOR_ID,
                )

            # Link to Production as creator
            create_entity(
                ProductionMembership,
                payload={
                    "production_id": prod_db.id,
                    "person_id": person_db.id,
                    "role": "creator",
                },
                get_if_exists={
                    "production_id": prod_db.id,
                    "person_id": person_db.id,
                    "role": "creator",
                },
                actor_id=ACTOR_ID,
            )

    print(f"Import complete!")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "import_all":
        import_all()
    else:
        print("Usage: python -m backend.ingestion.theapolis [download|import_all]")
        print("  download  — fetch latest productions from theapolis.de API")
        print("  import_all — import from productions_new.json into database")
        print()
        print("Or run download() or import_all() directly in Python.")
