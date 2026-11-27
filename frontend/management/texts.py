from collections import Counter
from collections.abc import Iterable
from uuid import UUID

import pandas as pd
import streamlit as st
from sqlalchemy import or_
from sqlmodel import Session, col, select

from backend.ai import parse_credits, propose_text_lineage, suggest_titles
from backend.config import LINEAGE_LABELS, ROLE_LABELS
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
from backend.reconciliation import decide_case, ensure_credit, normalize
from backend.texts import (
    FAMILY_RELATIONS,
    _inherited_names,  # ruff: ignore[import-private-name]
    add_lineage,
    add_title,
    family_roots,
    family_roots_many,
    resolve_staged_text,
    resolve_text_case,
)
from backend.user_management import get_session
from backend.wikidata import create_text_stub, search_texts
from frontend.management.authority import render_authority_panel
from frontend.management.uifactory import _actor_id, render_sqlmodel_crud

LABEL_ROLES = ("author", "translator", "adapter")
CREDIT_ROLES = ("composer", "author", "lyricist", "translator", "adapter")
LineageEdge = tuple[UUID, UUID, LineageRelation]


def _contributions(session: Session, ids: set[UUID], roles: tuple[str, ...]) -> Iterable[tuple[UUID, str | None, str]]:
    return session.exec(
        select(TextContribution.text_id, TextContribution.role, Person.name)
        .join(Person, col(Person.id) == col(TextContribution.person_id))
        .where(col(TextContribution.text_id).in_(ids), col(TextContribution.role).in_(roles))
        .order_by(Person.name)
    ).all()


def _format_labels(
    texts: list[Text], roots_by_text: dict[UUID, list[Text]], credits: Iterable[tuple[UUID, str | None, str]]
) -> dict[str, str]:
    contributions: dict[UUID, list[str]] = {}
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


def text_labels(texts: list[Text]) -> dict[str, str]:
    """Make text choices distinguishable without exposing raw foreign keys."""
    if not texts:
        return {}
    ids = {text.id for text in texts}
    with get_session() as session:
        return _format_labels(texts, family_roots_many(session, ids), _contributions(session, ids, LABEL_ROLES))


def _titles(session: Session, text: Text) -> None:  # ruff: ignore[complex-structure]
    st.subheader("Titelvarianten")
    titles = session.exec(select(TextTitle).where(TextTitle.text_id == text.id)).all()
    rows = [{"title": item.title, "language": item.language or ""} for item in titles]
    edited = st.data_editor(
        pd.DataFrame(rows, columns=["title", "language"]),
        num_rows="dynamic",
        hide_index=True,
        key=f"titles_{text.id}_{st.session_state.get(f'titles_epoch_{text.id}', 0)}",
        column_config={"title": "Titel", "language": "Sprache (ISO 639-1)"},
    )
    if st.button("Titelvarianten speichern", key=f"save_titles_{text.id}"):
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            values = [
                tuple("" if pd.isna(row[field]) else str(row[field]).strip() for field in ("title", "language"))
                for row in edited.to_dict("records")
            ]
            if any(not title for title, _ in values):
                raise ValueError("Titel darf nicht leer sein.")
            if len({normalize(title) for title, _ in values}) != len(values):
                raise ValueError("Titelvarianten müssen eindeutig sein.")
            if normalize(text.title) not in {normalize(title) for title, _ in values}:
                raise ValueError("Der Haupttitel muss als Titelvariante erhalten bleiben.")
            for _, language in values:
                if language:
                    normalize_text_languages([language])
            for item in titles:
                session.delete(item)
            session.flush()
            for title, language in values:
                add_title(session, text, title, language or None)
            session.commit()
            st.session_state[f"titles_epoch_{text.id}"] = st.session_state.get(f"titles_epoch_{text.id}", 0) + 1
            st.rerun()
        except Exception as exc:  # ruff: ignore[blind-except]
            session.rollback()
            st.error(str(exc))
    st.caption("KI-Titelvorschläge übermitteln Textdaten an den KI-Anbieter und können Wikidata abfragen.")
    if st.button("KI-Titelvorschläge abrufen", key=f"title_suggest_{text.id}"):
        try:
            st.session_state[f"title_suggestions_{text.id}"] = suggest_titles(session, text.id, use_ai=True)
        except Exception as exc:  # ruff: ignore[blind-except]
            st.error(f"Titelvorschläge fehlgeschlagen: {exc}")
    suggestions = st.session_state.get(f"title_suggestions_{text.id}", [])
    if suggestions:
        suggestion = st.selectbox(
            "Titelvorschlag",
            suggestions,
            format_func=lambda item: (
                f"{item['title']} ({item.get('language') or 'ohne Sprache'}) — "
                f"{'belegt: ' + item['source'] if item['verified'] else 'nicht verifiziert (KI)'}"
            ),
            key=f"title_suggestion_{text.id}",
        )
        confirmed = st.checkbox(
            "Titel und Sprache geprüft",
            key=f"title_confirm_{text.id}_{normalize(suggestion['title'])}_{suggestion.get('language')}",
        )
        if st.button("Geprüften Titel übernehmen", key=f"title_apply_{text.id}", disabled=not confirmed):
            try:
                add_title(session, text, suggestion["title"], suggestion.get("language"))
                session.commit()
                st.session_state[f"titles_epoch_{text.id}"] = st.session_state.get(f"titles_epoch_{text.id}", 0) + 1
                st.rerun()
            except Exception as exc:  # ruff: ignore[blind-except]
                session.rollback()
                st.error(str(exc))


def _walk_lineage(
    session: Session, seeds: set[UUID], relations: Iterable[LineageRelation], *, reverse: bool | None
) -> tuple[set[UUID], list[LineageEdge]]:
    """Fetch one frontier at a time; None follows edges in both directions."""
    seen, frontier = set(seeds), seeds
    edges: dict[LineageEdge, None] = {}
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


def _roots_by_text(texts: dict[UUID, Text], edges: list[LineageEdge]) -> dict[UUID, list[Text]]:
    neighbors: dict[UUID, set[UUID]] = {}
    derived_ids = set()
    for source, derived, _ in edges:
        neighbors.setdefault(source, set()).add(derived)
        neighbors.setdefault(derived, set()).add(source)
        derived_ids.add(derived)
    roots_by_text: dict[UUID, list[Text]] = {}
    for text_id in texts:
        if text_id in roots_by_text:
            continue
        component, frontier = {text_id}, {text_id}
        while frontier:
            frontier = {neighbor for item in frontier for neighbor in neighbors.get(item, ())} - component
            component.update(frontier)
        roots = sorted((texts[item] for item in component - derived_ids), key=lambda t: (t.title, t.id))
        roots_by_text.update(dict.fromkeys(component, roots))
    return roots_by_text


def _detail_credit_line(
    text: Text, upstream: list[LineageEdge], contributions: Iterable[tuple[UUID, str | None, str]]
) -> str:
    parents: dict[UUID, set[UUID]] = {}
    sources = set()
    for source, derived, relation in upstream:
        if relation == LineageRelation.BASED_ON:
            sources.add(source)
        else:
            parents.setdefault(derived, set()).add(source)
    credits: dict[tuple[UUID, str | None], set[str]] = {}
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


def _based_on_ancestors(member_ids: set[UUID], upstream: list[LineageEdge]) -> set[UUID]:
    parents: dict[UUID, set[UUID]] = {}
    for source, derived, relation in upstream:
        if relation == LineageRelation.BASED_ON:
            parents.setdefault(derived, set()).add(source)
    seen, frontier = set(member_ids), member_ids
    while frontier:
        frontier = {parent for item in frontier for parent in parents.get(item, ())} - seen
        seen.update(frontier)
    return seen - member_ids


def _family(session: Session, text: Text) -> tuple[set[UUID], bool]:  # ruff: ignore[complex-structure]
    member_ids, family_edges = _walk_lineage(session, {text.id}, FAMILY_RELATIONS, reverse=None)
    upstream_ids, upstream = _walk_lineage(session, member_ids, LineageRelation, reverse=True)
    # Credits follow all incoming relations; visible sources follow only based-on edges.
    ancestors = _based_on_ancestors(member_ids, upstream)
    downstream_ids, _ = _walk_lineage(session, member_ids, (LineageRelation.BASED_ON,), reverse=False)
    descendants = downstream_ids - member_ids
    relatives = ancestors | descendants
    label_ids, label_edges = _walk_lineage(session, relatives, FAMILY_RELATIONS, reverse=None)
    visible_ids = member_ids | relatives
    texts = {text.id: text}
    if other_ids := (member_ids | label_ids) - {text.id}:
        texts.update(
            (item.id, item)
            for item in session.exec(select(Text).where(col(Text.id).in_(other_ids)).order_by(Text.title, col(Text.id)))
        )
    roots = _roots_by_text(texts, family_edges + label_edges)
    credits = list(_contributions(session, upstream_ids | visible_ids, CREDIT_ROLES))
    labels = _format_labels([item for item in texts.values() if item.id in visible_ids], roots, credits)
    productions: dict[UUID | None, list[str]] = {}
    for text_id, name in session.exec(
        select(Production.text_id, Production.name).where(col(Production.text_id).in_(member_ids))
    ):
        productions.setdefault(text_id, []).append(name)

    st.subheader("Werkfamilie")
    st.write(_detail_credit_line(text, upstream, credits))
    if ancestors:
        st.write("Vorlage: " + " → ".join(labels[str(i)] for i in sorted(ancestors, key=lambda x: labels[str(x)])))
    children: dict[UUID, list[tuple[UUID, LineageRelation]]] = {}
    for source, derived, relation in family_edges:
        children.setdefault(source, []).append((derived, relation))
    derived_ids = {derived for _, derived, _ in family_edges}
    shown: set[UUID] = set()

    def show(item_id: UUID, depth: int, relation: str = "") -> None:
        if item_id in shown:
            return
        shown.add(item_id)
        st.text(f"{'  ' * depth}{'↳ ' if depth else ''}{relation}{labels[str(item_id)]}")
        if names := productions.get(item_id):
            st.caption("Produktionen: " + ", ".join(names))
        for derived, kind in sorted(children.get(item_id, []), key=lambda edge: labels[str(edge[0])]):
            show(derived, depth + 1, f"{LINEAGE_LABELS[kind]}: ")

    for item_id in sorted(member_ids, key=lambda item: labels[str(item)]):
        if item_id not in derived_ids:
            show(item_id, 0)
    for item_id in sorted(member_ids, key=lambda item: (texts[item].title, item)):
        show(item_id, 0)
    if descendants:
        st.write(
            "Darauf basierende Werke: "
            + ", ".join(labels[str(i)] for i in sorted(descendants, key=lambda x: labels[str(x)]))
        )
    return member_ids, text.id not in derived_ids


def _text_options(session: Session, query: str, excluded: set[UUID], suggested: str = "") -> dict[str, str]:
    stmt = select(Text).where(col(Text.id).not_in(excluded))
    if query:
        aliases = select(TextTitle.text_id).where(col(TextTitle.title).icontains(query, autoescape=True))
        stmt = stmt.where(or_(col(Text.title).icontains(query, autoescape=True), col(Text.id).in_(aliases)))
    rows = list(session.exec(stmt.order_by(col(Text.title), col(Text.id)).limit(50)))
    if suggested and all(str(row.id) != suggested for row in rows):
        suggested_row = session.get(Text, UUID(suggested))
        if suggested_row is not None:
            rows.append(suggested_row)
    return text_labels(rows)


def _link(session: Session, text: Text) -> None:
    st.subheader("Werk verknüpfen")
    relation = st.selectbox(
        "Dieser Text ist eine / hat eine",
        list(LineageRelation),
        format_func=lambda r: LINEAGE_LABELS[r],
        key=f"relation_{text.id}",
    )
    direction = st.selectbox(
        "Beziehung zum gewählten Werk",
        ["Dieser Text ist davon abgeleitet", "Das gewählte Werk ist von diesem Text abgeleitet"],
        key=f"direction_{text.id}",
    )
    query = st.text_input("Titel in Datenbank suchen", key=f"link_query_{text.id}", type="search", live="500ms").strip()
    labels = _text_options(session, query, {text.id}) if query else {}
    chosen = st.selectbox(
        "Passendes Werk",
        [None, *labels],
        format_func=lambda i: labels[i] if i else "Bitte wählen",
        key=f"link_target_{text.id}",
    )
    if relation == LineageRelation.BASED_ON:
        st.caption(
            "Dieser Text basiert auf …"
            if direction.startswith("Dieser")
            else "Das gewählte Werk basiert auf diesem Text."
        )
    else:
        st.caption(
            f"Dieser Text ist eine {LINEAGE_LABELS[relation]} von …"
            if direction.startswith("Dieser")
            else f"Das gewählte Werk ist eine {LINEAGE_LABELS[relation]} von diesem Text."
        )
    if st.button("Wikidata suchen", key=f"wikidata_search_{text.id}", disabled=not query):
        try:
            st.session_state[f"wikidata_results_{text.id}"] = search_texts(query)
        except Exception as exc:  # ruff: ignore[blind-except]
            st.error(f"Wikidata-Suche fehlgeschlagen: {exc}")
    results = st.session_state.get(f"wikidata_results_{text.id}", [])
    if results:
        result = st.selectbox(
            "Wikidata-Treffer",
            results,
            format_func=lambda r: f"{r['label']} — {r.get('description', '')} ({r['id']})",
            key=f"wikidata_choice_{text.id}",
        )
        if st.button("Ausgewählten Treffer als Werk anlegen", key=f"wikidata_create_{text.id}"):
            try:
                stub = create_text_stub(session, result["id"])
                session.commit()
                st.session_state[f"wikidata_created_{text.id}"] = str(stub.id)
                st.success(f"Werk angelegt: {stub.title}. Unten verknüpfen.")
            except Exception as exc:  # ruff: ignore[blind-except]
                session.rollback()
                st.error(str(exc))
    created_id = st.session_state.get(f"wikidata_created_{text.id}")
    if (
        created_id
        and session.get(Text, UUID(created_id)) is not None
        and st.checkbox("Neu angelegten Wikidata-Treffer verknüpfen", key=f"use_stub_{text.id}")
    ):
        chosen = created_id
    notes = st.text_input("Notiz zur Beziehung", key=f"link_notes_{text.id}")
    if st.button("Beziehung speichern", key=f"save_link_{text.id}", disabled=not chosen):
        try:
            source, derived = (UUID(chosen), text.id) if direction.startswith("Dieser") else (text.id, UUID(chosen))
            add_lineage(session, source, derived, relation, notes.strip() or None)
            session.commit()
            st.rerun()
        except Exception as exc:  # ruff: ignore[blind-except]
            session.rollback()
            st.error(str(exc))


def _production_adapters(session: Session, production: Production, names: list[str]) -> None:
    people = list(session.exec(select(Person)).all())
    for name in names:
        matches = [person for person in people if normalize(person.name) == normalize(name)]
        if len(matches) > 1:
            raise ValueError(f"Person nicht eindeutig: {name}")
        person = matches[0] if matches else Person(name=name, type=["adapter"])
        session.add(person)
        session.flush()
        if not matches:
            people.append(person)
        ensure_credit(session, production, person.id, "adapter", None, _actor_id())


def _credit(session: Session, text: Text, family_ids: set[UUID]) -> None:  # ruff: ignore[complex-structure]
    st.subheader("Credit übernehmen")
    credit = st.text_area("Credit einfügen", key=f"credit_{text.id}")
    if not credit.strip():
        return
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        parsed = parse_credits(credit, use_ai=False)
        st.caption("Optionaler KI-Aufruf übermittelt unerkannte Credit-Teile an den konfigurierten Anbieter.")
        if st.button("Unerkannte Teile mit KI vorschlagen", key=f"credit_ai_{text.id}"):
            st.session_state[f"credit_ai_result_{text.id}"] = (credit, parse_credits(credit, use_ai=True))
        ai_result = st.session_state.get(f"credit_ai_result_{text.id}")
        if ai_result and ai_result[0] == credit:
            parsed = ai_result[1]
        st.json(parsed)
        st.caption("Unerkannte Teile werden nicht automatisch übernommen. Fassung ist ein Produktionscredit.")
        language = st.text_input(
            "Sprache der erkannten Übersetzung (ISO 639-1, optional)", key=f"credit_lang_{text.id}"
        ).strip()
        source_id = None
        if parsed["source_authors"] or parsed["source_titles"] or parsed["source_notes"]:
            source_query = st.text_input(
                "Vorlagentitel suchen", key=f"credit_source_search_{text.id}", type="search", live="500ms"
            ).strip()
            source_labels = _text_options(session, source_query, family_ids)
            source_id = st.selectbox(
                "Vorlage zum nach-Credit (optional)",
                [None, *source_labels],
                format_func=lambda value: source_labels[value] if value else "Keine Vorlage verknüpfen",
                key=f"credit_source_{text.id}",
            )
        productions = session.exec(select(Production).where(col(Production.text_id).in_(family_ids))).all()
        choices = {str(production.id): production for production in productions}
        production_labels = {key: production.name for key, production in choices.items()}
        production_id = st.selectbox(
            "Produktion für Fassung (optional)",
            [None, *choices],
            format_func=lambda value: production_labels[value] if value else "Keine Produktion",
            key=f"credit_production_{text.id}",
        )
        relink = st.checkbox(
            "Produktion ausdrücklich mit dem aufgelösten Text verknüpfen", key=f"credit_relink_{text.id}"
        )
        if st.button("Credit prüfen und speichern", key=f"save_credit_{text.id}"):
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
                        "language": language or None,
                    },
                )
            if isinstance(result, ReconciliationCase):
                if result.status != "pending":
                    raise ValueError("Dieser Prüffall ist nicht offen. Bitte zuerst erneut öffnen.")
                context = dict(result.context or {})
                if production_id:
                    production_ids = list(context.get("production_ids", []))
                    if context.get("production_id"):
                        production_ids.append(str(context["production_id"]))
                    context["production_ids"] = list(dict.fromkeys([*production_ids, production_id]))
                    text_ids = dict(context.get("production_text_ids", {}))
                    current_id = choices[production_id].text_id
                    text_ids.setdefault(production_id, str(current_id) if current_id else None)
                    context["production_text_ids"] = text_ids
                    adapters = dict(context.get("production_adapter_names", {}))
                    adapters[production_id] = list(
                        dict.fromkeys([*adapters.get(production_id, []), *parsed["adapters"]])
                    )
                    context["production_adapter_names"] = adapters
                inputs = list(context.get("credit_inputs", []))
                entry = {
                    "text_id": str(text.id),
                    "credit": credit,
                    "parsed": parsed,
                    "language": language or None,
                    "source_id": source_id,
                    "production_id": production_id,
                    "relink": relink,
                }
                if entry not in inputs:
                    inputs.append(entry)
                context["credit_inputs"] = inputs
                result.context = context
                session.add(result)
                session.commit()
                if production_id:
                    st.warning("Mehrdeutig: Credit und Fassung sind im Prüffall gespeichert und warten auf Prüfung.")
                else:
                    st.warning(
                        "Mehrdeutig: Credit ist im Prüffall gespeichert. Für Fassung wurde keine Produktion gewählt."
                    )
                return
            if source_id:
                roots = family_roots(session, result.id)
                if len(roots) != 1:
                    raise ValueError("Die Werkfamilie hat mehrere Ursprünge; Vorlage bitte direkt verknüpfen.")
                add_lineage(session, UUID(source_id), roots[0].id, LineageRelation.BASED_ON)
            elif parsed["source_authors"] or parsed["source_titles"] or parsed["source_notes"]:
                st.warning("Vorlage nicht verknüpft: Bitte ein Werk auswählen.")
            if production_id:
                production = choices[production_id]
                _production_adapters(session, production, parsed["adapters"])
                if relink:
                    production.text_id = result.id
                    session.add(production)
                elif production.text_id != result.id:
                    st.warning("Produktion behält ihren bisherigen Text. Zum Umhängen die Bestätigung wählen.")
            elif parsed["adapters"]:
                st.warning("Fassung nicht gespeichert: Bitte eine Produktion auswählen.")
            session.commit()
            st.success(f"Credit aufgelöst: {result.title} ({', '.join(result.languages) or 'Sprache unbekannt'}).")
    except Exception as exc:  # ruff: ignore[blind-except]
        session.rollback()
        st.error(str(exc))


def _lineage_proposal(session: Session, text: Text) -> None:
    incident_edge = session.exec(
        select(TextLineage.id)
        .where(or_(col(TextLineage.source_text_id) == text.id, col(TextLineage.derived_text_id) == text.id))
        .limit(1)
    ).first()
    if incident_edge is not None:
        return
    query = st.text_input(
        "Kandidatentitel suchen", key=f"ai_candidate_search_{text.id}", type="search", live="500ms"
    ).strip()
    labels = _text_options(session, query, {text.id})
    selected = st.multiselect(
        "Mögliche Vorlagen für KI-Prüfung",
        list(labels),
        format_func=labels.__getitem__,
        key=f"ai_candidates_{text.id}",
    )
    st.caption("Nur auf ausdrücklichen Klick: Text und ausgewählte Kandidaten werden an den KI-Anbieter übermittelt.")
    if st.button("KI-Verknüpfung zur Prüfung vorschlagen", key=f"ai_lineage_{text.id}", disabled=not selected):
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            candidates = [{"id": item, "label": labels[item]} for item in selected]
            case = propose_text_lineage(session, text.id, candidates, use_ai=True)
            session.commit()
            if case:
                st.success("Vorschlag im Text-Prüfpostfach gespeichert.")
            else:
                st.info("Kein Vorschlag gefunden.")
        except Exception as exc:  # ruff: ignore[blind-except]
            session.rollback()
            st.error(f"KI-Vorschlag fehlgeschlagen: {exc}")


def _selected(row: dict) -> None:
    with get_session() as session:
        text = session.get(Text, UUID(str(row["id"])))
        if text:
            family_ids, is_root = _family(session, text)
            _titles(session, text)
            if is_root:
                render_authority_panel(EntityType.TEXT, row)
            else:
                st.caption("Normdaten-Abgleich ist am Ursprung der Werkfamilie verfügbar.")
            _link(session, text)
            _credit(session, text, family_ids)
            _lineage_proposal(session, text)


def _inbox() -> None:
    st.subheader("Text-Prüffälle")
    with get_session() as session:
        cases = session.exec(
            select(ReconciliationCase).where(
                ReconciliationCase.status == "pending",
                col(ReconciliationCase.operation).in_(["link_text", "text_lineage"]),
            )
        ).all()
        if not cases:
            st.caption("Keine offenen Text-Prüffälle.")
            return
        for case in cases:
            with st.expander(f"{case.operation}: {case.incoming.get('title') or case.reason}"):
                st.write(case.reason)
                st.json({"Vorschlag": case.incoming, "Kontext": case.context})
                suggested = str(case.target_id or case.context.get("suggested_target_id") or "")
                query = st.text_input(
                    "Texttitel suchen", key=f"case_search_{case.id}", type="search", live="500ms"
                ).strip()
                labels = _text_options(session, query, set(), suggested)
                options = [None, *labels]
                target_label = "Vorlage für den neuen Text" if case.context.get("pending_derived") else "Ziel"
                target = st.selectbox(
                    target_label,
                    options,
                    format_func=lambda i, labels=labels: labels[i] if i else "Bitte wählen",
                    index=options.index(suggested) if suggested in options else 0,
                    key=f"case_target_{case.id}",
                )
                source = st.selectbox(
                    "Vorlage / Ausgangstext (optional)",
                    options,
                    format_func=lambda i, labels=labels: labels[i] if i else "Keine Änderung",
                    key=f"case_source_{case.id}",
                )
                relation = st.selectbox(
                    "Beziehung (optional)",
                    [None, *list(LineageRelation)],
                    format_func=lambda r: LINEAGE_LABELS[r] if r else "Vorschlag übernehmen",
                    key=f"case_relation_{case.id}",
                )
                note = st.text_input("Ablehnungsgrund", key=f"case_note_{case.id}")
                accept, reject = st.columns(2)
                if accept.button("Annehmen / Änderungen speichern", key=f"case_accept_{case.id}"):
                    try:
                        resolve_text_case(
                            session,
                            case.id,
                            target_id=UUID(target) if target else None,
                            relation=relation,
                            source_id=UUID(source) if source else None,
                            actor_id=_actor_id() or "frontend",
                        )
                        session.commit()
                        st.rerun()
                    except Exception as exc:  # ruff: ignore[blind-except]
                        session.rollback()
                        st.error(str(exc))
                if reject.button("Ablehnen", key=f"case_reject_{case.id}", disabled=not note.strip()):
                    try:
                        decide_case(case.id, status="rejected", actor_id=_actor_id() or "frontend", note=note)
                        st.rerun()
                    except Exception as exc:  # ruff: ignore[blind-except]
                        st.error(str(exc))


def main() -> None:
    """Render text management page."""
    render_sqlmodel_crud(
        model_cls=Text,
        merge_kind="text",
        selected_callback=_selected,
        field_overrides={
            "contributions": {"exclude": True},
            "productions": {"exclude": True},
            "titles": {"exclude": True},
            "lineages_as_source": {"exclude": True},
            "lineages_as_derived": {"exclude": True},
        },
        inline_relations=[
            {
                "model": TextContribution,
                "fk_field": "text_id",
                "title": "Authors & Contributions",
                "order_by": "role",
                "editable_table": True,
                "fk_autocreate": {"person_id": {"payload_field": "name", "infer_type_from": "role"}},
                "field_overrides": {
                    "person_id": {"kind": "fk_select", "fk_model": Person, "fk_display": "name", "label": "Person"},
                    "role": {
                        "label": "Rolle",
                        "kind": "select",
                        "options": ["author", "translator", "adapter", "composer", "lyricist"],
                        "option_labels": ROLE_LABELS,
                    },
                },
            }
        ],
    )
    _inbox()


if __name__ == "__main__":
    main()
