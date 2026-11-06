"""Validated, database-independent contract for review-first theatre snapshots."""

import datetime as dt
import json
from pathlib import Path
from typing import Annotated, ClassVar
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)

from backend.date_precision import DatePrecision, normalize_dates, parse_date

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
    WithJsonSchema({
        "anyOf": [
            {"type": "string", "pattern": rf"^{_YEAR}(?:-(?:0[1-9]|1[0-2]))?$"},
            {"type": "string", "format": "date"},
            {"type": "string", "pattern": (
                rf"^{_DATE}[Tt ](?:[01]\d|2[0-3])(?::[0-5]\d(?::[0-5]\d(?:\.\d{{1,6}})?)?)?"
                r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$"
            )},
            {"type": "null"},
        ]
    }),
]


class StagedDomainModel(BaseModel):
    """Validate staged date/precision pairs."""

    date_fields: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_domain_dates(cls, value: object) -> object:
        """Store normalized interval starts with their precision."""
        return normalize_dates(value, cls.date_fields)


class StagedMembership(StagedDomainModel):
    """Person fields from Person, plus the source's production credit.

    A scalar type remains accepted for existing Karlsruhe snapshots; new
    producers may use Person.type's list form. IDs and database links are
    deliberately excluded: reconciliation resolves them after review.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_fields: ClassVar[tuple[str, ...]] = ("born", "died")

    name: NonEmptyText
    type: NonEmptyText | Annotated[list[NonEmptyText], Field(min_length=1)] | None = None
    url: HttpURL | None = None
    urls: list[HttpURL] = Field(default_factory=list)
    description: str | None = None
    born: DomainDate = None
    born_precision: DatePrecision | None = None
    died: DomainDate = None
    died_precision: DatePrecision | None = None
    nationality: str | None = None
    role: str | None = None
    character: str | None = None


class StagedProduction(StagedDomainModel):
    """Source-facing Production fields; title maps to Production.name and Text.title."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    date_fields: ClassVar[tuple[str, ...]] = ("premiere",)

    title: NonEmptyText
    url: HttpURL
    description: str | None = None
    premiere: DomainDate = None
    premiere_precision: DatePrecision | None = None
    premiere_year: Annotated[StrictInt, Field(ge=1, le=9999)] | None = Field(default=None, exclude=True)
    duration: NonNegativeInt | None = None
    age_rating: NonNegativeInt | None = None
    tags: list[NonEmptyText] = Field(default_factory=list)
    critics: list[NonEmptyText] = Field(default_factory=list)
    author: str | None = None
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
    """Validate the entire snapshot before any source or review cases are written."""
    return StagedProductions.model_validate(records).model_dump(mode="json")


def staging_schema() -> dict:
    """Generate the published schema from the same BaseModels used at runtime."""
    schema: dict = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **StagedProductions.model_json_schema(),
    }
    for model, fields in (("StagedProduction", ("premiere",)), ("StagedMembership", ("born", "died"))):
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
                definitions["allOf"].append({
                    "if": {"properties": {f"{field}_precision": {"const": precision}},
                           "required": [f"{field}_precision"]},
                    "then": then,
                })
    return schema


def write_schema(path: Path) -> None:
    """Write a reproducible schema for editors and external producers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(staging_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
