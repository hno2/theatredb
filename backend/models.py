import datetime as dt
import re
import uuid
from collections import defaultdict
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from itertools import batched
from typing import Any, ClassVar, Protocol, cast

from pgvector.sqlalchemy import Vector
from pydantic import model_validator
from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, Index, Uuid, event, select, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Text as SQLText
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import InstanceState, relationship
from sqlalchemy.orm import Session as SASession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint, col

from backend.date_precision import DatePrecision, normalize_dates


class DomainModel(SQLModel):
    """Validate calendar-domain dates together with their precision."""

    date_fields: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_domain_dates(cls, value: object) -> object:
        """Normalize input dates to their interval starts."""
        return normalize_dates(value, cls.date_fields)


class User(SQLModel, table=True):
    """Represents a user in the system."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(index=True, sa_column_kwargs={"unique": True})
    display_name: str | None = None
    email: str | None = Field(default=None, index=True, sa_column_kwargs={"unique": True})
    password_hash: str | None = None
    is_active: bool = True
    is_admin: bool = False


class LibraryEntry(SQLModel, table=True):
    """One user's private wish, stars and review for exactly one text or production."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    text_id: uuid.UUID | None = Field(default=None, foreign_key="text.id", index=True)
    production_id: uuid.UUID | None = Field(default=None, foreign_key="production.id", index=True)
    want_to_see: bool = False
    rating: int | None = None
    review: str | None = Field(default=None, sa_type=SQLText)
    updated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC), sa_type=DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "text_id"),
        UniqueConstraint("user_id", "production_id"),
        CheckConstraint("(text_id IS NULL) != (production_id IS NULL)", name="ck_libraryentry_one_target"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_libraryentry_rating"),
    )


class LibraryVisit(SQLModel, table=True):
    """A reading or theatre visit; repeat visits are separate rows."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    entry_id: uuid.UUID = Field(foreign_key="libraryentry.id", index=True)
    kind: str
    visited_on: dt.date | None = None
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC), sa_type=DateTime(timezone=True))

    __table_args__ = (CheckConstraint("kind IN ('read', 'watched')", name="ck_libraryvisit_kind"),)


class Playlist(SQLModel, table=True):
    """A named private list such as "Must see"."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    name: str

    __table_args__ = (UniqueConstraint("user_id", "name"),)


class PlaylistItem(SQLModel, table=True):
    """A library entry on a playlist, in insertion order."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    playlist_id: uuid.UUID = Field(foreign_key="playlist.id", index=True)
    entry_id: uuid.UUID = Field(foreign_key="libraryentry.id", index=True)
    position: int = 0

    __table_args__ = (UniqueConstraint("playlist_id", "entry_id"),)


class Source(SQLModel, table=True):
    """Represents a source of information for entities, e.g. a specific website, database, book, etc."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str | None = None
    url_pattern: str | None = None  # "https://theaterkompass.de/theatre/{source_id}"
    description: str | None = None

    crawl_config: SourceCrawl | None = Relationship(
        sa_relationship=relationship("SourceCrawl", back_populates="source", uselist=False, passive_deletes="all")
    )
    crawl_runs: list[CrawlRun] = Relationship(
        back_populates="source", sa_relationship_kwargs={"passive_deletes": "all"}
    )
    crawl_pages: list[CrawlPage] = Relationship(
        back_populates="source", sa_relationship_kwargs={"passive_deletes": "all"}
    )
    crawl_records: list[CrawlRecord] = Relationship(
        back_populates="source", sa_relationship_kwargs={"passive_deletes": "all"}
    )


class SourceCrawl(SQLModel, table=True):
    """One validated, source-owned crawler definition; absent for non-web sources."""

    source_id: uuid.UUID = Field(foreign_key="source.id", primary_key=True)
    definition: dict = Field(sa_column=Column(JSON, nullable=False))
    enabled: bool = True
    updated_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    source: Source = Relationship(back_populates="crawl_config")


class CrawlRun(SQLModel, table=True):
    """Durable execution history, independent of browser sessions and queue retention."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="source.id", index=True)
    definition: dict = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="queued", index=True)
    phase: str = "queued"
    job_id: int | None = None
    actor_id: str | None = None
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    heartbeat_at: dt.datetime | None = None
    cancel_requested_at: dt.datetime | None = None
    force_fetch: bool = False
    limit: int | None = None
    enrich_people: bool = True
    stats: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    artifact_path: str | None = None
    error: str | None = None
    source: Source = Relationship(back_populates="crawl_runs")

    __table_args__ = (
        Index(
            "uq_crawlrun_active_source",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
            sqlite_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
    )


class CrawlPage(SQLModel, table=True):
    """HTTP cache, not an import cursor; cached pages are still parsed and validated."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="source.id", index=True)
    variant: str
    url: str
    etag: str | None = None
    last_modified: str | None = None
    body: str = Field(sa_column=Column(SQLText, nullable=False))
    checked_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    source: Source = Relationship(back_populates="crawl_pages")

    __table_args__ = (UniqueConstraint("source_id", "variant", "url"),)


class CrawlRecord(SQLModel, table=True):
    """Last successfully submitted payload, separate from approved SourceRecord identities."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="source.id", index=True)
    namespace: str
    record_key: str
    payload_hash: str
    run_id: uuid.UUID = Field(foreign_key="crawlrun.id")
    source: Source = Relationship(back_populates="crawl_records")

    __table_args__ = (UniqueConstraint("source_id", "namespace", "record_key"),)


class TheapolisProfileCache(SQLModel, table=True):
    """Unapproved directory lookup results, never authoritative person records."""

    name: str = Field(primary_key=True)
    profile: dict | None = Field(default=None, sa_column=Column(JSON, nullable=False))
    captured_at: float = Field(index=True)


class TheapolisImportCheckpoint(SQLModel, table=True):
    """Last fully applied archive run for one archive and import scope."""

    archive_id: str = Field(primary_key=True)
    scope: str = Field(primary_key=True)
    run: int


class EntityType(StrEnum):
    """Represents the type of an entity."""

    ORGANIZATION = "organization"
    PERSON = "person"
    TEXT = "text"
    PRODUCTION = "production"
    AWARD = "award"


class _EntityWithId(Protocol):
    id: uuid.UUID


class EntitySource(SQLModel, table=True):
    """Represents a concrete source record tied to a specific entity."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="source.id")
    entity_type: EntityType
    entity_id: uuid.UUID  # no FK constraint — points to different tables
    external_id: str | None = None
    external_url: str | None = None
    title: str | None = None
    sourced_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    reviewed: bool = Field(default=False)

    source: Source = Relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "entity_type", "entity_id"),
        Index("ix_entitysource_external_identity", "source_id", "entity_type", "external_id"),
        Index("ix_entitysource_entity", "entity_type", "entity_id"),
    )


class SourceMixin:
    """Mixin for entities that can have sources attached."""

    _entity_type: ClassVar[EntityType]  # overridden per subclass

    @property
    def sources(self) -> list[EntitySource]:
        """Source links associated with this entity."""
        state = cast(InstanceState[Any], sa_inspect(self))
        session = state.session
        if session is None:
            return []
        entity_id = cast(_EntityWithId, self).id
        entity_type = type(self)._entity_type
        entity_type_filter = cast(
            ColumnElement[bool],
            EntitySource.entity_type == entity_type,
        )
        entity_id_filter = cast(
            ColumnElement[bool],
            EntitySource.entity_id == entity_id,
        )
        stmt = select(EntitySource).where(
            entity_id_filter,
            entity_type_filter,
        )
        return list(session.scalars(stmt).all())

    def get_source_url(self, source_name: str) -> str | None:
        """Get the URL of a source by its name."""
        return next(
            (es.external_url for es in self.sources if es.source and es.source.name == source_name),
            None,
        )

    def get_source_entry(self, source_name: str) -> EntitySource | None:
        """Get the source entry by its name."""
        return next(
            (es for es in self.sources if es.source and es.source.name == source_name),
            None,
        )


class ReconciliationCase(SQLModel, table=True):
    """Persistent proposals for importing records or merging existing duplicates."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    key: str = Field(unique=True, index=True)
    entity_type: EntityType = Field(index=True)
    operation: str
    status: str = Field(default="pending", index=True)
    target_id: uuid.UUID | None = None
    duplicate_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = Field(default=None, foreign_key="source.id")
    record_key: str | None = None
    incoming: dict = Field(default_factory=dict, sa_column=Column(JSON))
    context: dict = Field(default_factory=dict, sa_column=Column(JSON))
    reason: str
    captured_at: dt.datetime | None = None
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    decided_at: dt.datetime | None = None
    actor_id: str | None = None
    decision_note: str | None = None


class SourceRecord(SQLModel, table=True):
    """Approved source identities, including multiple records merged into one entity."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    source_id: uuid.UUID = Field(foreign_key="source.id")
    entity_type: EntityType
    record_key: str
    entity_id: uuid.UUID = Field(index=True)
    external_id: str | None = None
    external_url: str | None = None
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    captured_at: dt.datetime | None = None

    __table_args__ = (
        UniqueConstraint("source_id", "entity_type", "record_key"),
        Index("ix_sourcerecord_external_identity", "source_id", "entity_type", "external_id"),
    )


class ImportRun(SQLModel, table=True):
    """One uploaded theatre snapshot: what was imported, how much of the site it covered and the outcome."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organization.id", index=True)
    source_id: uuid.UUID = Field(foreign_key="source.id")
    file_name: str | None = None
    scope: str | None = None  # "all", "limit-<n>" or None when unknown
    website: str | None = None  # host the production URLs came from
    captured_at: dt.datetime | None = None
    imported_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC), index=True)
    actor_id: str | None = None
    total: int = 0
    merged: int = 0
    created: int = 0
    review: int = 0
    unchanged: int = 0


class EntityRedirect(SQLModel, table=True):
    """Retain the identity of an entity after an approved duplicate merge."""

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)
    entity_type: EntityType
    old_id: uuid.UUID
    target_id: uuid.UUID

    __table_args__ = (UniqueConstraint("entity_type", "old_id"),)


def _validate_assessment_block(values: tuple[object, ...], label: str, actor_index: int) -> None:
    if any(value is not None for value in values) and any(value is None for value in values):
        raise ValueError(f"{label} fields must be set or cleared together")
    if values[-1] is not None and not str(values[-1]).strip():
        raise ValueError(f"{label} evidence must not be blank")
    if values[actor_index] is not None and not str(values[actor_index]).strip():
        raise ValueError(f"{label} actor must not be blank")


class Organization(SourceMixin, DomainModel, table=True):
    """Represents a theater organization."""

    _entity_type: ClassVar[EntityType] = EntityType.ORGANIZATION
    date_fields: ClassVar[tuple[str, ...]] = ("founded", "closed")

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    type: str | None = None  # e.g. "theater", "opera house", "festival", "venue", "company", etc.
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    url: str | None = None
    url_plays: str | None = None
    street: str | None = None
    zip_code: str | None = None
    city: str | None = None
    country: str | None = None
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    description: str | None = None
    productions: list[Production] = Relationship(back_populates="organization")
    memberships: list[OrganizationMembership] = Relationship(back_populates="organization")
    founded: dt.datetime | None = Field(default=None, sa_type=DateTime)
    founded_precision: DatePrecision | None = None
    closed: dt.datetime | None = Field(default=None, sa_type=DateTime)
    closed_precision: DatePrecision | None = None
    venues: dict[str, int] | None = Field(default=None, sa_column=Column(JSON))  # format {"Name": "Seats"}
    general_verified_at: dt.datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    general_verified_by: str | None = None
    general_verification_evidence: str | None = Field(default=None, sa_type=SQLText)
    history_from_season: int | None = None
    history_through_season: int | None = None
    history_verified_at: dt.datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    history_verified_by: str | None = None
    history_verification_evidence: str | None = Field(default=None, sa_type=SQLText)
    update_scope: str | None = None
    update_reason: str | None = Field(default=None, sa_type=SQLText)
    update_flagged_at: dt.datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    update_flagged_by: str | None = None

    __table_args__ = (
        CheckConstraint(
            "(general_verified_at IS NULL AND general_verified_by IS NULL "
            "AND general_verification_evidence IS NULL) OR "
            "(general_verified_at IS NOT NULL AND general_verified_by IS NOT NULL "
            "AND general_verification_evidence IS NOT NULL "
            "AND length(trim(general_verified_by)) > 0 "
            "AND length(trim(general_verification_evidence)) > 0)",
            name="ck_organization_general_assessment",
        ),
        CheckConstraint(
            "(history_from_season IS NULL AND history_through_season IS NULL "
            "AND history_verified_at IS NULL AND history_verified_by IS NULL "
            "AND history_verification_evidence IS NULL) OR "
            "(history_from_season IS NOT NULL AND history_through_season IS NOT NULL "
            "AND history_from_season BETWEEN 1000 AND 9998 "
            "AND history_through_season BETWEEN history_from_season AND 9998 "
            "AND history_verified_at IS NOT NULL AND history_verified_by IS NOT NULL "
            "AND history_verification_evidence IS NOT NULL "
            "AND length(trim(history_verified_by)) > 0 "
            "AND length(trim(history_verification_evidence)) > 0)",
            name="ck_organization_history_assessment",
        ),
        CheckConstraint(
            "(update_scope IS NULL AND update_reason IS NULL "
            "AND update_flagged_at IS NULL AND update_flagged_by IS NULL) OR "
            "(update_scope IS NOT NULL AND update_scope IN ('general', 'productions', 'both') "
            "AND update_reason IS NOT NULL AND update_flagged_at IS NOT NULL "
            "AND update_flagged_by IS NOT NULL AND length(trim(update_flagged_by)) > 0 "
            "AND length(trim(update_reason)) > 0 "
            ")",
            name="ck_organization_update_flag",
        ),
    )

    @model_validator(mode="after")
    def validate_quality_blocks(self) -> Organization:
        """Require complete, internally consistent assessment blocks."""
        _validate_assessment_block(
            (self.general_verified_at, self.general_verified_by, self.general_verification_evidence),
            "General verification",
            1,
        )
        _validate_assessment_block(
            (
                self.history_from_season,
                self.history_through_season,
                self.history_verified_at,
                self.history_verified_by,
                self.history_verification_evidence,
            ),
            "History verification",
            3,
        )
        history_start, history_end = self.history_from_season, self.history_through_season
        if history_start is not None and history_end is not None:
            if (
                isinstance(history_start, bool)
                or isinstance(history_end, bool)
                or not 1000 <= history_start <= 9998
                or not 1000 <= history_end <= 9998
            ):
                raise ValueError("History seasons must be four-digit start years")
            if history_start > history_end:
                raise ValueError("History start season must not follow its end season")
        update_values = (self.update_scope, self.update_reason, self.update_flagged_at, self.update_flagged_by)
        if any(value is not None for value in update_values) and any(value is None for value in update_values):
            raise ValueError("Update flag fields must be set or cleared together")
        if self.update_scope is not None and self.update_scope not in {"general", "productions", "both"}:
            raise ValueError("Invalid update scope")
        if self.update_reason is not None and not self.update_reason.strip():
            raise ValueError("Update reason must not be blank")
        if self.update_flagged_by is not None and not self.update_flagged_by.strip():
            raise ValueError("Update flag actor must not be blank")
        return self


class OrganizationSeasonCheck(SQLModel, table=True):
    """Audited confirmation that one organization's programme was checked for a season."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(
        sa_column=Column(
            Uuid(as_uuid=True),
            ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    season_start_year: int
    checked_at: dt.datetime = Field(sa_type=DateTime(timezone=True))
    checked_by: str
    evidence: str = Field(sa_type=SQLText)

    __table_args__ = (
        UniqueConstraint("organization_id", "season_start_year", name="uq_organization_season_check"),
        CheckConstraint("season_start_year BETWEEN 1000 AND 9998", name="ck_season_check_start_year"),
        CheckConstraint("length(trim(checked_by)) > 0", name="ck_season_check_actor"),
        CheckConstraint("length(trim(evidence)) > 0", name="ck_season_check_evidence"),
    )

    @model_validator(mode="after")
    def validate_check(self) -> OrganizationSeasonCheck:
        """Reject invalid season, actor, and evidence values."""
        if isinstance(self.season_start_year, bool) or not 1000 <= self.season_start_year <= 9998:
            raise ValueError("Season must be a four-digit start year")
        if not self.checked_by.strip() or not self.evidence.strip():
            raise ValueError("A season check requires an actor and nonblank evidence")
        return self


class ProductionRole(StrEnum):
    """Represents a role that a person can have in a production."""

    DIRECTOR = "director"
    DRAMATURG = "dramaturg"
    SET_DESIGNER = "set_designer"
    ACTOR = "actor"
    COMPOSER = "composer"
    LIGHT_DESIGNER = "light_designer"
    COSTUME_DESIGNER = "costume_designer"
    VIDEO_DESIGNER = "video_designer"
    COREOGRAPHER = "choreographer"
    THEATRE_EDUCATOR = "theatre_educator"
    OTHER = "other"
    #    "author",


class Person(SourceMixin, DomainModel, table=True):
    """Represents a person involved in theater productions (e.g., actor, director, author, ...)."""

    _entity_type: ClassVar[EntityType] = EntityType.PERSON
    date_fields: ClassVar[tuple[str, ...]] = ("born", "died")

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    alt_names: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    type: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    born: dt.datetime | None = Field(default=None, sa_type=DateTime)
    born_precision: DatePrecision | None = None
    died: dt.datetime | None = Field(default=None, sa_type=DateTime)
    died_precision: DatePrecision | None = None
    nationality: str | None = None
    description: str | None = None
    urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    text_contributions: list[TextContribution] = Relationship(back_populates="person")
    production_memberships: list[ProductionMembership] = Relationship(back_populates="person")
    organization_memberships: list[OrganizationMembership] = Relationship(back_populates="person")


class OrganizationMembership(DomainModel, table=True):
    """Represents the involvement of a person in an organization with a specific role and time period."""

    date_fields: ClassVar[tuple[str, ...]] = ("start_date", "end_date")

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organization.id", index=True)
    person_id: uuid.UUID = Field(foreign_key="person.id", index=True)
    role: str | None = None  # e.g. "artistic director", "ensemble member", etc.
    start_date: dt.datetime | None = Field(default=None, sa_type=DateTime)
    start_date_precision: DatePrecision | None = None
    end_date: dt.datetime | None = Field(default=None, sa_type=DateTime)
    end_date_precision: DatePrecision | None = None

    organization: Organization = Relationship(back_populates="memberships")
    person: Person = Relationship(back_populates="organization_memberships")


class Text(SourceMixin, DomainModel, table=True):
    """Represents a text, such as a play, that can be used in productions."""

    _entity_type: ClassVar[EntityType] = EntityType.TEXT
    date_fields: ClassVar[tuple[str, ...]] = ("published",)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str
    languages: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    genre: str | None = None
    published: dt.datetime | None = Field(default=None, sa_type=DateTime)
    published_precision: DatePrecision | None = None
    description: str | None = None
    contributions: list[TextContribution] = Relationship(back_populates="text")
    productions: list[Production] = Relationship(back_populates="text")
    titles: list[TextTitle] = Relationship(back_populates="text")
    # lineage edges where this text is the source / the derivative
    lineages_as_source: list[TextLineage] = Relationship(
        back_populates="source_text",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.source_text_id]"},
    )
    lineages_as_derived: list[TextLineage] = Relationship(
        back_populates="derived_text",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.derived_text_id]"},
    )

    @model_validator(mode="before")
    @classmethod
    def validate_languages(cls, value: object) -> object:
        """Normalize language codes while preserving the main language."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        data["languages"] = normalize_text_languages(data.get("languages", []))
        if data.get("genre") not in {None, "play", "musical", "opera", "novel", "film", "other"}:
            raise ValueError("Invalid text genre")
        if "title" in data and not data["title"].strip():
            raise ValueError("Text title must not be empty")
        return data


def normalize_text_languages(values: list[str]) -> list[str]:
    """Validate ISO 639-1 shaped codes and the explicit multilingual code."""
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Text languages must be a list of language codes")
    result = list(dict.fromkeys(value.strip().lower() for value in values))
    if any(not re.fullmatch(r"[a-z]{2}|mul", value) for value in result):
        raise ValueError("Expected two-letter language codes or 'mul'")
    return result


class TextTitle(SQLModel, table=True):
    """A searchable title variant belonging to one licensable text."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    text_id: uuid.UUID = Field(foreign_key="text.id", index=True)
    title: str
    language: str | None = None
    normalized: str = Field(default="", index=True)
    text: Text = Relationship(back_populates="titles")

    __table_args__ = (UniqueConstraint("text_id", "normalized"),)


class LineageRelation(StrEnum):
    """Directed derivation; only translations and adaptations share a work."""

    TRANSLATION = "translation"
    ADAPTATION = "adaptation"
    BASED_ON = "based_on"


class TextContribution(SQLModel, table=True):
    """A person's contribution to a text; source authors belong to source texts."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    text_id: uuid.UUID = Field(foreign_key="text.id", index=True)
    person_id: uuid.UUID = Field(foreign_key="person.id", index=True)
    role: str | None = None

    text: Text = Relationship(back_populates="contributions")
    person: Person = Relationship(back_populates="text_contributions")


class TextLineage(SQLModel, table=True):
    """Represents a lineage relationship between two texts, e.g. that one text is an adaptation or translation of another."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_text_id: uuid.UUID = Field(foreign_key="text.id")  # the Vorlage
    derived_text_id: uuid.UUID = Field(foreign_key="text.id")  # the new text
    relation: LineageRelation = Field(
        sa_column=Column(
            SAEnum(
                LineageRelation,
                values_callable=lambda enum: [value.value for value in enum],
                native_enum=False,
                length=None,
            ),
            nullable=False,
        )
    )
    notes: str | None = None

    source_text: Text = Relationship(
        back_populates="lineages_as_source",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.source_text_id]"},
    )
    derived_text: Text = Relationship(
        back_populates="lineages_as_derived",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.derived_text_id]"},
    )

    __table_args__ = (
        UniqueConstraint("source_text_id", "derived_text_id", "relation"),
        CheckConstraint("source_text_id != derived_text_id", name="ck_textlineage_not_self"),
        CheckConstraint("relation IN ('translation', 'adaptation', 'based_on')", name="ck_textlineage_relation"),
    )


class Production(SourceMixin, DomainModel, table=True):
    """Represents a specific production of a play at an organization and time, with specific people involved (e.g. the 2022 production of "Hamlet" at "Theater XYZ", directed by Person ABC)."""

    _entity_type: ClassVar[EntityType] = EntityType.PRODUCTION
    date_fields: ClassVar[tuple[str, ...]] = ("premiere",)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    description: str | None = None
    premiere: dt.datetime | None = Field(default=None, sa_type=DateTime)
    premiere_precision: DatePrecision | None = None

    organization_id: uuid.UUID | None = Field(default=None, foreign_key="organization.id", index=True)
    organization: Organization | None = Relationship(back_populates="productions")

    memberships: list[ProductionMembership] = Relationship(back_populates="production")
    duration: int | None = None
    age_rating: int | None = None
    text_id: uuid.UUID | None = Field(default=None, foreign_key="text.id")
    text: Text | None = Relationship(back_populates="productions")
    url: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    critics: list[str] = Field(default_factory=list, sa_column=Column(JSON))


def _value_enum_column(enum: type[StrEnum]) -> Column:
    return Column(
        SAEnum(enum, values_callable=lambda members: [m.value for m in members], native_enum=False, length=None),
        nullable=False,
    )


class TagLayer(StrEnum):
    """Profil describes what a production is; Inhalt what it concerns."""

    PROFILE = "profile"
    CONTENT = "content"


class Tag(SQLModel, table=True):
    """Editor-owned controlled vocabulary entry, grouped into a layer and facet."""

    code: str = Field(primary_key=True)  # e.g. "form.tanz"; stable, never reused
    label: str
    layer: TagLayer = Field(sa_column=_value_enum_column(TagLayer))
    facet: str = Field(index=True)
    definition: str | None = None
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    position: int = 0
    active: bool = True


class TagStatus(StrEnum):
    """Only published assignments are public; rejections suppress repeated suggestions."""

    SUGGESTED = "suggested"
    PUBLISHED = "published"
    REJECTED = "rejected"


class TagOrigin(StrEnum):
    """Who asserted the assignment; manual decisions win over source and model."""

    MANUAL = "manual"
    SOURCE = "source"
    MODEL = "model"


class ProductionTag(SQLModel, table=True):
    """One controlled tag decision for one production, with its evidence."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    production_id: uuid.UUID = Field(
        sa_column=Column(Uuid, ForeignKey("production.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    tag_code: str = Field(foreign_key="tag.code", index=True)
    status: TagStatus = Field(sa_column=_value_enum_column(TagStatus))
    origin: TagOrigin = Field(sa_column=_value_enum_column(TagOrigin))
    evidence: str | None = None
    score: float | None = None  # model similarity margin, not a probability
    description_hash: str | None = None
    pipeline: str | None = None
    decided_by: str | None = None
    decided_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))

    __table_args__ = (UniqueConstraint("production_id", "tag_code"),)


EMBEDDING_DIMENSIONS = 640


class DescriptionChunk(SQLModel, table=True):
    """One embedded passage of a production or text description; stale when its hash or pipeline differs."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    production_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(Uuid, ForeignKey("production.id", ondelete="CASCADE"), index=True)
    )
    text_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(Uuid, ForeignKey("text.id", ondelete="CASCADE"), index=True)
    )
    chunk_no: int
    content: str
    description_hash: str
    pipeline: str
    embedding: list[float] = Field(sa_column=Column(Vector(EMBEDDING_DIMENSIONS), nullable=False))

    __table_args__ = (
        CheckConstraint("(production_id IS NULL) <> (text_id IS NULL)", name="ck_descriptionchunk_one_owner"),
    )


class ProductionMembership(SQLModel, table=True):
    """Represents the involvement of a person in a production with a specific role."""

    # person assignment with role name in this specific play (e.g. "Regie")
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    production_id: uuid.UUID = Field(foreign_key="production.id", index=True)
    person_id: uuid.UUID = Field(foreign_key="person.id", index=True)
    role: str | None = None  # e.g. "Regie", "Schauspieler", "Dramaturgie", etc. # TODO: define somewhere else
    character: str | None = None  # for actors

    production: Production = Relationship(back_populates="memberships")
    person: Person = Relationship(back_populates="production_memberships")


class AwardResult(StrEnum):
    """Represents the result of a theater award nomination."""

    WON = "won"
    NOMINATED = "nominated"


class Award(SourceMixin, SQLModel, table=True):
    """Represents a theater award."""

    _entity_type: ClassVar[EntityType] = EntityType.AWARD

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str  # e.g. "Deutscher Theaterpreis Der Faust"
    description: str | None = None
    country: str | None = None
    notes: str | None = None
    url: str | None = None

    nominations: list[AwardNomination] = Relationship(back_populates="award")


class AwardNomination(DomainModel, table=True):
    """Represents a nomination of a person and/or production for a theater award in a specific category and year."""

    date_fields: ClassVar[tuple[str, ...]] = ("date",)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    award_id: uuid.UUID = Field(foreign_key="award.id")
    production_id: uuid.UUID | None = Field(default=None, foreign_key="production.id")  # optional: ties to a production
    person_id: uuid.UUID | None = Field(default=None, foreign_key="person.id", index=True)  # optional: ties to a person
    category: str  # e.g. "Best Director", "Best Set Design"
    date: dt.datetime = Field(sa_type=DateTime)
    date_precision: DatePrecision
    result: AwardResult

    award: Award = Relationship(back_populates="nominations")
    production: Production | None = Relationship()
    person: Person | None = Relationship()


def _normalize_domain_row(_mapper: object, _connection: object, target: DomainModel) -> None:
    data = {key: getattr(target, key, None) for field in target.date_fields for key in (field, f"{field}_precision")}
    normalized = normalize_dates(data, target.date_fields)
    if not isinstance(normalized, dict):
        raise TypeError("Expected normalized date fields")
    for key, value in normalized.items():
        setattr(target, key, value)


for _model in (Organization, Person, OrganizationMembership, Text, Production, AwardNomination):
    event.listen(_model, "before_insert", _normalize_domain_row)
    event.listen(_model, "before_update", _normalize_domain_row)


@event.listens_for(SASession, "before_flush")
def _validate_text_changes(session: SASession, _context: object, _instances: object) -> None:
    changed = list(session.new) + list(session.dirty)
    texts = []
    lineage = []
    for row in changed:
        if isinstance(row, Text) and row not in session.deleted:
            texts.append(row)
        elif isinstance(row, TextTitle):
            _normalize_title_row(row)
        elif isinstance(row, TextContribution) and row.role == "source author":
            raise ValueError("Source authors must be credited on a based_on source text")
        elif isinstance(row, TextLineage):
            lineage.append(row)
    if texts:
        _sync_display_titles(session, texts)
    if any(isinstance(row, Production) for row in changed):
        from backend.taxonomy import on_before_flush  # ruff: ignore[import-outside-top-level]

        on_before_flush(session)
    if lineage:
        _validate_lineage_graph(session, lineage)


def _normalize_title_row(row: TextTitle) -> None:
    from backend.reconciliation import normalize  # ruff: ignore[import-outside-top-level]

    row.title = row.title.strip()
    row.normalized = normalize(row.title)
    if not row.normalized:
        raise ValueError("Text title must not be empty")
    if row.language is not None:
        row.language = normalize_text_languages([row.language])[0]
        if row.language == "mul":
            raise ValueError("Title language must be a two-letter code")


def _sync_display_titles(session: SASession, rows: list[Text]) -> None:
    from backend.reconciliation import normalize  # ruff: ignore[import-outside-top-level]

    titles: dict[uuid.UUID, list[TextTitle]] = defaultdict(list)
    for batch in batched({row.id for row in rows}, 500, strict=False):
        for text_id, title in session.execute(
            select(col(TextTitle.text_id), TextTitle).where(col(TextTitle.text_id).in_(batch))
        ):
            titles[text_id].append(title)
    for title in session.new:
        if isinstance(title, TextTitle):
            parent = next(iter(sa_inspect(title).attrs.text.history.added), None)
            titles[parent.id if parent is not None else title.text_id].append(title)

    for row in rows:
        validated = Text.model_validate(row.model_dump())
        row.languages = validated.languages
        normalized = normalize(row.title)
        if not any(title.normalized == normalized or normalize(title.title) == normalized for title in titles[row.id]):
            language = next(iter(row.languages), None)
            session.add(
                TextTitle(
                    text=row,
                    text_id=row.id,
                    title=row.title,
                    language=language if language != "mul" else None,
                    normalized=normalized,
                )
            )


def _lineage_endpoints(row: TextLineage) -> tuple[uuid.UUID, uuid.UUID]:
    state = sa_inspect(row)
    source = next(iter(state.attrs.source_text.history.added), None)
    derived = next(iter(state.attrs.derived_text.history.added), None)
    source_id = source.id if source is not None else row.source_text_id
    derived_id = derived.id if derived is not None else row.derived_text_id
    if source_id is None or derived_id is None:
        raise ValueError("Both lineage texts are required")
    return source_id, derived_id


def _validate_lineage_graph(session: SASession, changed: list[TextLineage]) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(current_database()), hashtext('theaterdb.textlineage'))")
        )
    replaced = {row.id for row in changed}
    replaced.update(row.id for row in session.deleted if isinstance(row, TextLineage))
    children: dict[uuid.UUID, set[uuid.UUID]] = {}
    derived_ids = set()
    for row in changed:
        row.relation = LineageRelation(row.relation)
        source_id, derived_id = _lineage_endpoints(row)
        if source_id == derived_id:
            raise ValueError("A text cannot derive from itself")
        children.setdefault(source_id, set()).add(derived_id)
        derived_ids.add(derived_id)

    # Every new cycle uses a changed edge. Seed from all changed destinations
    # to cover persisted path segments between changes, using the live transaction.
    source = col(TextLineage.source_text_id)
    edges = select(col(TextLineage.id), source, col(TextLineage.derived_text_id))
    for batch in batched(derived_ids, 500, strict=False):
        reachable = edges.where(source.in_(batch)).cte(recursive=True)
        reachable = reachable.union(edges.join(reachable, source == reachable.c.derived_text_id))
        for edge_id, source_id, derived_id in session.execute(select(reachable)):
            if edge_id not in replaced:
                children.setdefault(source_id, set()).add(derived_id)
    try:
        # Reversing all edges (successors as predecessors) preserves cycles.
        TopologicalSorter(children).prepare()
    except CycleError as error:
        raise ValueError("Text lineage cycle") from error
