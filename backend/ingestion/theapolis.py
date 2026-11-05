"""Separate Theapolis API export, sitemap image download and review-only import."""

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from email.utils import parsedate_to_datetime
from itertools import islice
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlsplit

import aiohttp
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlmodel import select

from backend.ingestion.snapshots import digest, snapshot_path
from backend.ingestion.staging import validate_records
from backend.models import EntityType, Source
from backend.reconciliation import propose_record, validate_payload
from backend.user_management import get_session, init_db

SITE = "https://www.theapolis.de"
DEFAULT_EXPORT_DIR = Path("data/staged/theapolis.de")
DEFAULT_IMAGES = Path("data/theapolis/images")
ACTOR = "theapolis-ingestion"
LOG = logging.getLogger(__name__)


async def _parallel[T, R](values: Iterable[T], action: Callable[[T], Awaitable[R]], concurrency: int) -> list[R]:
    if concurrency < 1:
        raise ValueError("Concurrency must be positive")
    iterator = iter(values)
    results = []
    while batch := list(islice(iterator, concurrency)):
        results.extend(await asyncio.gather(*(action(value) for value in batch)))
    return results


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
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def premiere_fields(item: dict) -> dict:
    """Keep year precision separate; accept legacy API dates and German page dates."""
    value = item.get("premierDate") or item.get("premiere")
    year = next(
        (item.get(key) for key in ("premierYear", "year", "premiere_year") if item.get(key) not in (None, "")),
        None,
    )
    premiere = None
    if value not in (None, ""):
        text = str(value).strip()
        if re.fullmatch(r"\d{4}", text):
            if year is not None and (isinstance(year, bool) or int(year) != int(text)):
                raise ValueError(f"Conflicting premiere date/year for production {item.get('id')}")
            year = int(text)
        else:
            if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", text):
                premiere = dt.datetime.strptime(text, "%d.%m.%Y").replace(tzinfo=dt.UTC)
            else:
                premiere = dt.datetime.fromisoformat(text)
                if premiere.tzinfo is None:
                    premiere = premiere.replace(tzinfo=dt.UTC)
            if year and int(year) != premiere.year:
                raise ValueError(f"Conflicting premiere date/year for production {item.get('id')}")
            year = premiere.year
    if year is not None:
        if isinstance(year, bool) or not re.fullmatch(r"\d{4}", str(year)) or not 1 <= int(year) <= 9999:
            raise ValueError(f"Invalid premiere year: {year!r}")
        year = int(year)
    return {"premiere": premiere.isoformat() if premiere else None, "premiere_year": year}


def staged_records(items: list[dict]) -> list[dict]:
    """Use the same validated production-array contract as venue scrapers."""
    records = []
    for item in items:
        members = [
            {"name": _name(person), "type": role, "role": role}
            for group, role in (("directors", "director"), ("creators", "author"))
            for person in item.get(group) or []
        ]
        fields = premiere_fields(item)
        records.append(
            {
                "title": _name(item),
                "url": f"https://www2.theapolis.de/de/produktionen/{quote(_identity(item), safe='')}",
                **fields,
                "memberships": members,
            }
        )
    return validate_records(records)


def _source_path(path: Path) -> Path:
    return path.with_suffix(".source.json")


def _export_scope(max_pages: int | None) -> str:
    config = {"url": f"{SITE}/api/de/productions", "items": 500, "status": [2, 4]}
    return f"{'all' if max_pages is None else f'limit-{max_pages * 500}'}-people-{digest(config)[:12]}"


async def download(output: Path | None = None, *, concurrency: int = 10, max_pages: int | None = None) -> Path:
    """Export shared-schema staged JSON and source metadata, without database writes."""
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    if output is None:
        output = snapshot_path(DEFAULT_EXPORT_DIR, _export_scope(max_pages))
    async with _session(concurrency) as session:

        async def page(number: int) -> dict:
            query = urlencode({"page": number, "items": 500, "status[0]": 2, "status[1]": 4, "search": ""})
            result = json.loads(await _fetch(session, f"{SITE}/api/de/productions?{query}"))
            if result.get("success") is not True:
                raise ValueError(f"Theapolis rejected production page {number}")
            return result["data"]

        first = await page(1)
        pages = math.ceil(first["totalCount"] / first["numItemsPerPage"])
        if max_pages:
            pages = min(pages, max_pages)
        items = list(first["items"])
        for result in await _parallel(range(2, pages + 1), page, concurrency):
            items.extend(result["items"])
        records = staged_records(items)
        _write_json(
            _source_path(output),
            {
                "success": True,
                "captured_at": dt.datetime.now(dt.UTC).isoformat(),
                "partial": pages < math.ceil(first["totalCount"] / first["numItemsPerPage"]),
                "items": items,
            },
        )
        _write_json(output, records)
    LOG.info("Exported %s productions to %s", len(items), output)
    return output


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

    pages = list(
        dict.fromkeys(
            _site_url(url)
            for group in await _parallel(maps, locations, concurrency)
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
    signatures = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF8": ".gif", b"RIFF": ".webp"}
    suffix = next((ext for signature, ext in signatures.items() if body.startswith(signature)), None)
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
        for start in range(0, len(pages), concurrency):
            for result in await _parallel(pages[start : start + concurrency], image, concurrency):
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
            person_id, person_name = _identity(person), _name(person)
            payload = people.setdefault(person_id, {"name": person_name, "type": []})
            if payload["name"] != person_name:
                raise ValueError(f"Inconsistent name for person ID {person_id}")
            if role not in payload["type"]:
                payload["type"].append(role)
            credits.append((person_id, role))
    return credits


def _organization(item: dict) -> tuple[dict, str | None]:
    location = item.get("location") or {}
    payload = {"name": _name(item), "city": location.get("city"), "country": location.get("country")}
    if geolocation := location.get("geolocation"):
        lat, lon = map(float, geolocation.split(","))
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError(f"Invalid Theapolis coordinates: {geolocation!r}")
        payload.update(lat=lat, lon=lon)
    slug = item.get("slug")
    url = f"{SITE}/de/organization/{quote(str(slug), safe='')}" if slug else None
    return payload, url


def _prepare_import(snapshot: dict) -> tuple[dict, dict, list, dt.datetime | None]:
    items = snapshot.get("items", snapshot.get("data", {}).get("items"))
    if not isinstance(items, list) or not items:
        raise ValueError("Expected a non-empty Theapolis items array")
    captured = snapshot.get("captured_at")
    captured_at = dt.datetime.fromisoformat(captured) if captured else None
    if captured_at and captured_at.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    organizations, people, productions = {}, {}, []
    for item in items:
        production_id, name = _identity(item), _name(item)
        org = item.get("organization") or {}
        org_id = _identity(org) if org else None
        if org_id:
            organizations[org_id] = _organization(org)
        credits = _add_people(item, people)
        payload = {"name": name, **premiere_fields(item)}
        productions.append((production_id, org_id, payload, credits))
    for payload, _ in organizations.values():
        validate_payload(EntityType.ORGANIZATION, payload, complete=True)
    for payload in people.values():
        validate_payload(EntityType.PERSON, payload, complete=True)
    for _, _, payload, _ in productions:
        validate_payload(EntityType.PRODUCTION, payload, complete=True)
    return organizations, people, productions, captured_at


def import_all(json_path: Path | None = None) -> dict[str, list[str]]:
    """Validate a whole export, then queue generic review cases, never venue uploads."""
    if json_path is None:
        snapshots = sorted(DEFAULT_EXPORT_DIR.glob(f"*_{_export_scope(None)}_productions.json"))
        if not snapshots:
            raise ValueError(f"No full Theapolis snapshot in {DEFAULT_EXPORT_DIR}; download first or pass --json")
        json_path = snapshots[-1]
    snapshot = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(snapshot, list):
        source_path = _source_path(json_path)
        if not source_path.exists():
            raise ValueError(f"Theapolis review import requires source metadata: {source_path}")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        if validate_records(snapshot) != staged_records(source["items"]):
            raise ValueError("Staged JSON differs from its Theapolis source metadata; re-export both files")
        snapshot = source
    organizations, people, productions, captured_at = _prepare_import(snapshot)
    init_db()
    with get_session() as session:
        source = session.exec(select(Source).where(Source.name == "theapolis.de")).first()
        if source is None:
            source = Source(name="theapolis.de", base_url=SITE, description="Theapolis directory; not an official site")
            session.add(source)
            session.commit()
            session.refresh(source)
        source_id = source.id
    result: dict[str, list[str]] = {"organization": [], "person": [], "text": [], "production": []}

    def propose(
        kind: EntityType,
        payload: dict,
        *,
        external_id: str | None = None,
        external_url: str | None = None,
        record_key: str | None = None,
        context: dict | None = None,
    ) -> str:
        case = propose_record(
            kind,
            payload,
            source_id=source_id,
            captured_at=captured_at,
            actor_id=ACTOR,
            external_id=external_id,
            external_url=external_url,
            record_key=record_key,
            context=context,
        )
        value = str(case.id)
        if value not in result[kind.value]:
            result[kind.value].append(value)
        return value

    org_cases = {
        key: propose(EntityType.ORGANIZATION, payload, external_id=key, external_url=url)
        for key, (payload, url) in organizations.items()
    }
    person_cases = {key: propose(EntityType.PERSON, payload, external_id=key) for key, payload in people.items()}
    for production_id, org_id, payload, raw_credits in productions:
        credits = [{"case_id": person_cases[key], "role": role} for key, role in raw_credits]
        authors = [people[key]["name"] for key, role in raw_credits if role == "author"]
        text_case = propose(
            EntityType.TEXT,
            {"title": payload["name"]},
            record_key=f"work:production:{production_id}",
            context={"authors": authors, "credits": [c for c in credits if c["role"] == "author"]},
        )
        propose(
            EntityType.PRODUCTION,
            payload,
            external_id=production_id,
            external_url=f"https://www2.theapolis.de/de/produktionen/{production_id}",
            context={
                "credits": credits,
                "directors": [people[key]["name"] for key, role in raw_credits if role == "director"],
                "dependencies": [
                    *([{"case_id": org_cases[org_id], "field": "organization_id"}] if org_id else []),
                    {"case_id": text_case, "field": "text_id"},
                ],
            },
        )
    return result


def main() -> None:
    """Run export, independent image download, or review-only reimport."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("download", help="Export API productions; no database writes")
    export.add_argument("--json", type=Path, help="Override the timestamped staged snapshot filename")
    export.add_argument("--max-pages", type=int)
    export.add_argument("--concurrency", type=int, default=10)
    images = sub.add_parser("images", help="Download public sitemap profile photos and theatre logos")
    images.add_argument("--output", type=Path, default=DEFAULT_IMAGES)
    images.add_argument("--limit", type=int)
    images.add_argument("--concurrency", type=int, default=10)
    review = sub.add_parser("import", aliases=["import_all"], help="Queue review cases only")
    review.add_argument("--json", type=Path, help="Snapshot to import; defaults to latest full staged export")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.command == "download":
        asyncio.run(download(args.json, concurrency=args.concurrency, max_pages=args.max_pages))
    elif args.command == "images":
        asyncio.run(download_images(args.output, concurrency=args.concurrency, limit=args.limit))
    else:
        result = import_all(args.json)
        print(json.dumps({kind: len(cases) for kind, cases in result.items()}))


if __name__ == "__main__":
    main()
