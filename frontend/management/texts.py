from uuid import UUID

import pandas as pd
import streamlit as st
from sqlmodel import Session, col, select

from backend.ai import parse_credits, propose_text_lineage, suggest_titles
from backend.config import LINEAGE_LABELS, ROLE_LABELS
from backend.models import (
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
    add_lineage,
    add_title,
    credit_line,
    family_roots,
    resolve_staged_text,
    resolve_text_case,
    work_family,
)
from backend.user_management import get_session, init_db
from backend.wikidata import create_text_stub, enrich_text, search_texts
from frontend.management.uifactory import _actor_id, render_sqlmodel_crud


def text_labels(texts: list[Text]) -> dict[str, str]:
    """Make text choices distinguishable without exposing raw foreign keys."""
    with get_session() as session:
        people = {p.id: p.name for p in session.exec(select(Person)).all()}
        contributions: dict[UUID, list[str]] = {}
        for item in session.exec(select(TextContribution)).all():
            if item.role in {"author", "translator", "adapter"} and item.person_id in people:
                contributions.setdefault(item.text_id, []).append(f"{ROLE_LABELS[item.role]}: {people[item.person_id]}")
        labels = {}
        for text in texts:
            details = [", ".join(text.languages), ", ".join(contributions.get(text.id, []))]
            roots = family_roots(session, text.id)
            roots_label = ", ".join(root.title for root in roots if root.id != text.id and root.title != text.title)
            if roots_label:
                details.insert(0, f"Werk: {roots_label}")
            labels[str(text.id)] = text.title + (" — " + "; ".join(d for d in details if d) if any(details) else "")
        duplicates = {label for label in labels.values() if list(labels.values()).count(label) > 1}
        return {key: f"{label} [{key[:8]}]" if label in duplicates else label for key, label in labels.items()}


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


def _enrich(session: Session, text: Text) -> None:
    if text.id not in {root.id for root in family_roots(session, text.id)}:
        st.caption("Wikidata-Anreicherung ist am Ursprung der Werkfamilie verfügbar.")
        return
    if st.button("Verknüpftes Wikidata-Werk anreichern", key=f"enrich_{text.id}"):
        try:
            report = enrich_text(session, text)
            session.commit()
            st.success("Wikidata-Daten übernommen.")
            st.json(report)
        except Exception as exc:  # ruff: ignore[blind-except]
            session.rollback()
            st.error(f"Anreicherung fehlgeschlagen: {exc}")


def _family(session: Session, text: Text) -> None:  # ruff: ignore[complex-structure]
    members = work_family(session, text.id)
    all_texts = {t.id: t for t in session.exec(select(Text)).all()}
    edges = session.exec(select(TextLineage)).all()
    member_ids = {t.id for t in members}
    labels = text_labels(list(all_texts.values()))
    st.subheader("Werkfamilie")
    st.write(credit_line(session, text.id))
    ancestors, frontier = set(), set(member_ids)
    while frontier:
        parents = {
            edge.source_text_id
            for edge in edges
            if edge.relation == LineageRelation.BASED_ON
            and edge.derived_text_id in frontier
            and edge.source_text_id not in ancestors
            and edge.source_text_id not in member_ids
        }
        ancestors.update(parents)
        frontier = parents
    if ancestors:
        st.write("Vorlage: " + " → ".join(labels[str(i)] for i in sorted(ancestors, key=lambda x: labels[str(x)])))
    children: dict[UUID, list[TextLineage]] = {}
    for edge in edges:
        if (
            edge.relation in {LineageRelation.TRANSLATION, LineageRelation.ADAPTATION}
            and edge.source_text_id in member_ids
            and edge.derived_text_id in member_ids
        ):
            children.setdefault(edge.source_text_id, []).append(edge)
    derived = {edge.derived_text_id for edges_ in children.values() for edge in edges_}
    shown: set[UUID] = set()

    def show(item_id: UUID, depth: int, relation: str = "") -> None:
        if item_id in shown or item_id not in all_texts:
            return
        shown.add(item_id)
        st.text(f"{'  ' * depth}{'↳ ' if depth else ''}{relation}{labels[str(item_id)]}")
        productions = session.exec(select(Production).where(Production.text_id == item_id)).all()
        if productions:
            st.caption("Produktionen: " + ", ".join(p.name for p in productions))
        for edge in sorted(children.get(item_id, []), key=lambda e: labels[str(e.derived_text_id)]):
            show(edge.derived_text_id, depth + 1, f"{LINEAGE_LABELS[edge.relation]}: ")

    for item in sorted(members, key=lambda t: labels[str(t.id)]):
        if item.id not in derived:
            show(item.id, 0)
    for item in members:
        show(item.id, 0)
    descendants, frontier = set(), set(member_ids)
    while frontier:
        following = {
            edge.derived_text_id
            for edge in edges
            if edge.relation == LineageRelation.BASED_ON
            and edge.source_text_id in frontier
            and edge.derived_text_id not in descendants
            and edge.derived_text_id not in member_ids
        }
        descendants.update(following)
        frontier = following
    if descendants:
        st.write(
            "Darauf basierende Werke: "
            + ", ".join(labels[str(i)] for i in sorted(descendants, key=lambda x: labels[str(x)]))
        )


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
    query = st.text_input("Titel in Datenbank suchen", key=f"link_query_{text.id}").strip()
    texts = session.exec(select(Text)).all()
    variants = session.exec(select(TextTitle)).all()
    matches = {v.text_id for v in variants if query.casefold() in v.title.casefold()} if query else set()
    choices = [
        t for t in texts if t.id != text.id and query and (query.casefold() in t.title.casefold() or t.id in matches)
    ]
    labels = text_labels(choices)
    chosen = st.selectbox(
        "Passendes Werk",
        [None, *[str(t.id) for t in choices]],
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
        and created_id in {str(t.id) for t in texts}
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


def _credit(session: Session, text: Text) -> None:  # ruff: ignore[complex-structure]
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
        family_ids = {member.id for member in work_family(session, text.id)}
        source_id = None
        if parsed["source_authors"] or parsed["source_titles"] or parsed["source_notes"]:
            sources = [item for item in session.exec(select(Text)).all() if item.id not in family_ids]
            source_labels = text_labels(sources)
            source_id = st.selectbox(
                "Vorlage zum nach-Credit (optional)",
                [None, *source_labels],
                format_func=lambda value: source_labels[value] if value else "Keine Vorlage verknüpfen",
                key=f"credit_source_{text.id}",
            )
        productions = [
            production for production in session.exec(select(Production)).all() if production.text_id in family_ids
        ]
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
    edges = session.exec(select(TextLineage)).all()
    if any(edge.source_text_id == text.id or edge.derived_text_id == text.id for edge in edges):
        return
    others = [item for item in session.exec(select(Text)).all() if item.id != text.id]
    labels = text_labels(others)
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
            _family(session, text)
            _titles(session, text)
            _enrich(session, text)
            _link(session, text)
            _credit(session, text)
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
        texts = list(session.exec(select(Text)).all())
        labels = text_labels(texts)
        for case in cases:
            with st.expander(f"{case.operation}: {case.incoming.get('title') or case.reason}"):
                st.write(case.reason)
                st.json({"Vorschlag": case.incoming, "Kontext": case.context})
                options = [None, *[str(t.id) for t in texts]]
                suggested = str(case.target_id or case.context.get("suggested_target_id") or "")
                target_label = "Vorlage für den neuen Text" if case.context.get("pending_derived") else "Ziel"
                target = st.selectbox(
                    target_label,
                    options,
                    format_func=lambda i: labels[i] if i else "Bitte wählen",
                    index=options.index(suggested) if suggested in options else 0,
                    key=f"case_target_{case.id}",
                )
                source = st.selectbox(
                    "Vorlage / Ausgangstext (optional)",
                    options,
                    format_func=lambda i: labels[i] if i else "Keine Änderung",
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
    init_db()
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
