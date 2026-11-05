"""Declarative CSS extraction and bounded HTTP scraping for theatre websites."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

import aiohttp
import yaml
from bs4 import BeautifulSoup, NavigableString
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node
from selectolax.lexbor import SelectolaxError
from tqdm import tqdm

from backend.ingestion.base_scraper import (
    extract_author_info,
    infer_person_type,
    normalize_person_name,
    parse_date_dmy,
    parse_duration_minutes,
)
from backend.ingestion.staging import HttpURL, NonEmptyText, StagedMembership, StagedProduction, validate_records

CONFIG_DIR = Path(__file__).with_name("configs")
type PageKind = Literal["production", "person"]
type PositiveInt = Annotated[int, Field(strict=True, gt=0)]


def _validate_selector(selector: str) -> None:
    try:
        HTMLParser("<html></html>").css(selector)
    except SelectolaxError as error:
        raise ValueError(f"Invalid CSS selector {selector!r}: {error}") from error


def _validate_pattern(pattern: str | None) -> None:
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.PatternError as error:
            raise ValueError(f"Invalid regex {pattern!r}: {error}") from error


class ConfigModel(BaseModel):
    """Reject misspelled or unsupported configuration keys."""

    model_config = ConfigDict(extra="forbid")


class FieldRule(ConfigModel):
    """Extract the first matching value, or join matching blocks."""

    selector: NonEmptyText
    attribute: str | None = None
    match: str | None = None
    exclude: str | None = None
    extract: str | None = None
    strip: str | None = None
    join: str | None = None
    many: bool = False
    split: str | None = None
    min_length: Annotated[int, Field(ge=0)] = 0
    limit: PositiveInt | None = None
    stop_tag: str | None = None
    required: bool = False
    format: Literal["text", "markdown", "date_de", "duration", "integer", "author", "url"] = "text"

    @model_validator(mode="after")
    def validate_expressions(self) -> FieldRule:
        """Fail on broken selectors and patterns before making requests."""
        _validate_selector(self.selector)
        for pattern in (self.match, self.exclude, self.extract, self.strip, self.split):
            _validate_pattern(pattern)
        if (self.many or self.split) and (self.join is not None or self.format not in {"text", "url"}):
            raise ValueError("many/split requires text or URL format and cannot be combined with join")
        return self


class CreditRule(ConfigModel):
    """Describe a repeated credit row with optional continuation roles."""

    selector: NonEmptyText
    name: FieldRule
    role: FieldRule
    url: FieldRule | None = None
    carry_role: bool = False
    infer_type: bool = True
    default_type: NonEmptyText = "actor"
    actor_labels: list[str] = Field(default_factory=list)


class ListingRule(ConfigModel):
    """Describe production links and optional bounded next-page navigation."""

    urls: Annotated[list[HttpURL], Field(min_length=1)]
    selector: NonEmptyText
    include: NonEmptyText
    exclude: str | None = None
    next_selector: str | None = None
    max_pages: PositiveInt = 20


class ScraperConfig(ConfigModel):
    """Venue identity, extraction rules and polite request settings."""

    source_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")]
    organization_name: NonEmptyText
    base_url: HttpURL
    listing: ListingRule
    production: dict[str, FieldRule]
    credits: list[CreditRule]
    person: dict[str, FieldRule]
    role_types: dict[str, NonEmptyText] = Field(default_factory=dict)
    timeout_seconds: Annotated[float, Field(gt=0)] = 30
    request_delay: Annotated[float, Field(ge=0)] = 1
    user_agent: NonEmptyText = "TheatreDB/0.1 (research; single-page checks supported)"

    @model_validator(mode="after")
    def validate_rules(self) -> ScraperConfig:
        """Allow only staging fields and compile selectors before network access."""
        allowed_production = set(StagedProduction.model_fields) - {"url", "memberships"}
        allowed_person = set(StagedMembership.model_fields) - {"role", "character", "url", "type"}
        for label, rules, allowed, required in (
            ("production", self.production, allowed_production, "title"),
            ("person", self.person, allowed_person, "name"),
        ):
            if unknown := set(rules) - allowed:
                raise ValueError(f"Unknown {label} fields: {sorted(unknown)}")
            if required not in rules or not rules[required].required:
                raise ValueError(f"{label}.{required} must have required: true")
        for selector in [
            self.listing.selector,
            *([self.listing.next_selector] if self.listing.next_selector else []),
            *(credit.selector for credit in self.credits),
        ]:
            _validate_selector(selector)
        for pattern in [self.listing.include, self.listing.exclude, *self.role_types]:
            _validate_pattern(pattern)
        return self


def load_config(path_or_name: str | Path) -> ScraperConfig:
    """Load a checked JSON/YAML file or one of the bundled venue names."""
    path = Path(path_or_name)
    if not path.exists() and not path.suffix:
        path = CONFIG_DIR / f"{path.name.casefold()}.yaml"
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    return ScraperConfig.model_validate(data)


def parse_date_de(text: str) -> str | None:
    """Parse numeric dates or German month names, including '16. Okt 26'."""
    if result := parse_date_dmy(text):
        return dt.date.fromisoformat(result).isoformat()
    months = {
        "jan": 1,
        "feb": 2,
        "mär": 3,
        "mar": 3,
        "apr": 4,
        "mai": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "okt": 10,
        "nov": 11,
        "dez": 12,
    }
    if match := re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\.?\s+(\d{4}|\d{2})\b", text):
        month = months.get(match[2][:3].casefold())
        if month is not None:
            year = int(match[3]) + (2000 if len(match[3]) == 2 else 0)
            return dt.date(year, month, int(match[1])).isoformat()
    return None


def _past_stop(node: Node, tag: str | None) -> bool:
    sibling = node.prev
    while tag and sibling is not None:
        if sibling.tag == tag:
            return True
        sibling = sibling.prev
    return False


def _clean_text(text: str) -> str:
    return text.translate(str.maketrans({"\u00ad": None, "\u200b": None, "\ufeff": None, "\u00a0": " "}))


def _markdown(node: Node, base_url: str) -> str:
    if node.html is None:
        raise ValueError("Cannot convert a node without HTML to Markdown")
    soup = BeautifulSoup(node.html, "html.parser")
    for element in soup.select("script, style, noscript, template"):
        element.decompose()
    for text in list(soup.strings):
        if isinstance(text, NavigableString):
            text.replace_with(_clean_text(str(text)))
    for heading in soup.select("h1, h2, h3, h4, h5, h6"):
        heading.name = "p"
    for link in soup.select("a[href]"):
        href = link.get("href")
        if isinstance(href, str):
            link["href"] = urljoin(base_url, _clean_text(href))
    return MarkdownConverter(bullets="-", autolinks=False).convert_soup(soup).strip()


def _extract_text(node: Node, rule: FieldRule, base_url: str) -> str | None:
    if rule.format == "markdown" and not rule.attribute:
        value = _markdown(node, base_url)
    else:
        value = (node.attributes.get(rule.attribute) or "") if rule.attribute else node.text(separator=" ", strip=True)
        value = " ".join(_clean_text(value).split())
    if rule.match and not re.search(rule.match, value, re.IGNORECASE):
        return None
    if rule.exclude and re.search(rule.exclude, value, re.IGNORECASE):
        return None
    if rule.extract:
        match = re.search(rule.extract, value, re.IGNORECASE)
        if not match:
            return None
        value = match[1] if match.lastindex else match[0]
    if rule.strip:
        flags = re.IGNORECASE | (re.DOTALL if rule.format == "markdown" else 0)
        value = re.sub(rule.strip, "", value, flags=flags).strip()
    return value if value and len(value) >= rule.min_length else None


def _convert(text: str | None, rule: FieldRule, base_url: str) -> str | int | None:
    result: str | int | None = text
    if text:
        match rule.format:
            case "date_de":
                result = parse_date_de(text)
            case "duration":
                result = parse_duration_minutes(text)
            case "integer":
                match = re.search(r"\d+", text)
                result = int(match[0]) if match else None
            case "author":
                result = extract_author_info(text)[0]
            case "url":
                result = urldefrag(urljoin(base_url, text))[0]
    if text and result is None:
        raise ValueError(f"Cannot parse {rule.format} from {rule.selector!r}: {text!r}")
    return result


def extract_field(root: HTMLParser | Node, rule: FieldRule, base_url: str) -> str | int | list[str] | None:
    """Apply a declarative extraction rule without evaluating config as code."""
    values = []
    for node in root.css(rule.selector):
        if _past_stop(node, rule.stop_tag):
            break
        value = _extract_text(node, rule, base_url)
        if value:
            values.extend(re.split(rule.split, value) if rule.split else [value])
        first_only = rule.join is None and not rule.many
        if values and (first_only or (rule.limit and len(values) >= rule.limit)):
            break
    if not values and rule.required:
        raise ValueError(f"Required selector {rule.selector!r} produced no value")
    if rule.many or rule.split:
        return [
            urldefrag(urljoin(base_url, value.strip()))[0] if rule.format == "url" else value.strip()
            for value in values
            if value.strip()
        ]
    return _convert((rule.join or "").join(values) if values else None, rule, base_url)


def _strings(value: str | int | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, int):
        raise TypeError("Credit names and profile URLs must be text")
    return value if isinstance(value, list) else [value]


class ConfigurableScraper:
    """Scrape only explicitly requested pages; fail without saving partial snapshots."""

    def __init__(self, config: ScraperConfig) -> None:
        """Bind validated rules and validate listing origins before any requests."""
        self.config = config
        self._last_request = 0.0
        for url in config.listing.urls:
            self.check_url(url)

    def check_url(self, url: str) -> str:
        """Keep all requests and redirects on the configured HTTP(S) origin."""
        url = urldefrag(url)[0]
        parts, base = urlsplit(url), urlsplit(self.config.base_url)
        if parts.scheme != base.scheme or parts.netloc != base.netloc or parts.username or parts.password:
            raise ValueError(f"URL outside configured origin: {url}")
        return url

    def _base(self, tree: HTMLParser, url: str) -> str:
        node = tree.css_first("base[href]")
        return self.check_url(urljoin(url, node.attributes["href"])) if node else url

    def parse_list(self, html: str, url: str) -> list[str]:
        """Return deduplicated production URLs, excluding profiles and overview links."""
        tree = HTMLParser(html)
        base = self._base(tree, url)
        urls = set()
        for node in tree.css(self.config.listing.selector):
            href = node.attributes.get("href")
            if not href:
                continue
            candidate = urldefrag(urljoin(base, href))[0]
            if not re.search(self.config.listing.include, candidate):
                continue
            if self.config.listing.exclude and re.search(self.config.listing.exclude, candidate):
                continue
            urls.add(self.check_url(candidate))
        return sorted(urls)

    def parse_page(self, html: str, url: str, kind: PageKind) -> dict:
        """Parse a production or standalone person page, without following any links."""
        self.check_url(url)
        tree = HTMLParser(html)
        base = self._base(tree, url)
        rules = self.config.production if kind == "production" else self.config.person
        record: dict = {field: extract_field(tree, rule, base) for field, rule in rules.items()}
        record["url"] = url
        if kind == "person":
            name = record["name"]
            if not isinstance(name, str):
                raise ValueError("A person name must be text")
            record["name"] = normalize_person_name(name)
            return StagedMembership.model_validate(record).model_dump(mode="json")
        record["memberships"] = self._memberships(tree, base)
        return StagedProduction.model_validate(record).model_dump(mode="json")

    def _memberships(self, tree: HTMLParser, base: str) -> list[dict]:
        members = []
        for group in self.config.credits:
            previous_role = ""
            for row in tree.css(group.selector):
                if not _clean_text(row.text(separator=" ", strip=True)).strip():
                    continue
                role = extract_field(row, group.role, base)
                if role is not None and not isinstance(role, str):
                    raise ValueError("A credit role must be text")
                role = role or (previous_role if group.carry_role else "")
                previous_role = role
                names = _strings(extract_field(row, group.name, base))
                if not names:
                    raise ValueError(f"Credit row has no person name: {group.selector}")
                urls = _strings(extract_field(row, group.url, base)) if group.url else []
                if urls and len(urls) != len(names):
                    raise ValueError(f"Ambiguous person/profile mapping in {group.selector}")
                person_type = self._person_type(group, role)
                role = "" if role.casefold() in {label.casefold() for label in group.actor_labels} else role
                for index, name in enumerate(names):
                    member = {
                        "name": normalize_person_name(name),
                        "role": role,
                        "type": person_type,
                        "url": self.check_url(urls[index]) if urls else None,
                    }
                    if member not in members:
                        members.append(member)
        return members

    def _person_type(self, group: CreditRule, role: str) -> str:
        if not group.infer_type:
            return group.default_type
        inferred = next(
            (kind for pattern, kind in self.config.role_types.items() if re.search(pattern, role, re.IGNORECASE)),
            infer_person_type(role),
        )
        return group.default_type if inferred == "actor" else inferred

    async def fetch(self, session: aiohttp.ClientSession, url: str) -> str:
        """Fetch one page with bounded redirects, a timeout and a delay between requests."""
        url = self.check_url(url)
        for _ in range(6):
            await asyncio.sleep(max(0, self.config.request_delay - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError(f"Redirect without Location: {url}")
                    url = self.check_url(urljoin(url, location))
                    continue
                response.raise_for_status()
                if "html" not in response.headers.get("Content-Type", "").casefold():
                    raise ValueError(f"Expected HTML at {url}")
                return await response.text()
        raise ValueError(f"Too many redirects: {url}")

    def session(self) -> aiohttp.ClientSession:
        """Create a session using this venue's timeout and user agent."""
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds),
            headers={"User-Agent": self.config.user_agent},
        )

    async def scrape(self, *, limit: int | None = None, enrich_people: bool = True) -> list[dict]:
        """Collect a bounded production set and optionally enrich its linked people once each."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive, or None for all productions")
        async with self.session() as session:
            urls = await self._discover(session, limit)
            if not urls:
                raise ValueError("No production links found; check listing selectors")
            with tqdm(urls, desc="Productions", unit="page") as progress:
                records = [self.parse_page(await self.fetch(session, url), url, "production") for url in progress]
            if enrich_people:
                profiles = {}
                person_urls = sorted({m["url"] for r in records for m in r["memberships"] if m["url"]})
                with tqdm(person_urls, desc="People", unit="page") as progress:
                    for url in progress:
                        profiles[url] = self.parse_page(await self.fetch(session, url), url, "person")
                for record in records:
                    for member in record["memberships"]:
                        profile = profiles.get(member["url"])
                        if profile:
                            for field in ("description", "born", "died", "nationality", "urls"):
                                member[field] = profile[field]
            return validate_records(records)

    async def _discover(self, session: aiohttp.ClientSession, limit: int | None) -> list[str]:
        with tqdm(desc="Listing pages", unit="page") as progress:
            return await self._discover_pages(session, limit, progress)

    async def _discover_pages(
        self,
        session: aiohttp.ClientSession,
        limit: int | None,
        progress: tqdm,
    ) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for seed in self.config.listing.urls:
            url: str | None = seed
            for _ in range(self.config.listing.max_pages):
                if url is None or url in seen:
                    break
                seen.add(url)
                html = await self.fetch(session, url)
                progress.update()
                urls = list(dict.fromkeys([*urls, *self.parse_list(html, url)]))
                if limit is not None and len(urls) >= limit:
                    return urls[:limit]
                tree = HTMLParser(html)
                next_node = (
                    tree.css_first(self.config.listing.next_selector) if self.config.listing.next_selector else None
                )
                href = next_node.attributes.get("href") if next_node else None
                url = self.check_url(urljoin(self._base(tree, url), href)) if href else None
            else:
                if url and url not in seen:
                    raise ValueError("Listing max_pages reached; refusing an incomplete snapshot")
        return urls
