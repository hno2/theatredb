"""Download public Rowohlt text metadata, then import offline through publisher reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from markdownify import markdownify
from pydantic import BaseModel, Field, StrictInt
from selectolax.lexbor import LexborHTMLParser
from sqlmodel import Session
from tqdm import tqdm

from backend.ingestion.publishers import PublisherText, ingest_text_metadata

API = "https://website-api.metadaten-holtzbrinckverlage.de/rowohlt-theater/plays"
WEBSITE = "https://www.rowohlt-theaterverlag.de"
DEFAULT_OUTPUT = Path("data/staged/rowohlt/plays.json")
logger = logging.getLogger(__name__)


class CatalogPage(BaseModel):
    """Pagination contract returned by the public API."""

    data: list[dict]
    page: StrictInt = Field(ge=1)
    total_items: StrictInt = Field(ge=0, alias="totalItems")
    total_pages: StrictInt = Field(ge=0, alias="totalPages")
    next_page_url: str | None = Field(default=None, alias="nextPageUrl")


def _names(play: dict, field: str) -> list[str]:
    names = []
    for person in play.get(field, []):
        name = " ".join(part for part in (person.get("givenName"), person.get("surname")) if part).strip()
        if not name:
            raise ValueError(f"Rowohlt {field} credit has no name")
        names.append(name)
    return list(dict.fromkeys(names))


def _text(value: str | None) -> str | None:
    return LexborHTMLParser(value).text(separator=" ", strip=True) or None if value else None


def _markdown(value: str | None) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Expected HTML text for Rowohlt description or rights note")
    return markdownify(value).strip() or None if value else None


def normalize_play(payload: dict) -> PublisherText:
    """Map explicit credits and source-book evidence; retain all other API fields verbatim."""
    play = payload["data"]["play"]
    identifier = play["id"]
    if type(identifier) is not int or identifier < 1:
        raise ValueError("Rowohlt play ID must be a positive integer")
    slug = play["seoUrl"]
    if not isinstance(slug, str) or not re.fullmatch(r"[\w-]+", slug) or not slug.endswith(f"-{identifier}"):
        raise ValueError("Rowohlt play slug does not match its ID")
    authors, translators, editors = (_names(play, field) for field in ("authors", "translators", "editors"))
    editor_label = _text(play.get("freeTextBeforeEditors")) or ""
    is_adaptation = bool(editors) and bool(re.search(r"bearbeitet|bearbeitung|fassung", editor_label, re.IGNORECASE))
    source_title = _text(play.get("titleBookTemplate"))
    source_author = _text(play.get("authorBookTemplate"))
    original_title = _text(play.get("originalTitle"))
    # Book-template metadata alone does not prove a particular dramatic adaptation exists.
    is_material = play.get("isNovelOrFilmMaterial") is True and not is_adaptation
    metadata: dict = {
        "url": f"{WEBSITE}/theaterstueck/{slug}",
        "external_id": str(identifier),
        "title": _text(play["title"]),
        "subtitle": _text(play.get("subtitle")),
        "subtitle_en": _text(play.get("subtitleEnglish")),
        "description": _markdown(play.get("description")),
        "description_en": _markdown(play.get("descriptionEnglish")),
        "language": "de",
        "original_language": play.get("originalLanguage"),
        "genre": "other" if is_material else "play",
        "genre_labels": play.get("genreLabel") or play.get("genre") or [],
        "keywords": play.get("keywords") or [],
        "authors": authors,
        "translators": translators,
        "adapters": editors if is_adaptation else [],
        "editors": [] if is_adaptation else editors,
        "cast_note": _text(play.get("castNote")),
        "cast_note_en": _text(play.get("castNoteEnglish")),
        "school_amateur_note": _markdown(play.get("freeTextSchoolAmateurPlay")),
        "foreign_rights_note": _markdown(play.get("foreignRightsText")),
        "translations_note": _markdown(play.get("translations")),
        "translations_note_en": _markdown(play.get("translationsEnglish")),
        "source_metadata": payload,
    }
    for upstream, field in (
        ("isFreeForSchoolAmateurPlay", "is_free_for_school_amateur_play"),
        ("isNovelOrFilmMaterial", "is_novel_or_film_material"),
        ("releasedForUdse", "released_for_udse"),
        ("hasRoleBook", "has_role_book"),
        ("foreignRights", "foreign_rights"),
    ):
        metadata[field] = play.get(upstream)
    metadata.update(_cast_metadata(play))
    age = play.get("ageRating")
    if isinstance(age, str) and (match := re.fullmatch(r"\s*(\d+)\+\s*", age)):
        metadata["suggested_min_age"] = int(match[1])
    if title := _text(play.get("titleEnglish")):
        metadata["titles"] = [{"title": title, "language": "en"}]
    if (performances := _text(play.get("performances"))) and "Uraufführung" in performances:
        metadata["world_premiere"] = performances
    if is_adaptation:
        metadata["authors"] = editors
        metadata["based_on"] = {
            "title": source_title or original_title or metadata["title"],
            "authors": [source_author] if source_author else authors,
            "genre": "other" if source_title else "play",
        }
    elif original_title:
        metadata["original_title"] = original_title
        if original_subtitle := _text(play.get("originalSubtitle")):
            metadata["original_subtitle"] = original_subtitle
    return PublisherText.model_validate({key: value for key, value in metadata.items() if value is not None})


def _cast_metadata(play: dict) -> dict:
    fields = {}
    for upstream, group in (("women", "women"), ("men", "men"), ("totalActors", "total_cast")):
        lower, upper = play.get(f"{upstream}Min"), play.get(f"{upstream}Max")
        if type(lower) is int and type(upper) is int and 0 <= upper < lower:
            logger.warning(
                "Rowohlt play %s: contradictory %s range %s..%s; omitted, raw values retained",
                play["id"],
                upstream,
                lower,
                upper,
            )
            continue
        fields[f"{group}_min"], fields[f"{group}_max"] = lower, upper
    return fields


def _api_url(value: str) -> str:
    url = urljoin(API, value)
    parsed, expected = urlsplit(url), urlsplit(API)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.path != expected.path
        or parsed.fragment
    ):
        raise ValueError(f"Unsafe Rowohlt pagination URL: {value}")
    return url


async def _fetch(session: aiohttp.ClientSession, url: str) -> dict:
    await asyncio.sleep(0.2)
    async with session.get(url, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status != 200:
            raise ValueError(f"Unexpected Rowohlt HTTP status: {response.status}")
        return await response.json()


async def download(*, limit: int | None = None, play_id: int | None = None) -> list[dict]:
    """Fetch complete details with progress; fail without producing a partial snapshot."""
    if (limit is not None and limit < 1) or (play_id is not None and play_id < 1):
        raise ValueError("limit and play_id must be positive")
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": "theaterdb Rowohlt metadata downloader"}
    ) as session:
        with tqdm(total=1 if play_id is not None else None, desc="Rowohlt plays", unit="play") as progress:
            if play_id is not None:
                record = parse_detail(await _fetch(session, f"{API}/{play_id}"), play_id)
                progress.update()
                return [record.model_dump(mode="json", exclude_none=True)]
            return await _download_pages(session, limit, progress)


async def _download_pages(session: aiohttp.ClientSession, limit: int | None, progress: tqdm) -> list[dict]:
    records = []
    next_url: str | None = f"{API}?page=1&limit=100"
    visited, identifiers = set(), set()
    expected_page = 1
    total_items = None
    while next_url:
        url = _api_url(next_url)
        if url in visited:
            raise ValueError("Rowohlt pagination repeated a page")
        visited.add(url)
        page = CatalogPage.model_validate(await _fetch(session, url))
        if page.page != expected_page or not page.data:
            raise ValueError("Rowohlt pagination is incomplete or out of order")
        if total_items is not None and total_items != page.total_items:
            raise ValueError("Rowohlt catalog changed during download; retry")
        total_items = page.total_items
        if progress.total is None:
            progress.total = min(total_items, limit) if limit is not None else total_items
            progress.refresh()
        for item in page.data:
            identifier = _catalog_id(item, identifiers)
            identifiers.add(identifier)
            record = parse_detail(await _fetch(session, f"{API}/{identifier}"), identifier)
            records.append(record.model_dump(mode="json", exclude_none=True))
            progress.update()
            if limit is not None and len(records) >= limit:
                return records
        next_url = page.next_page_url
        expected_page += 1
    if len(records) != total_items:
        raise ValueError("Rowohlt catalog ended before all plays were downloaded")
    return records


def _catalog_id(item: dict, identifiers: set[int]) -> int:
    identifier = item["id"]
    if type(identifier) is not int or identifier < 1 or identifier in identifiers:
        raise ValueError("Rowohlt catalog contains an invalid or duplicate ID")
    return identifier


def parse_detail(payload: dict, identifier: int) -> PublisherText:
    """Normalize a detail response and verify the requested publisher identity."""
    record = normalize_play(payload)
    if record.external_id != str(identifier):
        raise ValueError("Rowohlt detail ID does not match request")
    return record


def main(argv: list[str] | None = None) -> None:
    """Download without database writes, or import a validated snapshot in one transaction."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    downloader = commands.add_parser("download")
    downloader.add_argument("--limit", type=int)
    downloader.add_argument("--play-id", type=int)
    downloader.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    importer = commands.add_parser("import")
    importer.add_argument("--json", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.command == "download":
        records = asyncio.run(download(limit=args.limit, play_id=args.play_id))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(args.output)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Downloaded {len(records)} texts to {args.output}")
    else:
        from backend.user_management import _get_engine, init_db  # ruff: ignore[import-outside-top-level]

        records = json.loads(args.json.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not records:
            raise ValueError("Expected a nonempty Rowohlt publisher snapshot")
        records = [
            PublisherText.model_validate(record).model_dump(mode="json", exclude_none=True) for record in records
        ]
        init_db()
        with Session(_get_engine()) as session:
            with tqdm(total=len(records), desc="Rowohlt import", unit="observation") as progress:
                results = ingest_text_metadata(session, "Rowohlt", records, progress=progress)
            session.commit()
            pending = sum(getattr(result, "status", None) == "pending" for result in results)
        print(f"Imported {len(results)} observations; {pending} awaiting review")


if __name__ == "__main__":
    main()
