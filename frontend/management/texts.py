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
from backend.reconciliation import decide_case, normalize
from backend.texts import (
    add_lineage,
    add_title,
    labels_for_texts,
    resolve_text_case,
    save_text_credit,
    text_family_view,
)
from backend.user_management import get_session
from backend.wikidata import create_text_stub, search_texts
from frontend.management.authority import render_authority_panel
from frontend.management.uifactory import _actor_id, render_sqlmodel_crud


def text_labels(texts: list[Text]) -> dict[str, str]:
    """Make text choices distinguishable without exposing raw foreign keys."""
    if not texts:
        return {}
    with get_session() as session:
        return labels_for_texts(session, texts)


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


def _family(session: Session, text: Text) -> tuple[set[UUID], bool]:
    view = text_family_view(session, text.id)
    st.subheader("Werkfamilie")
    st.write(view.credit_line)
    if view.ancestors:
        st.write(
            "Vorlage: "
            + " → ".join(view.labels[str(i)] for i in sorted(view.ancestors, key=lambda x: view.labels[str(x)]))
        )
    derived_ids = {derived for children in view.children.values() for derived, _ in children}
    shown: set[UUID] = set()

    def show(item_id: UUID, depth: int, relation: str = "") -> None:
        if item_id in shown:
            return
        shown.add(item_id)
        st.text(f"{'  ' * depth}{'↳ ' if depth else ''}{relation}{view.labels[str(item_id)]}")
        if names := view.productions.get(item_id):
            st.caption("Produktionen: " + ", ".join(names))
        for derived, kind in sorted(view.children.get(item_id, []), key=lambda edge: view.labels[str(edge[0])]):
            show(derived, depth + 1, f"{LINEAGE_LABELS[kind]}: ")

    for item_id in sorted(view.member_ids, key=lambda item: view.labels[str(item)]):
        if item_id not in derived_ids:
            show(item_id, 0)
    for item_id in sorted(view.member_ids, key=lambda item: (view.texts[item].title, item)):
        show(item_id, 0)
    if view.descendants:
        st.write(
            "Darauf basierende Werke: "
            + ", ".join(view.labels[str(i)] for i in sorted(view.descendants, key=lambda x: view.labels[str(x)]))
        )
    return view.member_ids, view.is_root


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
            production = session.get(Production, UUID(production_id)) if production_id else None
            result = save_text_credit(
                session,
                text,
                credit,
                parsed,
                language or None,
                source_id,
                production_id,
                relink,
                _actor_id(),
            )
            if isinstance(result, ReconciliationCase):
                session.commit()
                if production_id:
                    st.warning("Mehrdeutig: Credit und Fassung sind im Prüffall gespeichert und warten auf Prüfung.")
                else:
                    st.warning(
                        "Mehrdeutig: Credit ist im Prüffall gespeichert. Für Fassung wurde keine Produktion gewählt."
                    )
                return
            if not source_id and (parsed["source_authors"] or parsed["source_titles"] or parsed["source_notes"]):
                st.warning("Vorlage nicht verknüpft: Bitte ein Werk auswählen.")
            if not production_id and parsed["adapters"]:
                st.warning("Fassung nicht gespeichert: Bitte eine Produktion auswählen.")
            if production is not None and not relink and production.text_id != result.id:
                st.warning("Produktion behält ihren bisherigen Text. Zum Umhängen die Bestätigung wählen.")
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
