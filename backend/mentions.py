"""Derived description links: graph-backed matching and explicit mention review."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from html import escape
from operator import itemgetter
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from backend.config import ENTITY_MODELS, canonical_role
from backend.entity_management import verified_organization_filter
from backend.models import (
    EntityRedirect,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    ReconciliationCase,
    Text,
    TextContribution,
    TextTitle,
)
from backend.reconciliation import entity_snapshot, normalize, resolve_id, stable_digest
from backend.user_management import get_session

OPERATION = "description_mention"
MATCHER_VERSION = 1
OWNER_KINDS = (EntityType.PERSON, EntityType.TEXT, EntityType.ORGANIZATION, EntityType.PRODUCTION)
TARGET_KINDS = (EntityType.PERSON, EntityType.TEXT, EntityType.ORGANIZATION)
PARSER = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
type Identity = tuple[EntityType, uuid.UUID]
type Confidence = Literal["high", "medium", "ambiguous"]


@dataclass(frozen=True)
class Candidate:
    """A lightweight catalog entry, without recursive relationships or descriptions."""

    kind: EntityType
    id: uuid.UUID
    label: str
    aliases: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    facts: str = ""


@dataclass(frozen=True)
class Evidence:
    """An actual typed relationship path; repeated credits do not multiply confidence."""

    path: str
    role: str | None = None
    year: int | None = None
    strong: bool = True
    start_year: int | None = None
    end_year: int | None = None


@dataclass
class Match:
    """A span inside a parsed text token, bound to the exact stored description."""

    node: int
    start: int
    end: int
    quote: str
    context: str
    candidates: list[Candidate]
    evidence: dict[str, list[Evidence]]
    confidence: Confidence
    target: Candidate | None = None


MATCH_ADAPTER = TypeAdapter(Match)


@dataclass
class Catalog:
    """Labels indexed once for a page/scan batch, with normalized aliases."""

    candidates: list[Candidate]
    aliases: dict[str, list[Candidate]] = field(init=False)
    pattern: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        """Index each alias once per candidate identity."""
        self.aliases = {}
        for candidate in self.candidates:
            for label in {candidate.label, *candidate.aliases}:
                key = normalize(label)
                if key and candidate not in self.aliases.setdefault(key, []):
                    self.aliases[key].append(candidate)
        alternatives = "|".join(re.escape(key) for key in sorted(self.aliases, key=len, reverse=True))
        self.pattern = re.compile(rf"(?<!\w)(?=({alternatives})(?!\w))" if alternatives else r"(?!)")


def catalog(session: Session) -> Catalog:
    """Load only labels and summary facts; previews are fetched for resolved targets."""
    candidates = []
    for identity, name, aliases, roles in session.exec(select(Person.id, Person.name, Person.alt_names, Person.type)):
        candidates.append(Candidate(EntityType.PERSON, identity, name, tuple(aliases), facts=", ".join(roles or [])))
    titles: dict[uuid.UUID, list[str]] = {}
    for identity, title in session.exec(select(TextTitle.text_id, TextTitle.title)):
        titles.setdefault(identity, []).append(title)
    for identity, title, created, published in session.exec(select(Text.id, Text.title, Text.created, Text.published)):
        years = tuple(sorted({date.year for date in (created, published) if date}))
        candidates.append(Candidate(EntityType.TEXT, identity, title, tuple(titles.get(identity, [])), years))
    for identity, name, city in session.exec(select(Organization.id, Organization.name, Organization.address_locality)):
        candidates.append(Candidate(EntityType.ORGANIZATION, identity, name, facts=city or ""))
    return Catalog(candidates)


def load_catalog() -> Catalog:
    """Read a fresh candidate catalog without writing."""
    with get_session() as session:
        return catalog(session)


def _direct_evidence(session: Session, owner: Identity) -> dict[Identity, list[Evidence]]:
    kind, identity = owner
    result: dict[Identity, list[Evidence]] = {}
    if kind in {EntityType.PERSON, EntityType.TEXT}:
        condition = (
            TextContribution.person_id == identity
            if kind == EntityType.PERSON
            else TextContribution.text_id == identity
        )
        for contribution in session.exec(select(TextContribution).where(condition)):
            target_kind = EntityType.TEXT if kind == EntityType.PERSON else EntityType.PERSON
            target_id = contribution.text_id if kind == EntityType.PERSON else contribution.person_id
            result.setdefault((target_kind, target_id), []).append(Evidence("TextContribution", contribution.role))
    if kind in {EntityType.PERSON, EntityType.ORGANIZATION}:
        condition = (
            OrganizationMembership.person_id == identity
            if kind == EntityType.PERSON
            else OrganizationMembership.organization_id == identity
        )
        for membership in session.exec(select(OrganizationMembership).where(condition)):
            target_kind = EntityType.ORGANIZATION if kind == EntityType.PERSON else EntityType.PERSON
            target_id = membership.organization_id if kind == EntityType.PERSON else membership.person_id
            result.setdefault((target_kind, target_id), []).append(
                Evidence(
                    "OrganizationMembership",
                    membership.role,
                    start_year=membership.start_date.year if membership.start_date else None,
                    end_year=membership.end_date.year if membership.end_date else None,
                )
            )
    return result


def _owner_productions(session: Session, owner: Identity) -> list[Production]:
    kind, identity = owner
    stmt = select(Production)
    if kind == EntityType.PERSON:
        stmt = stmt.where(
            col(Production.id).in_(
                select(ProductionMembership.production_id).where(ProductionMembership.person_id == identity)
            )
        )
    elif kind == EntityType.TEXT:
        stmt = stmt.where(Production.text_id == identity)
    elif kind == EntityType.ORGANIZATION:
        stmt = stmt.where(Production.organization_id == identity)
    else:
        stmt = stmt.where(Production.id == identity)
    return list(session.exec(stmt).all())


def graph_evidence(session: Session, owner: Identity, targets: set[Identity]) -> dict[Identity, list[Evidence]]:
    """Query bounded owner paths, then retain evidence only for mention candidates."""
    kind, identity = owner
    result = {key: values for key, values in _direct_evidence(session, owner).items() if key in targets}

    def add(target_kind: EntityType, target_id: uuid.UUID | None, evidence: Evidence) -> None:
        if target_id is not None and (target_kind, target_id) in targets:
            values = result.setdefault((target_kind, target_id), [])
            if evidence not in values:
                values.append(evidence)

    productions = _owner_productions(session, owner)
    credits: dict[uuid.UUID, list[ProductionMembership]] = {}
    if productions:
        for membership in session.exec(
            select(ProductionMembership).where(
                col(ProductionMembership.production_id).in_([row.id for row in productions])
            )
        ):
            credits.setdefault(membership.production_id, []).append(membership)
    for production in productions:
        year = production.premiere.year if production.premiere else None
        owner_roles = [row.role_name for row in credits.get(production.id, []) if row.person_id == identity]
        path = f"Production:{production.id}"
        for role in owner_roles or [None]:
            add(EntityType.TEXT, production.text_id, Evidence(path, role, year))
            add(EntityType.ORGANIZATION, production.organization_id, Evidence(path, role, year))
        for membership in credits.get(production.id, []):
            add(
                EntityType.PERSON,
                membership.person_id,
                Evidence(path, membership.role_name, year, strong=kind != EntityType.PERSON),
            )
    return result


def text_nodes(tokens: list[Token]) -> list[Token]:
    """Return ordinary inline text nodes, excluding existing links, images, and code."""
    nodes = []
    for token in tokens:
        if token.type != "inline":
            continue
        linked = 0
        for child in token.children or []:
            if child.type == "link_open":
                linked += 1
            elif child.type == "link_close":
                linked -= 1
            elif child.type == "text" and not linked and not re.search(r"</?[A-Za-z][^>]*>", child.content):
                nodes.append(child)
    return nodes


def _normalized_spans(value: str) -> tuple[str, list[int]]:
    # Case folding can expand characters (e.g. ß); retain original token offsets.
    chars, offsets = [], []
    index = 0
    while index < len(value):
        end = index + 1
        while end < len(value) and unicodedata.combining(value[end]):
            end += 1
        for folded in unicodedata.normalize("NFKC", value[index:end]).casefold():
            if folded.isalnum() or folded == "_":
                chars.append(folded)
                offsets.append(index)
            elif chars and chars[-1] != " ":
                chars.append(" ")
                offsets.append(index)
        index = end
    return "".join(chars).rstrip(), offsets


def _spans(text: str, labels: Catalog, owner: Identity) -> list[tuple[int, int, list[Candidate]]]:
    value, offsets = _normalized_spans(text)
    found = []
    for match in labels.pattern.finditer(value):
        entries = labels.aliases[match.group(1)]
        candidates = [candidate for candidate in entries if (candidate.kind, candidate.id) != owner]
        if not candidates:
            continue
        end = offsets[match.end(1) - 1] + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        found.append((offsets[match.start(1)], end, candidates))
    selected = []
    for span in sorted(found, key=lambda item: (-(item[1] - item[0]), item[0])):
        if not any(span[0] < previous[1] and previous[0] < span[1] for previous in selected):
            selected.append(span)
    return sorted(selected, key=itemgetter(0))


def _years_for_context(candidate: Candidate, paths: list[Evidence], role: str | None) -> set[int]:
    if role == "director":
        return {path.year for path in paths if path.year and path.role and canonical_role(path.role) == role}
    if role in {"author", "translator"}:
        return set(candidate.years)
    return {*candidate.years, *(path.year for path in paths if path.year)}


def _classify(match: Match, following: str) -> None:
    year_match = re.match(r"\s*\((\d{4})\)", following)
    year = int(year_match[1]) if year_match else None
    context = normalize(match.context)
    theatre_context = bool(re.search(r"\b(ensemblemitglied|theater|theatre|bühne)\b", context))
    text_context = bool(year or re.search(r"\b(stück|stücks|schauspiel|text|play|roman|inszenierung)\b", context))
    required_role = next(
        (
            role
            for word, role in (
                ("regie", "director"),
                ("inszenierte", "director"),
                ("schrieb", "author"),
                ("übersetzte", "translator"),
            )
            if word in context.split()
        ),
        None,
    )
    supported = []
    for candidate in match.candidates:
        paths = match.evidence.get(str(candidate.id), [])
        years = _years_for_context(candidate, paths, required_role)
        if year and years and year not in years:
            continue
        if theatre_context and not text_context and candidate.kind == EntityType.TEXT:
            continue
        strong = [
            path
            for path in paths
            if path.strong
            and (
                not year
                or (
                    (path.start_year is None or path.start_year <= year)
                    and (path.end_year is None or year <= path.end_year)
                )
            )
        ]
        if (
            required_role
            and strong
            and not any(path.role and canonical_role(path.role) == required_role for path in strong)
        ):
            continue
        if strong and (candidate.kind != EntityType.TEXT or text_context or len(normalize(match.quote).split()) > 1):
            supported.append(candidate)
    if len(supported) == 1:
        match.confidence, match.target = "high", supported[0]
    elif len(match.candidates) == 1:
        match.confidence = "medium"
    else:
        match.confidence = "ambiguous"


def find_mentions(
    session: Session, owner: Identity, description: str, labels: Catalog
) -> tuple[list[Token], list[Match]]:
    """Resolve matching spans using the owner's recorded graph, without writes."""
    tokens = PARSER.parse(description)
    nodes = text_nodes(tokens)
    surrounding: dict[int, tuple[str, str]] = {}
    for inline in tokens:
        children = inline.children or []
        for index, child in enumerate(children):
            surrounding[id(child)] = (
                "".join(token.content for token in children[:index]),
                "".join(token.content for token in children[index + 1 :]),
            )
    matches = [
        Match(
            index,
            start,
            end,
            node.content[start:end],
            node.content[max(0, start - 80) : end + 80],
            candidates,
            {},
            "medium",
        )
        for index, node in enumerate(nodes)
        for start, end, candidates in _spans(node.content, labels, owner)
    ]
    targets = {(candidate.kind, candidate.id) for match in matches for candidate in match.candidates}
    evidence = graph_evidence(session, owner, targets) if targets else {}
    for match in matches:
        node = nodes[match.node]
        before, after = surrounding[id(node)]
        match.context = (
            (before + node.content[: match.start])[-80:] + match.quote + (node.content[match.end :] + after)[:80]
        )
        match.evidence = {
            str(candidate.id): evidence.get((candidate.kind, candidate.id), []) for candidate in match.candidates
        }
        _classify(match, node.content[match.end :] + after)
    return tokens, matches


def _key(owner: Identity, description: str, match: Match) -> str:
    return stable_digest([OPERATION, *owner, stable_digest(description), match.node, match.start, match.end])


def _description_cases(session: Session, owner: Identity, description: str) -> list[ReconciliationCase]:
    old_ids = session.exec(
        select(EntityRedirect.old_id).where(
            EntityRedirect.entity_type == owner[0],
            EntityRedirect.target_id == owner[1],
        )
    ).all()
    return list(
        session.exec(
            select(ReconciliationCase).where(
                ReconciliationCase.operation == OPERATION,
                col(ReconciliationCase.incoming)["owner_kind"].as_string() == owner[0].value,
                col(ReconciliationCase.incoming)["owner_id"].as_string().in_([str(owner[1]), *map(str, old_ids)]),
                col(ReconciliationCase.incoming)["description_hash"].as_string() == stable_digest(description),
            )
        )
    )


def _apply_decisions(
    session: Session,
    tokens: list[Token],
    matches: list[Match],
    cases: list[ReconciliationCase],
    labels: Catalog,
) -> list[Match]:
    nodes = text_nodes(tokens)
    for case in cases:
        if case.status not in {"approved", "rejected"}:
            continue
        data = case.incoming
        node, start, end = data["node"], data["start"], data["end"]
        if not 0 <= node < len(nodes) or not 0 <= start < end <= len(nodes[node].content):
            raise ValueError("Invalid stored mention span")
        if nodes[node].content[start:end] != data["quote"]:
            continue
        matches = [match for match in matches if match.node != node or match.end <= start or match.start >= end]
        if case.status == "approved" and case.target_id:
            target_id = resolve_id(session, case.entity_type, case.target_id)
            target = next(
                (
                    candidate
                    for candidate in labels.candidates
                    if (candidate.kind, candidate.id) == (case.entity_type, target_id)
                ),
                None,
            )
            if target:
                matches.append(Match(node, start, end, data["quote"], "", [], {}, "high", target))
    return sorted(matches, key=lambda match: (match.node, match.start))


def _previews(session: Session, matches: list[Match]) -> dict[Identity, dict[str, str]]:
    targets = {(match.target.kind, match.target.id) for match in matches if match.target}
    authors: dict[uuid.UUID, list[str]] = {}
    text_ids = [identity for kind, identity in targets if kind == EntityType.TEXT]
    if text_ids:
        for identity, name in session.exec(
            select(TextContribution.text_id, Person.name)
            .join(Person)
            .where(
                col(TextContribution.text_id).in_(text_ids),
                TextContribution.role == "author",
            )
        ):
            authors.setdefault(identity, []).append(name)
    result = {}
    for kind in TARGET_KINDS:
        ids = [identity for target_kind, identity in targets if target_kind == kind]
        if not ids:
            continue
        model = ENTITY_MODELS[kind]
        for entity in session.exec(select(model).where(col(model.id).in_(ids))):
            plain = " ".join(token.content for token in text_nodes(PARSER.parse(entity.description or "")))
            if isinstance(entity, Person):
                dates = " · ".join(
                    f"{label} {value.year}"
                    for label, value in (("Geboren", entity.born), ("Gestorben", entity.died))
                    if value
                )
                facts = " · ".join(filter(None, (dates, ", ".join(entity.type or []))))
                name = entity.name
            elif isinstance(entity, Text):
                dates = ", ".join(str(value.year) for value in (entity.created, entity.published) if value)
                facts = " · ".join(filter(None, (", ".join(authors.get(entity.id, [])), dates)))
                name = entity.title
            else:
                facts = getattr(entity, "address_locality", None) or ""
                name = entity.name
            result[kind, entity.id] = {
                "name": name,
                "facts": facts,
                "excerpt": plain[:240] + ("…" if len(plain) > 240 else ""),
            }
    return result


def render_description(
    owner_kind: EntityType | str, owner_id: str, description: str, labels: Catalog
) -> tuple[str, list[dict]]:
    """Build safe Markdown HTML and bounded preview data; never enqueue review cases."""
    owner = (EntityType(owner_kind), uuid.UUID(owner_id))
    with get_session() as session:
        owner = (owner[0], resolve_id(session, owner[0], owner[1]))
        if owner[0] not in OWNER_KINDS:
            raise ValueError("Invalid description owner type")
        entity = session.get(ENTITY_MODELS[owner[0]], owner[1])
        if entity is None:
            raise ValueError("Description owner no longer exists")
        tokens, matches = find_mentions(session, owner, description, labels)
        if entity.description == description:
            matches = _apply_decisions(
                session, tokens, matches, _description_cases(session, owner, description), labels
            )
        summaries = _previews(session, matches)
        theatre_ids = [identity for kind, identity in summaries if kind == EntityType.ORGANIZATION]
        verified = (
            set(
                session.exec(
                    select(Organization.id).where(
                        col(Organization.id).in_(theatre_ids),
                        verified_organization_filter(),
                    )
                ).all()
            )
            if theatre_ids
            else set()
        )
        previews, by_node, indices = [], {}, {}
        for match in matches:
            target = match.target
            if target is None or (target.kind, target.id) not in summaries:
                continue
            identity = (target.kind, target.id)
            if identity not in indices:
                indices[identity] = len(previews)
                previews.append(
                    {
                        **summaries[identity],
                        "kind": target.kind.value,
                        "id": str(target.id),
                        "verified": str(target.id in verified),
                    }
                )
            index = indices[identity]
            by_node.setdefault(match.node, []).append((match, index))
        nodes = text_nodes(tokens)
        for node_index, links in by_node.items():
            node = nodes[node_index]
            parts, position = [], 0
            for match, index in links:
                parts.extend(
                    (
                        escape(node.content[position : match.start]),
                        f'<span data-mention="{index}">{escape(match.quote)}</span>',
                    )
                )
                position = match.end
            parts.append(escape(node.content[position:]))
            node.type, node.content = "html_inline", "".join(parts)
        return PARSER.renderer.render(tokens, PARSER.options, {}), previews


def scan_descriptions(kind: EntityType | str, *, offset: int, limit: int = 50) -> dict[str, int]:
    """Scan one bounded admin-selected page and idempotently queue uncertain spans."""
    kind = EntityType(kind)
    if kind not in OWNER_KINDS or offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid description scan scope")
    with get_session() as session:
        labels = catalog(session)
        model = ENTITY_MODELS[kind]
        owners = session.exec(
            select(model).where(col(model.description).is_not(None)).order_by(col(model.id)).offset(offset).limit(limit)
        ).all()
        queued = 0
        for entity in owners:
            description = entity.description or ""
            _, matches = find_mentions(session, (kind, entity.id), description, labels)
            decided_spans = {
                (case.incoming["node"], case.incoming["start"], case.incoming["end"])
                for case in _description_cases(session, (kind, entity.id), description)
            }
            for match in matches:
                key = _key((kind, entity.id), description, match)
                if match.target or (match.node, match.start, match.end) in decided_spans:
                    continue
                incoming = {
                    "owner_kind": kind.value,
                    "owner_id": str(entity.id),
                    "field": "description",
                    "description_hash": stable_digest(description),
                    "node": match.node,
                    "start": match.start,
                    "end": match.end,
                    "quote": match.quote,
                }
                session.add(
                    ReconciliationCase(
                        key=key,
                        operation=OPERATION,
                        entity_type=match.candidates[0].kind,
                        incoming=incoming,
                        context={"matcher": MATCHER_VERSION, "match": MATCH_ADAPTER.dump_python(match, mode="json")},
                        reason=f"Erwähnung prüfen: {match.quote}",
                    )
                )
                session.flush()
                queued += 1
        session.commit()
        return {"scanned": len(owners), "queued": queued, "next_offset": offset + len(owners)}


def mention_cases(*, status: str = "pending", offset: int = 0, limit: int = 50) -> list[dict]:
    """List only mention decisions, separately from imports and duplicates."""
    if status not in {"pending", "approved", "rejected"} or offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid mention case filter")
    with get_session() as session:
        return [
            entity_snapshot(case)
            for case in session.exec(
                select(ReconciliationCase)
                .where(ReconciliationCase.operation == OPERATION, ReconciliationCase.status == status)
                .order_by(col(ReconciliationCase.created_at), col(ReconciliationCase.id))
                .offset(offset)
                .limit(limit)
            )
        ]


def _case_preview(session: Session, case: ReconciliationCase, target: Identity | None) -> dict:
    incoming = case.incoming
    kind = EntityType(incoming["owner_kind"])
    identity = resolve_id(session, kind, incoming["owner_id"])
    entity = session.get(ENTITY_MODELS[kind], identity)
    if entity is None:
        raise ValueError("Description owner no longer exists")
    description = entity.description or ""
    if stable_digest(description) != incoming["description_hash"]:
        raise ValueError("Description changed; scan and review again")
    _, matches = find_mentions(session, (kind, identity), description, catalog(session))
    match = next(
        (
            match
            for match in matches
            if (match.node, match.start, match.end, match.quote)
            == (
                incoming["node"],
                incoming["start"],
                incoming["end"],
                incoming["quote"],
            )
        ),
        None,
    )
    if match is None:
        raise ValueError("Mention changed; scan and review again")
    target_state = None
    if target:
        if target[0] not in TARGET_KINDS:
            raise ValueError("Invalid mention target type")
        target_id = resolve_id(session, target[0], target[1])
        target_entity = session.get(ENTITY_MODELS[target[0]], target_id)
        if target_entity is None or (kind, identity) == (target[0], target_id):
            raise ValueError("Mention target is missing or is the description owner")
        target_state = entity_snapshot(target_entity)
        target = (target[0], target_id)
    data = {
        "owner": entity_snapshot(entity),
        "match": MATCH_ADAPTER.dump_python(match, mode="json"),
        "target": [target[0].value, str(target[1])] if target else None,
        "target_state": target_state,
    }
    return {**data, "revision": stable_digest([case.status, MATCHER_VERSION, data])}


def preview_mention(case_id: str, *, target_kind: str | None = None, target_id: str | None = None) -> dict:
    """Return current context/evidence and a revision checked on decision."""
    if bool(target_kind) != bool(target_id):
        raise ValueError("Both target type and identity are required")
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(case_id))
        if case is None or case.operation != OPERATION:
            raise ValueError("Mention case not found")
        target = (EntityType(target_kind), uuid.UUID(target_id)) if target_kind and target_id else None
        return _case_preview(session, case, target)


def decide_mention(
    case_id: str,
    *,
    revision: str,
    actor_id: str,
    note: str,
    target_kind: str | None = None,
    target_id: str | None = None,
) -> None:
    """Approve an existing target or reject a span; never modify domain records."""
    if not actor_id.strip() or not note.strip() or bool(target_kind) != bool(target_id):
        raise ValueError("Actor, decision reason, and complete target are required")
    with get_session() as session:
        case = session.get(ReconciliationCase, uuid.UUID(case_id), with_for_update=True)
        if case is None or case.operation != OPERATION or case.status != "pending":
            raise ValueError("Mention case is missing or already decided")
        target = (EntityType(target_kind), uuid.UUID(target_id)) if target_kind and target_id else None
        preview = _case_preview(session, case, target)
        if preview["revision"] != revision:
            raise ValueError("Mention evidence changed; refresh and review again")
        if preview["target"]:
            case.entity_type, case.target_id = EntityType(preview["target"][0]), uuid.UUID(preview["target"][1])
        case.status = "approved" if target else "rejected"
        case.actor_id, case.decision_note, case.decided_at = actor_id, note.strip(), dt.datetime.now(dt.UTC)
        case.context = {**case.context, "decision": preview}
        session.add(case)
        session.commit()
