"""Typed, data-only crawler definitions bound to a Source."""

import uuid
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, model_validator

from backend.config import ENTITY_MODELS
from backend.ingestion.configurable import (
    ConfigModel,
    FieldRule,
    ListingRule,
    ScraperConfig,
    _validate_pattern,
    _validate_selector,
)
from backend.ingestion.staging import HttpURL, NonEmptyText
from backend.models import EntityType


class TheatreCrawl(ConfigModel):
    """Keep the existing YAML extraction contract and explicit theatre identity."""

    kind: Literal["theatre"] = "theatre"
    organization_id: uuid.UUID
    config: ScraperConfig


class RecordCrawl(ConfigModel):
    """CSS-based crawler for other sources; results enter generic reconciliation."""

    kind: Literal["records"] = "records"
    base_url: HttpURL
    listing: ListingRule
    entity_type: EntityType
    fields: Annotated[dict[str, FieldRule], Field(min_length=1)]
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30
    request_delay: Annotated[float, Field(ge=0)] = 1
    user_agent: NonEmptyText = "TheatreDB/0.1"

    @model_validator(mode="after")
    def validate_fields(self) -> RecordCrawl:
        """Reject unknown fields and broken discovery rules before queueing work."""
        model = ENTITY_MODELS[self.entity_type]
        allowed = set(model.model_fields) - {"id"}
        if unknown := set(self.fields) - allowed:
            raise ValueError(f"Unknown record fields: {sorted(unknown)}")
        # Foreign keys are deliberately not inferred from page text.
        if protected := {name for name in self.fields if name.endswith("_id")}:
            raise ValueError(f"Relationship fields require a dedicated adapter: {sorted(protected)}")
        label = "title" if self.entity_type == EntityType.TEXT else "name"
        if label not in self.fields or not self.fields[label].required:
            raise ValueError(f"{label} must have required: true")
        for selector in (self.listing.selector, self.listing.next_selector):
            if selector:
                _validate_selector(selector)
        for pattern in (self.listing.include, self.listing.exclude):
            if pattern:
                _validate_pattern(pattern)
        base = urlsplit(self.base_url)
        if any((urlsplit(url).scheme, urlsplit(url).netloc) != (base.scheme, base.netloc) for url in self.listing.urls):
            raise ValueError("Listing URLs must stay on the configured origin")
        return self


type CrawlDefinition = Annotated[TheatreCrawl | RecordCrawl, Field(discriminator="kind")]
DEFINITION = TypeAdapter(CrawlDefinition)
