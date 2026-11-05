"""Validated, database-independent contract for review-first theatre snapshots."""

import datetime as dt
import json
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    StringConstraints,
    field_validator,
)

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


class StagedMembership(BaseModel):
    """Person fields from Person, plus the source's production credit.

    A scalar type remains accepted for existing Karlsruhe snapshots; new
    producers may use Person.type's list form. IDs and database links are
    deliberately excluded: reconciliation resolves them after review.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: NonEmptyText
    type: NonEmptyText | Annotated[list[NonEmptyText], Field(min_length=1)] | None = None
    url: HttpURL | None = None
    urls: list[HttpURL] = Field(default_factory=list)
    description: str | None = None
    born: dt.date | None = None
    died: dt.date | None = None
    nationality: str | None = None
    role: str | None = None
    character: str | None = None

    @field_validator("born", "died", mode="before")
    @classmethod
    def validate_date_input(cls, value: object) -> object:
        """Do not accept Unix timestamps or basic dates absent from the JSON Schema."""
        if value is not None and not (
            isinstance(value, dt.date) or (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        ):
            raise ValueError("Expected an ISO date (YYYY-MM-DD)")
        return value


class StagedProduction(BaseModel):
    """Source-facing Production fields; title maps to Production.name and Text.title."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: NonEmptyText
    url: HttpURL
    description: str | None = None
    premiere: dt.date | AwareDatetime | None = None
    premiere_year: Annotated[StrictInt, Field(ge=1, le=9999)] | None = None
    duration: NonNegativeInt | None = None
    age_rating: NonNegativeInt | None = None
    tags: list[NonEmptyText] = Field(default_factory=list)
    critics: list[NonEmptyText] = Field(default_factory=list)
    author: str | None = None
    memberships: list[StagedMembership] = Field(default_factory=list)

    @field_validator("premiere", mode="before")
    @classmethod
    def validate_premiere_input(cls, value: object) -> object:
        """Match JSON Schema's date/RFC3339 strings rather than coercing timestamps."""
        if isinstance(value, str):
            if not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}(?:[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2}))?",
                value,
            ):
                raise ValueError("Expected an ISO date or RFC3339 datetime with timezone")
        elif value is not None and not isinstance(value, dt.date):
            raise ValueError("Expected an ISO date or RFC3339 datetime with timezone")
        return value


class StagedProductions(RootModel[list[StagedProduction]]):
    """Non-empty production array accepted by the staging importer."""

    root: Annotated[list[StagedProduction], Field(min_length=1)]


def validate_records(records: object) -> list[dict]:
    """Validate the entire snapshot before any source or review cases are written."""
    return StagedProductions.model_validate(records).model_dump(mode="json")


def staging_schema() -> dict:
    """Generate the published schema from the same BaseModels used at runtime."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **StagedProductions.model_json_schema(),
    }


def write_schema(path: Path) -> None:
    """Write a reproducible schema for editors and external producers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(staging_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
