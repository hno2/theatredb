"""Compatibility entry point using the shared Karlsruhe YAML configuration.

Prefer ``python -m backend.ingestion.scrape`` for page checks and other venues.
This entry point now defaults to one production; use --all for a full scrape.
"""

import argparse
import asyncio
import re
from pathlib import Path

from backend.ingestion.base_scraper import TheaterScraper
from backend.ingestion.configurable import ConfigurableScraper, load_config
from backend.ingestion.snapshots import save_scrape

BASE_URL = "https://www.staatstheater-karlsruhe.de"


class KarlsruheScraper(TheaterScraper):
    """Keep the former Python API while sharing extraction, enrichment and diffs."""

    label = "productions"
    source_id = "staatstheater-karlsruhe.de"
    paginated = False

    def __init__(
        self,
        section: str = "schauspiel",
        max_productions: int | None = 1,
        *,
        enrich_people: bool = True,
    ) -> None:
        """Select a section and explicit production bound (None means all)."""
        if not re.fullmatch(r"[a-z-]+", section):
            raise ValueError("section must contain lowercase letters and hyphens only")
        if max_productions is not None and max_productions < 1:
            raise ValueError("max_productions must be positive or None")
        config = load_config("karlsruhe")
        config.listing.urls = [f"{BASE_URL}/programm/{section}/"]
        self.parser = ConfigurableScraper(config)
        self.list_urls = config.listing.urls
        self.max_productions = max_productions
        self.enrich_people = enrich_people

    def parse_list(self, html: str) -> list[dict]:
        """Return production stubs using the shared list selectors."""
        return [{"url": url} for url in self.parser.parse_list(html, self.list_urls[0])]

    def parse_detail(self, html: str, item: dict) -> dict:
        """Parse one production without fetching linked people."""
        return self.parser.parse_page(html, item["url"], "production")

    def deduplicate(self, items: list[dict]) -> list[dict]:
        """Keep the first stub per URL and apply the configured limit."""
        seen, unique = set(), []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        return unique[: self.max_productions]

    async def run(self) -> list[dict]:
        """Scrape, compare with the matching previous scope and save a dated snapshot."""
        records = await self.parser.scrape(limit=self.max_productions, enrich_people=self.enrich_people)
        path, report = save_scrape(
            records,
            self.parser.config,
            Path("data/staged"),
            limit=self.max_productions,
            enrich_people=self.enrich_people,
        )
        print(report)
        print(f"Saved {len(records)} productions to {path}")
        return records


def parse_args() -> argparse.Namespace:
    """Parse the former section/max options with an explicit full-scrape opt-in."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", default="schauspiel")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--max", type=int, default=1)
    scope.add_argument("--all", action="store_true")
    parser.add_argument("--no-people", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        KarlsruheScraper(
            section=args.section,
            max_productions=None if args.all else args.max,
            enrich_people=not args.no_people,
        ).run()
    )
