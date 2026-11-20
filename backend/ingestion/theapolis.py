"""Separate Theapolis API export, sitemap image download and accepted source import."""

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from functools import lru_cache
from itertools import batched, chain
from pathlib import Path
from time import monotonic, time
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp
from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, col, select
from tqdm import tqdm

from backend.config import IMAGE_SIGNATURES, canonical_role
from backend.date_precision import parse_date
from backend.ingestion.snapshots import digest, snapshot_path, without_null_fields
from backend.ingestion.staging import validate_records
from backend.ingestion.theapolis_archive import Archive, open_archive
from backend.ingestion.theapolis_import import (
    ImportRecord,
    accept_records,
    add_credits_batch,
    merge_scoped_people,
    migrate_pending_cases,
    normalize_character,
)
from backend.locations import split_country
from backend.models import EntityType, Source, SourceRecord, TheapolisImportCheckpoint, TheapolisProfileCache
from backend.production_import import author_name
from backend.reconciliation import canonical_url, merge_entities, preview_entity_merge, validate_payload
from backend.user_management import get_session, init_db

SITE = "https://www.theapolis.de"
DEFAULT_EXPORT_DIR = Path("data/staged/theapolis.de")
ARCHIVE_NAME = "theapolis.sqlite3"
DEFAULT_IMAGES = Path("data/theapolis/images")
ACTOR = "theapolis-ingestion"
LOG = logging.getLogger(__name__)
BATCH_SIZE = 500
PROFILE_CACHE_AGE = 30 * 86400
PROFILE_DIRECTORY_THRESHOLD = 1000
PARTICIPATION_CACHE_AGE = 7 * 86400
DETAIL_CONCURRENCY = 32
DACH_COUNTRIES = frozenset(country for code in ("DE", "AT", "CH") if (country := split_country(code)[0]))
type PreparedCredit = tuple[str, str] | tuple[str, str, str | None]
type PreparedProduction = tuple[str, str | None, dict, list[PreparedCredit]]


async def _parallel[T, R](
    values: Iterable[T],
    action: Callable[[T], Awaitable[R]],
    concurrency: int,
    *,
    progress: tqdm | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> list[R]:
    if concurrency < 1:
        raise ValueError("Concurrency must be positive")
    iterator = enumerate(values)
    results: dict[int, R] = {}

    async def worker() -> None:
        for index, value in iterator:
            results[index] = await action(value)
            if progress is not None:
                progress.update()
            if checkpoint is not None and len(results) % concurrency == 0:
                checkpoint()
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if checkpoint is not None:
            checkpoint()
    return [results[index] for index in sorted(results)]


async def _response(session: aiohttp.ClientSession, url: str, attempt: int) -> bytes | float:
    async with session.get(url) as response:
        if response.status != 429 and response.status < 500:
            response.raise_for_status()
            return await response.read()
        if attempt == 4:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                return max(0, (parsedate_to_datetime(retry_after) - dt.datetime.now(dt.UTC)).total_seconds())
        return 1.5 * 2**attempt


async def _fetch(session: aiohttp.ClientSession, url: str) -> bytes:
    for attempt in range(5):
        delay = 1.5 * 2**attempt
        try:
            result = await _response(session, url, attempt)
        except TimeoutError, aiohttp.ClientConnectionError:
            if attempt == 4:
                raise
        else:
            if isinstance(result, bytes):
                return result
            delay = result
        LOG.warning("Retrying %s in %.1fs (attempt %s/5)", url, delay, attempt + 1)
        await asyncio.sleep(delay)
    raise RuntimeError(f"Failed to fetch {url}")


def _session(concurrency: int) -> aiohttp.ClientSession:
    if concurrency < 1:
        raise ValueError("Concurrency must be positive")
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=concurrency),
        timeout=aiohttp.ClientTimeout(total=60, connect=15),
        headers={"User-Agent": "TheatreDB-Theapolis/1.0", "Accept": "*/*"},
    )


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(without_null_fields(data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _premiere_year(year: object) -> int | None:
    if year is not None:
        if isinstance(year, bool) or not re.fullmatch(r"\d{4}", str(year)) or not 1 <= int(str(year)) <= 9999:
            raise ValueError(f"Invalid premiere year: {year!r}")
        return int(str(year))
    return None


def premiere_fields(item: dict) -> dict:
    """Convert partial API dates without inventing their precision."""
    value = item.get("premierDate") or item.get("premiere")
    year = _premiere_year(
        next(
            (item.get(key) for key in ("premierYear", "year", "premiere_year") if item.get(key) not in (None, "")), None
        )
    )
    result = None
    if value not in (None, ""):
        text = str(value).strip()
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
            text = dt.datetime.strptime(text, "%d.%m.%Y").date().isoformat()
        result = parse_date(text)
        if year is not None and year != result[0].year:
            raise ValueError(f"Conflicting premiere date/year for production {item.get('id')}")
    elif year is not None:
        result = parse_date(f"{year:04d}")
    return {
        "premiere": result[0].isoformat() if result else None,
        "premiere_precision": result[1] if result else None,
    }


def _production_tags(item: dict) -> list[str]:
    tags = []
    genre = item.get("genre") or {}
    if isinstance(genre, dict) and isinstance(genre.get("name"), str) and genre["name"].strip():
        tags.append(genre["name"].strip())
    elif isinstance(genre, str) and genre.strip():
        tags.append(genre.strip())
    production_type = item.get("type") or {}
    if isinstance(production_type, dict):
        label = production_type.get("pill") or production_type.get("name")
        if isinstance(label, str) and label.strip():
            tags.append(label.strip())
    for tag in item.get("tags") or []:
        value = tag.get("name") if isinstance(tag, dict) else tag
        if isinstance(value, str) and value.strip():
            tags.append(value.strip())
    if item.get("independent") is True:
        tags.append("Freie Darstellende Künste")
    return list(dict.fromkeys(tags))


@lru_cache(maxsize=4096)
def _description_text(value: str) -> str:
    return markdownify(value).strip()


def _description(item: dict) -> dict[str, str]:
    value = item.get("description")
    if value is None:
        return {}
    if not isinstance(value, str):
        raise TypeError(f"Invalid Theapolis description for {item.get('id')!r}")
    text = _description_text(value)
    return {"description": text} if text else {}


def _profile_birthday(profile: dict) -> dict:
    value = profile.get("birthDate")
    if not value:
        return {}
    text = str(value).strip()
    if re.fullmatch(r"_+", text):
        return {}
    try:
        result = parse_date(text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else text)
    except ValueError:
        LOG.warning(
            "Ignoring invalid Theapolis profile birthday %r for %s", value, profile.get("url") or profile.get("slug")
        )
        return {}
    return {"born": result[0].isoformat(), "born_precision": result[1]}


def staged_records(items: list[dict]) -> list[dict]:
    """Use the same validated production-array contract as venue scrapers."""
    records = []
    for item in items:
        members = []
        for group, role in (("directors", "director"), ("creators", "author")):
            for person in item.get(group) or []:
                member: dict[str, object] = {
                    "name": _name(person),
                    "external_id": _identity(person),
                    "type": role,
                    "role": role,
                }
                if profile := person.get("profile"):
                    member.update(_profile_birthday(profile))
                    if profile.get("url"):
                        member["url"] = profile["url"]
                member.update(_description(person.get("profile") or {}) or _description(person))
                members.append(member)
        fields = premiere_fields(item)
        tags = _production_tags(item)
        records.append(
            {
                "title": _name(item),
                "url": f"https://www2.theapolis.de/de/produktionen/{quote(_identity(item), safe='')}",
                **fields,
                "tags": tags,
                **_description(item),
                "memberships": members,
            }
        )
    return validate_records(records)


async def _profile_for_name(session: aiohttp.ClientSession, name: str) -> dict | None:
    query = urlencode({"search": name, "items": 100})
    result = json.loads(await _fetch(session, f"{SITE}/api/de/profiles?{query}"))
    if result.get("success") is not True:
        raise ValueError(f"Theapolis rejected profile search for {name!r}")
    matches = [
        profile
        for profile in result.get("data", {}).get("items", [])
        if isinstance(profile.get("name"), str) and profile["name"].strip() == name
    ]
    if len(matches) != 1:
        return None
    return await _profile_for_slug(session, matches[0].get("slug"))


async def _profile_for_slug(session: aiohttp.ClientSession, slug: object) -> dict | None:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
        raise ValueError(f"Invalid Theapolis profile slug: {slug!r}")
    try:
        details = json.loads(await _fetch(session, f"{SITE}/de/people/api/show/{quote(slug, safe='')}"))
    except aiohttp.ClientResponseError as error:
        if error.status != 404:
            raise
        return None
    if details.get("error") is not False or details.get("code") != 200:
        raise ValueError(f"Theapolis rejected profile {slug!r}")
    profile = details.get("data", {}).get("profile")
    if not isinstance(profile, dict) or profile.get("slug") != slug:
        raise ValueError(f"Theapolis profile identity changed for {slug!r}")
    personal_data = profile.get("personalData") or {}
    result = {
        "slug": slug,
        "url": f"{SITE}/de/profil/{quote(slug, safe='')}",
        "birthDate": personal_data.get("birthDate") or personal_data.get("birthData"),
    }
    biography = "\n\n".join(
        dict.fromkeys(value for key in ("biographyText", "extraBiographyText") if (value := profile.get(key)))
    )
    result["description"] = biography or None
    return result


class _ProfileCache:
    def __init__(self, entries: dict[str, TheapolisProfileCache]) -> None:
        self.entries = entries
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _load() -> dict[str, TheapolisProfileCache]:
        with get_session() as session:
            SQLModel.metadata.tables["theapolisprofilecache"].create(session.get_bind(), checkfirst=True)
        return _read_profile_cache()

    @classmethod
    async def load(cls) -> _ProfileCache:
        return cls(await asyncio.to_thread(cls._load))

    def get(self, name: str) -> tuple[bool, dict | None]:
        entry = self.entries.get(name)
        current = entry and entry.captured_at >= time() - PROFILE_CACHE_AGE
        # Older cache entries have no biography field and need one refresh.
        complete = entry and (entry.profile is None or "description" in entry.profile)
        return (True, entry.profile) if current and complete else (False, None)

    @staticmethod
    def _put(entry: TheapolisProfileCache) -> None:
        table = SQLModel.metadata.tables["theapolisprofilecache"]
        statement = insert(table).values(**entry.model_dump())
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.name],
            set_={"profile": statement.excluded.profile, "captured_at": statement.excluded.captured_at},
            where=table.c.captured_at <= statement.excluded.captured_at,
        )
        with get_session() as session:
            session.exec(statement)
            session.commit()

    async def put(self, name: str, profile: dict | None) -> None:
        entry = TheapolisProfileCache(
            name=name,
            profile={**profile, "description": profile.get("description")} if profile else None,
            captured_at=time(),
        )
        async with self._write_lock:
            task = asyncio.create_task(asyncio.to_thread(self._put, entry))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            self.entries[name] = entry


async def _enrich_people(
    session: aiohttp.ClientSession,
    items: list[dict],
    concurrency: int,
    *,
    checkpoint: Callable[[], None] | None = None,
    cache: _ProfileCache | None = None,
) -> None:
    """Attach public profile details by their unique URL slug, not numeric person IDs."""
    people: dict[tuple[str, str], str] = {}
    by_name: dict[str, list[dict]] = {}
    for item in items:
        for group in ("directors", "creators"):
            for person in item.get(group) or []:
                name = _name(person)
                people[group, _identity(person)] = name
                by_name.setdefault(name, []).append(person)
    names = list(by_name)
    found = set()

    async def enrich(name: str) -> None:
        cached, profile = cache.get(name) if cache is not None else (False, None)
        if not cached:
            profile = await _profile_for_name(session, name)
        if profile:
            found.add(name)
            for person in by_name[name]:
                person["profile"] = profile
        if not cached and cache is not None:
            await cache.put(name, profile)

    with tqdm(total=len(names), desc="People profiles", unit="profile") as progress:
        await _parallel(
            names,
            enrich,
            concurrency,
            progress=progress,
            checkpoint=checkpoint,
        )
    missing = sum(name not in found for name in people.values())
    LOG.info(
        "Theapolis profiles: %s/%s people missing (%.1f%%); all production credits retained",
        missing,
        len(people),
        100 * missing / len(people) if people else 0,
    )


async def _enrich_organizations(
    session: aiohttp.ClientSession,
    items: list[dict],
    concurrency: int,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    organizations: dict[str, list[dict]] = {}
    for item in items:
        if organization := item.get("organization"):
            organizations.setdefault(_identity(organization), []).append(organization)
    if not organizations:
        return
    found = set()

    async def page(number: int) -> dict:
        query = urlencode({"page": number, "items": 500})
        result = json.loads(await _fetch(session, f"{SITE}/api/de/organizations?{query}"))
        if result.get("success") is not True:
            raise ValueError(f"Theapolis rejected organization page {number}")
        data = result["data"]
        for organization in data["items"]:
            identity = _identity(organization)
            for target in organizations.get(identity, ()):
                if organization.get("description"):
                    target["description"] = organization["description"]
                found.add(identity)
        return data

    first = await page(1)
    pages = math.ceil(first["totalCount"] / first["numItemsPerPage"])
    with tqdm(total=pages, initial=1, desc="Organization pages", unit="page") as progress:
        await _parallel(range(2, pages + 1), page, concurrency, progress=progress, checkpoint=checkpoint)
    if missing := organizations.keys() - found:
        LOG.warning("%s Theapolis organizations missing from directory; production summaries retained", len(missing))


def _source_path(path: Path) -> Path:
    return path.with_suffix(".source.json")


def _export_scope(max_pages: int | None) -> str:
    config = {"url": f"{SITE}/api/de/productions", "items": 500, "status": [2, 4]}
    return f"{'all' if max_pages is None else f'limit-{max_pages * 500}'}-people-{digest(config)[:12]}"


def _periodic_checkpoint(checkpoint: Callable[[], None]) -> Callable[[], None]:
    last_checkpoint = monotonic()

    def save() -> None:
        nonlocal last_checkpoint
        if monotonic() - last_checkpoint >= 60:
            checkpoint()
            last_checkpoint = monotonic()

    return save


async def download(output: Path | None = None, *, concurrency: int = 10, max_pages: int | None = None) -> Path:
    """Checkpoint staged JSON and raw source metadata without database writes."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    if output is None:
        output = snapshot_path(DEFAULT_EXPORT_DIR, _export_scope(max_pages))
    items: list[dict] = []
    page_items: dict[int, list[dict]] = {}
    metadata = {
        "success": False,
        "format_version": 2,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "partial": True,
        "items": items,
    }

    def checkpoint() -> None:
        _write_json(_source_path(output), metadata)
        _write_json(output, staged_records(items) if items else [])

    def checkpoint_pages() -> None:
        items[:] = [item for number in sorted(page_items) for item in page_items[number]]
        checkpoint()

    async with _session(concurrency) as session:
        if not output.exists():
            checkpoint()

        async def page(number: int) -> dict:
            query = urlencode({"page": number, "items": 500, "status[0]": 2, "status[1]": 4, "search": ""})
            result = json.loads(await _fetch(session, f"{SITE}/api/de/productions?{query}"))
            if result.get("success") is not True:
                raise ValueError(f"Theapolis rejected production page {number}")
            page_items[number] = result["data"]["items"]
            return result["data"]

        first = await page(1)
        checkpoint_pages()
        total_pages = math.ceil(first["totalCount"] / first["numItemsPerPage"])
        pages = min(total_pages, max_pages or total_pages)
        with tqdm(total=pages, initial=1, desc="Production pages", unit="page") as progress:
            await _parallel(range(2, pages + 1), page, concurrency, progress=progress, checkpoint=checkpoint_pages)
        if not items:
            raise ValueError("Theapolis returned no productions")
        try:
            await _enrich_organizations(session, items, concurrency, checkpoint=_periodic_checkpoint(checkpoint))
            await _enrich_people(
                session,
                items,
                concurrency,
                checkpoint=_periodic_checkpoint(checkpoint),
                cache=await _ProfileCache.load(),
            )
        finally:
            checkpoint()
    metadata.update(success=True, partial=pages < total_pages)
    _write_json(_source_path(output), metadata)
    LOG.info("Exported %s productions to %s", len(items), output)
    return output


def _read_profile_cache() -> dict[str, TheapolisProfileCache]:
    with get_session() as session:
        return {
            entry.name: entry
            for entry in session.exec(
                select(TheapolisProfileCache).where(TheapolisProfileCache.captured_at >= time() - PROFILE_CACHE_AGE)
            ).all()
        }


async def _profile_directory(session: aiohttp.ClientSession, names: set[str], concurrency: int) -> dict[str, set[str]]:
    matches: dict[str, set[str]] = {}
    seen: set[str] = set()

    async def page(number: int) -> tuple[int, int]:
        query = urlencode({"page": number, "items": 500})
        result = json.loads(await _fetch(session, f"{SITE}/api/de/profiles?{query}"))
        if result.get("success") is not True:
            raise ValueError(f"Theapolis rejected profile directory page {number}")
        data = result["data"]
        if data["currentPageNumber"] != number:
            raise ValueError(f"Theapolis returned the wrong profile directory page for {number}")
        for profile in data["items"]:
            slug = profile["slug"]
            if not isinstance(slug, str) or slug in seen:
                raise ValueError(f"Theapolis profile directory has an invalid or repeated slug on page {number}")
            seen.add(slug)
            if isinstance(name := profile["name"], str) and (name := name.strip()) in names:
                matches.setdefault(name, set()).add(slug)
        return data["totalCount"], data["numItemsPerPage"]

    total, page_size = await page(1)
    if page_size < 1:
        raise ValueError("Theapolis returned an invalid profile directory page size")
    pages = math.ceil(total / page_size)
    with tqdm(total=pages, initial=1, desc="Profile directory", unit="page") as progress:
        results = await _parallel(range(2, pages + 1), page, concurrency, progress=progress)
    if len(seen) != total or any(result != (total, page_size) for result in results):
        raise ValueError("Theapolis profile directory changed during download; retry to resume")
    return matches


async def _archive_profiles(
    archive: Archive, session: aiohttp.ClientSession, names: set[str], concurrency: int, *, reuse_profile_cache: bool
) -> dict:
    now = time()
    profiles = archive.cached("profile", now - PROFILE_CACHE_AGE)
    negatives = archive.cached("no_profile", now - PROFILE_CACHE_AGE)
    remaining = names.difference(profiles, negatives)
    if reuse_profile_cache and remaining:
        legacy = await asyncio.to_thread(_read_profile_cache)
        reusable = remaining.intersection(legacy)
        with archive.db:
            for name in reusable:
                entry = legacy[name]
                if entry.profile is not None and "description" not in entry.profile:
                    continue
                if entry.profile is not None:
                    profiles[name] = entry.profile
                archive.cache_profile(name, entry.profile, entry.captured_at)
                remaining.remove(name)
        LOG.info("Reused %s existing profile lookups", len(reusable - remaining))

    directory = (
        await _profile_directory(session, remaining, concurrency)
        if len(remaining) >= PROFILE_DIRECTORY_THRESHOLD
        else None
    )
    if directory is not None:
        with archive.db:
            for name in remaining:
                if len(directory.get(name, ())) != 1:
                    archive.cache_profile(name, None, now)
        remaining.intersection_update(name for name, slugs in directory.items() if len(slugs) == 1)

    async def lookup(name: str) -> None:
        profile = (
            await _profile_for_name(session, name)
            if directory is None
            else await _profile_for_slug(session, next(iter(directory[name])))
        )
        if profile:
            profiles[name] = profile
        archive.cache_profile(name, profile, now)

    with tqdm(total=len(remaining), desc="New or expired profile lookups", unit="profile") as progress:
        await _parallel(sorted(remaining), lookup, concurrency, progress=progress, checkpoint=archive.db.commit)
    return profiles


async def _archive_enrichment(
    archive: Archive,
    session: aiohttp.ClientSession,
    run: int,
    pages: int,
    concurrency: int,
    *,
    reuse_profile_cache: bool,
) -> None:
    organizations: dict[str, dict] = {}
    names: set[str] = set()
    for number in range(1, pages + 1):
        for item in archive.raw_items(run, number):
            if org := item.get("organization"):
                organizations.setdefault(_identity(org), {"id": org["id"]})
            for group in ("directors", "creators"):
                names.update(_name(person) for person in item.get(group) or [])
    await _enrich_organizations(session, [{"organization": org} for org in organizations.values()], concurrency)
    profiles = await _archive_profiles(archive, session, names, concurrency, reuse_profile_cache=reuse_profile_cache)
    for number in range(1, pages + 1):
        items = list(archive.raw_items(run, number))
        for item in items:
            if (org := item.get("organization")) and (description := organizations[_identity(org)].get("description")):
                item["organization"] = {**org, "description": description}
            for group in ("directors", "creators"):
                if isinstance(item.get(group), list):
                    item[group] = [
                        {**person, "profile": profile} if (profile := profiles.get(_name(person))) else person
                        for person in item[group]
                    ]
        archive.put_ready(run, items)


async def download_archive(
    path: Path,
    *,
    concurrency: int = 10,
    max_pages: int | None = None,
    restart: bool = False,
    reuse_profile_cache: bool = False,
    detail_concurrency: int = DETAIL_CONCURRENCY,
) -> int:
    """Capture production pages and enrich them without writing to Postgres."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    if detail_concurrency < 1:
        raise ValueError("Detail concurrency must be positive")
    scope = _export_scope(max_pages)
    with open_archive(path, write=True) as archive:
        if restart:
            archive.abandon(scope)
        async with _session(concurrency) as session:

            async def fetch_page(number: int) -> dict:
                query = urlencode({"page": number, "items": 500, "status[0]": 2, "status[1]": 4, "search": ""})
                result = json.loads(await _fetch(session, f"{SITE}/api/de/productions?{query}"))
                if result.get("success") is not True:
                    raise ValueError(f"Theapolis rejected production page {number}")
                return result["data"]

            if pending := archive.pending(scope):
                run, pages, total = pending
            else:
                first = await fetch_page(1)
                total = first["totalCount"]
                expected = math.ceil(total / first["numItemsPerPage"])
                pages = min(expected, max_pages or expected)
                run = archive.start(scope, pages, total, partial=max_pages is not None)
                archive.write_page(run, 1, first)

            async def page(number: int) -> None:
                data = await fetch_page(number)
                if data["totalCount"] != total:
                    raise ValueError("Theapolis production count drifted; restart the list capture")
                archive.write_page(run, number, data)

            completed = archive.completed_pages(run)
            with tqdm(total=pages, initial=len(completed), desc="Production pages", unit="page") as progress:
                await _parallel(
                    (number for number in range(1, pages + 1) if number not in completed),
                    page,
                    concurrency,
                    progress=progress,
                )
            await _archive_enrichment(
                archive, session, run, pages, concurrency, reuse_profile_cache=reuse_profile_cache
            )
            async with _session(detail_concurrency) as detail_session:
                await _download_participations(archive, detail_session, run, detail_concurrency)
            archive.seal(run, carry_details=True)
    return run


def seed_archive(path: Path, snapshot_path: Path) -> int:
    """Bootstrap an archive from an existing complete legacy snapshot."""
    # Legacy CLI also accepts the existing source sidecar directly, avoiding a second JSON parse.
    snapshot = _read_import_snapshot(snapshot_path)
    rows = snapshot.get("items", snapshot.get("data", {}).get("items"))
    if snapshot.get("success") is False or snapshot.get("partial") or not rows:
        raise ValueError("Seeding requires a complete Theapolis snapshot")
    with open_archive(path, write=True) as archive:
        if archive.latest() is not None:
            raise ValueError("Archive already has a full snapshot; run download or details instead")
        pages = math.ceil(len(rows) / BATCH_SIZE)
        run = archive.start("legacy-seed", pages, len(rows), partial=False, captured_at=snapshot.get("captured_at"))
        for number, items in enumerate(batched(rows, BATCH_SIZE, strict=False), 1):
            archive.write_page(run, number, {"items": list(items)}, ready=True)
        archive.seal(run)
    return run


def archive_details(path: Path, feed: Path) -> int:
    """Append authorized cast and crew observations from a JSON Lines feed."""

    def entries() -> Generator[dict]:
        with feed.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    validate_detail(entry)
                except (ValueError, TypeError) as error:
                    raise ValueError(f"{feed}:{number}: {error}") from error
                yield entry

    with open_archive(path, write=True) as archive:
        return archive.add_details(entries())


def _participation_credit(row: dict) -> dict:
    profile = row.get("profile") or {}
    person_id = (
        f"profile:{_detail_id(profile['id'])}"
        if profile.get("id") is not None
        else f"participation:{_detail_id(row['id'])}"
    )
    occupation = row["occupation"]
    role = occupation["title"]
    if not isinstance(role, str) or not role.strip():
        raise ValueError(f"Missing occupation for Theapolis participation {row['id']}")
    if row.get("assistant"):
        role = f"Assistenz {role}"
    if row.get("management"):
        role = f"Leitung {role}"
    return {
        "person_id": person_id,
        "name": _name(row),
        "role": role,
        "character": row.get("roleName") or None,
        **({"profile_url": canonical_url(profile["url"])} if profile.get("url") else {}),
    }


async def _participations(session: aiohttp.ClientSession, identity: str) -> dict:
    pages: list[dict] = []
    seen: set[str] = set()
    credits = []
    number = 1
    total = None
    while True:
        query = urlencode({"page": number, "items": 1000})
        result = json.loads(
            await _fetch(session, f"{SITE}/api/de/productions/{quote(identity, safe='')}/participations?{query}")
        )
        if result.get("success") is not True:
            raise ValueError(f"Theapolis rejected participations for production {identity}, page {number}")
        data = result["data"]
        if total is None:
            total = data["totalCount"]
        if data["currentPageNumber"] != number or data["totalCount"] != total or data["numItemsPerPage"] < 1:
            raise ValueError(f"Inconsistent Theapolis participation pagination for {identity}")
        for row in data["items"]:
            key = _detail_id(row["id"])
            if key in seen:
                raise ValueError(f"Repeated Theapolis participation {key} for {identity}")
            seen.add(key)
            credits.append(_participation_credit(row))
        pages.append(result)
        if number * data["numItemsPerPage"] >= total:
            break
        number += 1
    if len(seen) != total:
        raise ValueError(f"Incomplete Theapolis participation coverage for {identity}: {len(seen)}/{total}")
    entry = {
        "production_id": identity,
        "theapolis_detail_status": "parsed" if credits else "confirmed_empty",
        "theapolis_detail": {"credits": credits, "api_pages": pages},
    }
    validate_detail(entry)
    return entry


async def _download_participations(
    archive: Archive, session: aiohttp.ClientSession, run: int, concurrency: int
) -> None:
    archive.reuse_participations(run)
    targets = list(archive.detail_targets(run, time() - PARTICIPATION_CACHE_AGE))
    LOG.info("Theapolis participations: %s productions need first capture or refresh", len(targets))
    pending: list[tuple[str, dict, float]] = []

    def checkpoint() -> None:
        if pending:
            archive.put_participations(run, pending)
            pending.clear()

    async def fetch(identity: str) -> None:
        entry = await _participations(session, identity)
        pending.append((identity, entry, time()))

    with tqdm(total=len(targets), desc="Cast and crew API", unit="production") as progress:
        await _parallel(targets, fetch, concurrency, progress=progress, checkpoint=checkpoint)


async def download_credits(path: Path, *, concurrency: int = DETAIL_CONCURRENCY) -> int:
    """Download all missing or expired cast and crew via the public JSON API."""
    with open_archive(path, write=True) as archive:
        run = archive.begin_details()
        async with _session(concurrency) as session:
            await _download_participations(archive, session, run, concurrency)
        archive.seal(run)
    return run


def _sitemap_locations(content: bytes) -> list[str]:
    return [node.text().strip() for node in HTMLParser(content).css("loc")]


def _site_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "www.theapolis.de" or parts.username or parts.password:
        raise ValueError(f"Unexpected Theapolis sitemap URL: {url}")
    return url


def _image_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"www.theapolis.de", "media.theapolis.de"}
        or parts.username
        or parts.password
    ):
        raise ValueError(f"Unexpected Theapolis image URL: {url}")
    return url


def profile_image(content: bytes, page_url: str) -> dict:
    """Extract the profile/logo image, retaining its public source."""
    tree = HTMLParser(content)
    node = tree.css_first('meta[property="og:image"]')
    image = node.attributes.get("content") if node else None
    if not image or "/bundles/" in image or "/-1/" in image:
        LOG.warning("No profile image/logo on %s", page_url)
        return {"url": None, "status": "no-image"}
    return {"url": _image_url(urljoin(page_url, image))}


async def _profile_pages(session: aiohttp.ClientSession, concurrency: int, limit: int | None) -> list[str]:
    maps = _sitemap_locations(await _fetch(session, f"{SITE}/sitemap.xml"))
    maps = [
        _site_url(url)
        for url in maps
        if re.fullmatch(r"/sitemaps/sitemap_(?:people|organization)_\d+\.xml", urlsplit(url).path)
    ]
    if not maps:
        raise ValueError("No people/organization sitemaps found")
    # A bounded smoke test reads only the first sitemap of each kind.
    if limit:
        maps = list({("people" if "people_" in url else "organization"): url for url in reversed(maps)}.values())

    async def locations(url: str) -> list[str]:
        return _sitemap_locations(await _fetch(session, url))

    with tqdm(total=len(maps), desc="Sitemaps", unit="sitemap") as progress:
        sitemap_groups = await _parallel(maps, locations, concurrency, progress=progress)
    pages = list(
        dict.fromkeys(
            _site_url(url)
            for group in sitemap_groups
            for url in group
            if re.fullmatch(r"/de/(?:profil|organization)/[^/]+", urlsplit(url).path)
        )
    )
    if limit:
        pages = pages[:limit]
    if not pages:
        raise ValueError("No public profiles found")
    return pages


async def _download_image(session: aiohttp.ClientSession, page_url: str, output: Path) -> dict:
    path = urlsplit(page_url).path
    kind = "people" if "/profil/" in path else "organizations"
    slug = path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
        raise ValueError(f"Unsafe profile slug: {slug!r}")
    metadata = profile_image(await _fetch(session, page_url), page_url)
    record = {"id": slug, "kind": kind, "page_url": page_url, **metadata}
    if not metadata["url"]:
        return record
    body = await _fetch(session, metadata["url"])
    suffix = next((ext for signature, ext in IMAGE_SIGNATURES.items() if body.startswith(signature)), None)
    if suffix is None:
        raise ValueError(f"Not a supported image: {metadata['url']}")
    target = output / kind / f"{slug}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(body)
    temporary.replace(target)
    return {**record, "file": str(target.relative_to(output)), "status": "downloaded"}


async def download_images(output: Path = DEFAULT_IMAGES, *, concurrency: int = 10, limit: int | None = None) -> None:
    """Download every public sitemap profile photo/logo, named by profile slug."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    async with _session(concurrency) as session:
        pages = await _profile_pages(session, concurrency, limit)
        # Persist each batch so interrupted downloads retain their source mapping.
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

        async def image(page_url: str) -> dict:
            try:
                return await _download_image(session, page_url, output)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                LOG.exception("Image download failed: %s", page_url)
                path = urlsplit(page_url).path
                return {
                    "id": path.rsplit("/", 1)[-1],
                    "kind": "people" if "/profil/" in path else "organizations",
                    "page_url": page_url,
                    "status": "failed",
                    "error": str(error),
                }

        failures = 0
        with tqdm(total=len(pages), desc="Profile images", unit="profile") as progress:
            for start in range(0, len(pages), concurrency):
                for result in await _parallel(
                    pages[start : start + concurrency], image, concurrency, progress=progress
                ):
                    failures += result["status"] == "failed"
                    manifest[f"{result['kind']}/{result['id']}"] = result
                _write_json(manifest_path, manifest)
        if failures:
            raise RuntimeError(f"{failures} image downloads failed; see {manifest_path}")
    LOG.info("Image manifest: %s", output / "manifest.json")


def _identity(item: dict) -> str:
    value = item.get("id")
    if value is None or isinstance(value, bool) or not str(value).strip():
        raise ValueError(f"Missing Theapolis ID: {item.get('name')!r}")
    return str(value)


def _name(item: dict) -> str:
    value = item.get("name")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Theapolis record needs a name")
    return value.strip()


def _add_people(item: dict, people: dict) -> list[tuple[str, str]]:
    credits = []
    for group, role in (("directors", "director"), ("creators", "author")):
        for person in item.get(group) or []:
            person_name = author_name(_name(person)) if role == "author" else _name(person)
            profile = person.get("profile") or {}
            person_id = f"{group}:{_identity(person)}"
            payload = people.setdefault(person_id, {"name": person_name, "type": []})
            if payload["name"] != person_name:
                raise ValueError(f"Inconsistent name for person ID {person_id}")
            if profile.get("url"):
                payload["profile_url"] = profile["url"]
            payload.update(_description(profile) or _description(person))
            payload.update(_profile_birthday(profile))
            if role not in payload["type"]:
                payload["type"].append(role)
            credits.append((person_id, role))
    return credits


def _detail_id(value: object) -> str:
    if type(value) not in (str, int) or not str(value).strip() or "\x00" in str(value):
        raise ValueError("Invalid Theapolis detail person or legacy ID")
    if isinstance(value, str) and value != value.strip():
        raise ValueError("Theapolis detail IDs must not have surrounding whitespace")
    return str(value)


def _detail_credit(row: object) -> tuple[str, str, str, str, str | None, list[str]]:
    if not isinstance(row, dict):
        raise TypeError("Invalid Theapolis detail credit")
    key = f"detail:{_detail_id(row.get('person_id'))}"
    name, raw_role = _name(row), row.get("role")
    if not isinstance(raw_role, str) or not (role := canonical_role(raw_role)):
        raise ValueError("Theapolis detail credit requires a role")
    character = row.get("character")
    if character is not None and not isinstance(character, str):
        raise TypeError("Theapolis detail character must be a string or null")
    if any("\x00" in value for value in (name, raw_role, character or "")):
        raise ValueError("NUL byte in Theapolis detail credit")
    legacy_ids = row.get("legacy_ids", {})
    if not isinstance(legacy_ids, dict) or legacy_ids.keys() - {"directors", "creators"}:
        raise ValueError("Theapolis detail legacy_ids must be an object scoped to directors or creators")
    aliases = [f"{group}:{_detail_id(value)}" for group, value in legacy_ids.items()]
    return key, name, raw_role, role, normalize_character(character), aliases


def _detail_person(people: dict, key: str, name: str) -> dict:
    person = people.setdefault(key, {"name": name, "type": [], "legacy_ids": set()})
    if person["name"] != name:
        if not re.fullmatch(r"detail:profile:\d+", key):
            raise ValueError(f"Inconsistent name for person ID {key}")
        LOG.warning(
            "Theapolis profile %s has credit names %r and %r; retaining one identity and both source names",
            key,
            person["name"],
            name,
        )
    return person


def _add_detail_people(item: dict, people: dict, alias_owners: dict[str, str]) -> tuple[list[PreparedCredit], dict]:
    """Read authorized detail observations; absence is not evidence of an empty cast."""
    if "theapolis_detail" not in item and "theapolis_detail_status" not in item:
        return [], {}
    detail, status = item.get("theapolis_detail"), item.get("theapolis_detail_status")
    if not isinstance(detail, dict) or not isinstance(rows := detail.get("credits"), list):
        raise TypeError("Theapolis detail requires a credits array")
    if status != ("parsed" if rows else "confirmed_empty"):
        raise ValueError("Theapolis detail status must be parsed for credits or confirmed_empty for no credits")

    credits: list[PreparedCredit] = []
    raw_credits = []
    for row in rows:
        key, name, raw_role, role, character, aliases = _detail_credit(row)
        person = _detail_person(people, key, name)
        if row.get("profile_url"):
            person["profile_url"] = canonical_url(row["profile_url"])
        if role not in person["type"]:
            person["type"].append(role)
        for alias in aliases:
            if alias_owners.setdefault(alias, key) != key:
                raise ValueError(f"Conflicting Theapolis detail IDs for legacy alias {alias}")
        person["legacy_ids"].update(aliases)
        credits.append((key, role, character))
        raw_credits.append(
            {
                "source_person_id": key,
                "name": name,
                "role": raw_role,
                "canonical_role": role,
                "character": character,
                **({"legacy_ids": aliases} if aliases else {}),
            }
        )
    return credits, {"status": status, "credits": raw_credits}


def validate_detail(entry: dict) -> None:
    """Validate a provider entry with production_id, theapolis_detail_status and theapolis_detail.

    Detail credits have a global person_id, name, role, optional character, and optional
    legacy_ids object scoped to directors/creators. All IDs are strings or integers.
    """
    if not isinstance(entry, dict):
        raise TypeError("Theapolis detail entry must be an object")
    _detail_id(entry.get("production_id"))
    if "theapolis_detail" not in entry:
        raise ValueError("Missing theapolis_detail")
    _add_detail_people(entry, {}, {})


def _organization(item: dict, previous: dict) -> tuple[dict, str | None]:
    location = item.get("location") or {}
    country, region = split_country(location.get("country"))
    payload = {"name": _name(item), "city": location.get("city"), "country": country, **_description(item)}
    if previous.get("description") and not payload.get("description"):
        payload["description"] = previous["description"]
    if region:
        payload["region"] = region
    if geolocation := location.get("geolocation"):
        lat, lon = map(float, geolocation.split(","))
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError(f"Invalid Theapolis coordinates: {geolocation!r}")
        payload.update(lat=lat, lon=lon)
    slug = item.get("slug")
    url = f"{SITE}/de/organization/{quote(str(slug), safe='')}" if slug else None
    return payload, url


def _clean_import_strings(value: object, path: str) -> object:
    if isinstance(value, dict):
        return {key: _clean_import_strings(child, f"{path}.{key}") for key, child in value.items()}
    if isinstance(value, list):
        return [_clean_import_strings(child, f"{path}[{index}]") for index, child in enumerate(value)]
    if isinstance(value, str) and "\x00" in value:
        if path.endswith((".id", ".person_id", ".legacy_ids.directors", ".legacy_ids.creators")):
            raise ValueError(f"NUL byte in Theapolis source identity: {path}")
        LOG.warning("Removed %s NUL byte(s) from Theapolis %s", value.count("\x00"), path)
        return value.replace("\x00", "")
    return value


def _import_items(snapshot: dict, *, archived: bool) -> tuple[Iterable[dict], dt.datetime | None]:
    if snapshot.get("success") is False:
        raise ValueError("Theapolis export is incomplete; download again before importing")
    items = snapshot.get("items", snapshot.get("data", {}).get("items"))
    if archived and (not isinstance(items, Iterable) or isinstance(items, (str, bytes, dict))):
        raise ValueError("Expected iterable Theapolis archive items")
    if not archived and (not isinstance(items, list) or not items):
        raise ValueError("Expected a non-empty Theapolis items array")
    captured = snapshot.get("captured_at")
    captured_at = dt.datetime.fromisoformat(captured) if captured else None
    if captured_at and captured_at.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    return items, captured_at


def _prepare_import(
    snapshot: dict,
    *,
    archived: bool = False,
) -> tuple[dict[str, tuple[dict, str | None]], dict[str, dict], list[PreparedProduction], dt.datetime | None]:
    items, captured_at = _import_items(snapshot, archived=archived)
    organizations, people, productions, alias_owners = {}, {}, [], {}
    for raw_item in items:
        item = {
            key: _clean_import_strings(value, f"production[{raw_item.get('id')!r}].{key}")
            for key, value in raw_item.items()
        }
        production_id, name = _identity(item), _name(item)
        org = item.get("organization") or {}
        if not isinstance(org, dict):
            raise TypeError(f"Invalid Theapolis organization for production {production_id}")
        org_id = _identity(org) if org else None
        if org_id:
            organizations[org_id] = _organization(org, organizations.get(org_id, ({}, None))[0])
        credits: list[PreparedCredit] = [*_add_people(item, people)]
        detail_credits, detail = _add_detail_people(raw_item, people, alias_owners)
        credits.extend(detail_credits)
        payload = validate_payload(
            EntityType.PRODUCTION,
            {"name": name, **premiere_fields(item), "tags": _production_tags(item), **_description(item)},
            complete=True,
        )
        if detail:
            payload["_theapolis_detail"] = detail
        productions.append((production_id, org_id, payload, credits))
    for key, (payload, url) in organizations.items():
        organizations[key] = (
            validate_payload(EntityType.ORGANIZATION, payload, complete=True),
            canonical_url(url) if url else None,
        )
    for key, payload in people.items():
        people[key] = validate_payload(
            EntityType.PERSON,
            {field: value for field, value in payload.items() if field not in {"profile_url", "legacy_ids"}},
            complete=True,
        )
        if payload.get("profile_url"):
            people[key]["profile_url"] = canonical_url(payload["profile_url"])
        if payload.get("legacy_ids"):
            people[key]["legacy_ids"] = sorted(payload["legacy_ids"])
    # Register explicitly linked detail identities before accepting their legacy aliases.
    people = dict(sorted(people.items(), key=lambda entry: not entry[0].startswith("detail:")))
    return organizations, people, productions, captured_at


def _latest_export() -> Path:
    snapshots = sorted(DEFAULT_EXPORT_DIR.glob(f"*_{_export_scope(None)}_productions.json"), reverse=True)
    for candidate in snapshots:
        source_path = _source_path(candidate)
        if source_path.exists() and json.loads(source_path.read_text(encoding="utf-8")).get("success") is False:
            LOG.warning("Skipping incomplete Theapolis export: %s", candidate)
            continue
        return candidate
    raise ValueError(f"No full Theapolis snapshot in {DEFAULT_EXPORT_DIR}; download first or pass --json")


def _read_import_snapshot(json_path: Path) -> dict:
    snapshot = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(snapshot, list):
        source_path = _source_path(json_path)
        if not source_path.exists():
            raise ValueError(f"Theapolis import requires source metadata: {source_path}")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        if source.get("success") is False:
            raise ValueError("Theapolis export is incomplete; download again before importing")
        actual = validate_records(snapshot)
        expected = staged_records(source["items"])
        # Older snapshots lack person IDs; their source metadata still supplies them.
        for record, source_record in zip(actual, expected, strict=False):
            for member, source_member in zip(record["memberships"], source_record["memberships"], strict=False):
                if member["external_id"] is None:
                    source_member["external_id"] = None
        if source.get("format_version", 1) < 2:
            _legacy_staged_metadata(actual, expected, source["items"])
        if actual != expected:
            raise ValueError("Staged JSON differs from its Theapolis source metadata; re-export both files")
        snapshot = source
    return snapshot


def _legacy_staged_metadata(actual: list[dict], expected: list[dict], items: list[dict]) -> None:
    """Compare old staged files without fields the old exporter never extracted."""
    for record, source_record, item in zip(actual, expected, items, strict=False):
        if record["tags"] != source_record["tags"] and record["tags"] == _production_tags({**item, "type": None}):
            source_record["tags"] = record["tags"]
        if not record["description"]:
            source_record["description"] = None
        for member, source_member in zip(record["memberships"], source_record["memberships"], strict=False):
            if not member["description"]:
                source_member["description"] = None


@contextmanager
def _import_batch(session: Session, stage: str, number: int) -> Generator[None]:
    try:
        yield
        session.commit()
    except SQLAlchemyError, ValueError:
        session.rollback()
        LOG.exception("Theapolis %s batch %s failed; earlier committed batches remain. Rerun to resume.", stage, number)
        raise


def _import_record(
    payload: dict,
    *,
    external_id: str | None = None,
    external_url: str | None = None,
    record_key: str | None = None,
    context: dict | None = None,
) -> ImportRecord:
    key = record_key or f"id:{external_id}"
    return {
        "record_key": key,
        "incoming": payload,
        "context": {
            **(context or {}),
            "external_id": external_id,
            "external_url": canonical_url(external_url) if external_url else None,
            "record_key": key,
        },
    }


def _import_productions(
    session: Session,
    batch: tuple[PreparedProduction, ...],
    org_ids: dict[str, uuid.UUID],
    person_ids: dict[str, uuid.UUID],
    people: dict[str, dict],
    accept: Callable[[EntityType, list[ImportRecord]], tuple[list[uuid.UUID], set[uuid.UUID]]],
) -> None:
    credits = [
        [
            {
                "person_id": str(person_ids[key]),
                "role": role,
                **({"character": character[0]} if character and character[0] is not None else {}),
            }
            for key, role, *character in raw_credits
        ]
        for _, _, _, raw_credits in batch
    ]
    authors = [
        [
            (credit[0], member)
            for credit, member in zip(raw, members, strict=True)
            if len(credit) == 2 and credit[1] == "author"
        ]
        for (_, _, _, raw), members in zip(batch, credits, strict=True)
    ]
    text_ids, text_changes = accept(
        EntityType.TEXT,
        [
            _import_record(
                {"title": payload["name"]},
                record_key=f"work:production:{production_id}",
                context={
                    "authors": [people[key]["name"] for key, _ in work_credits],
                    "credits": [credit for _, credit in work_credits],
                },
            )
            for (production_id, _, payload, _), work_credits in zip(batch, authors, strict=True)
        ],
    )
    production_ids, production_changes = accept(
        EntityType.PRODUCTION,
        [
            _import_record(
                {
                    **{field: value for field, value in payload.items() if field != "_theapolis_detail"},
                    "text_id": str(text_id),
                    **({"organization_id": str(org_ids[org_id])} if org_id else {}),
                },
                external_id=production_id,
                external_url=f"https://www2.theapolis.de/de/produktionen/{production_id}",
                context={
                    "credits": members,
                    **({"theapolis_detail": payload["_theapolis_detail"]} if "_theapolis_detail" in payload else {}),
                },
            )
            for (production_id, org_id, payload, _), text_id, members in zip(batch, text_ids, credits, strict=True)
        ],
    )
    for kind, identities, changes, members_by_entity in (
        (EntityType.TEXT, text_ids, text_changes, [[credit for _, credit in group] for group in authors]),
        (EntityType.PRODUCTION, production_ids, production_changes, credits),
    ):
        links: dict[uuid.UUID, list[dict]] = {}
        for identity, members in zip(identities, members_by_entity, strict=True):
            if identity in changes:
                links.setdefault(identity, []).extend(members)
        add_credits_batch(session, kind, links, actor_id=ACTOR)


def _merge_people_by_name(session: Session, source_id: uuid.UUID, names: set[str]) -> dict[uuid.UUID, uuid.UUID]:
    groups: dict[str, dict[str, set[uuid.UUID]]] = {}
    LOG.info("Loading Theapolis people for cross-role matching")
    records = session.exec(
        select(
            SourceRecord.external_id, SourceRecord.entity_id, col(SourceRecord.payload)["incoming"]["name"].as_string()
        ).where(SourceRecord.source_id == source_id, SourceRecord.entity_type == EntityType.PERSON)
    )
    for external_id, identity, name in records:
        group, _, source_id_part = (external_id or "").partition(":")
        if group in {"directors", "creators"} and source_id_part and name in names:
            groups.setdefault(name, {}).setdefault(group, set()).add(identity)
    session.commit()
    # Connected name groups can share an identity after an earlier manual merge.
    selections: dict[str, set[uuid.UUID]] = {}
    owners: dict[uuid.UUID, str] = {}
    for name, roles in groups.items():
        if {"directors", "creators"} <= roles.keys():
            identities = set().union(*roles.values())
            if len(identities) < 2:
                continue
            related = {owners[identity] for identity in identities if identity in owners}
            for previous in related:
                identities.update(selections.pop(previous))
            selections[name] = identities
            owners.update(dict.fromkeys(identities, name))
    redirects = {}
    with tqdm(total=len(selections), desc="Matching people", unit="name") as progress:
        for number, batch in enumerate(batched(selections.items(), BATCH_SIZE, strict=False), 1):
            with _import_batch(session, "matching people", number):
                merged, deferred = merge_scoped_people(session, source_id, dict(batch), actor_id=ACTOR)
                redirects.update(merged)
            for name, identities in deferred.items():
                preview = preview_entity_merge(EntityType.PERSON, identities)
                if any(field["conflict"] for field in preview["fields"]):
                    raise ValueError(
                        f"Conflicting fields for Theapolis person {name!r}; merge manually before reimporting"
                    )
                target = merge_entities(
                    EntityType.PERSON,
                    identities,
                    target_id=preview["target_id"],
                    revision=preview["revision"],
                    actor_id=ACTOR,
                )
                redirects.update(dict.fromkeys(identities, target))
            progress.update(len(batch))
    session.expire_all()
    return redirects


def _dach_import(
    organizations: dict[str, tuple[dict, str | None]], people: dict[str, dict], productions: list[PreparedProduction]
) -> tuple[dict[str, tuple[dict, str | None]], dict[str, dict], list[PreparedProduction]]:
    production_ids: dict[str, set[str]] = {}
    for production_id, org_id, _, _ in productions:
        if org_id:
            production_ids.setdefault(org_id, set()).add(production_id)
    organizations = {
        key: value
        for key, value in organizations.items()
        if value[0].get("country") in DACH_COUNTRIES and len(production_ids.get(key, set())) > 1
    }
    productions = [production for production in productions if production[1] in organizations]
    person_keys = {key for _, _, _, credits in productions for key, *_ in credits}
    return organizations, {key: value for key, value in people.items() if key in person_keys}, productions


def _apply_import(
    organizations: dict[str, tuple[dict, str | None]],
    people: dict[str, dict],
    productions: list[PreparedProduction],
    captured_at: dt.datetime | None,
    *,
    legacy: bool,
) -> dict[str, list[str]]:
    with get_session() as session:
        source = session.exec(select(Source).where(Source.name == "theapolis.de")).first()
        if source is None:
            source = Source(name="theapolis.de", base_url=SITE, description="Theapolis directory; not an official site")
            session.add(source)
            session.flush()
            session.refresh(source)
        source_id = source.id
        result: dict[str, list[str]] = {"organization": [], "person": [], "text": [], "production": []}

        seen: dict[EntityType, set[uuid.UUID]] = {kind: set() for kind in EntityType}

        def accept(kind: EntityType, batch: list[ImportRecord]) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
            identities, changed = accept_records(
                session, source_id, kind, batch, captured_at=captured_at, actor_id=ACTOR
            )
            for identity in identities:
                if identity not in seen[kind]:
                    result[kind.value].append(str(identity))
                    seen[kind].add(identity)
            return identities, changed

        org_ids, person_ids = {}, {}

        for kind, values, mapping in (
            (
                EntityType.ORGANIZATION,
                [
                    (key, _import_record(payload, external_id=key, external_url=url))
                    for key, (payload, url) in organizations.items()
                ],
                org_ids,
            ),
            (
                EntityType.PERSON,
                [
                    (
                        key,
                        _import_record(
                            {field: value for field, value in payload.items() if field != "profile_url"},
                            external_id=key,
                            external_url=payload.get("profile_url"),
                        ),
                    )
                    for key, payload in people.items()
                ],
                person_ids,
            ),
        ):
            with tqdm(total=len(values), desc=kind.value.title(), unit=kind.value) as progress:
                for batch_number, batch in enumerate(batched(values, BATCH_SIZE, strict=False), 1):
                    with _import_batch(session, kind.value, batch_number):
                        identities, _ = accept(kind, [value for _, value in batch])
                        mapping.update(zip((key for key, _ in batch), identities, strict=True))
                    progress.update(len(batch))
        if legacy:
            redirects = _merge_people_by_name(session, source_id, {payload["name"] for payload in people.values()})
            person_ids = {key: redirects.get(identity, identity) for key, identity in person_ids.items()}
        result["person"] = list(dict.fromkeys(str(person_ids[key]) for key in people))
        with tqdm(total=len(productions), desc="Productions", unit="production") as progress:
            for batch_number, batch in enumerate(batched(productions, BATCH_SIZE, strict=False), 1):
                with _import_batch(session, "productions", batch_number):
                    _import_productions(session, batch, org_ids, person_ids, people, accept)
                progress.update(len(batch))
    return result


def import_all(json_path: Path | None = None, *, dach_only: bool = True) -> dict[str, list[str]]:
    """Replay a legacy export, preserving the legacy matching behavior."""
    organizations, people, productions, captured_at = _prepare_import(
        _read_import_snapshot(json_path if json_path is not None else _latest_export())
    )
    if dach_only:
        organizations, people, productions = _dach_import(organizations, people, productions)
    init_db()
    with _import_lock():
        return _apply_import(organizations, people, productions, captured_at, legacy=True)


@contextmanager
def _import_lock() -> Generator[None]:
    # Keep one transaction alive for the lock while the importer commits separate batches.
    with get_session() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("select pg_advisory_xact_lock(hashtext(current_database()), hashtext('theaterdb.theapolis'))")
            )
        yield


def import_archive(path: Path, *, dach_only: bool = True) -> dict[str, list[str]]:
    """Apply changes since the last successful import of this archive and scope."""
    init_db()
    with _import_lock(), open_archive(path) as archive:
        run = archive.latest()
        if run is None:
            raise ValueError("No sealed full Theapolis archive run")
        identity = archive.identity
        scope = "dach" if dach_only else "all"
        with get_session() as session:
            checkpoint = session.get(TheapolisImportCheckpoint, (identity, scope))
            since = checkpoint.run if checkpoint else 0
        rows = archive.items(run, since=since, countries=tuple(sorted(DACH_COUNTRIES)) if dach_only else ())
        if (first := next(rows, None)) is None:
            result: dict[str, list[str]] = {"organization": [], "person": [], "text": [], "production": []}
        else:
            result = _apply_import(
                *_prepare_import({"items": chain((first,), rows), "captured_at": archive.captured(run)}, archived=True),
                legacy=False,
            )
        if since != run:
            with get_session() as session:
                checkpoint = session.get(TheapolisImportCheckpoint, (identity, scope))
                if checkpoint is None:
                    session.add(TheapolisImportCheckpoint(archive_id=identity, scope=scope, run=run))
                else:
                    checkpoint.run = max(checkpoint.run, run)
                session.commit()
        LOG.info(
            "Theapolis archive run %s, delta from %s: %s",
            run,
            since or "baseline",
            {k: len(v) for k, v in result.items()},
        )
        return result


def main() -> None:
    """Run export, independent image download, accepted import or explicit case migration."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("download", help="Export API productions; no database writes")
    export.add_argument("--json", type=Path, help="Write the legacy two-file JSON export instead of the archive")
    export.add_argument("--archive", type=Path)
    export.add_argument("--max-pages", type=int)
    export.add_argument("--concurrency", type=int, default=10)
    export.add_argument(
        "--detail-concurrency",
        type=int,
        default=DETAIL_CONCURRENCY,
        help="Concurrent cast/crew API requests (default: 32)",
    )
    export.add_argument("--restart", action="store_true", help="Abandon incomplete archive run and restart capture")
    export.add_argument(
        "--reuse-profile-cache", action="store_true", help="Read fresh legacy profile results from Postgres"
    )
    seed = sub.add_parser("seed", help="Build an archive from an existing full export; no DB writes")
    seed.add_argument("--json", type=Path, required=True)
    seed.add_argument("--archive", type=Path)
    details = sub.add_parser("details", help="Archive cast and crew from an authorized JSON Lines feed")
    details.add_argument("--file", type=Path, help="Read a JSON Lines feed instead of downloading the public API")
    details.add_argument("--archive", type=Path)
    details.add_argument("--concurrency", type=int, default=DETAIL_CONCURRENCY)
    images = sub.add_parser("images", help="Download public sitemap profile photos and theatre logos")
    images.add_argument("--output", type=Path, default=DEFAULT_IMAGES)
    images.add_argument("--limit", type=int)
    images.add_argument("--concurrency", type=int, default=10)
    review = sub.add_parser(
        "import", aliases=["import_all"], help="Accept source records without overwriting existing values"
    )
    inputs = review.add_mutually_exclusive_group()
    inputs.add_argument("--json", type=Path, help="Import a legacy snapshot instead of the archive")
    inputs.add_argument("--archive", type=Path)
    scope = review.add_mutually_exclusive_group()
    scope.add_argument(
        "--dach-only",
        dest="dach_only",
        action="store_true",
        default=True,
        help="Import only organizations in Germany, Austria or Switzerland with at least two productions (default)",
    )
    scope.add_argument("--all", dest="dach_only", action="store_false", help="Import all organizations and productions")
    sub.add_parser("migrate-cases", help="Explicitly accept stored pending Theapolis import cases")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.command == "download":
        if args.json and (args.archive or args.restart or args.reuse_profile_cache):
            parser.error("--json cannot be combined with --archive, --restart or --reuse-profile-cache")
        if args.json:
            asyncio.run(download(args.json, concurrency=args.concurrency, max_pages=args.max_pages))
        else:
            run = asyncio.run(
                download_archive(
                    args.archive or DEFAULT_EXPORT_DIR / ARCHIVE_NAME,
                    concurrency=args.concurrency,
                    max_pages=args.max_pages,
                    restart=args.restart,
                    reuse_profile_cache=args.reuse_profile_cache,
                    detail_concurrency=args.detail_concurrency,
                )
            )
            print(json.dumps({"run": run}))
    elif args.command == "seed":
        print(json.dumps({"run": seed_archive(args.archive or DEFAULT_EXPORT_DIR / ARCHIVE_NAME, args.json)}))
    elif args.command == "details":
        path = args.archive or DEFAULT_EXPORT_DIR / ARCHIVE_NAME
        run = (
            archive_details(path, args.file)
            if args.file
            else asyncio.run(download_credits(path, concurrency=args.concurrency))
        )
        print(json.dumps({"run": run}))
    elif args.command == "images":
        asyncio.run(download_images(args.output, concurrency=args.concurrency, limit=args.limit))
    elif args.command == "migrate-cases":
        print(json.dumps({kind: len(cases) for kind, cases in migrate_pending_cases().items()}))
    else:
        started = monotonic()
        try:
            path = args.archive or DEFAULT_EXPORT_DIR / ARCHIVE_NAME
            result = (
                import_archive(path, dach_only=args.dach_only)
                if args.json is None and (args.archive or path.exists())
                else import_all(args.json, dach_only=args.dach_only)
            )
        finally:
            elapsed = monotonic() - started
            LOG.info("Theapolis import took %dm %.1fs", elapsed // 60, elapsed % 60)
        print(json.dumps({kind: len(cases) for kind, cases in result.items()}))


if __name__ == "__main__":
    main()
