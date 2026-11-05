"""Scrapes play listings from Badisches Staatstheater Karlsruhe.
  https://www.staatstheater-karlsruhe.de/programm/schauspiel/

Each play record includes title, description, premiere, duration,
age rating, author, and full cast with roles.

Run:
    python -m backend.ingestion.scrapers.karlsruhe
    python -m backend.ingestion.scrapers.karlsruhe --section oper
    python -m backend.ingestion.scrapers.karlsruhe --max 10
"""

import argparse
import asyncio
import html as html_lib
import re
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from selectolax.parser import HTMLParser
from tqdm.asyncio import tqdm

from backend.ingestion.base_scraper import (
    DATE,
    TheaterScraper,
    extract_author_info,
    infer_person_type,
    node_text,
    normalize_person_name,
    parse_date_dmy,
    parse_duration_minutes,
)

BASE_URL = "https://www.staatstheater-karlsruhe.de"

# Paragraph-level patterns that signal metadata, not description text
_META_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^Deutsch von\b",
        r"^Ab\s+\d+\s+Jahren\b",
        r"^Dauer:\b",
        r"^Premiere\b",
        r"^Karlsruher Premiere\b",
        r"^Inhaltliche Hinweise\b",
        r"^Übernahme vom\b",
    ]
]

_BIO_TRAILING_NOISE = re.compile(
    r"\s*Möchten Sie sich auf die Warteliste setzen lassen\?.*$",
    re.IGNORECASE | re.DOTALL,
)


class KarlsruheScraper(TheaterScraper):
    label = "productions"
    source_id = "staatstheater-karlsruhe.de"

    paginated = False

    def __init__(self, section: str = "schauspiel", max_productions: int | None = None):
        self.list_urls = [f"{BASE_URL}/programm/{section}/"]
        self.max_productions = max_productions
        self._person_desc_cache: dict[str, str | None] = {}

    # ── list page ─────────────────────────────────────────────────────────────

    def parse_list(self, html: str) -> list[dict]:
        tree = HTMLParser(html)
        links = []
        for a in tree.css("h3.recife a"):
            href = a.attributes.get("href", "")
            if "programm/info/" in href:
                links.append(urljoin(BASE_URL, href))
        return [{"url": u} for u in sorted(dict.fromkeys(links))]

    def deduplicate(self, items: list[dict]) -> list[dict]:
        seen, unique = set(), []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        if self.max_productions is not None:
            unique = unique[: self.max_productions]
        return unique

    # ── detail page ───────────────────────────────────────────────────────────

    def parse_detail(self, html: str, item: dict) -> dict:
        tree = HTMLParser(html)

        title = node_text(tree.css_first("h1.h2"))
        description = _extract_description(tree)

        # metadata: scan all <p> text for labelled values
        all_p_texts = [p.text(strip=True) for p in tree.css("p")]
        premiere_text = next((t for t in all_p_texts if "Premiere" in t), "")
        duration_text = next((t for t in all_p_texts if "Dauer:" in t), "")
        age_text = next((t for t in all_p_texts if "Jahren" in t), "")

        premiere = parse_date_dmy(premiere_text)
        duration = parse_duration_minutes(duration_text)
        age_rating = None
        if m := re.search(r"Ab\s+(\d+)\s+Jahren", age_text):
            age_rating = int(m.group(1))

        # author line e.g. "Musik von Hans Werner Henze"
        author_line = node_text(tree.css_first("div.col-md-12.col-lg-8 h2.h4"))
        author_name, author_type = extract_author_info(author_line or "")
        if not author_name and title:
            author_name = _extract_author_from_title(title)
            author_type = "author" if author_name else author_type
        if author_name:
            author_type = "author"

        # one combined output item: memberships (person + role + metadata)
        memberships: list[dict] = []
        seen_persons: dict[str, dict] = {}

        if author_name:
            p = {
                "name": author_name,
                "type": author_type,
                "url": None,
                "description": None,
                "role": "",
            }
            seen_persons[author_name] = p
            memberships.append(p)

        cast_rows = _rows_after_id(tree, "besetzung")
        current_role = ""
        for row in cast_rows:
            role_cell = node_text(row.css_first("td:nth-child(1)"))
            person_cell = node_text(row.css_first("td:nth-child(2)"))
            person_href = None
            if a := row.css_first("td:nth-child(2) a"):
                href = a.attributes.get("href", "")
                if "ensemble/id/" in href:
                    person_href = urljoin(BASE_URL, href)

            if role_cell:
                current_role = role_cell
            current_role = _normalize_role(current_role)
            effective_role = _normalize_role(current_role or role_cell or "")

            person_name = normalize_person_name(person_cell or "")
            if not person_name:
                continue
            self._add_or_update_membership(
                memberships=memberships,
                seen_persons=seen_persons,
                person_name=person_name,
                person_href=person_href,
                effective_role=effective_role,
            )

        return {
            "title": title,
            "description": description,
            "url": item["url"],
            "premiere": premiere,
            "duration": duration,
            "age_rating": age_rating,
            "author": author_name,
            "memberships": memberships,
        }

    def _add_or_update_membership(
        self,
        memberships: list[dict],
        seen_persons: dict[str, dict],
        person_name: str,
        person_href: str | None,
        effective_role: str,
    ) -> None:
        if person_name not in seen_persons:
            p = {
                "name": person_name,
                "type": infer_person_type(effective_role),
                "url": person_href,
                "description": None,
                "role": effective_role,
            }
            seen_persons[person_name] = p
            memberships.append(p)
            return

        if person_href and not seen_persons[person_name].get("url"):
            seen_persons[person_name]["url"] = person_href

        if effective_role:
            seen_persons[person_name]["role"] = effective_role

    async def run(self) -> list[dict]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        connector = aiohttp.TCPConnector(limit=self.concurrency, ttl_dns_cache=300)
        semaphore = asyncio.Semaphore(self.concurrency)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            print(f"[{self.source_id}] Fetching list pages…")
            items = await self.fetch_list(session, semaphore)
            items = self.deduplicate(items)
            print(f"  {len(items)} {self.label} to process")

            results = await self._fetch_details(session, semaphore, items)
            await self._enrich_person_descriptions(session, semaphore, results)

        path = Path(f"data/staged/{self.source_id}/{DATE}_{self.label}.json")
        self._save(path, results)
        print(f"  → Saved {len(results)} {self.label} to {path}")
        return results

    async def _enrich_person_descriptions(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        productions: list[dict],
    ) -> None:
        urls = {
            m.get("url")
            for production in productions
            for m in production.get("memberships", [])
            if isinstance(m, dict) and m.get("url") and "ensemble/id/" in m.get("url", "")
        }
        if not urls:
            return

        async def fetch_one(url: str) -> tuple[str, str | None]:
            if url in self._person_desc_cache:
                return url, self._person_desc_cache[url]
            html = await self._fetch(session, semaphore, url)
            description = _extract_person_description(html) if html else None
            self._person_desc_cache[url] = description
            return url, description

        tasks = [fetch_one(url) for url in sorted(urls)]
        for task in tqdm.as_completed(tasks, total=len(tasks), desc="person descriptions"):
            await task

        for production in productions:
            for membership in production.get("memberships", []):
                if not isinstance(membership, dict):
                    continue
                url = membership.get("url")
                if url:
                    membership["description"] = self._person_desc_cache.get(url)


# ── Private parse helpers ─────────────────────────────────────────────────────


def _rows_after_id(tree: HTMLParser, element_id: str) -> list:
    """Return all <tr> elements inside the first <div> that follows an element with the given id."""
    anchor = tree.css_first(f"#{element_id}")
    if not anchor:
        return []
    sib = anchor.next
    while sib:
        if sib.tag == "div":
            return sib.css("table tr")
        sib = sib.next
    return []


def _extract_description(tree: HTMLParser) -> str | None:
    """Collect <p> tags from the main content column.
    Stop at <hr> — everything after is venue/logistics noise.
    Skip short (<60 chars) and metadata paragraphs.
    """
    blocks = []
    for node in tree.css("div.col-md-12.col-lg-8 > *"):
        if node.tag == "hr":
            break
        if node.tag != "p":
            continue
        text = _html_to_markdown_text(node.html)
        if not text or len(text) < 60:
            continue
        meta_probe = re.sub(r"[*_`#>-]", "", text)
        if any(pat.search(meta_probe) for pat in _META_PATTERNS):
            continue
        text = re.sub(r"\s*Übernahme vom [^.]+\.?$", "", text, flags=re.IGNORECASE).strip()
        blocks.append(text)
    return "\n\n".join(blocks) or None


def _normalize_role(role_text: str) -> str:
    role = role_text.strip()
    return "" if role.lower() == "mit" else role


def _extract_author_from_title(title: str) -> str | None:
    clean = re.sub(r"\s+", " ", title).strip()
    if not clean:
        return None

    # Common forms in titles: "... von <name>", "... nach <name>"
    if m := re.search(r"\b(?:von|nach)\s+(.+)$", clean, flags=re.IGNORECASE):
        candidate = m.group(1).strip(' -:;,."')
        return candidate or None
    return None


def _extract_person_description(html: str) -> str | None:
    tree = HTMLParser(html)
    paragraphs: list[str] = []
    for p in tree.css("div.col-md-12.col-lg-8 p, article p, main p, p"):
        text = _html_to_markdown_text(p.html)
        if text and len(text) >= 80:
            paragraphs.append(text)
        if len(paragraphs) >= 2:
            break
    if not paragraphs:
        return None
    cleaned = _BIO_TRAILING_NOISE.sub("", "\n\n".join(paragraphs)).strip()
    return cleaned or None


def _html_to_markdown_text(fragment: str) -> str:
    text = fragment
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(strong|b)\b[^>]*>(.*?)<\s*/\s*\1\s*>", r"**\2**", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*(em|i)\b[^>]*>(.*?)<\s*/\s*\1\s*>", r"*\2*", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return text.strip()


# ── Entry point ───────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape Staatstheater Karlsruhe production listings.")
    p.add_argument("--section", default="schauspiel", help="Programme section, e.g. schauspiel, oper, ballett")
    p.add_argument("--max", type=int, default=None, help="Cap number of productions (for testing)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(KarlsruheScraper(section=args.section, max_productions=args.max).run())
