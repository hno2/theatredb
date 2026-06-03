import datetime as dt
import uuid
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import JSON, Column, select
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


class User(SQLModel, table=True):
    """Represents a user in the system."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    username: str = Field(index=True, sa_column_kwargs={"unique": True})
    display_name: str | None = None
    email: str | None = Field(default=None, index=True, sa_column_kwargs={"unique": True})
    password_hash: str
    is_active: bool = True
    is_admin: bool = False


class Source(SQLModel, table=True):
    """Represents a source of information for entities, e.g. a specific website, database, book, etc."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True, unique=True)
    base_url: str | None = None
    url_pattern: str | None = None  # "https://theaterkompass.de/theatre/{source_id}"
    description: str | None = None


class EntityType(StrEnum):
    """Represents the type of an entity."""

    ORGANIZATION = "organization"
    PERSON = "person"
    TEXT = "text"
    PRODUCTION = "production"
    AWARD = "award"


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

    __table_args__ = (UniqueConstraint("source_id", "entity_type", "entity_id"),)


class SourceMixin:
    """Mixin for entities that can have sources attached."""

    _entity_type: ClassVar[EntityType]  # overridden per subclass

    @property
    def sources(self) -> list[EntitySource]:
        """Get all source links for this entity."""
        session = sa_inspect(self).session
        if session is None:
            return []
        stmt = select(EntitySource).where(
            EntitySource.entity_id == self.id,
            EntitySource.entity_type == self.__class__._entity_type,
        )
        return list(session.exec(stmt).all())

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


class Organization(SourceMixin, SQLModel, table=True):
    """Represents a theater organization."""

    _entity_type: ClassVar[EntityType] = EntityType.ORGANIZATION

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    type: str | None = None  # e.g. "theater", "opera house", "festival", "venue", "company", etc.
    url: str | None = None
    url_plays: str | None = None
    street: str | None = None
    zip_code: str | None = None
    city: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    description: str | None = None
    productions: list[Production] = Relationship(back_populates="organization")
    memberships: list[OrganizationMembership] = Relationship(back_populates="organization")
    founded_year: int | None = None
    closed_year: int | None = None
    venues: list[dict[str, str]] = Field(default_factory=list, sa_column=Column(JSON))  # format {"Name": "Seats"}


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
    OTHER = "other"
    #    "author",


class Person(SourceMixin, SQLModel, table=True):
    """Represents a person involved in theater productions (e.g., actor, director, author, ...)."""

    _entity_type: ClassVar[EntityType] = EntityType.PERSON

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    type: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    born: dt.date | None = None
    died: dt.date | None = None
    nationality: str | None = None
    urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    text_contributions: list[TextContribution] = Relationship(back_populates="person")
    production_memberships: list[ProductionMembership] = Relationship(back_populates="person")
    organization_memberships: list[OrganizationMembership] = Relationship(back_populates="person")


class OrganizationMembership(SQLModel, table=True):
    """Represents the involvement of a person in an organization with a specific role and time period."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    organization_id: uuid.UUID = Field(foreign_key="organization.id")
    person_id: uuid.UUID = Field(foreign_key="person.id")
    role: str | None = None  # e.g. "artistic director", "ensemble member", etc.
    start_date: dt.date | None = None
    end_date: dt.date | None = None

    organization: Organization = Relationship(back_populates="memberships")
    person: Person = Relationship(back_populates="organization_memberships")


class Text(SourceMixin, SQLModel, table=True):
    """Represents a text, such as a play, that can be used in productions."""

    _entity_type: ClassVar[EntityType] = EntityType.TEXT

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    title: str
    language: str | None = None  # ISO 639-1
    published_year: int | None = None
    contributions: list[TextContribution] = Relationship(back_populates="text")
    productions: list[Production] = Relationship(back_populates="text")
    # lineage edges where this text is the source / the derivative
    lineages_as_source: list[TextLineage] = Relationship(
        back_populates="source_text",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.source_text_id]"},
    )
    lineages_as_derived: list[TextLineage] = Relationship(
        back_populates="derived_text",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.derived_text_id]"},
    )


class TextContribution(SQLModel, table=True):
    """Represents the contribution of a person to a text, e.g. that a person is an author, translator, adapter, source author (for lineages), etc. of a text."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    text_id: uuid.UUID = Field(foreign_key="text.id")
    person_id: uuid.UUID = Field(foreign_key="person.id")
    role: str | None = None  # e.g. "author", "translator", "adapter", "source author" (for lineages), etc.

    text: Text = Relationship(back_populates="contributions")
    person: Person = Relationship(back_populates="text_contributions")


class TextLineage(SQLModel, table=True):
    """Represents a lineage relationship between two texts, e.g. that one text is an adaptation or translation of another."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source_text_id: uuid.UUID = Field(foreign_key="text.id")  # the Vorlage
    derived_text_id: uuid.UUID = Field(foreign_key="text.id")  # the new text
    relation: str  # e.g. "adaptation", "translation", etc.
    notes: str | None = None

    source_text: Text = Relationship(
        back_populates="lineages_as_source",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.source_text_id]"},
    )
    derived_text: Text = Relationship(
        back_populates="lineages_as_derived",
        sa_relationship_kwargs={"foreign_keys": "[TextLineage.derived_text_id]"},
    )


class Production(SourceMixin, SQLModel, table=True):
    """Represents a specific production of a play at an organization and time, with specific people involved (e.g. the 2022 production of "Hamlet" at "Theater XYZ", directed by Person ABC)."""

    _entity_type: ClassVar[EntityType] = EntityType.PRODUCTION

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(index=True)
    description: str | None = None
    premiere: dt.datetime | None = None

    organization_id: uuid.UUID | None = Field(default=None, foreign_key="organization.id")
    organization: Organization | None = Relationship(back_populates="productions")

    memberships: list[ProductionMembership] = Relationship(back_populates="production")
    duration: int | None = None
    age_rating: int | None = None
    text_id: uuid.UUID | None = Field(default=None, foreign_key="text.id")
    text: Text | None = Relationship(back_populates="productions")
    url: str | None = None
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    critics: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class ProductionMembership(SQLModel, table=True):
    """Represents the involvement of a person in a production with a specific role."""

    # person assignment with role name in this specific play (e.g. "Regie")
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    production_id: uuid.UUID = Field(foreign_key="production.id")
    person_id: uuid.UUID = Field(foreign_key="person.id")
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


class AwardNomination(SQLModel, table=True):
    """Represents a nomination of a person and/or production for a theater award in a specific category and year."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    award_id: uuid.UUID = Field(foreign_key="award.id")
    production_id: uuid.UUID | None = Field(default=None, foreign_key="production.id")  # optional: ties to a production
    person_id: uuid.UUID | None = Field(default=None, foreign_key="person.id")  # optional: ties to a person
    category: str  # e.g. "Best Director", "Best Set Design"
    year: int
    result: AwardResult

    award: Award = Relationship(back_populates="nominations")
    production: Production | None = Relationship()
    person: Person | None = Relationship()
