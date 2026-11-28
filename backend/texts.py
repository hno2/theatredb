"""Shared text identity, computed work families, and reviewed lineage changes."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_
from sqlmodel import Session, col, select

from backend.config import ROLE_LABELS
from backend.models import (
    EntityType,
    LineageRelation,
    Person,
    Production,
    ReconciliationCase,
    Text,
    TextContribution,
    TextLineage,
    TextTitle,
    normalize_text_languages,
)
from backend.reconciliation import audit_event, ensure_credit, normalize, resolve_id, stable_digest

FAMILY_RELATIONS = frozenset({LineageRelation.TRANSLATION, LineageRelation.ADAPTATION})
LABEL_ROLES = ("author", "translator", "adapter")
CREDIT_ROLES = ("composer", "author", "lyricist", "translator", "adapter")


@dataclass(slots=True)
class TextFamilyView:
    """Data needed to render text-family details without UI-layer traversal."""

    member_ids: set[uuid.UUID]
    is_root: bool
    credit_line: str
    labels: dict[str, str]
    ancestors: set[uuid.UUID]
    descendants: set[uuid.UUID]
    children: dict[uuid.UUID, list[tuple[uuid.UUID, LineageRelation]]]
    productions: dict[uuid.UUID, list[str]]
    texts: dict[uuid.UUID, Text]


def _contributions(
    session: Session, ids: set[uuid.UUID], roles: tuple[str, ...]
) -> list[tuple[uuid.UUID, str | None, str]]:
    return list(
        session.exec(
            select(TextContribution.text_id, TextContribution.role, Person.name)
            .join(Person, col(Person.id) == col(TextContribution.person_id))
            .where(col(TextContribution.text_id).in_(ids), col(TextContribution.role).in_(roles))
            .order_by(Person.name)
        )
    )


def _format_labels(
    texts: list[Text], roots_by_text: dict[uuid.UUID, list[Text]], credits: Iterable[tuple[uuid.UUID, str | None, str]]
) -> dict[str, str]:
    contributions: dict[uuid.UUID, list[str]] = {}
    for text_id, role, name in credits:
        if role is None:
            raise ValueError("Text picker contribution has no role")
        if role in LABEL_ROLES:
            contributions.setdefault(text_id, []).append(f"{ROLE_LABELS[role]}: {name}")
    labels = {}
    for text in texts:
        details = [", ".join(text.languages), ", ".join(contributions.get(text.id, []))]
        roots_label = ", ".join(
            root.title for root in roots_by_text[text.id] if root.id != text.id and root.title != text.title
        )
        if roots_label:
            details.insert(0, f"Werk: {roots_label}")
        labels[str(text.id)] = text.title + (" — " + "; ".join(d for d in details if d) if any(details) else "")
    counts = Counter(labels.values())
    return {key: f"{label} [{key[:8]}]" if counts[label] > 1 else label for key, label in labels.items()}


def labels_for_texts(session: Session, texts: list[Text]) -> dict[str, str]:
    """Make text choices distinguishable without exposing raw foreign keys."""
    if not texts:
        return {}
    ids = {text.id for text in texts}
    roots = family_roots_many(session, ids)
    return _format_labels(texts, roots, _contributions(session, ids, LABEL_ROLES))


def _walk_lineage(
    session: Session, seeds: set[uuid.UUID], relations: Iterable[LineageRelation], *, reverse: bool | None
) -> tuple[set[uuid.UUID], list[tuple[uuid.UUID, uuid.UUID, LineageRelation]]]:
    """Fetch one frontier at a time; None follows edges in both directions."""
    seen, frontier = set(seeds), seeds
    edges: dict[tuple[uuid.UUID, uuid.UUID, LineageRelation], None] = {}
    while frontier:
        outgoing = col(TextLineage.source_text_id).in_(frontier)
        incoming = col(TextLineage.derived_text_id).in_(frontier)
        endpoints = outgoing if reverse is False else incoming if reverse is True else or_(outgoing, incoming)
        rows = session.exec(
            select(TextLineage.source_text_id, TextLineage.derived_text_id, TextLineage.relation).where(
                endpoints, col(TextLineage.relation).in_(relations)
            )
        ).all()
        for source, derived, relation in rows:
            edges[source, derived, LineageRelation(relation)] = None
        frontier = {item for source, derived, _ in rows for item in (source, derived)} - seen
        seen.update(frontier)
    return seen, list(edges)


def _roots_by_text(
    texts: dict[uuid.UUID, Text], edges: list[tuple[uuid.UUID, uuid.UUID, LineageRelation]]
) -> dict[uuid.UUID, list[Text]]:
    neighbors: dict[uuid.UUID, set[uuid.UUID]] = {}
    derived_ids = set()
    for source, derived, _ in edges:
        neighbors.setdefault(source, set()).add(derived)
        neighbors.setdefault(derived, set()).add(source)
        derived_ids.add(derived)
    roots_by_text: dict[uuid.UUID, list[Text]] = {}
    for text_id in texts:
        if text_id in roots_by_text:
            continue
        component, frontier = {text_id}, {text_id}
        while frontier:
            frontier = {neighbor for item in frontier for neighbor in neighbors.get(item, ())} - component
            component.update(frontier)
        roots = sorted((texts[item] for item in component - derived_ids), key=lambda text: (text.title, text.id))
        roots_by_text.update(dict.fromkeys(component, roots))
    return roots_by_text


def _detail_credit_line(
    text: Text,
    upstream: list[tuple[uuid.UUID, uuid.UUID, LineageRelation]],
    contributions: Iterable[tuple[uuid.UUID, str | None, str]],
) -> str:
    parents: dict[uuid.UUID, set[uuid.UUID]] = {}
    sources = set()
    for source, derived, relation in upstream:
        if relation == LineageRelation.BASED_ON:
            sources.add(source)
        else:
            parents.setdefault(derived, set()).add(source)
    credits: dict[tuple[uuid.UUID, str | None], set[str]] = {}
    for text_id, role, name in contributions:
        credits.setdefault((text_id, role), set()).add(name)
    parts = []
    for role in CREDIT_ROLES:
        names = (
            _inherited_names(text.id, role, parents, credits)
            if role in {"composer", "author", "lyricist"}
            else credits.get((text.id, role), set())
        )
        if names:
            label = "Buch" if role == "author" and text.genre == "musical" else ROLE_LABELS[role]
            parts.append(f"{label} {', '.join(sorted(names))}")
    names = {name for source in sources for name in _inherited_names(source, "author", parents, credits)}
    if names:
        parts.append(f"nach {', '.join(sorted(names))}")
    return " · ".join(parts)


def _based_on_ancestors(
    member_ids: set[uuid.UUID], upstream: list[tuple[uuid.UUID, uuid.UUID, LineageRelation]]
) -> set[uuid.UUID]:
    parents: dict[uuid.UUID, set[uuid.UUID]] = {}
    for source, derived, relation in upstream:
        if relation == LineageRelation.BASED_ON:
            parents.setdefault(derived, set()).add(source)
    seen, frontier = set(member_ids), member_ids
    while frontier:
        frontier = {parent for item in frontier for parent in parents.get(item, ())} - seen
        seen.update(frontier)
    return seen - member_ids


def text_family_view(session: Session, text_id: uuid.UUID) -> TextFamilyView:
    """Load text-family data for management rendering without putting traversal in the UI."""
    text = session.get(Text, text_id)
    if text is None:
        raise ValueError("Text not found")
    member_ids, family_edges = _walk_lineage(session, {text_id}, FAMILY_RELATIONS, reverse=None)
    upstream_ids, upstream = _walk_lineage(session, member_ids, LineageRelation, reverse=True)
    ancestors = _based_on_ancestors(member_ids, upstream)
    downstream_ids, _ = _walk_lineage(session, member_ids, (LineageRelation.BASED_ON,), reverse=False)
    descendants = downstream_ids - member_ids
    relatives = ancestors | descendants
    label_ids, label_edges = _walk_lineage(session, relatives, FAMILY_RELATIONS, reverse=None)
    visible_ids = member_ids | relatives
    text_ids = member_ids | label_ids
    texts = {
        item.id: item
        for item in session.exec(select(Text).where(col(Text.id).in_(text_ids)).order_by(Text.title, col(Text.id)))
    }
    roots = _roots_by_text(texts, family_edges + label_edges)
    credits = _contributions(session, upstream_ids | visible_ids, CREDIT_ROLES)
    labels = _format_labels([texts[item] for item in visible_ids], roots, credits)
    productions: dict[uuid.UUID, list[str]] = {}
    for production_text_id, name in session.exec(
        select(Production.text_id, Production.name).where(col(Production.text_id).in_(member_ids))
    ):
        if production_text_id is not None:
            productions.setdefault(production_text_id, []).append(name)
    children: dict[uuid.UUID, list[tuple[uuid.UUID, LineageRelation]]] = {}
    for source, derived, relation in family_edges:
        children.setdefault(source, []).append((derived, relation))
    derived_ids = {derived for _, derived, _ in family_edges}
    return TextFamilyView(
        member_ids=member_ids,
        is_root=text_id not in derived_ids,
        credit_line=_detail_credit_line(text, upstream, credits),
        labels=labels,
        ancestors=ancestors,
        descendants=descendants,
        children=children,
        productions=productions,
        texts=texts,
    )


def add_title(session: Session, text: Text, title: str, language: str | None = None) -> TextTitle:
    """Keep title variants unique within a text, using the common normalization."""
    normalized = normalize(title)
    if not normalized:
        raise ValueError("Text title must not be empty")
    if language is not None:
        language = normalize_text_languages([language])[0]
        if language == "mul":
            raise ValueError("Title language must be a two-letter code")
    session.add(text)
    session.flush()
    existing = session.exec(
        select(TextTitle).where(TextTitle.text_id == text.id, TextTitle.normalized == normalized)
    ).first()
    if existing is not None:
        if existing.language is None and language is not None:
            existing.language = language
            session.add(existing)
        return existing
    row = TextTitle(text_id=text.id, title=title.strip(), language=language, normalized=normalized)
    session.add(row)
    session.flush()
    return row


def add_lineage(
    session: Session,
    source_id: uuid.UUID,
    derived_id: uuid.UUID,
    relation: LineageRelation | str,
    notes: str | None = None,
) -> TextLineage:
    """Add an idempotent directed edge; flush validates the complete graph."""
    relation = LineageRelation(relation)
    if source_id == derived_id:
        raise ValueError("A text cannot derive from itself")
    if session.get(Text, source_id) is None or session.get(Text, derived_id) is None:
        raise ValueError("Both lineage texts must exist")
    existing = session.exec(
        select(TextLineage).where(
            TextLineage.source_text_id == source_id,
            TextLineage.derived_text_id == derived_id,
            TextLineage.relation == relation,
        )
    ).first()
    if existing is not None:
        if notes and notes != existing.notes:
            existing.notes = "\n".join(filter(None, (existing.notes, notes)))
            session.add(existing)
        return existing
    row = TextLineage(source_text_id=source_id, derived_text_id=derived_id, relation=relation, notes=notes)
    session.add(row)
    session.flush()
    return row


def work_family(session: Session, text_id: uuid.UUID) -> list[Text]:
    """Compute an undirected translation/adaptation component; based_on never joins works."""
    if session.get(Text, text_id) is None:
        raise ValueError("Text not found")
    components, _ = _family_components(session, {text_id})
    return list(
        session.exec(select(Text).where(col(Text.id).in_(components[text_id])).order_by(Text.title, col(Text.id))).all()
    )


def _family_components(
    session: Session, text_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, frozenset[uuid.UUID]], set[uuid.UUID]]:
    neighbors: dict[uuid.UUID, set[uuid.UUID]] = {}
    incoming = set()
    edges = session.exec(
        select(TextLineage.source_text_id, TextLineage.derived_text_id).where(
            col(TextLineage.relation).in_(FAMILY_RELATIONS)
        )
    ).all()
    for source_id, derived_id in edges:
        neighbors.setdefault(source_id, set()).add(derived_id)
        neighbors.setdefault(derived_id, set()).add(source_id)
        incoming.add(derived_id)
    components: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
    for text_id in text_ids:
        if text_id in components:
            continue
        found = {text_id}
        pending = deque([text_id])
        while pending:
            for neighbor in neighbors.get(pending.popleft(), ()):
                if neighbor not in found:
                    found.add(neighbor)
                    pending.append(neighbor)
        component = frozenset(found)
        components.update(dict.fromkeys(found, component))
    return components, incoming


def family_roots(session: Session, text_id: uuid.UUID) -> list[Text]:
    """Return all roots, preserving multi-source adaptations rather than inventing one."""
    return family_roots_many(session, [text_id])[text_id]


def family_roots_many(session: Session, text_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Text]]:
    """Load roots for many texts in two queries, traversing each component once."""
    requested = set(text_ids)
    if not requested:
        return {}
    components, incoming = _family_components(session, requested)
    root_ids = set(components) - incoming
    roots = {text.id: text for text in session.exec(select(Text).where(col(Text.id).in_(root_ids))).all()}
    if root_ids - roots.keys():
        raise ValueError("Text not found")
    by_component = {
        component: sorted((roots[text_id] for text_id in component - incoming), key=lambda text: (text.title, text.id))
        for component in set(components.values())
    }
    return {text_id: by_component[components[text_id]] for text_id in requested}


def _names(session: Session, texts: Iterable[Text], role: str) -> set[str]:
    ids = {text.id for text in texts}
    if not ids:
        return set()
    rows = session.exec(
        select(Person.name)
        .join(TextContribution, col(TextContribution.person_id) == col(Person.id))
        .where(col(TextContribution.text_id).in_(ids), TextContribution.role == role)
    ).all()
    return {normalize(name) for name in rows}


class CreditPeople:
    """Lazy name lookup for one import or resolution, not a session-wide cache."""

    def __init__(self, session: Session) -> None:
        """Bind the lookup to the caller's session."""
        self._session = session
        self._ids_by_name: dict[str, list[uuid.UUID]] | None = None
        self.ambiguous_credits: list[tuple[str, str]] = []

    def person_id(self, name: str, role: str) -> uuid.UUID | None:
        """Reuse the unique normalized match or create a person for this role.

        Returns None if the credit is ambiguous (matches multiple people);
        caller should handle via add_credits to create a reconciliation case.
        """
        if self._ids_by_name is None:
            ids_by_name: dict[str, list[uuid.UUID]] = {}
            for person_id, stored_name in self._session.exec(select(Person.id, Person.name)):
                ids_by_name.setdefault(normalize(stored_name), []).append(person_id)
            self._ids_by_name = ids_by_name
        key = normalize(name)
        matches = self._ids_by_name.get(key, [])
        if len(matches) > 1:
            self.ambiguous_credits.append((name, role))
            return None
        if matches:
            self._session.flush()
            return matches[0]
        person = Person(name=name, type=[role])
        self._session.add(person)
        self._session.flush()
        self._ids_by_name[key] = [person.id]
        return person.id

    def add_credits(self, text: Text, names: Iterable[str], role: str) -> None:
        """Attach credits in input order without duplicating them.

        Ambiguous credits (multiple person matches) are tracked but not attached;
        callers should check ambiguous_credits and create reconciliation cases.
        """
        for name in dict.fromkeys(name.strip() for name in names if name.strip()):
            person_id = self.person_id(name, role)
            if person_id is not None:
                ensure_credit(self._session, text, person_id, role, None, None)


def _link_case(session: Session, payload: dict, candidates: list[Text], reason: str) -> Text | ReconciliationCase:
    key = f"link_text:{stable_digest(payload)}"
    case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
    if case is None:
        case = ReconciliationCase(
            key=key,
            entity_type=EntityType.TEXT,
            operation="link_text",
            incoming=payload,
            context={"candidate_ids": [str(text.id) for text in candidates]},
            reason=reason,
        )
        session.add(case)
        session.flush()
    if case.status == "approved" and case.target_id is not None:
        target = session.get(Text, resolve_id(session, EntityType.TEXT, case.target_id))
        if target is None:
            raise ValueError("Approved text link points to a missing text")
        return target
    return case


def _lineage_relation(value: LineageRelation | str) -> LineageRelation:
    relation = LineageRelation(value)
    if relation not in {LineageRelation.TRANSLATION, LineageRelation.BASED_ON}:
        raise ValueError("Text sources must use translation or based_on lineage")
    return relation


def resolve_text(
    session: Session,
    title: str,
    authors: Iterable[str],
    translators: Iterable[str] = (),
    language: str | None = None,
    based_on: Text | dict | str | None = None,
    *,
    people: CreditPeople | None = None,
    adapters: Iterable[str] = (),
    genre: str | None = None,
    lineage_relation: LineageRelation | str = LineageRelation.BASED_ON,
) -> Text | ReconciliationCase:
    """Match titles across families, then choose the credited translation without guessing."""
    normalized = normalize(title)
    if not normalized:
        raise ValueError("Text title must not be empty")
    if language is not None:
        language = normalize_text_languages([language])[0]
    authors, translators = list(authors), list(translators)
    adapters = list(adapters)
    lineage_relation = _lineage_relation(lineage_relation)
    people = people or CreditPeople(session)
    source = _resolve_source(session, based_on, people)
    payload: dict = {"title": title, "authors": authors, "translators": translators, "language": language}
    if genre:
        payload["genre"] = genre
    if adapters:
        payload["adapters"] = adapters
    if based_on is not None:
        payload["based_on"] = {"text_id": str(source.id)} if isinstance(source, Text) else based_on
        payload["lineage_relation"] = lineage_relation.value
    if isinstance(source, ReconciliationCase):
        return _source_review(session, source, payload)
    matched = _match_text(session, payload, source, people)
    if matched is not None:
        return matched
    text = Text.model_validate({"title": title, "languages": [language] if language else [], "genre": genre})
    session.add(text)
    session.flush()
    if source is None or lineage_relation != LineageRelation.TRANSLATION:
        people.add_credits(text, authors, "author")
    people.add_credits(text, translators, "translator")
    people.add_credits(text, adapters, "adapter")
    if people.ambiguous_credits:
        key = f"ambiguous_credits:{stable_digest(payload)}"
        case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
        if case is None:
            case = ReconciliationCase(
                key=key,
                entity_type=EntityType.TEXT,
                operation="ambiguous_credits",
                target_id=text.id,
                incoming=payload,
                context={"ambiguous_credits": people.ambiguous_credits},
                reason=f"Ambiguous person credits: {'; '.join(f'{name} ({role})' for name, role in people.ambiguous_credits)}",
            )
            session.add(case)
    if source is not None:
        add_lineage(session, source.id, text.id, lineage_relation)
    return text


def _match_text(
    session: Session, payload: dict, source: Text | None, people: CreditPeople
) -> Text | ReconciliationCase | None:
    text_ids = session.exec(
        select(Text.id).join(TextTitle).where(TextTitle.normalized == normalize(payload["title"])).distinct()
    ).all()
    matches = list(session.exec(select(Text).where(Text.id.in_(text_ids))).all())
    if genre := payload.get("genre"):
        compatible = {None, genre} | ({"novel", "film"} if genre == "other" else set())
        matches = [match for match in matches if match.genre in compatible]
    if source is not None:
        candidates = _source_title_matches(session, source, matches, payload)
        if isinstance(candidates, ReconciliationCase):
            return candidates
        matches = candidates
    if adapters := payload.get("adapters"):
        adapter_names = {normalize(name) for name in adapters}
        matches = [match for match in matches if adapter_names == _names(session, [match], "adapter")]
    author_names = {normalize(name) for name in payload["authors"]}
    families: dict[frozenset[uuid.UUID], list[Text]] = {}
    for match in matches:
        family = work_family(session, match.id)
        known_authors = _names(session, family_roots(session, match.id), "author")
        if not author_names or not known_authors or author_names & known_authors:
            families[frozenset(text.id for text in family)] = family
    if len(families) > 1:
        return _link_case(session, payload, matches, "Several works share this title; select a text.")
    if families:
        return _choose_family_text(session, next(iter(families.values())), matches, payload, people)
    return None


def _source_title_matches(
    session: Session, source: Text, matches: list[Text], payload: dict
) -> list[Text] | ReconciliationCase:
    matches = [match for match in matches if match.id != source.id]
    relation = LineageRelation(payload.get("lineage_relation", LineageRelation.BASED_ON))
    linked_ids = set(
        session.exec(
            select(TextLineage.derived_text_id).where(
                TextLineage.source_text_id == source.id, TextLineage.relation == relation
            )
        ).all()
    )
    linked = [match for match in matches if match.id in linked_ids]
    if not linked and matches:
        reason = (
            "Confirm the source work for this translation."
            if relation == LineageRelation.TRANSLATION
            else ("Confirm the source work for this adaptation.")
        )
        result = _link_case(session, payload, matches, reason)
        if isinstance(result, ReconciliationCase):
            result.context = {
                **result.context,
                "credit_inputs": [{"source_id": str(source.id), "relation": relation.value}],
            }
            session.add(result)
        return [result] if isinstance(result, Text) else result
    return linked


def _source_review(session: Session, source: ReconciliationCase, payload: dict) -> Text | ReconciliationCase:
    candidates = [
        candidate
        for candidate_id in source.context.get("candidate_ids", [])
        if (candidate := session.get(Text, uuid.UUID(candidate_id))) is not None
    ]
    case = _link_case(session, payload, candidates, "Select a source work before creating the derived text.")
    if isinstance(case, ReconciliationCase):
        case.context = {**case.context, "pending_derived": payload, "source_case_id": str(source.id)}
        session.add(case)
    return case


def _resolve_source(
    session: Session, source: Text | dict | str | None, people: CreditPeople
) -> Text | ReconciliationCase | None:
    if source is None or isinstance(source, Text):
        return source
    if isinstance(source, dict) and "text_id" in source:
        text = session.get(Text, resolve_id(session, EntityType.TEXT, source["text_id"]))
        if text is None:
            raise ValueError("Source text not found")
        return text
    return resolve_staged_text(session, {"title": source} if isinstance(source, str) else source, people=people)


def _choose_family_text(
    session: Session, family: list[Text], title_matches: list[Text], payload: dict, people: CreditPeople
) -> Text | ReconciliationCase:
    title, language = payload["title"], payload["language"]
    if payload["translators"]:
        return _resolve_translation(session, family, payload, people)
    matching = [text for text in family if text in title_matches]
    matching_language = [
        text for text in family if language and text.languages and text.languages[0] == language.lower()
    ]
    roots = family_roots(session, family[0].id)
    for matches in (matching_language, matching, roots):
        if len(matches) == 1:
            selected = matches[0]
            if language and not selected.languages:
                selected.languages = [language]
                session.add(selected)
            root_authors = _names(session, roots, "author")
            if not root_authors and payload["authors"] and len(roots) == 1:
                people.add_credits(roots[0], payload["authors"], "author")
            add_title(session, selected, title, language if language != "mul" else None)
            return selected
    return _link_case(session, payload, matching, "Select the most specific known text.")


def _resolve_translation(
    session: Session, family: list[Text], payload: dict, people: CreditPeople
) -> Text | ReconciliationCase:
    translator_names = {normalize(name) for name in payload["translators"]}
    language = payload["language"]
    matches = [
        text
        for text in family
        if translator_names <= _names(session, [text], "translator")
        and (not language or not text.languages or text.languages[0] == language.lower())
    ]
    if len(matches) == 1:
        add_title(session, matches[0], payload["title"], language if language != "mul" else None)
        return matches[0]
    if len(matches) > 1:
        return _link_case(session, payload, matches, "Several texts share these translator credits.")
    roots = family_roots(session, family[0].id)
    if len(roots) != 1:
        return _link_case(session, payload, roots, "Several possible source texts for this translation.")
    text = Text.model_validate({"title": payload["title"], "languages": [language or "de"], "genre": roots[0].genre})
    session.add(text)
    session.flush()
    people.add_credits(text, payload["translators"], "translator")
    add_lineage(session, roots[0].id, text.id, LineageRelation.TRANSLATION)
    return text


def resolve_staged_text(
    session: Session, data: dict, *, people: CreditPeople | None = None
) -> Text | ReconciliationCase:
    """Use one normalized contract for theatre and publisher text metadata."""
    people = people or CreditPeople(session)
    original_title = data.get("original_title")
    authors = data.get("authors", [])
    if original_title:
        root = resolve_text(
            session,
            original_title,
            authors,
            language=data.get("original_language"),
            based_on=data.get("based_on"),
            people=people,
            genre=data.get("genre"),
        )
        if isinstance(root, ReconciliationCase):
            candidates = [
                candidate
                for candidate_id in root.context.get("candidate_ids", [])
                if (candidate := session.get(Text, uuid.UUID(candidate_id))) is not None
            ]
            incoming = {
                "title": data["title"],
                "authors": authors,
                "translators": data.get("translators", []),
                "language": data.get("language"),
                "lineage_relation": LineageRelation.TRANSLATION.value,
            }
            case = _link_case(
                session, incoming, candidates, "Select the original work before importing its translation."
            )
            if isinstance(case, ReconciliationCase):
                pending = {key: value for key, value in data.items() if key != "original_title"}
                pending["lineage_relation"] = LineageRelation.TRANSLATION.value
                case.context = {
                    **case.context,
                    "pending_derived": pending,
                    "source_case_id": str(root.id),
                }
                session.add(case)
            return case
        if root.genre is None and data.get("genre"):
            root.genre = data["genre"]
        if data.get("original_subtitle") and not root.subtitle:
            root.subtitle = data["original_subtitle"]
            session.add(root)
        result = resolve_text(
            session,
            data["title"],
            authors,
            data.get("translators", []),
            data.get("language"),
            root,
            people=people,
            genre=data.get("genre"),
            lineage_relation=LineageRelation.TRANSLATION,
        )
    else:
        result = resolve_text(
            session,
            data["title"],
            authors,
            data.get("translators", []),
            data.get("language"),
            data.get("based_on"),
            people=people,
            adapters=data.get("adapters", []),
            genre=data.get("genre"),
            lineage_relation=data.get("lineage_relation", LineageRelation.BASED_ON),
        )
    if isinstance(result, Text):
        fill_text_metadata(session, result, data)
        for role in ("composer", "lyricist", "adapter", "editor"):
            people.add_credits(result, data.get(f"{role}s", []), role)
        session.add(result)
    return result


def _save_credit_review_case(
    session: Session,
    result: ReconciliationCase,
    text_id: uuid.UUID,
    credit: str,
    parsed: Mapping[str, Any],
    language: str | None,
    source_id: str | None,
    production_id: str | None,
    relink: bool,
) -> ReconciliationCase:
    if result.status != "pending":
        raise ValueError("Dieser Prüffall ist nicht offen. Bitte zuerst erneut öffnen.")
    context = dict(result.context or {})
    production = session.get(Production, uuid.UUID(production_id)) if production_id else None
    if production_id and production is None:
        raise ValueError("Production no longer exists")
    if production is not None:
        production_ids = list(context.get("production_ids", []))
        if context.get("production_id"):
            production_ids.append(str(context["production_id"]))
        context["production_ids"] = list(dict.fromkeys([*production_ids, production_id]))
        text_ids = dict(context.get("production_text_ids", {}))
        text_ids.setdefault(production_id, str(production.text_id) if production.text_id else None)
        context["production_text_ids"] = text_ids
        adapters = dict(context.get("production_adapter_names", {}))
        adapters[production_id] = list(dict.fromkeys([*adapters.get(production_id, []), *parsed["adapters"]]))
        context["production_adapter_names"] = adapters
    inputs = list(context.get("credit_inputs", []))
    entry = {
        "text_id": str(text_id),
        "credit": credit,
        "parsed": dict(parsed),
        "language": language,
        "source_id": source_id,
        "production_id": production_id,
        "relink": relink,
    }
    if entry not in inputs:
        inputs.append(entry)
    context["credit_inputs"] = inputs
    result.context = context
    session.add(result)
    return result


def _apply_text_credit(
    session: Session,
    result: Text,
    parsed: Mapping[str, Any],
    source_id: str | None,
    production_id: str | None,
    relink: bool,
    actor_id: str | None,
) -> Text | ReconciliationCase:
    if source_id:
        roots = family_roots(session, result.id)
        if len(roots) != 1:
            raise ValueError("Die Werkfamilie hat mehrere Ursprünge; Vorlage bitte direkt verknüpfen.")
        add_lineage(session, uuid.UUID(source_id), roots[0].id, LineageRelation.BASED_ON)
    if production_id:
        production = session.get(Production, uuid.UUID(production_id))
        if production is None:
            raise ValueError("Production no longer exists")
        people = CreditPeople(session)
        for name in dict.fromkeys(name.strip() for name in parsed["adapters"] if name.strip()):
            person_id = people.person_id(name, "adapter")
            if person_id is not None:
                ensure_credit(session, production, person_id, "adapter", None, actor_id)
        if people.ambiguous_credits:
            key = f"ambiguous_production_adapters:{stable_digest({'production_id': production_id, 'credits': people.ambiguous_credits})}"
            case = session.exec(select(ReconciliationCase).where(ReconciliationCase.key == key)).first()
            if case is None:
                case = ReconciliationCase(
                    key=key,
                    entity_type=EntityType.PRODUCTION,
                    operation="ambiguous_credits",
                    target_id=production_id,
                    incoming={"adapters": parsed["adapters"]},
                    context={"ambiguous_credits": people.ambiguous_credits, "text_id": str(result.id)},
                    reason=f"Ambiguous adapter credits: {'; '.join(f'{name} (adapter)' for name, _ in people.ambiguous_credits)}",
                )
                session.add(case)
            return case
        if relink:
            production.text_id = result.id
            session.add(production)
    return result


def save_text_credit(
    session: Session,
    text: Text,
    credit: str,
    parsed: Mapping[str, Any],
    language: str | None,
    source_id: str | None,
    production_id: str | None,
    relink: bool,
    actor_id: str | None,
) -> Text | ReconciliationCase:
    """Apply a reviewed credit and any selected source/production links in the caller's transaction."""
    title = parsed.get("title") or text.title
    result = (
        resolve_staged_text(session, {"title": title, "authors": parsed["authors"]})
        if parsed["authors"] or parsed.get("title")
        else text
    )
    if not isinstance(result, ReconciliationCase) and parsed["translators"]:
        result = resolve_staged_text(
            session,
            {
                "title": title,
                "authors": parsed["authors"],
                "translators": parsed["translators"],
                "language": language,
            },
        )
    if isinstance(result, ReconciliationCase):
        return _save_credit_review_case(
            session, result, text.id, credit, parsed, language, source_id, production_id, relink
        )
    return _apply_text_credit(session, result, parsed, source_id, production_id, relink, actor_id)


def fill_text_metadata(session: Session, text: Text, data: dict) -> None:
    """Fill unknown text fields and add attested title variants without overwriting edits."""
    fields = (
        "genre",
        "description",
        "description_en",
        "published",
        "published_precision",
        "subtitle",
        "subtitle_en",
        "keywords",
        "genre_labels",
        "women_min",
        "women_max",
        "men_min",
        "men_max",
        "total_cast_min",
        "total_cast_max",
        "cast_note",
        "cast_note_en",
        "suggested_min_age",
        "is_free_for_school_amateur_play",
        "is_novel_or_film_material",
        "released_for_udse",
        "has_role_book",
        "foreign_rights",
        "school_amateur_note",
        "foreign_rights_note",
        "translations_note",
        "translations_note_en",
    )
    updates = {
        key: data[key] for key in fields if getattr(text, key) in (None, "", []) and data.get(key) not in (None, "", [])
    }
    if "published" in updates:
        updates["published_precision"] = data.get("published_precision")
    if updates:
        validated = Text.model_validate({**text.model_dump(), **updates})
        for key in updates:
            setattr(text, key, getattr(validated, key))
        session.add(text)
    for title in data.get("titles", []):
        add_title(session, text, title["title"], title.get("language"))


def _inherited_names(
    text_id: uuid.UUID,
    role: str,
    parents: dict[uuid.UUID, set[uuid.UUID]],
    credits: dict[tuple[uuid.UUID, str | None], set[str]],
) -> set[str]:
    frontier, seen = {text_id}, set()
    while frontier:
        names = {name for member in frontier for name in credits.get((member, role), ())}
        if names:
            return names
        seen.update(frontier)
        frontier = {parent for member in frontier for parent in parents.get(member, ())} - seen
    return set()


def credit_line(session: Session, text_id: uuid.UUID) -> str:
    """Render direct credits and source authors reached through based_on edges."""
    family = work_family(session, text_id)
    texts = {text.id: text for text in family}
    if text_id not in texts:
        raise ValueError("Text not found")
    ancestors = {text.id for text in family}
    seen = set(ancestors)
    sources: set[uuid.UUID] = set()
    edges = session.exec(select(TextLineage.source_text_id, TextLineage.derived_text_id, TextLineage.relation)).all()
    parents: dict[uuid.UUID, set[uuid.UUID]] = {}
    for source_id, derived_id, relation in edges:
        if relation in FAMILY_RELATIONS:
            parents.setdefault(derived_id, set()).add(source_id)
    while ancestors:
        preceding = {source_id for source_id, derived_id, _ in edges if derived_id in ancestors}
        sources.update(
            source_id
            for source_id, derived_id, relation in edges
            if derived_id in ancestors and relation == LineageRelation.BASED_ON
        )
        ancestors = preceding - seen
        seen.update(preceding)
    roles = ("composer", "author", "lyricist", "translator", "adapter")
    credits: dict[tuple[uuid.UUID, str | None], set[str]] = {}
    for member_id, role, name in session.exec(
        select(TextContribution.text_id, TextContribution.role, Person.name)
        .join(Person, col(Person.id) == col(TextContribution.person_id))
        .where(col(TextContribution.text_id).in_(seen), col(TextContribution.role).in_(roles))
    ).all():
        credits.setdefault((member_id, role), set()).add(name)
    parts = []
    for role in roles:
        names = (
            _inherited_names(text_id, role, parents, credits)
            if role in {"composer", "author", "lyricist"}
            else credits.get((text_id, role), set())
        )
        if names:
            label = "Buch" if role == "author" and texts[text_id].genre == "musical" else ROLE_LABELS[role]
            parts.append(f"{label} {', '.join(sorted(names))}")
    if sources:
        names = set().union(*(_inherited_names(source_id, "author", parents, credits) for source_id in sources))
        if names:
            parts.append(f"nach {', '.join(sorted(names))}")
    return " · ".join(parts)


def resolve_text_case(
    session: Session,
    case_id: uuid.UUID | str,
    target_id: uuid.UUID | str | None = None,
    relation: LineageRelation | str | None = None,
    source_id: uuid.UUID | str | None = None,
    actor_id: str = "management",
) -> Text:
    """Apply an explicit text/lineage decision within the caller's transaction."""
    case = session.get(ReconciliationCase, uuid.UUID(str(case_id)), with_for_update=True)
    if case is None or case.status != "pending" or case.operation not in {"link_text", "text_lineage"}:
        raise ValueError("Pending text review case required")
    chosen = target_id or case.target_id
    if chosen is None:
        raise ValueError("Select a text")
    text = session.get(Text, resolve_id(session, EntityType.TEXT, chosen))
    if text is None:
        raise ValueError("Text not found")
    if case.operation == "link_text":
        text = _reviewed_text(session, case, text, actor_id)
    if case.operation == "text_lineage":
        source_id = source_id or case.incoming.get("source_candidate_id") or case.incoming.get("source_text_id")
        relation = relation or case.incoming.get("relation")
        if source_id is None or relation is None:
            raise ValueError("Source text and relation required")
        source_uuid = resolve_id(session, EntityType.TEXT, source_id)
        edge = add_lineage(session, source_uuid, text.id, relation, case.incoming.get("reason"))
        audit_event(session, edge, "insert", actor_id)
    else:
        _apply_text_link(session, case, text, actor_id)
    case.target_id, case.status, case.actor_id = text.id, "approved", actor_id
    case.decided_at = dt.datetime.now(dt.UTC)
    session.add(case)
    session.flush()
    return text


def _apply_text_link(session: Session, case: ReconciliationCase, text: Text, actor_id: str) -> None:
    production_ids = list(case.context.get("production_ids", []))
    if case.context.get("production_id"):
        production_ids.append(case.context["production_id"])
    adapters = case.context.get("production_adapter_names", {})
    people = CreditPeople(session)
    for production_id in dict.fromkeys(production_ids):
        production = session.get(Production, uuid.UUID(str(production_id)), with_for_update=True)
        if production is None:
            raise ValueError("Production no longer exists")
        expected = case.context.get("production_text_ids", {}).get(str(production.id))
        allowed = {None, text.id} if expected is None else {uuid.UUID(expected), text.id}
        if production.text_id not in allowed:
            raise ValueError("Production text changed since proposal")
        credit_inputs = [
            entry for entry in case.context.get("credit_inputs", []) if entry.get("production_id") == str(production.id)
        ]
        if not credit_inputs or credit_inputs[-1].get("relink"):
            production.text_id = text.id
            session.add(production)
            audit_event(session, production, "update", actor_id)
        for name in adapters.get(str(production.id), []):
            person_id = people.person_id(name, "adapter")
            if person_id is not None:
                ensure_credit(session, production, person_id, "adapter", None, actor_id)
    _apply_reviewed_sources(session, case, text)
    _apply_publisher_observations(session, case, text)


def _apply_publisher_observations(session: Session, case: ReconciliationCase, text: Text) -> None:
    if not (observations := case.context.get("publisher_observations")):
        return
    from backend.ingestion.publishers import resolve_publisher_observations  # ruff: ignore[import-outside-top-level]

    resolve_publisher_observations(session, text, observations)
    for observation in observations:
        fill_text_metadata(session, text, observation)
        if original_subtitle := observation.get("original_subtitle"):
            roots = family_roots(session, text.id)
            if len(roots) == 1 and not roots[0].subtitle:
                roots[0].subtitle = original_subtitle
                session.add(roots[0])


def _reviewed_text(session: Session, case: ReconciliationCase, text: Text, actor_id: str) -> Text:
    if case.context.get("pending_derived"):
        return _complete_derived(session, case, text, actor_id)
    translators = case.incoming.get("translators", [])
    people = CreditPeople(session)
    if translators and not {normalize(name) for name in translators} <= _names(session, [text], "translator"):
        result = _resolve_translation(
            session,
            work_family(session, text.id),
            {
                "title": case.incoming["title"],
                "authors": case.incoming.get("authors", []),
                "translators": translators,
                "language": case.incoming.get("language"),
            },
            people,
        )
        if isinstance(result, ReconciliationCase):
            raise ValueError("Choose a specific translation")
        text = result
    roots = family_roots(session, text.id)
    if len(roots) == 1 and not _names(session, roots, "author"):
        people.add_credits(roots[0], case.incoming.get("authors", []), "author")
    if title := case.incoming.get("title"):
        language = case.incoming.get("language")
        add_title(session, text, title, language if language != "mul" else None)
    for role in ("adapter", "editor"):
        people.add_credits(text, case.incoming.get(f"{role}s", []), role)
    return text


def _complete_derived(session: Session, case: ReconciliationCase, source: Text, actor_id: str) -> Text:
    source_case = session.get(ReconciliationCase, uuid.UUID(case.context["source_case_id"]))
    if source_case is None:
        raise ValueError("Source review case no longer exists")
    if source_case.status == "pending":
        source = resolve_text_case(session, source_case.id, target_id=source.id, actor_id=actor_id)
    if (original_subtitle := case.context["pending_derived"].get("original_subtitle")) and not source.subtitle:
        source.subtitle = original_subtitle
        session.add(source)
    result = resolve_staged_text(
        session,
        {
            **case.context["pending_derived"],
            "based_on": {"text_id": str(source.id)},
        },
    )
    if isinstance(result, ReconciliationCase):
        raise ValueError("Derived text is still ambiguous; resolve its work first")  # ruff: ignore[type-check-without-type-error] - Valid result, unresolved review.
    return result


def _apply_reviewed_sources(session: Session, case: ReconciliationCase, text: Text) -> None:
    sources = {
        entry["source_id"]: entry.get("relation", LineageRelation.BASED_ON)
        for entry in case.context.get("credit_inputs", [])
        if entry.get("source_id")
    }
    if sources:
        roots = family_roots(session, text.id)
        if len(roots) != 1:
            raise ValueError("Select one family root before linking source works")
        for source_id, relation in sources.items():
            add_lineage(session, resolve_id(session, EntityType.TEXT, source_id), roots[0].id, relation)
