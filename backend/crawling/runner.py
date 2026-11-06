"""Crawl adapters and transactional incremental submission."""

import asyncio
import datetime as dt
import logging
import math
import os
import re
import time
import uuid
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import aiohttp
from selectolax.lexbor import LexborHTMLParser
from sqlmodel import Session, select

from backend.crawling.config import DEFINITION, RecordCrawl, TheatreCrawl
from backend.crawling.service import ACTIVE, utcnow
from backend.ingestion.configurable import ConfigurableScraper, PageKind, extract_field
from backend.ingestion.snapshots import digest, snapshot_path, write_json
from backend.models import CrawlPage, CrawlRecord, CrawlRun, Source
from backend.production_import import ingest_records
from backend.reconciliation import canonical_url, propose_record, validate_payload
from backend.user_management import get_session

LOG = logging.getLogger(__name__)
MAX_BODY = 5 * 1024 * 1024
MAX_RECORDS = 10000


class CrawlStoppedError(Exception):
    """The run lost ownership or a user requested cancellation."""


def _running(session: Session, run_id: uuid.UUID) -> CrawlRun:
    run = session.get(CrawlRun, run_id, with_for_update=True)
    if run is None or run.status != "running" or run.cancel_requested_at is not None:
        raise CrawlStoppedError("Crawl stopped before checkpoint")
    return run


def checkpoint(
    run_id: uuid.UUID,
    *,
    phase: str | None = None,
    stats: dict | None = None,
    artifact_path: str | None = None,
) -> None:
    """Fence stale workers and keep a durable heartbeat/progress checkpoint."""
    with get_session() as session:
        run = _running(session, run_id)
        run.heartbeat_at = utcnow()
        if phase:
            run.phase = phase
        if stats is not None:
            run.stats = dict(stats)
        if artifact_path:
            run.artifact_path = artifact_path
        session.add(run)
        session.commit()


def payload_hash(value: object) -> str:
    """Ignore object/list ordering and empty optional fields, as snapshot diffs do."""
    if isinstance(value, dict):
        value = {key: payload_hash(item) for key, item in value.items() if item not in (None, "", [], {})}
    elif isinstance(value, list):
        value = sorted(payload_hash(item) for item in value)
    return digest(value)


class Fetcher:
    """Bounded, cancellable conditional HTTP transport shared by both adapters."""

    def __init__(self, run: CrawlRun, *, base_url: str, user_agent: str, timeout: float, delay: float) -> None:
        """Bind cache identity and request policy to this source/run."""
        self.run_id = run.id
        self.source_id = run.source_id
        self.force = run.force_fetch
        self.base_url = base_url
        self.user_agent = user_agent
        self.timeout = timeout
        self.delay = delay
        if not math.isfinite(timeout) or not math.isfinite(delay):
            raise ValueError("Request timeout and delay must be finite")
        self.variant = digest([user_agent, "text/html"])
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.stats = {"requests": 0, "not_modified": 0, "bytes": 0}

    def check_url(self, url: str) -> str:
        """Apply the existing same-origin policy to discovery and every redirect."""
        url = urldefrag(url)[0]
        parts, base = urlsplit(url), urlsplit(self.base_url)
        if parts.scheme != base.scheme or parts.netloc != base.netloc or parts.username or parts.password:
            raise ValueError(f"URL outside configured origin: {url}")
        return url

    def session(self) -> aiohttp.ClientSession:
        """Keep total request timeout and representation headers explicit."""
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": self.user_agent, "Accept": "text/html"},
        )

    async def fetch(self, session: aiohttp.ClientSession, url: str) -> str:
        """Serialize per-source requests to enforce politeness even with theatre fan-out."""
        async with self.lock:
            return await self._fetch(session, self.check_url(url))

    def _cached(self, url: str) -> CrawlPage | None:
        with get_session() as session:
            return session.exec(
                select(CrawlPage).where(
                    CrawlPage.source_id == self.source_id, CrawlPage.variant == self.variant, CrawlPage.url == url
                )
            ).first()

    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> str:
        redirects = 0
        attempts = 0
        while True:
            checkpoint(self.run_id, stats=self.stats)
            cached = self._cached(url)
            headers = {}
            if cached is not None and not self.force:
                if cached.etag:
                    headers["If-None-Match"] = cached.etag
                if cached.last_modified:
                    headers["If-Modified-Since"] = cached.last_modified
            await asyncio.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                result, redirect = await self._request(session, url, headers, cached)
            except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ClientResponseError) as error:
                if isinstance(error, aiohttp.ClientResponseError) and error.status != 429 and error.status < 500:
                    raise
                attempts += 1
                if attempts >= 4:
                    raise
                delay = _retry_delay(error, attempts)
                LOG.warning("Retrying %s after %s: %s", url, delay, error)
                await asyncio.sleep(delay)
                continue
            if not redirect:
                return result
            redirects += 1
            if redirects > 5:
                raise ValueError(f"Too many redirects: {url}")
            url = self.check_url(result)

    async def _request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        cached: CrawlPage | None,
    ) -> tuple[str, bool]:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            self.stats["requests"] += 1
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(f"Redirect without Location: {url}")
                return urljoin(url, location), True
            if response.status == 304:
                if cached is None or self.force or not headers:
                    raise ValueError(f"304 without a usable cache at {url}")
                self.stats["not_modified"] += 1
                self._save_page(url, cached.body, response, cached=cached)
                return cached.body, False
            response.raise_for_status()
            body = await self._read(response, url)
            self._save_page(url, body, response)
            return body, False

    async def _read(self, response: aiohttp.ClientResponse, url: str) -> str:
        if response.status != 200 or "html" not in response.headers.get("Content-Type", "").casefold():
            raise ValueError(f"Expected HTML 200 response at {url}")
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise ValueError(f"Page exceeds {MAX_BODY} bytes: {url}")
        self.stats["bytes"] += len(data)
        return data.decode(response.charset or "utf-8")

    def _save_page(
        self, url: str, body: str, response: aiohttp.ClientResponse, *, cached: CrawlPage | None = None
    ) -> None:
        with get_session() as session:
            run = _running(session, self.run_id)
            page = session.exec(
                select(CrawlPage).where(
                    CrawlPage.source_id == self.source_id, CrawlPage.variant == self.variant, CrawlPage.url == url
                )
            ).first()
            if page is None:
                page = CrawlPage(source_id=self.source_id, variant=self.variant, url=url, body=body)
            page.body = body
            page.etag = response.headers.get("ETag", cached.etag if cached else None)
            page.last_modified = response.headers.get("Last-Modified", cached.last_modified if cached else None)
            page.checked_at = utcnow()
            run.stats = dict(self.stats)
            run.heartbeat_at = utcnow()
            session.add(page)
            session.add(run)
            session.commit()


def _retry_delay(error: Exception, attempts: int) -> float:
    header = (
        error.headers.get("Retry-After") if isinstance(error, aiohttp.ClientResponseError) and error.headers else None
    )
    if not header:
        return min(60.0, 2**attempts)
    try:
        seconds = float(header)
    except ValueError:
        seconds = (parsedate_to_datetime(header) - utcnow()).total_seconds()
    if not math.isfinite(seconds) or seconds > 120:
        raise ValueError(f"Retry-After {header!r} exceeds the worker wait budget; retry this crawl later")
    return max(0, seconds)


class IncrementalTheatreScraper(ConfigurableScraper):
    """Reuse theatre extraction/discovery without changing the database-free CLI."""

    def __init__(self, definition: TheatreCrawl, fetcher: Fetcher) -> None:
        """Replace only HTTP transport, not theatre parsing semantics."""
        super().__init__(definition.config)
        self.fetcher = fetcher

    async def fetch(self, session: aiohttp.ClientSession, url: str) -> str:
        """Return conditional/cached HTML using the source-owned transport."""
        return await self.fetcher.fetch(session, url)

    def session(self) -> aiohttp.ClientSession:
        """Use the same representation headers for cache variants and requests."""
        return self.fetcher.session()

    def parse_page(self, html: str, url: str, kind: PageKind) -> dict:
        """Publish extraction phase without changing the existing staged payload."""
        checkpoint(self.fetcher.run_id, phase="people" if kind == "person" else "productions", stats=self.fetcher.stats)
        return super().parse_page(html, url, kind)


def _page_base(tree: LexborHTMLParser, url: str, fetcher: Fetcher) -> str:
    node = tree.css_first("base[href]")
    return fetcher.check_url(urljoin(url, node.attributes["href"])) if node else url


def _links(tree: LexborHTMLParser, url: str, definition: RecordCrawl, fetcher: Fetcher) -> list[str]:
    result = []
    for node in tree.css(definition.listing.selector):
        href = node.attributes.get("href")
        if not href:
            continue
        candidate = urldefrag(urljoin(url, href))[0]
        if not re.search(definition.listing.include, candidate):
            continue
        if definition.listing.exclude and re.search(definition.listing.exclude, candidate):
            continue
        result.append(fetcher.check_url(candidate))
    return result


async def _discover_records(
    session: aiohttp.ClientSession,
    definition: RecordCrawl,
    run: CrawlRun,
    fetcher: Fetcher,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for seed in definition.listing.urls:
        url: str | None = fetcher.check_url(seed)
        for _ in range(definition.listing.max_pages):
            if url is None:
                break
            if url in seen:
                raise ValueError(f"Repeated listing page; coverage is uncertain: {url}")
            seen.add(url)
            tree = LexborHTMLParser(await fetcher.fetch(session, url))
            base = _page_base(tree, url, fetcher)
            urls = list(dict.fromkeys([*urls, *_links(tree, base, definition, fetcher)]))
            if len(urls) > MAX_RECORDS:
                raise ValueError(f"Source exceeds the {MAX_RECORDS} record safety limit")
            if run.limit is not None and len(urls) >= run.limit:
                return urls[: run.limit]
            node = tree.css_first(definition.listing.next_selector) if definition.listing.next_selector else None
            href = node.attributes.get("href") if node else None
            url = fetcher.check_url(urljoin(base, href)) if href else None
        else:
            if url is not None:
                raise ValueError("Listing max_pages reached; refusing an incomplete crawl")
    if not urls:
        raise ValueError("No record links found; check listing selectors")
    return urls


async def _records(definition: RecordCrawl, run: CrawlRun, fetcher: Fetcher) -> list[dict]:
    async with fetcher.session() as session:
        urls = await _discover_records(session, definition, run, fetcher)
        checkpoint(run.id, phase="details", stats=fetcher.stats)
        records = []
        for url in urls[: run.limit]:
            tree = LexborHTMLParser(await fetcher.fetch(session, url))
            base = _page_base(tree, url, fetcher)
            payload = {field: extract_field(tree, rule, base) for field, rule in definition.fields.items()}
            payload = validate_payload(definition.entity_type, payload, complete=True)
            records.append({"url": url, "payload": payload})
        return records


def _submit(run_id: uuid.UUID, records: list[dict], artifact: Path, stats: dict) -> None:
    with get_session() as session:
        run = _running(session, run_id)
        definition = DEFINITION.validate_python(run.definition)
        source = session.get(Source, run.source_id)
        if source is None:
            raise ValueError("Source not found")
        if isinstance(definition, TheatreCrawl):
            namespace = digest(["theatre", str(definition.organization_id), run.enrich_people])
        else:
            namespace = digest(["records", definition.entity_type])
        previous = {
            row.record_key: row
            for row in session.exec(
                select(CrawlRecord).where(CrawlRecord.source_id == run.source_id, CrawlRecord.namespace == namespace)
            )
        }
        changed = []
        pending = []
        seen = set()
        for record in records:
            key = canonical_url(record["url"])
            if key in seen:
                raise ValueError(f"Duplicate record identity in crawl: {key}")
            seen.add(key)
            fingerprint = payload_hash(record)
            old = previous.get(key)
            if old is not None and old.payload_hash == fingerprint:
                continue
            changed.append(record)
            entry = old or CrawlRecord(
                source_id=run.source_id,
                namespace=namespace,
                record_key=key,
                payload_hash=fingerprint,
                run_id=run.id,
            )
            entry.payload_hash = fingerprint
            entry.run_id = run.id
            pending.append(entry)
        outcome = {}
        if changed and isinstance(definition, TheatreCrawl):
            result = ingest_records(
                changed,
                organization_id=definition.organization_id,
                source_name=source.name,
                source_id=source.id,
                source_base_url=definition.config.base_url,
                captured_at=run.started_at.replace(tzinfo=dt.UTC) if run.started_at else None,
                actor_id=run.actor_id,
                file_name=artifact.name,
                scope="delta",
                session=session,
            )
            outcome = {key: len(value) for key, value in result.items()}
        elif changed and isinstance(definition, RecordCrawl):
            for record in changed:
                propose_record(
                    definition.entity_type,
                    record["payload"],
                    source_id=source.id,
                    external_url=record["url"],
                    actor_id=run.actor_id,
                    captured_at=run.started_at.replace(tzinfo=dt.UTC) if run.started_at else None,
                    reason="Automatischer Quellen-Crawl",
                    session=session,
                )
            outcome = {"review": len(changed)}
        for entry in pending:
            session.add(entry)
        run.status = "succeeded"
        run.phase = "finished"
        run.finished_at = utcnow()
        run.heartbeat_at = utcnow()
        run.artifact_path = str(artifact)
        run.stats = {
            **stats,
            "records": len(records),
            "changed": len(changed),
            "unchanged": len(records) - len(changed),
            "import": outcome,
        }
        session.add(run)
        session.commit()


def _finish_error(run_id: uuid.UUID, status: str, error: str) -> None:
    with get_session() as session:
        run = session.get(CrawlRun, run_id, with_for_update=True)
        if run is not None and run.status in ACTIVE:
            run.status = status
            run.phase = status
            run.error = error
            run.finished_at = utcnow()
            session.add(run)
            session.commit()


async def _heartbeat(run_id: uuid.UUID) -> None:
    while True:
        await asyncio.sleep(5)
        checkpoint(run_id)


async def _collect_submit(run: CrawlRun, heartbeat: asyncio.Task) -> None:
    definition = DEFINITION.validate_python(run.definition)
    config = definition.config if isinstance(definition, TheatreCrawl) else definition
    fetcher = Fetcher(
        run,
        base_url=config.base_url,
        user_agent=config.user_agent,
        timeout=config.timeout_seconds,
        delay=config.request_delay,
    )
    if isinstance(definition, TheatreCrawl):
        scraper = IncrementalTheatreScraper(definition, fetcher)
        records = await scraper.scrape(limit=run.limit, enrich_people=run.enrich_people)
    else:
        records = await _records(definition, run, fetcher)
    checkpoint(run.id, phase="importing", stats=fetcher.stats)
    scope = "all" if run.limit is None else f"limit-{run.limit}"
    scope += f"-{'people' if run.enrich_people else 'no-people'}-{digest(run.definition)[:12]}"
    root = Path(os.environ.get("THEATERDB_CRAWL_DIR", "data/crawls"))
    directory = root / str(run.source_id) / str(run.id)
    path = (
        snapshot_path(directory, scope) if isinstance(definition, TheatreCrawl) else directory / "records.json"
    ).resolve()
    write_json(path, records)
    checkpoint(run.id, artifact_path=str(path))
    if heartbeat.done():
        heartbeat.result()
    _submit(run.id, records, path, fetcher.stats)


async def execute(run_id: uuid.UUID) -> None:
    """Run one frozen definition; submit data and cursors together, or neither."""
    with get_session() as session:
        run = session.get(CrawlRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("Crawl run not found")
        if run.status != "queued":
            LOG.info("Skipping run %s in state %s", run_id, run.status)
            return
        run.status = "running"
        run.phase = "discovery"
        run.started_at = utcnow()
        run.heartbeat_at = utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
    heartbeat = asyncio.create_task(_heartbeat(run_id))
    try:
        await _collect_submit(run, heartbeat)
    except (asyncio.CancelledError, CrawlStoppedError) as error:
        LOG.info("Crawl %s cancelled: %s", run_id, error)
        _finish_error(run_id, "cancelled", "Crawl cancelled; no partial import was published.")
        raise
    except Exception as error:
        LOG.exception("Crawl %s failed", run_id)
        _finish_error(run_id, "failed", f"{type(error).__name__}: {error}")
        raise
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
