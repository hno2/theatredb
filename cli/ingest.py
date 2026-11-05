"""Fast, failsafe theapolis importer.

Strategy:
- One session for the entire import
- Bulk-prefetch existing entities into dicts (no N+1 queries)
- Fuzzy dedup for Productions (premiere_year + org_id + normalized name ≥ 80%)
- Per-item error handling: failures are logged, import continues
- create_entity bypassed; inline upsert with shared session
"""

import json
import logging
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, SQLModel, select
from tqdm import tqdm

from backend.entity_management import _attach_missing_sources, _snapshot, add_scd2_event
from backend.models import (
    EntitySource,
    EntityType,
    Organization,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextContribution,
)
from backend.user_management import _get_engine

log = logging.getLogger(__name__)

OUTPUT_FILE = "theapolis_export.json"
ACTOR_ID = "import_theapolis"
BATCH_SIZE = 1000

SOURCE_NAME = "theapolis.de"
SOURCE_BASE_URL = "https://www.theapolis.de/de/"
SOURCE_URL_PATTERN = "https://www2.theapolis.de/de/produktionen/{id}"

# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _clean(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    return v.replace("\x00", "").strip() or None


def _geo(s: str | None) -> tuple[float, float] | None:
    if not s:
        return None
    try:
        a, b = s.split(",", 1)
        return float(a), float(b)
    except ValueError, AttributeError:
        return None


def _norm(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    return re.sub(r"[^a-z0-9 ]+", "", nfkd.encode("ascii", "ignore").decode().lower()).strip()


def _year(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", s)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Bulk flush
# ---------------------------------------------------------------------------


def _col_count(table_cls: type) -> int:
    """Count actual DB columns (incl. server-default cols not in insert dicts)."""
    return len(table_cls.__table__.columns)  # type: ignore[attr-defined]


def _chunk_size(table_cls: type) -> int:
    """Max rows per statement to stay under PostgreSQL's 65535 parameter limit."""
    return max(1, 65000 // _col_count(table_cls))


def _bulk_insert(session: Session, table_cls: type, rows: list[dict]) -> None:
    """INSERT ... ON CONFLICT DO NOTHING, chunked to stay under PG parameter limit."""
    if not rows:
        return
    size = _chunk_size(table_cls)
    for i in range(0, len(rows), size):
        stmt = pg_insert(table_cls).values(rows[i : i + size]).on_conflict_do_nothing()
        session.exec(stmt)  # type: ignore[arg-type]


def _flush_entity_sources(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    size = _chunk_size(EntitySource)
    for i in range(0, len(rows), size):
        stmt = (
            pg_insert(EntitySource)
            .values(rows[i : i + size])
            .on_conflict_do_nothing(index_elements=["source_id", "entity_type", "entity_id"])
        )
        session.exec(stmt)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def in_dach(item: dict) -> bool:
    ALLOWED_COUNTRIES = {"Germany", "Austria", "Switzerland", "Deutschland", "Österreich", "Schweiz"}

    return (
        isinstance((item.get("organization") or {}).get("location"), dict)
        and ((item.get("organization") or {}).get("location") or {}).get("country") in ALLOWED_COUNTRIES
    )


def import_all(json_path: Path = Path(OUTPUT_FILE)) -> None:
    with open(json_path) as f:
        items: list[dict] = json.load(f).get("items", [])
    # filter out items not in germany, austria or switzerland
    items = [item for item in items if in_dach(item) and item.get("genre", {}).get("id") in {1, 8}]
    # and only those of category type id 1

    print(f"Filtered to {len(items)} items in Germany, Austria, Switzerland")

    if not items:
        log.warning("No items in %s", json_path)
        return
    # items = items[:1000]
    log.info("Loaded %d items", len(items))

    with Session(_get_engine()) as session:
        # ── Source ──────────────────────────────────────────────────────────
        source = session.exec(select(Source).where(Source.name == SOURCE_NAME)).first()
        if source is None:
            source = Source(
                name=SOURCE_NAME,
                base_url=SOURCE_BASE_URL,
                url_pattern=SOURCE_URL_PATTERN,
                description="Theater productions from theapolis.de",
            )
            session.add(source)
            session.flush()
        log.info("Source id=%s", source.id)

        # ── Prefetch existing entities into memory ───────────────────────────
        log.info("Prefetching existing entities…")

        existing_orgs: dict[str, uuid.UUID] = {  # name → id
            r.name: r.id for r in session.exec(select(Organization.name, Organization.id))
        }
        existing_persons: dict[str, uuid.UUID] = {  # name → id
            r.name: r.id for r in session.exec(select(Person.name, Person.id))
        }
        existing_texts: dict[str, uuid.UUID] = {  # title → id
            r.title: r.id for r in session.exec(select(Text.title, Text.id))
        }
        # productions: (norm_name, org_id) → id
        existing_prods: dict[tuple, uuid.UUID] = {
            (_norm(r.name), r.organization_id): r.id
            for r in session.exec(select(Production.name, Production.id, Production.organization_id))
        }
        existing_memberships: set[tuple] = {  # (prod_id, person_id, role)
            (r.production_id, r.person_id, r.role)
            for r in session.exec(
                select(ProductionMembership.production_id, ProductionMembership.person_id, ProductionMembership.role)
            )
        }
        existing_contributions: set[tuple] = {  # (text_id, person_id, role)
            (r.text_id, r.person_id, r.role)
            for r in session.exec(select(TextContribution.text_id, TextContribution.person_id, TextContribution.role))
        }
        existing_entity_sources: set[tuple] = {  # (source_id, entity_type, entity_id)
            (r.source_id, r.entity_type, r.entity_id)
            for r in session.exec(select(EntitySource.source_id, EntitySource.entity_type, EntitySource.entity_id))
        }

        log.info(
            "Prefetched: %d orgs, %d persons, %d texts, %d productions",
            len(existing_orgs),
            len(existing_persons),
            len(existing_texts),
            len(existing_prods),
        )

        # ── Accumulators ────────────────────────────────────────────────────
        new_orgs: list[dict] = []
        new_persons: list[dict] = []
        new_texts: list[dict] = []
        new_prods: list[dict] = []
        new_memberships: list[dict] = []
        new_contributions: list[dict] = []
        new_entity_sources: list[dict] = []

        def _es(
            entity_type: EntityType, entity_id: uuid.UUID, ext_id: Any, ext_url: str | None, title: str | None
        ) -> None:
            key = (source.id, entity_type, entity_id)
            if key not in existing_entity_sources:
                new_entity_sources.append(
                    {
                        "id": uuid.uuid7(),
                        "source_id": source.id,
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "external_id": str(ext_id) if ext_id else None,
                        "external_url": ext_url,
                        "title": title,
                    }
                )
                existing_entity_sources.add(key)

        # ── Collect unique orgs ──────────────────────────────────────────────
        orga_map = {org["id"]: org for item in items if (org := item.get("organization")) and org.get("id")}

        for ext_id, org in orga_map.items():
            name = _clean(org.get("name"))
            if not name or name in existing_orgs:
                if name:
                    _es(
                        EntityType.ORGANIZATION,
                        existing_orgs[name],
                        ext_id,
                        f"https://www.theapolis.de/de/organization/{org.get('slug', '')}",
                        name,
                    )
                continue
            loc = org.get("location") or {}
            geo = _geo(loc.get("geolocation"))
            oid = uuid.uuid7()
            new_orgs.append(
                {
                    "id": oid,
                    "name": name,
                    "city": loc.get("city"),
                    "country": loc.get("country"),
                    "lat": geo[0] if geo else None,
                    "lon": geo[1] if geo else None,
                }
            )
            existing_orgs[name] = oid
            _es(
                EntityType.ORGANIZATION,
                oid,
                ext_id,
                f"https://www.theapolis.de/de/organization/{org.get('slug', '')}",
                name,
            )

        # Flush orgs before productions reference them
        _bulk_insert(session, Organization, new_orgs)
        new_orgs.clear()
        _flush_entity_sources(session, new_entity_sources)
        new_entity_sources.clear()
        session.flush()
        log.info("Organizations done")

        # ── Productions + persons ────────────────────────────────────────────
        errors = 0
        for item in tqdm(items, desc="Productions", unit="prod"):
            try:
                _process_item(
                    item,
                    source,
                    existing_orgs,
                    existing_persons,
                    existing_texts,
                    existing_prods,
                    existing_memberships,
                    existing_contributions,
                    existing_entity_sources,
                    new_persons,
                    new_texts,
                    new_prods,
                    new_memberships,
                    new_contributions,
                    new_entity_sources,
                )
            except Exception:
                errors += 1
                log.exception("Failed: %r (id=%s)", item.get("name"), item.get("id"))

            # Flush in batches to keep memory bounded
            if len(new_prods) >= BATCH_SIZE:
                _flush_batch(
                    session, new_persons, new_texts, new_prods, new_memberships, new_contributions, new_entity_sources
                )

        # Final flush
        _flush_batch(session, new_persons, new_texts, new_prods, new_memberships, new_contributions, new_entity_sources)

        session.commit()

    log.info("Import complete. Errors: %d / %d", errors, len(items))


def _flush_batch(
    session: Session,
    new_persons,
    new_texts,
    new_prods,
    new_memberships,
    new_contributions,
    new_entity_sources,
) -> None:
    # Order matters for FK constraints
    _bulk_insert(session, Person, new_persons)
    new_persons.clear()
    _bulk_insert(session, Text, new_texts)
    new_texts.clear()
    _bulk_insert(session, Production, new_prods)
    new_prods.clear()
    _bulk_insert(session, ProductionMembership, new_memberships)
    new_memberships.clear()
    _bulk_insert(session, TextContribution, new_contributions)
    new_contributions.clear()
    _flush_entity_sources(session, new_entity_sources)
    new_entity_sources.clear()
    session.flush()


# ---------------------------------------------------------------------------
# Per-item logic (pure accumulation, no DB calls)
# ---------------------------------------------------------------------------


def _process_item(
    item: dict,
    source: Source,
    existing_orgs: dict[str, uuid.UUID],
    existing_persons: dict[str, uuid.UUID],
    existing_texts: dict[str, uuid.UUID],
    existing_prods: dict[tuple, uuid.UUID],
    existing_memberships: set[tuple],
    existing_contributions: set[tuple],
    existing_entity_sources: set[tuple],
    new_persons: list[dict],
    new_texts: list[dict],
    new_prods: list[dict],
    new_memberships: list[dict],
    new_contributions: list[dict],
    new_entity_sources: list[dict],
) -> None:
    ext_id = item.get("id")
    name = _clean(item.get("name"))
    premiere_date = item.get("premiereDate")  # as datetime or string? if string, try to parse year from it
    if not name:
        return

    slug = item.get("slug", "")
    prod_url = f"https://www2.theapolis.de/de/produktionen/{ext_id}"
    creators = item.get("creators") or []
    directors = item.get("directors") or []

    org_ext_id = (item.get("organization") or {}).get("id")
    org_id = existing_orgs.get((item.get("organization") or {}).get("name", ""))

    def _es(entity_type: EntityType, entity_id: uuid.UUID, eid: Any, eurl: str | None, title: str | None) -> None:
        key = (source.id, entity_type, entity_id)
        if key not in existing_entity_sources:
            new_entity_sources.append(
                {
                    "id": uuid.uuid7(),
                    "source_id": source.id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "external_id": str(eid) if eid else None,
                    "external_url": eurl,
                    "title": title,
                }
            )
            existing_entity_sources.add(key)

    def _get_or_new_person(p_name: str, p_id: Any, p_type: list[str]) -> uuid.UUID:
        if p_name in existing_persons:
            return existing_persons[p_name]
        pid = uuid.uuid7()
        new_persons.append({"id": pid, "name": p_name, "type": p_type})
        existing_persons[p_name] = pid
        _es(EntityType.PERSON, pid, p_id, None, p_name)
        return pid

    # ── Text ────────────────────────────────────────────────────────────────
    text_id: uuid.UUID | None = None
    if creators:
        text_id = existing_texts.get(name)
        if text_id is None:
            text_id = uuid.uuid7()
            new_texts.append({"id": text_id, "title": name})
            existing_texts[name] = text_id
            _es(EntityType.TEXT, text_id, ext_id, prod_url, name)

    # ── Production ──────────────────────────────────────────────────────────
    prod_key = (_norm(name), org_id)
    prod_id = existing_prods.get(prod_key)

    if prod_id is None:
        prod_id = uuid.uuid7()
        new_prods.append(
            {
                "id": prod_id,
                "name": name,
                "premiere": None,
                "organization_id": org_id,
                "text_id": text_id,
                "url": prod_url,
            }
        )
        existing_prods[prod_key] = prod_id
    _es(EntityType.PRODUCTION, prod_id, ext_id, prod_url, name)

    # ── Directors ───────────────────────────────────────────────────────────
    for d in directors:
        d_name = _clean(d.get("name"))
        if not d_name:
            continue
        person_id = _get_or_new_person(d_name, d.get("id"), ["director"])
        mkey = (prod_id, person_id, "director")
        if mkey not in existing_memberships:
            new_memberships.append(
                {
                    "id": uuid.uuid7(),
                    "production_id": prod_id,
                    "person_id": person_id,
                    "role": "director",
                }
            )
            existing_memberships.add(mkey)

    # ── Creators ────────────────────────────────────────────────────────────
    for c in creators:
        c_name = _clean(c.get("name"))
        if not c_name:
            continue
        person_id = _get_or_new_person(c_name, c.get("id"), ["author"])
        mkey = (prod_id, person_id, "creator")
        if mkey not in existing_memberships:
            new_memberships.append(
                {
                    "id": uuid.uuid7(),
                    "production_id": prod_id,
                    "person_id": person_id,
                    "role": "creator",
                }
            )
            existing_memberships.add(mkey)
        if text_id:
            ckey = (text_id, person_id, "author")
            if ckey not in existing_contributions:
                new_contributions.append(
                    {
                        "id": uuid.uuid7(),
                        "text_id": text_id,
                        "person_id": person_id,
                        "role": "author",
                    }
                )
                existing_contributions.add(ckey)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SQLModel.metadata.create_all(_get_engine())
    import_all(Path("/Users/simon/DEV/theaterdb/productions_new.json"))
