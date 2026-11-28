"""Validated, database-independent contract for review-first theatre snapshots."""

import datetime as dt
import json
from pathlib import Path
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from backend.date_precision import DatePrecision, normalize_dates, parse_date
from backend.models import PerformanceDate
from backend.orif import as_staged

type NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"\S")]


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Expected an absolute HTTP(S) URL without credentials")
    return value


type HttpURL = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^https?://[^\s/?#]+[^\s]*$"),
    Field(json_schema_extra={"format": "uri"}),
    AfterValidator(_http_url),
]
type NonNegativeInt = Annotated[StrictInt, Field(ge=0)]

_YEAR = r"(?:000[1-9]|00[1-9]\d|0[1-9]\d{2}|[1-9]\d{3})"
_LEAP_YEAR = r"(?:\d{2}(?:0[48]|[2468][048]|[13579][26])|(?:0[48]|[2468][048]|[13579][26])00)"
_DATE = (
    rf"(?:{_YEAR}-(?:(?:01|03|05|07|08|10|12)-(?:0[1-9]|[12]\d|3[01])"
    rf"|(?:04|06|09|11)-(?:0[1-9]|[12]\d|30)|02-(?:0[1-9]|1\d|2[0-8]))"
    rf"|{_LEAP_YEAR}-02-29)"
)
type DomainDate = Annotated[
    dt.datetime | None,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "pattern": rf"^{_YEAR}(?:-(?:0[1-9]|1[0-2]))?$"},
                {"type": "string", "format": "date"},
                {
                    "type": "string",
                    "pattern": (
                        rf"^{_DATE}[Tt ](?:[01]\d|2[0-3])(?::[0-5]\d(?::[0-5]\d(?:\.\d{{1,6}})?)?)?"
                        r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$"
                    ),
                },
                {"type": "null"},
            ]
        }
    ),
]


class StagedDomainModel(BaseModel):
    """Validate staged date/precision pairs and accept pre-ORIF field names."""

    date_fields: ClassVar[tuple[str, ...]] = ()
    legacy_fields: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def normalize_domain_dates(cls, value: object) -> object:
        """Rename legacy keys, then store normalized interval starts with their precision."""
        if isinstance(value, dict) and cls.legacy_fields.keys() & value.keys():
            value = dict(value)
            for old, new in cls.legacy_fields.items():
                if old in value:
                    legacy = value.pop(old)
                    if value.get(new, legacy) != legacy:
                        raise ValueError(f"Conflicting {old!r} and {new!r}")
                    value[new] = legacy
        return normalize_dates(value, cls.date_fields)


class StagedMembership(StagedDomainModel):
    """Person fields from Person, plus the source's production credit.

    A scalar type remains accepted for existing Karlsruhe snapshots; new
    producers may use Person.type's list form. external_id is source metadata,
    not a database ID; database links are resolved by reconciliation after review.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_fields: ClassVar[tuple[str, ...]] = ("born", "died")
    legacy_fields: ClassVar[dict[str, str]] = {"role": "role_name", "character": "character_name"}

    name: NonEmptyText
    external_id: NonEmptyText | None = None
    type: NonEmptyText | Annotated[list[NonEmptyText], Field(min_length=1)] | None = None
    url: HttpURL | None = None
    urls: list[HttpURL] = Field(default_factory=list)
    description: str | None = None
    born: DomainDate = None
    born_precision: DatePrecision | None = None
    died: DomainDate = None
    died_precision: DatePrecision | None = None
    nationality: str | None = None
    role_name: str | None = None
    character_name: str | None = None


class StagedTextTitle(BaseModel):
    """An attested title variant, not evidence for a separate translated text."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: NonEmptyText
    language: Annotated[str, Field(pattern=r"^[a-z]{2}$")] | None = None


class StagedText(StagedDomainModel):
    """Normalized text metadata shared by theatre snapshots and publisher imports."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_fields: ClassVar[tuple[str, ...]] = ("published",)

    title: NonEmptyText
    original_title: NonEmptyText | None = None
    original_subtitle: NonEmptyText | None = None
    original_language: Annotated[str, Field(pattern=r"^(?:[a-z]{2}|mul)$")] | None = None
    authors: list[NonEmptyText] = Field(default_factory=list)
    translators: list[NonEmptyText] = Field(default_factory=list)
    composers: list[NonEmptyText] = Field(default_factory=list)
    lyricists: list[NonEmptyText] = Field(default_factory=list)
    adapters: list[NonEmptyText] = Field(default_factory=list)
    editors: list[NonEmptyText] = Field(default_factory=list)
    titles: list[StagedTextTitle] = Field(default_factory=list)
    based_on: StagedText | NonEmptyText | None = None
    language: Annotated[str, Field(pattern=r"^(?:[a-z]{2}|mul)$")] | None = None
    genre: Literal["play", "musical", "opera", "novel", "film", "other"] | None = None
    published: DomainDate = None
    published_precision: DatePrecision | None = None
    description: str | None = None
    description_en: str | None = None
    subtitle: NonEmptyText | None = None
    subtitle_en: NonEmptyText | None = None
    keywords: list[NonEmptyText] = Field(default_factory=list)
    genre_labels: list[NonEmptyText] = Field(default_factory=list)
    women_min: NonNegativeInt | None = None
    women_max: NonNegativeInt | None = None
    men_min: NonNegativeInt | None = None
    men_max: NonNegativeInt | None = None
    total_cast_min: NonNegativeInt | None = None
    total_cast_max: NonNegativeInt | None = None
    cast_note: NonEmptyText | None = None
    cast_note_en: NonEmptyText | None = None
    suggested_min_age: NonNegativeInt | None = None
    is_free_for_school_amateur_play: StrictBool | None = None
    is_novel_or_film_material: StrictBool | None = None
    released_for_udse: StrictBool | None = None
    has_role_book: StrictBool | None = None
    foreign_rights: StrictBool | None = None
    school_amateur_note: NonEmptyText | None = None
    foreign_rights_note: NonEmptyText | None = None
    translations_note: NonEmptyText | None = None
    translations_note_en: NonEmptyText | None = None

    @model_validator(mode="after")
    def validate_cast_ranges(self) -> StagedText:
        """Keep independent cast ranges; total need not equal gender subtotals."""
        for group in ("women", "men", "total_cast"):
            lower, upper = getattr(self, f"{group}_min"), getattr(self, f"{group}_max")
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"{group}_min must not exceed {group}_max")
        return self


class StagedProduction(StagedDomainModel):
    """Source-facing Production fields; title maps to Production.name and Text.title."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_fields: ClassVar[tuple[str, ...]] = ("premiere",)
    legacy_fields: ClassVar[dict[str, str]] = {"age_rating": "suggested_min_age"}

    title: NonEmptyText
    url: HttpURL
    description: str | None = None
    premiere: DomainDate = None
    premiere_precision: DatePrecision | None = None
    performance_dates: list[
        Annotated[
            PerformanceDate,
            WithJsonSchema({"type": "string", "pattern": rf"^{_DATE}(?:T(?:[01]\d|2[0-3]):[0-5]\d)?$"}),
        ]
    ] = Field(default_factory=list)
    premiere_year: Annotated[StrictInt, Field(ge=1, le=9999)] | None = Field(default=None, exclude=True)
    duration: NonNegativeInt | None = None
    suggested_min_age: NonNegativeInt | None = None
    tags: list[NonEmptyText] = Field(default_factory=list)
    critics: list[NonEmptyText] = Field(default_factory=list)
    author: str | None = None
    text: StagedText | None = None
    memberships: list[StagedMembership] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def legacy_year(cls, value: object) -> object:
        """Accept existing snapshots with a separate premiere year."""
        if not isinstance(value, dict) or value.get("premiere_year") is None:
            return value
        data = dict(value)
        year = data["premiere_year"]
        if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
            raise ValueError(f"Invalid premiere year: {year!r}")
        if data.get("premiere"):
            if parse_date(data["premiere"])[0].year != year:
                raise ValueError("Conflicting premiere date/year")
        else:
            data["premiere"] = f"{year:04d}"
        return data


class StagedProductions(RootModel[list[StagedProduction]]):
    """Non-empty production array accepted by the staging importer."""

    root: Annotated[list[StagedProduction], Field(min_length=1)]


def validate_records(records: object) -> list[dict]:
    """Validate the entire snapshot (staged list or ORIF document) before any source or review cases are written."""
    return StagedProductions.model_validate(as_staged(records)).model_dump(mode="json")


def staging_schema() -> dict:
    """Generate the published schema from the same BaseModels used at runtime."""
    schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **StagedProductions.model_json_schema(),
    }
    for model, fields in (
        ("StagedProduction", ("premiere",)),
        ("StagedMembership", ("born", "died")),
        ("StagedText", ("published",)),
    ):
        definitions = schema["$defs"][model]
        definitions["allOf"] = []
        for field in fields:
            for precision, suffix in (
                ("year", r"^\d{4}"),
                ("month", r"^\d{4}-\d{2}"),
                ("day", r"^\d{4}-\d{2}-\d{2}"),
                ("minute", r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}"),
            ):
                then: dict = {"properties": {field: {"type": "string", "pattern": suffix}}, "required": [field]}
                if field == "premiere" and precision == "year":
                    then = {
                        "anyOf": [
                            then,
                            {"properties": {"premiere_year": {"type": "integer"}}, "required": ["premiere_year"]},
                        ]
                    }
                definitions["allOf"].append(
                    {
                        "if": {
                            "properties": {f"{field}_precision": {"const": precision}},
                            "required": [f"{field}_precision"],
                        },
                        "then": then,
                    }
                )
    return schema


def write_schema(path: Path) -> None:
    """Write a reproducible schema for editors and external producers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(staging_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
