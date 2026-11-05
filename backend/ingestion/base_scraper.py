"""backend/ingestion/base_scraper.py

Abstract base class for all theater scrapers.

Subclasses must implement:
  - parse_list(html)  → list of items (dicts with at least a "url" key,
                        or any structure parse_detail can consume)
  - parse_detail(html, item) → dict matching the output schema

Subclasses may override:
  - list_urls         → one or more paginated/listing URLs to seed from
  - deduplicate(items)→ collapse duplicates before fetching detail pages
  - run()             → orchestrate multiple datasets (default: one pass)

Output schema (all fields optional except name):
  name, type, url, url_plays,
  street, zip_code, city, country, lat, lon,
  description, founded_year, closed_year,
  venues:      [{venue_name: seat_count}, ...]
  cast:        [{person_name: role}, ...]
  memberships: [{person_name: role}, ...]
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

import aiohttp
from selectolax.parser import HTMLParser, Node
from tqdm.asyncio import tqdm

DATE = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
EMPTY = (None, "", [], {})


# ── HTML helpers (available to all subclasses) ────────────────────────────────


def node_text(node: Node | None) -> str | None:
    """Return stripped text of a selectolax node, or None."""
    return node.text(strip=True) if node else None


def section_after_heading(tree: HTMLParser, tag: str, heading: str) -> Node | None:
    """Return the first <div> sibling after a heading tag whose text matches."""
    for h in tree.css(tag):
        if h.text(strip=True) == heading:
            sib = h.next
            while sib and sib.tag != "div":
                sib = sib.next
            return sib
    return None


def br_lines(node: Node) -> list[str]:
    """Split a node's inner HTML on <br> tags and return clean text lines."""
    lines = [re.sub(r"<.*?>", "", part).strip() for part in node.html.split("<br>")]
    return [l for l in lines if l]


def parse_zip_city(line: str) -> tuple[str | None, str | None]:
    """Split '12345 Berlin' into ('12345', 'Berlin'). Returns (None, None) on no match."""
    if m := re.match(r"(\d+)\s+(.*)", line):
        return m.group(1), m.group(2)
    return None, None


def parse_date_dmy(text: str) -> str | None:
    """Extract DD.MM.YYYY date string, return as ISO YYYY-MM-DD or None."""
    if m := re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text):
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None


def parse_duration_minutes(text: str) -> int | None:
    """Extract duration in minutes from strings like: 'Dauer: 1 Std. 45 Min.' or 'Dauer: 1h 45' or 'Dauer: ca. 1h 45'."""
    hours = 0
    minutes = 0
    if m := re.search(r"(\d+)\s*(?:Std\.?|h)\b", text):
        hours = int(m.group(1))
    if (m := re.search(r"(\d+)\s*(?:Min\.?|m)\b", text)) or (m := re.search(r"(?:Std\.?|h)\s*(\d+)\b", text)):
        minutes = int(m.group(1))
    total = hours * 60 + minutes
    return total or None


# ── Person / cast helpers (available to all subclasses) ──────────────────────

# Roles that map to non-actor person types
_ROLE_TYPE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Regie|Inszenierung", re.IGNORECASE), "director"),
    (re.compile(r"Bühnenbild|Ausstattung", re.IGNORECASE), "set_designer"),
    (re.compile(r"Kostüm", re.IGNORECASE), "costume_designer"),
    (re.compile(r"Musik|Komposition", re.IGNORECASE), "composer"),
    (re.compile(r"Choreograph|Tanz", re.IGNORECASE), "choreographer"),
    (re.compile(r"Dramaturgi", re.IGNORECASE), "dramaturg"),
    (re.compile(r"Licht", re.IGNORECASE), "lighting_designer"),
    (re.compile(r"Video|Medien", re.IGNORECASE), "video_designer"),
    (re.compile(r"Text|Buch|Übersetz|Bearb", re.IGNORECASE), "author"),
]

# Prefixes/suffixes to strip when normalising person names
_NAME_NOISE = re.compile(
    r"\b(Herr|Frau|Dr\.?|Prof\.?)\b\.?|\s*\(.*?\)|\s*\(?\bStaats?schauspieler\b\)?",
    re.IGNORECASE,
)


def infer_person_type(role: str) -> str:
    """Map a role label to a person type string; defaults to 'actor'."""
    for pattern, kind in _ROLE_TYPE_MAP:
        if pattern.search(role):
            return kind
    return "actor"


def normalize_person_name(name: str) -> str:
    """Strip honorifics/noise and normalise whitespace."""
    return re.sub(r"\s{2,}", " ", _NAME_NOISE.sub("", name)).strip()


def extract_author_info(line: str) -> tuple[str | None, str]:
    """Parse an author line like 'Musik von Hans Werner Henze'.
    Returns (name, person_type).
    """
    if not line:
        return None, "author"
    # strip leading role phrase "X von " or "X: "
    name = re.sub(r"^[^:]+:\s*|^.*?\bvon\b\s*", "", line, flags=re.IGNORECASE).strip()
    kind = infer_person_type(line)
    return (name or None), kind


# ── Dict helpers ──────────────────────────────────────────────────────────────


def nested_get(data: dict, *path: str) -> str | None:
    """Safely retrieve a value from a nested dict via a sequence of keys."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def merge_prefer_first(*sources: dict) -> dict:
    """Merge dicts left-to-right; the first non-empty value wins for each key."""
    result: dict = {}
    for source in sources:
        for k, v in source.items():
            if result.get(k) in EMPTY:
                result[k] = v
    return result


def completeness(record: dict) -> int:
    """Count how many fields have a non-empty value."""
    return sum(v not in EMPTY for v in record.values())


# ── Base class ────────────────────────────────────────────────────────────────


class TheaterScraper(ABC):
    """Async base scraper.  Subclasses implement parse_list / parse_detail.

    Usage:
        scraper = MyTheaterScraper()
        asyncio.run(scraper.run())
    """

    # ── subclass configuration ────────────────────────────────────────────────

    #: Human-readable label used in progress bars and log output.
    label: str = "items"

    #: Folder name under data/staged/ for output files.
    source_id: str = "unnamed"

    #: Seed URLs for the list/overview pages.
    list_urls: list[str] = []

    #: Max simultaneous open connections.
    concurrency: int = 50

    #: Per-request timeout in seconds.
    timeout_seconds: int = 30

    # ── abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def parse_list(self, html: str) -> list[dict]:
        """Parse one list/overview page.
        Return a list of stub dicts — must include "url" for detail fetching.
        Return an empty list to signal end of pagination.
        """

    @abstractmethod
    def parse_detail(self, html: str, item: dict) -> dict:
        """Parse one detail page.
        Receives the raw HTML and the stub dict from parse_list.
        Return a fully populated record dict.
        """

    # ── optional hooks ────────────────────────────────────────────────────────

    def deduplicate(self, items: list[dict]) -> list[dict]:
        """Override to collapse duplicates before detail fetching.
        Default: keep all items as-is.
        """
        return items

    #: Set to False for sources that serve all results on a single page.
    paginated: bool = True

    async def fetch_list(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
    ) -> list[dict]:
        """Collect stubs from all list_urls.
        If paginated=True: fetches url, url/2, url/3 … stopping when
          parse_list returns empty OR the raw HTML repeats (site ignores page number).
        If paginated=False: fetches each list_url exactly once.
        """
        items: list[dict] = []
        for base_url in self.list_urls:
            if not self.paginated:
                html = await self._fetch(session, semaphore, base_url)
                if html:
                    items.extend(self.parse_list(html))
                continue

            seen_html: set[int] = set()
            page = 1
            while True:
                url = base_url if page == 1 else f"{base_url}/{page}"
                html = await self._fetch(session, semaphore, url)
                if not html:
                    break
                fingerprint = hash(html)
                if fingerprint in seen_html:
                    break  # site returned a page we've seen — stop
                seen_html.add(fingerprint)
                batch = self.parse_list(html)
                if not batch:
                    break
                items.extend(batch)
                page += 1
        return items

    async def run(self) -> list[dict]:
        """Full pipeline: list → deduplicate → detail → save.
        Override to orchestrate multiple datasets.
        """
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        connector = aiohttp.TCPConnector(limit=self.concurrency, ttl_dns_cache=300)
        semaphore = asyncio.Semaphore(self.concurrency)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            print(f"[{self.source_id}] Fetching list pages…")
            items = await self.fetch_list(session, semaphore)
            items = self.deduplicate(items)
            print(f"  {len(items)} {self.label} to process")

            results = await self._fetch_details(session, semaphore, items)

        out = Path(f"data/staged/{self.source_id}/{DATE}_{self.label}.json")
        self._save(out, results)
        print(f"  → Saved {len(results)} {self.label} to {out}")
        return results

    # ── shared infrastructure (not meant to be overridden) ───────────────────

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        url: str,
        **kwargs: dict,
    ) -> str | None:
        try:
            async with semaphore, session.get(url, **kwargs) as r:
                if r.status == 404:
                    return None
                r.raise_for_status()
                return await r.text()
        except TimeoutError:
            print(f"  ✗ timeout: {url}")
            return None
        except aiohttp.ClientError as e:
            print(f"  ✗ error fetching {url}: {e}")
            return None

    async def _fetch_details(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        items: list[dict],
    ) -> list[dict]:
        async def process(item: dict) -> dict:
            html = await self._fetch(session, semaphore, item["url"])
            if not html:
                return item
            detail = await asyncio.to_thread(self.parse_detail, html, item)
            return merge_prefer_first(item, detail)

        tasks = [process(item) for item in items]
        return [await t for t in tqdm.as_completed(tasks, total=len(tasks), desc=self.label)]

    @staticmethod
    def _save(path: Path, data: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
