"""German review desk for staged imports and manually submitted duplicates."""

import datetime as dt
import json
import re

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from backend.entity_management import list_entities
from backend.ingestion.staged_to_db import stage_records
from backend.models import Source
from backend.reconciliation import (
    approve_case,
    decide_case,
    discover_duplicates,
    list_cases,
    preview_case,
    queue_duplicate,
    search_entities,
)
from backend.user_management import init_db
from frontend.management.uifactory import _actor_id

KINDS = {
    "organization": "Organisationen",
    "person": "Personen",
    "text": "Texte",
    "production": "Produktionen",
    "award": "Auszeichnungen",
}


def _label(record: dict) -> str:
    name = record.get("name") or record.get("title") or "Ohne Titel"
    detail = record.get("city") or record.get("premiere") or record.get("born") or ""
    return f"{name} · {detail} · {record['id']}"


def _actor() -> str:
    return _actor_id() or "reconciliation-ui"


def _import_tab() -> None:
    st.write("Theater-JSON hochladen. Es entstehen nur Prüffälle, keine neuen Personen oder Produktionen.")
    st.caption("Reihenfolge: Organisation → Personen → Texte → Produktionen. Jeder neue Datensatz braucht Zustimmung.")
    uploaded = st.file_uploader("Produktionsdatei (JSON)", type=["json"])
    source = st.text_input("Quellenname", placeholder="staatstheater-karlsruhe.de")
    organization = st.text_input("Name der Organisation", placeholder="Badisches Staatstheater Karlsruhe")
    match = re.match(r"(\d{8})_", uploaded.name) if uploaded else None
    inferred_date = dt.datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=dt.UTC).date() if match else None
    known_date = st.checkbox("Erfassungsdatum bekannt", value=bool(inferred_date))
    capture = st.date_input("Stand der Quelldaten (nicht Importdatum)", value=inferred_date) if known_date else None
    st.caption("Ohne Erfassungsdatum wird kein aktueller Quellstand behauptet.")
    if st.button("Zur Prüfung einreichen", disabled=uploaded is None) and uploaded is not None:
        records = json.loads(uploaded.getvalue())
        result = stage_records(
            records,
            source_name=source,
            organization_name=organization,
            actor_id=_actor(),
            captured_at=dt.datetime.combine(capture, dt.time(), tzinfo=dt.UTC) if capture else None,
        )
        st.success("Eingereicht. Identische Einreichungen verwenden bestehende Prüffälle.")
        st.write({KINDS[kind]: len(ids) for kind, ids in result.items()})


def _duplicates_tab() -> None:
    st.write("Bestehende Duplikate finden oder zwei bekannte Datensätze manuell zur Prüfung einreichen.")
    kind = st.selectbox("Datensatztyp", list(KINDS), format_func=KINDS.__getitem__, key="duplicate_kind")
    query = st.text_input("Name, Titel oder UUID suchen", key="duplicate_query")
    records = search_entities(kind, query)
    st.caption("Maximal 50 Treffer. Bei gleichen Namen helfen UUID, Ort und Datum.")
    choices = {row["id"]: row for row in records}
    left = st.selectbox(
        "Bevorzugter Datensatz",
        [None, *choices],
        format_func=lambda key: "Bitte wählen" if key is None else _label(choices[key]),
        key="duplicate_left",
    )
    right_query = st.text_input("Zweiten Datensatz suchen (auch anderer Name möglich)", key="duplicate_right_query")
    right_records = search_entities(kind, right_query) if right_query else records
    right_choices = {row["id"]: row for row in right_records if row["id"] != left}
    right = st.selectbox(
        "Mögliches Duplikat",
        [None, *right_choices],
        format_func=lambda key: "Bitte wählen" if key is None else _label(right_choices[key]),
        key="duplicate_right",
    )
    reason = st.text_input("Begründung", key="duplicate_reason")
    if (
        st.button("Duplikat zur Prüfung einreichen", disabled=not (left and right and reason.strip()))
        and left
        and right
    ):
        case = queue_duplicate(kind, left, right, reason=reason, actor_id=_actor())
        st.success(f"Prüffall {case.id} gespeichert. Noch nichts zusammengeführt.")
    st.divider()
    st.caption(
        "Ähnlichkeitssuche ist begrenzt, kein vollständiger Datenbankscan. Vorschläge werden nie automatisch gemergt."
    )
    if st.button("Ähnliche Datensätze suchen"):
        st.session_state.duplicate_results = (kind, query, discover_duplicates(kind, query, limit=30))
    saved = st.session_state.get("duplicate_results")
    if saved and saved[:2] == (kind, query):
        if not saved[2]:
            st.info("Keine Vorschläge im untersuchten Ausschnitt.")
        for pair in saved[2]:
            candidate = pair["right"]
            with st.expander(f"{_label(pair['left'])} ↔ {candidate['label']}"):
                st.write(f"Titel-/Namensähnlichkeit: {candidate['similarity']:.0%}")
                st.write("Belege:", candidate["evidence"])
                if candidate["conflicts"]:
                    st.warning(" · ".join(candidate["conflicts"]))
                st.json({"links": pair["left"], "rechts": candidate["record"]}, expanded=False)
                if st.button("Als Prüffall speichern", key=f"pair_{pair['left']['id']}_{candidate['id']}"):
                    case = queue_duplicate(
                        kind,
                        pair["left"]["id"],
                        candidate["id"],
                        reason="Ähnlichkeitssuche: " + "; ".join(candidate["evidence"]),
                        actor_id=_actor(),
                    )
                    st.success(f"Prüffall {case.id} gespeichert.")


def _select_target(case: dict, initial: dict) -> str | None:
    if case["operation"] == "merge":
        ids = [initial["target_state"]["record"]["id"], initial["duplicate_state"]["record"]["id"]]
        records = {
            initial["target_state"]["record"]["id"]: initial["target_state"]["record"],
            initial["duplicate_state"]["record"]["id"]: initial["duplicate_state"]["record"],
        }
        return st.selectbox(
            "Diese UUID bleibt erhalten",
            ids,
            format_func=lambda key: _label(records[key]),
            key=f"survivor_{case['id']}",
        )
    query = st.text_input("Zusätzlich nach bestehendem Ziel suchen (Name oder UUID)", key=f"target_query_{case['id']}")
    options = {c["id"]: c["record"] for c in initial["candidates"]}
    if query:
        options.update({row["id"]: row for row in search_entities(case["entity_type"], query)})
    choice = st.selectbox(
        "Ziel auswählen",
        ["choose", "new", *options],
        format_func=lambda key: (
            "Bitte wählen"
            if key == "choose"
            else "Neuen Datensatz anlegen (nur nach Zustimmung)"
            if key == "new"
            else _label(options[key])
        ),
        key=f"target_{case['id']}",
    )
    return choice


def _comparison(case: dict, preview: dict, target_id: str | None) -> list[str]:
    case_id = case["id"]
    if preview["candidates"]:
        st.dataframe(
            [
                {
                    "UUID": row["id"],
                    "Name/Titel": row["label"],
                    "Ähnlichkeit": row["similarity"],
                    "Belege": "; ".join(row["evidence"]),
                    "Konflikte": "; ".join(row["conflicts"]),
                    "Empfehlung": row["recommendation"],
                }
                for row in preview["candidates"]
            ],
            hide_index=True,
        )
    if preview["blocked"]:
        st.warning("Noch nicht genehmigbar: " + " · ".join(preview["blocked"]))
        st.caption("Prüffälle mit diesen UUIDs zuerst bearbeiten. Fehlende Angaben gelten nicht als Widerspruch.")
    if preview["credits"]:
        st.subheader("Beteiligte Personen & Credits")
        st.dataframe(preview["credits"], hide_index=True)
        st.caption(
            "Bestehende Credits bleiben erhalten. Neue Credits werden nur mit genehmigter Personenidentität ergänzt."
        )
    if case["operation"] == "merge":
        st.warning(
            "Beim Zusammenführen wird das Duplikat gelöscht. Alle Verknüpfungen werden auf die gewählte UUID "
            "umgehängt; Quellbelege, Weiterleitungen und Änderungsverlauf bleiben erhalten. "
            "Nicht ausgewählte Konfliktwerte bleiben nur im Verlauf erhalten."
        )
        with st.expander("Betroffene Verknüpfungen und Quellen"):
            st.json({"Ziel": preview["target_state"], "Duplikat": preview["duplicate_state"]})
    st.subheader("Felder vergleichen")
    selected = []
    for row in preview["fields"]:
        with st.expander(row["field"], expanded=False):
            st.json({"Datenbank": row["current"], "Quelle / Duplikat": row["incoming"], "Ergebnis": row["value"]})
            if st.checkbox(
                "Wert übernehmen",
                value=row["selected"],
                key=f"field_{case_id}_{target_id}_{row['field']}_{preview['revision']}",
            ):
                selected.append(row["field"])
    if not preview["fields"]:
        st.info("Keine Feldänderungen. Quellzuordnung und neue, geprüfte Credits können trotzdem übernommen werden.")
    with st.expander("Vollständige Quelldaten und Credits"):
        st.json({"Daten": preview["incoming"], "Kontext": case["context"]})
    return selected


def _decision_buttons(case_id: str, preview: dict, target_choice: str | None, selected: list[str]) -> None:
    target_id = preview["target_id"]
    note = st.text_input("Entscheidungsnotiz", key=f"note_{case_id}")
    confirmed = st.checkbox(
        "Identität, Felder und Credits geprüft; Entscheidung ausführen",
        key=f"confirm_{case_id}_{target_choice}_{preview['revision']}",
    )
    if target_choice == "new":
        st.info("Neuanlage übernimmt alle Quelldaten. Feldhäkchen gelten nur für Updates / Zusammenführungen.")
    allowed = confirmed and target_choice not in {None, "choose"} and not preview["blocked"]
    if st.button("Genehmigen und speichern", type="primary", disabled=not allowed):
        entity_id = approve_case(
            case_id,
            revision=preview["revision"],
            target_id=target_id,
            fields=selected,
            allow_create=target_choice == "new",
            actor_id=_actor(),
            note=note,
        )
        st.session_state["_flash"] = f"Gespeichert: {entity_id}"
        st.rerun()


def _pending_case(case: dict) -> None:
    case_id = case["id"]
    initial = preview_case(case_id)
    target_choice = _select_target(case, initial)
    target_id = target_choice if target_choice not in {None, "choose", "new"} else None
    preview_key = f"preview_{case_id}_{target_id}"
    if st.button("Vergleich aktualisieren", key=f"refresh_{case_id}"):
        st.session_state.pop(preview_key, None)
    if preview_key not in st.session_state:
        st.session_state[preview_key] = preview_case(case_id, target_id=target_id) if target_id else initial
    preview = st.session_state[preview_key]
    selected = _comparison(case, preview, target_id)
    _decision_buttons(case_id, preview, target_choice, selected)


def _review_tab() -> None:
    st.caption("Vorschläge sind Hinweise, keine automatischen Entscheidungen. Namen allein beweisen keine Identität.")
    kind = st.selectbox(
        "Typ filtern",
        ["all", *KINDS],
        format_func=lambda k: "Alle Typen" if k == "all" else KINDS[k],
        key="review_kind",
    )
    status = st.selectbox(
        "Status",
        ["pending", "approved", "rejected"],
        format_func={"pending": "Offen", "approved": "Genehmigt", "rejected": "Abgelehnt"}.__getitem__,
    )
    query = st.text_input("Prüffälle suchen (Name, Titel, Grund oder Fall-UUID)")
    page = st.number_input("Seite", min_value=1, value=1, step=1)
    cases = list_cases(status=status, kind=None if kind == "all" else kind, query=query, offset=(page - 1) * 200)
    if not cases:
        st.info("Keine Prüffälle für diesen Filter.")
        return
    by_id = {case["id"]: case for case in cases}
    st.caption("Bis zu 200 Fälle, älteste zuerst. Voraussetzungen zuerst genehmigen.")
    case_id = st.selectbox(
        "Prüffall",
        list(by_id),
        format_func=lambda key: (
            f"{KINDS[by_id[key]['entity_type']]} · "
            f"{by_id[key]['incoming'].get('name') or by_id[key]['incoming'].get('title') or by_id[key]['reason']} · {key}"
        ),
        key=f"review_case_{kind}_{status}",
    )
    if case_id is None:
        return
    case = by_id[case_id]
    st.write(case["reason"])
    sources = {str(source.id): source.name for source in list_entities(Source)}
    source_name = sources.get(case["source_id"], case["source_id"] or "Manuelle Einreichung")
    st.caption(f"Quelle: {source_name} · Quellstand: {case['captured_at'] or 'Unbekannt'}")
    if status == "pending":
        with st.expander("Prüffall ablehnen"):
            note = st.text_input("Ablehnungsgrund", key=f"reject_note_{case_id}")
            if st.button("Ablehnen", disabled=not note.strip()):
                decide_case(case_id, status="rejected", actor_id=_actor(), note=note)
                st.rerun()
        _pending_case(case)
    else:
        st.json(case)
        if status == "rejected":
            note = st.text_input("Begründung für erneute Prüfung")
            if st.button("Erneut öffnen", disabled=not note.strip()):
                decide_case(case_id, status="pending", actor_id=_actor(), note=note)
                st.rerun()


def _render_tabs() -> None:
    init_db()
    review, duplicates, imports = st.tabs(["Prüfliste", "Duplikate suchen / melden", "Theater-JSON einreichen"])
    with review:
        _review_tab()
    with duplicates:
        _duplicates_tab()
    with imports:
        _import_tab()


def render() -> None:
    """Render the review desk and surface validation/database failures explicitly."""
    st.title("Datenabgleich & Duplikate")
    st.write("Import prüfen, bestehende Daten ergänzen und Duplikate kontrolliert zusammenführen.")
    try:
        _render_tabs()
    except (ValueError, KeyError, TypeError, SQLAlchemyError) as exc:
        st.error(f"Datenabgleich fehlgeschlagen: {exc}")


if __name__ == "__main__":
    render()
