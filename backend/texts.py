"""Shared text identity, computed work families, and reviewed lineage changes."""

from __future__ import annotations

import datetime as dt
import uuid
from collections import deque
from collections.abc import Iterable

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

    def person_id(self, name: str, role: str) -> uuid.UUID:
        """Reuse the unique normalized match or create a person for this role."""
        if self._ids_by_name is None:
            ids_by_name: dict[str, list[uuid.UUID]] = {}
            for person_id, stored_name in self._session.exec(select(Person.id, Person.name)):
                ids_by_name.setdefault(normalize(stored_name), []).append(person_id)
            self._ids_by_name = ids_by_name
        key = normalize(name)
        matches = self._ids_by_name.get(key, [])
        if len(matches) > 1:
            raise ValueError(f"Ambiguous person credit: {name}")
        if matches:
            self._session.flush()
            return matches[0]
        person = Person(name=name, type=[role])
        self._session.add(person)
        self._session.flush()
        self._ids_by_name[key] = [person.id]
        return person.id

    def add_credits(self, text: Text, names: Iterable[str], role: str) -> None:
        """Attach credits in input order without duplicating them."""
        for name in dict.fromkeys(name.strip() for name in names if name.strip()):
            ensure_credit(self._session, text, self.person_id(name, role), role, None, None)


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


def resolve_text(
    session: Session,
    title: str,
    authors: Iterable[str],
    translators: Iterable[str] = (),
    language: str | None = None,
    based_on: Text | dict | str | None = None,
    *,
    people: CreditPeople | None = None,
) -> Text | ReconciliationCase:
    """Match titles across families, then choose the credited translation without guessing."""
    normalized = normalize(title)
    if not normalized:
        raise ValueError("Text title must not be empty")
    if language is not None:
        language = normalize_text_languages([language])[0]
    authors, translators = list(authors), list(translators)
    author_names = {normalize(name) for name in authors}
    title_matches = list(
        session.exec(select(Text).join(TextTitle).where(TextTitle.normalized == normalized).distinct()).all()
    )
    families: dict[frozenset[uuid.UUID], list[Text]] = {}
    for match in title_matches:
        family = work_family(session, match.id)
        known_authors = _names(session, family_roots(session, match.id), "author")
        if not author_names or not known_authors or author_names & known_authors:
            families[frozenset(text.id for text in family)] = family
    payload: dict = {"title": title, "authors": authors, "translators": translators, "language": language}
    if based_on is not None:
        payload["based_on"] = {"text_id": str(based_on.id)} if isinstance(based_on, Text) else based_on
    if len(families) > 1:
        return _link_case(session, payload, title_matches, "Several works share this title; select a text.")
    people = people or CreditPeople(session)
    if families:
        return _choose_family_text(session, next(iter(families.values())), title_matches, payload, people)
    source = _resolve_source(session, based_on, people)
    if isinstance(source, ReconciliationCase):
        return _source_review(session, source, payload)
    text = Text.model_validate({"title": title, "languages": [language] if language else []})
    session.add(text)
    session.flush()
    people.add_credits(text, authors, "author")
    people.add_credits(text, translators, "translator")
    if source is not None:
        add_lineage(session, source.id, text.id, LineageRelation.BASED_ON)
    return text


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
            root_authors = _names(session, roots, "author")
            if not root_authors and payload["authors"] and len(roots) == 1:
                people.add_credits(roots[0], payload["authors"], "author")
            add_title(session, matches[0], title, language if language != "mul" else None)
            return matches[0]
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
        )
        if isinstance(root, ReconciliationCase):
            return root
        if root.genre is None and data.get("genre"):
            root.genre = data["genre"]
        language = data.get("language")
        add_title(session, root, data["title"], language if language != "mul" else None)
        result = resolve_text(
            session, data["title"], authors, data.get("translators", []), data.get("language"), people=people
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
        )
    if isinstance(result, Text):
        if data.get("genre") and result.genre is None:
            result.genre = data["genre"]
        if data.get("published") and result.published is None:
            validated = Text.model_validate(
                {
                    **result.model_dump(),
                    "published": data["published"],
                    "published_precision": data.get("published_precision"),
                }
            )
            result.published, result.published_precision = validated.published, validated.published_precision
        for role in ("composer", "lyricist"):
            people.add_credits(result, data.get(f"{role}s", []), role)
        session.add(result)
    return result


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
            ensure_credit(session, production, people.person_id(name, "adapter"), "adapter", None, actor_id)
    if observations := case.context.get("publisher_observations"):
        from backend.ingestion.publishers import resolve_publisher_observations  # ruff: ignore[import-outside-top-level]

        resolve_publisher_observations(session, text, observations)
    _apply_reviewed_sources(session, case, text)


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
    return text


def _complete_derived(session: Session, case: ReconciliationCase, source: Text, actor_id: str) -> Text:
    source_case = session.get(ReconciliationCase, uuid.UUID(case.context["source_case_id"]))
    if source_case is None:
        raise ValueError("Source review case no longer exists")
    if source_case.status == "pending":
        source = resolve_text_case(session, source_case.id, target_id=source.id, actor_id=actor_id)
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
    sources = {entry["source_id"] for entry in case.context.get("credit_inputs", []) if entry.get("source_id")}
    if sources:
        roots = family_roots(session, text.id)
        if len(roots) != 1:
            raise ValueError("Select one family root before linking source works")
        for source_id in sources:
            add_lineage(session, resolve_id(session, EntityType.TEXT, source_id), roots[0].id, LineageRelation.BASED_ON)
