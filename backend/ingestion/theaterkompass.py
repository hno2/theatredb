"""backend/ingestion/scrapers/theaterkompass.py.

Scrapes two datasets from theaterkompass.de:
  1. Organizations  — JSON feed + HTML detail pages, merged
  2. Critiques      — paginated HTML overview + full article text

Run:
    python -m backend.ingestion.scrapers.theaterkompass
"""

import asyncio
import re
from urllib.parse import urljoin

import aiohttp
from selectolax.parser import HTMLParser

from backend.ingestion.base_scraper import (
    TheaterScraper,
    br_lines,
    completeness,
    nested_get,
    node_text,
    parse_zip_city,
    section_after_heading,
)

BASE_URL = "https://theaterkompass.de"

# ── Field mapping: output key → path in JSON feed ────────────────────────────

ORG_JSON_MAP: dict[str, tuple[str, ...]] = {
    "name": ("name",),
    "lat": ("lat",),
    "lon": ("lng",),
    "street": ("address", "street"),
    "zip_code": ("address", "zip"),
    "city": ("address", "city"),
    "country": ("address", "country"),
    "founded_year": ("foundedYear",),
    "closed_year": ("dissolvedYears",),
}


# ── Organizations ─────────────────────────────────────────────────────────────


class OrganizationScraper(TheaterScraper):
    label = "organizations"
    source_id = "theaterkompass.de"

    def parse_list(self, html: str) -> list[dict]:
        # unused — overridden by fetch_list below
        return []

    def parse_detail(self, html: str, item: dict) -> dict:
        """Parse the detail page of an organization."""
        tree = HTMLParser(html)

        name = node_text(tree.css_first("h1"))
        description = node_text(tree.css_first('[class*="prose"] p'))

        # "Typ:" and "Gegründet:" are <strong> labels inside a <p>.
        # The value is the text that follows the </strong> tag within that <p>,
        # before any other tag starts — extract via the parent's raw HTML.
        def label_value(label: str) -> str | None:
            for strong in tree.css("strong"):
                if strong.text(strip=True).startswith(label):
                    parent_html = strong.parent.html if strong.parent else ""
                    # grab only the text immediately after </strong>, stop at next tag
                    if m := re.search(r"</strong>\s*([^<]+)", parent_html):
                        return m.group(1).strip() or None
            return None

        theater_type = label_value("Typ:")
        founded = label_value("Gegründet:")
        rechtsform = label_value("Rechtsform:")  # e.g. "gemeinnütziger Verein"
        website = label_value("Website:")
        # external website link
        url_node = tree.css_first('a[href*="http"]:not([href*="theaterkompass"])')

        # address
        street = zip_code = city = country = None
        addr_div = section_after_heading(tree, "h2", "Adressen")
        if addr_div:
            lines = br_lines(addr_div)
            street = lines[0] if len(lines) > 0 else None
            country = lines[2] if len(lines) > 2 else None
            if len(lines) > 1:
                zip_code, city = parse_zip_city(lines[1])

        # venues with seat count
        venues = []
        for h3 in tree.css("h3"):
            sib = h3.next
            while sib:
                t = sib.text(strip=True) if hasattr(sib, "text") else ""
                if m := re.search(r"Gesamtkapazität:\s*(\d+)", t):
                    venues.append({h3.text(strip=True): m.group(1)})
                    break
                sib = sib.next

        # memberships (person → role)
        memberships = []
        people_div = section_after_heading(tree, "h2", "Personen")
        if people_div:
            for strong in people_div.css("strong"):
                person = strong.text(strip=True)
                role_node = strong.next
                while role_node and role_node.tag not in ("span", "a"):
                    role_node = role_node.next
                role = node_text(role_node)
                if person and role:
                    memberships.append({person: role})

        return {
            "name": name,
            "type": theater_type,
            "url": url_node.attributes.get("href") if url_node else None,
            "url_plays": None,
            "street": street,
            "zip_code": zip_code,
            "city": city,
            "country": country,
            "lat": None,
            "lon": None,
            "description": description,
            "founded_year": int(founded) if founded else None,
            "closed_year": None,
            "rechtsform": rechtsform,
            "website": website,
            "venues": venues,
            "memberships": memberships,
        }

    # ── custom list fetching: JSON feed + per-org detail URLs ─────────────────

    async def fetch_list(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> list[dict]:
        """Fetch the list of organizations from the JSON feed."""
        async with session.get(f"{BASE_URL}/?type=1710883200") as r:
            r.raise_for_status()
            data = await r.json()

        # deduplicate by uid keeping most complete record
        unique: dict[int, dict] = {}
        for org in data:
            uid = org["uid"]
            if uid not in unique or completeness(org) > completeness(unique[uid]):
                unique[uid] = org

        print(f"  {len(unique)} unique organizations (from {len(data)} total)")

        # map JSON fields → output schema and attach detail URL
        stubs = []
        for org in unique.values():
            stub = {target: nested_get(org, *path) for target, path in ORG_JSON_MAP.items()}
            stub["url"] = f"{BASE_URL}/theaterverzeichnisse/organisation/{org['uid']}"
            stubs.append(stub)

        return stubs


# ── Critiques ─────────────────────────────────────────────────────────────────


class CritiqueScraper(TheaterScraper):
    """Scrape critique articles from theaterkompass.de."""

    label = "critiques"
    source_id = "theaterkompass.de"
    list_urls = [f"{BASE_URL}/kritiken/schauspiel"]

    def parse_list(self, html: str) -> list[dict]:
        """Parse the list of critiques from the HTML."""
        tree = HTMLParser(html)
        items = []
        for node in tree.css("article[data-href]"):
            title = node.css_first("h3 a")
            author = node.css_first('a[href*="author:"]')
            date = node.css_first("time")
            items.append(
                {
                    "url": urljoin(BASE_URL, node.attributes["data-href"]),
                    "title": node_text(title),
                    "author": node_text(author),
                    "published_date": date.attributes.get("datetime") if date else None,
                }
            )
        return items

    def parse_detail(self, html: str, item: dict) -> dict:
        """Extract full article text from critique detail page."""
        tree = HTMLParser(html)
        article = tree.css_first("article")
        return {"text": article.text(separator="\n", strip=True) if article else None}


# ── Entry point ───────────────────────────────────────────────────────────────


async def main() -> None:
    """Run all scrapers and save results to data/staged/theaterkompass.de/."""
    await OrganizationScraper().run()
    await CritiqueScraper().run()


if __name__ == "__main__":
    asyncio.run(main())
