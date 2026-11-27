"""Authority matching: one provider interface for GND and Wikidata, high-confidence proposals and bulk apply.

A proposal needs evidence beyond the name, because names are not identities:
- cross-link: the entity already carries the other authority's ID and that record names exactly one ID here;
- exact name/title plus a corroborating fact: person life year, text author linked to the same authority,
  or theatre place of business. Exactly one hit may pass, otherwise the entity is left for manual review.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from backend import gnd, wikidata
from backend.authority import owner
from backend.models import EntityType, Organization, Person, Text, TextContribution, TextLineage
from backend.reconciliation import normalize
from backend.texts import FAMILY_RELATIONS

MODELS: dict[EntityType, type[Person | Text | Organization]] = {
    EntityType.PERSON: Person,
    EntityType.TEXT: Text,
    EntityType.ORGANIZATION: Organization,
}


@dataclass(frozen=True)
class Provider:
    """Uniform access to one authority file."""

    name: str
    parse_id: Callable[[str], str]
    search: Callable[[str, EntityType], list[dict]]
    fetch: Callable[[str], dict]
    record_id: Callable[[dict], str]
    summary: Callable[[dict], str]
    url: Callable[[str], str]
    names: Callable[[dict], list[str]]
    years: Callable[[dict], dict[str, int]]
    author_ids: Callable[[dict], set[str]]
    places: Callable[[dict], list[str]]
    cross_ids: Callable[[dict], set[str]]  # IDs of the other provider that this record declares identical
    predecessors: Callable[[dict], list[tuple[str, str]]]
    enrich: Callable[..., dict]
    identity: Callable[[Session, EntityType, uuid.UUID], str | None]
    errors: tuple[type[Exception], ...]


# Wrappers resolve the module function per call, so tests and callers can swap the network functions.
def _gnd_search(query: str, kind: EntityType) -> list[dict]:
    return gnd.search(query, kind)


def _gnd_fetch(gnd_id: str) -> dict:
    return gnd.fetch_record(gnd_id)


def _gnd_enrich(*args: object, **kwargs: object) -> dict:
    return gnd.enrich(*args, **kwargs)  # pyrefly: ignore[bad-argument-type]


def _wikidata_search(query: str, kind: EntityType) -> list[dict]:
    return wikidata.search(query, kind)


def _wikidata_fetch(qid: str) -> dict:
    return wikidata.fetch_entity(qid)


def _wikidata_enrich(*args: object, **kwargs: object) -> dict:
    return wikidata.enrich(*args, **kwargs)  # pyrefly: ignore[bad-argument-type]


GND = Provider(
    name="GND",
    parse_id=gnd.parse_gnd_id,
    search=_gnd_search,
    fetch=_gnd_fetch,
    record_id=gnd.record_id,
    summary=gnd.summary,
    url=lambda i: gnd.GND_URI + i,
    names=gnd.names,
    years=gnd.years,
    author_ids=gnd.author_ids,
    places=gnd.places,
    cross_ids=gnd.wikidata_ids,
    predecessors=lambda r: gnd.refs(r, "precedingWork"),
    enrich=_gnd_enrich,
    identity=gnd.identity,
    errors=(gnd.GndError,),
)
WIKIDATA = Provider(
    name="Wikidata",
    parse_id=wikidata.parse_qid,
    search=_wikidata_search,
    fetch=_wikidata_fetch,
    record_id=wikidata.record_id,
    summary=wikidata.summary,
    url=lambda i: wikidata.WIKI_URL + i,
    names=wikidata.names,
    years=wikidata.years,
    author_ids=wikidata.author_ids,
    places=wikidata.places,
    cross_ids=wikidata.gnd_ids,
    predecessors=lambda r: wikidata.refs(r, "P144"),
    enrich=_wikidata_enrich,
    identity=wikidata.identity,
    errors=(wikidata.WikidataError,),
)
PROVIDERS = (GND, WIKIDATA)
NETWORK_ERRORS = (gnd.GndError, wikidata.WikidataError)


@dataclass
class Proposal:
    """A high-confidence identity for one entity; ``record`` is None when it still has to be fetched."""

    kind: EntityType
    entity_id: uuid.UUID
    label: str
    provider: Provider
    external_id: str
    evidence: str
    record: dict | None = field(default=None, repr=False)


def label(entity: Person | Text | Organization) -> str:
    """The name or title the authority is searched with."""
    return entity.title if isinstance(entity, Text) else entity.name


def _bare(name: str) -> str:
    """Drop trailing qualifiers like GND's "Thalia-Theater (Rostock)"."""
    return normalize(re.sub(r"\s*\([^)]*\)\s*$", "", name))


def _person_evidence(person: Person, provider: Provider, record: dict) -> str | None:
    local = {key: value.year for key in ("born", "died") if (value := getattr(person, key)) is not None}
    remote = provider.years(record)
    shared = local.keys() & remote.keys()
    if not shared or any(local[key] != remote[key] for key in shared):
        return None
    return "Name + " + " + ".join("Geburtsjahr" if key == "born" else "Todesjahr" for key in sorted(shared))


def _text_evidence(session: Session, text: Text, provider: Provider, record: dict) -> str | None:
    person_ids = session.exec(
        select(TextContribution.person_id).where(
            TextContribution.text_id == text.id, col(TextContribution.role).in_(["author", "composer"])
        )
    ).all()
    linked = {provider.identity(session, EntityType.PERSON, person_id) for person_id in person_ids} - {None}
    return "Titel + verknüpfte:r Autor:in" if linked & provider.author_ids(record) else None


def _organization_evidence(organization: Organization, provider: Provider, record: dict) -> str | None:
    locality = organization.address_locality
    if locality and normalize(locality) in {_bare(place) for place in provider.places(record)}:
        return "Name + Ort"
    return None


def evidence(
    session: Session, provider: Provider, kind: EntityType, entity: Person | Text | Organization, record: dict
) -> str | None:
    """Why this record is the entity, or None if only the name (or nothing) matches."""
    if _bare(label(entity)) not in {_bare(name) for name in provider.names(record)}:
        return None
    if isinstance(entity, Person):
        return _person_evidence(entity, provider, record)
    if isinstance(entity, Text):
        return _text_evidence(session, entity, provider, record)
    return _organization_evidence(entity, provider, record)


def agreement(gnd_record: dict, wikidata_record: dict) -> bool | None:
    """True if either record names the other, False if one names a different ID, None without cross-links."""
    gnd_id, qid = GND.record_id(gnd_record), WIKIDATA.record_id(wikidata_record)
    declared = [(GND.cross_ids(gnd_record), qid), (WIKIDATA.cross_ids(wikidata_record), gnd_id)]
    if any(other in ids for ids, other in declared):
        return True
    return False if any(ids for ids, _ in declared) else None


def _cross_link(
    session: Session, provider: Provider, kind: EntityType, entity_id: uuid.UUID, fetch: Callable[[Provider, str], dict]
) -> tuple[str, str] | None:
    for other in PROVIDERS:
        if other is provider or (other_id := other.identity(session, kind, entity_id)) is None:
            continue
        ids = other.cross_ids(fetch(other, other_id))
        if len(ids) == 1:
            return ids.pop(), f"{other.name} {other_id} verweist darauf"
    return None


def propose(
    session: Session,
    provider: Provider,
    kind: EntityType,
    entity: Person | Text | Organization,
    *,
    hits: list[dict] | None = None,
    fetch: Callable[[Provider, str], dict] | None = None,
) -> Proposal | None:
    """The single high-confidence record for an entity not yet linked to ``provider``, else None."""
    if provider.identity(session, kind, entity.id) is not None:
        return None
    fetch = fetch or (lambda p, i: p.fetch(i))
    if (cross := _cross_link(session, provider, kind, entity.id, fetch)) is not None:
        external_id, why = cross
        if owner(session, provider.name, kind, external_id) is None:
            return Proposal(kind, entity.id, label(entity), provider, external_id, why)
        return None
    hits = provider.search(label(entity), kind) if hits is None else hits
    matches = [(hit, why) for hit in hits if (why := evidence(session, provider, kind, entity, hit["record"]))]
    if len(matches) != 1:
        return None
    hit, why = matches[0]
    if owner(session, provider.name, kind, hit["id"]) is not None:
        return None
    return Proposal(kind, entity.id, label(entity), provider, hit["id"], why, hit["record"])


def _unlinked(session: Session, provider: Provider, kind: EntityType) -> Iterator[Person | Text | Organization]:
    model = MODELS[kind]
    derived: set[uuid.UUID] = set()
    if kind == EntityType.TEXT:
        derived = set(
            session.exec(
                select(TextLineage.derived_text_id).where(
                    col(TextLineage.relation).in_([str(relation) for relation in FAMILY_RELATIONS])
                )
            ).all()
        )
    order = getattr(model, "title" if model is Text else "name")
    for entity in session.exec(select(model).order_by(order)).all():
        if entity.id not in derived and provider.identity(session, kind, entity.id) is None:
            yield entity


def bulk_proposals(
    session: Session,
    provider: Provider,
    kind: EntityType,
    *,
    offset: int = 0,
    limit: int = 25,
    progress: Callable[[int, int], object] | None = None,
) -> tuple[list[Proposal], int, bool]:
    """Examine up to ``limit`` unlinked entities from ``offset``; never writes.

    Returns proposals, the offset to continue from and whether all entities were examined.
    Network errors propagate so a broken connection stops the pass instead of timing out per entity.
    """
    proposals: list[Proposal] = []
    examined = 0
    for index, entity in enumerate(_unlinked(session, provider, kind)):
        if index < offset:
            continue
        if examined == limit:
            return proposals, offset + examined, False
        if (proposal := propose(session, provider, kind, entity)) is not None:
            proposals.append(proposal)
        examined += 1
        if progress:
            progress(examined, limit)
    return proposals, offset + examined, True


def apply(session: Session, proposals: Collection[Proposal]) -> list[tuple[Proposal, dict | str]]:
    """Link and fill blanks per proposal in its own commit; lineage is left to the manual panel."""
    results: list[tuple[Proposal, dict | str]] = []
    for proposal in proposals:
        try:
            report = proposal.provider.enrich(
                session,
                proposal.kind,
                proposal.entity_id,
                proposal.external_id,
                record=proposal.record,
                predecessor_ids=(),
            )
            session.commit()
            results.append((proposal, report))
        except (ValueError, *NETWORK_ERRORS) as exc:
            session.rollback()
            results.append((proposal, str(exc)))
    return results
