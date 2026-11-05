"""German review desk: theatre uploads, production conflicts and manually submitted duplicates."""

import datetime as dt
import json
import re
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st
from pandas.io.formats.style import Styler
from sqlalchemy.exc import SQLAlchemyError

from backend.entity_management import list_entities
from backend.import_status import FIELDS, scrape_overview, theatre_status
from backend.models import Organization, Source
from backend.production_import import (
    OPERATION,
    ingest_records,
    list_production_cases,
    production_review,
    reject_production_case,
    resolve_production_case,
)
from backend.reconciliation import (
    approve_case,
    decide_case,
    discover_duplicates,
    list_cases,
    merge_people,
    preview_case,
    preview_people_merge,
    queue_duplicate,
    search_entities,
    search_people_for_merge,
    stable_digest,
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
ROLES = {
    "director": "Regie",
    "actor": "Schauspiel",
    "dramaturg": "Dramaturgie",
    "set_designer": "Bühne",
    "costume_designer": "Kostüme",
    "light_designer": "Licht",
    "choreographer": "Choreografie",
    "composer": "Musik",
    "video_designer": "Video",
    "author": "Autor:in",
    "translator": "Übersetzung",
    "adapter": "Fassung",
}
FIELD_STATUS = {
    "same": "✅ gleich",
    "new": "🆕 wird ergänzt",
    "extended": "🔗 wird erweitert",
    "kept": "◻️ bleibt (nur Bestand)",
    "conflict": "⚠️ Konflikt - bitte wählen",
}
PEOPLE_STATUS = {
    "same": "✅ in beiden",
    "new_known": "🆕 wird verknüpft (Person existiert)",
    "new_person": "🆕 Person wird neu angelegt",
    "kept": "◻️ nur Bestand",
}
ROW_COLORS = {
    "conflict": "#fde2e1",
    "new": "#e3f6e8",
    "extended": "#e3f6e8",
    "new_known": "#e3f6e8",
    "new_person": "#fff4d6",
}
NEW_PRODUCTION = "__new__"


@st.cache_data(ttl=30)
def _theatre_names() -> dict[str, str]:
    return {str(org.id): org.name for org in list_entities(Organization)}


def _label(record: dict) -> str:
    name = record.get("name") or record.get("title") or "Ohne Titel"
    details = [
        record.get("city")
        or (record.get("premiere") or "")[:10]
        or str(record.get("premiere_year") or "")
        or record.get("born")
        or ""
    ]
    if record.get("organization_id"):
        details.append(_theatre_names().get(record["organization_id"], ""))
    return " · ".join([name, *[detail for detail in details if detail]])


def _unique(labels: dict) -> dict:
    """Streamlit keys selectbox state by label: number repeated labels (namesakes) instead of showing UUIDs."""
    seen: dict[str, int] = {}
    result = {}
    for key, label in labels.items():
        seen[label] = seen.get(label, 0) + 1
        result[key] = label if seen[label] == 1 else f"{label} ({seen[label]})"
    return result


def _actor() -> str:
    return _actor_id() or "reconciliation-ui"


def _theatre_label(org: Organization) -> str:
    return f"{org.name} · {org.city}" if org.city else org.name


def _import_tab() -> None:
    st.write(
        "Theater wählen und JSON hochladen. Gleiche Produktionen dieses Theaters werden automatisch ergänzt, "
        "neue Produktionen angelegt. Nur echte Widersprüche landen unter **Produktionskonflikte**."
    )
    theatres = sorted(list_entities(Organization), key=lambda org: org.name.casefold())
    if not theatres:
        st.info("Noch kein Theater vorhanden. Bitte zuerst unter Verwaltung → Organisationen anlegen.")
        return
    by_id = {str(org.id): org for org in theatres}
    theatre_labels = _unique({key: _theatre_label(org) for key, org in by_id.items()})
    theatre = st.selectbox(
        "Theater",
        list(by_id),
        index=None,
        placeholder="Theater suchen und wählen",
        format_func=theatre_labels.__getitem__,
        key="upload_theatre",
    )
    if theatre:
        _theatre_status(theatre)
    uploaded = st.file_uploader("Produktionsdatei (JSON)", type=["json"])
    records = None
    if uploaded is not None:
        try:
            records = json.loads(uploaded.getvalue())
        except json.JSONDecodeError as exc:
            st.error(f"Keine gültige JSON-Datei: {exc}")
    first_url = (
        records[0].get("url", "") if isinstance(records, list) and records and isinstance(records[0], dict) else ""
    )
    source = st.text_input(
        "Quelle",
        value=urlsplit(first_url).netloc,
        key=f"upload_source_{uploaded.name if uploaded else ''}",
        help="Wird als zusätzliche Quelle an jeder Produktion gespeichert.",
    )
    match = re.match(r"(\d{8})_", uploaded.name) if uploaded else None
    inferred_date = dt.datetime.strptime(match[1], "%Y%m%d").replace(tzinfo=dt.UTC).date() if match else None
    known_date = st.checkbox("Erfassungsdatum bekannt", value=bool(inferred_date))
    capture = st.date_input("Stand der Quelldaten (nicht Importdatum)", value=inferred_date) if known_date else None
    ready = records is not None and theatre is not None and bool(source.strip())
    if st.button("Importieren", type="primary", disabled=not ready) and ready:
        with st.spinner(f"{len(records)} Produktionen werden geprüft und mit dem Bestand abgeglichen …"):
            result = ingest_records(
                records,
                organization_id=theatre,
                source_name=source,
                actor_id=_actor(),
                captured_at=dt.datetime.combine(capture, dt.time(), tzinfo=dt.UTC) if capture else None,
                file_name=uploaded.name if uploaded else None,
            )
        st.session_state["upload_result"] = (by_id[theatre].name, result)
        # Rerun so the conflict tab (rendered before this tab) shows the new cases; stay on this tab.
        st.session_state["show_import_tab"] = True
        st.rerun()
    _import_summary()
    _scrape_overview()


def _import_summary() -> None:
    saved = st.session_state.get("upload_result")
    if saved:
        theatre_name, result = saved
        st.success(f"Import für {theatre_name} abgeschlossen.")
        labels = {
            "merged": "Ergänzt",
            "created": "Neu angelegt",
            "review": "Zur Prüfung",
            "unchanged": "Unverändert",
        }
        for column, (key, label) in zip(st.columns(len(labels)), labels.items(), strict=True):
            column.metric(label, len(result[key]))
        for key, label in labels.items():
            if result[key]:
                with st.expander(f"{label}: {len(result[key])}"):
                    st.write("\n".join(f"- {name} · {theatre_name}" for name in result[key]))
        if result["review"]:
            st.info("Offene Konflikte im Reiter **Produktionskonflikte** bearbeiten.")


SCOPES = {"all": "vollständig"}
FRESHNESS = ((30, "🟢"), (90, "🟡"))


def _scope(scope: str | None) -> str:
    if scope is None:
        return "Umfang unbekannt"
    return SCOPES.get(scope, f"Teil-Scrape (max. {scope.removeprefix('limit-')})")


def _age(days: int) -> str:
    icon = next((icon for limit, icon in FRESHNESS if days <= limit), "🔴")
    return f"{icon} heute" if days <= 0 else f"{icon} vor {days} Tagen"


def _share(count: int, total: int) -> str:
    return f"{count} ({count / total:.0%})" if total else "-"


def _theatre_status(organization_id: str) -> None:
    status = theatre_status(organization_id)
    latest = status["latest"]
    with st.container(border=True):
        if latest is None:
            st.markdown(f"**{status['theatre']}**: noch kein Upload.")
        else:
            date_label = "Stand" if latest["captured_known"] else "Hochgeladen (Stand unbekannt)"
            st.markdown(
                f"**{status['theatre']}** · {date_label} {latest['data_date']:%d.%m.%Y} ({_age(latest['age_days'])}) · "
                f"{_scope(latest['scope'])} · {latest['total']} Produktionen · Quelle {latest['source']}"
            )
        total = status["productions"]
        columns = st.columns(4)
        columns[0].metric("Produktionen", total)
        columns[1].metric("mit Upload-Quelle", _share(status["scraped"], total))
        columns[2].metric(
            "von eigener Website",
            _share(status["own_website"], total),
            help="Website: " + (", ".join(status["own_hosts"]) or "unbekannt - beim Theater hinterlegen"),
        )
        columns[3].metric("offene Konflikte", status["open_conflicts"])
        if total:
            st.dataframe(
                pd.DataFrame(
                    {"Feld": list(FIELDS.values()), "Vollständig": [n / total for n in status["fields"].values()]}
                ),
                column_config={
                    "Vollständig": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)
                },
                hide_index=True,
                width="stretch",
            )
        if status["runs"]:
            with st.expander(f"Bisherige Uploads ({len(status['runs'])})"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Stand": run["data_date"].strftime("%d.%m.%Y"),
                                "Hochgeladen": run["imported_at"].strftime("%d.%m.%Y %H:%M"),
                                "Umfang": _scope(run["scope"]),
                                "Produktionen": run["total"],
                                "Ergänzt": run["merged"],
                                "Neu": run["created"],
                                "Konflikte": run["review"],
                                "Unverändert": run["unchanged"],
                                "Datei": run["file_name"] or "",
                            }
                            for run in status["runs"]
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )


def _scrape_overview() -> None:
    st.divider()
    st.subheader("Scraping-Stand aller Theater")
    days = st.segmented_control(
        "Als aktuell gilt",
        [30, 90, 365],
        default=90,
        format_func=lambda value: f"≤ {value} Tage",
        key="overview_days",
    )
    overview = scrape_overview(recent_days=days or 90)
    columns = st.columns(3)
    columns[0].metric(f"Aktuell (≤ {overview['recent_days']} Tage)", _share(overview["recent"], overview["theatres"]))
    columns[1].metric("Jemals hochgeladen", _share(overview["uploaded"], overview["theatres"]))
    columns[2].metric("Theater mit Produktionen", overview["theatres"])
    if not overview["rows"]:
        st.info("Noch keine Uploads.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Theater": row["theatre"],
                    "Stand": row["latest"]["data_date"].strftime("%d.%m.%Y"),
                    "Alter": _age(row["latest"]["age_days"]),
                    "Umfang": _scope(row["latest"]["scope"]),
                    "Produktionen": row["productions"],
                    "mit Upload-Quelle": row["scraped"] / row["productions"] if row["productions"] else 0.0,
                    "eigene Website": row["own_website"] / row["productions"] if row["productions"] else 0.0,
                    "Konflikte": row["open_conflicts"],
                }
                for row in overview["rows"]
            ]
        ),
        column_config={
            column: st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1)
            for column in ("mit Upload-Quelle", "eigene Website")
        },
        hide_index=True,
        width="stretch",
    )
    st.caption("Älteste zuerst. 🟢 ≤ 30 Tage · 🟡 ≤ 90 Tage · 🔴 älter. Stand = Erfassungsdatum der Daten.")


def _format(field: str, value: object) -> str:
    if value in (None, "", [], {}):
        return "—"
    if field == "premiere":
        return dt.date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if field == "duration":
        return f"{value} Min."
    if field == "age_rating":
        return f"ab {value} J."
    return str(value)


def _styled(frame: pd.DataFrame, statuses: list[str]) -> Styler:
    styles = pd.DataFrame(
        [[f"background-color: {ROW_COLORS[s]}" if s in ROW_COLORS else ""] * len(frame.columns) for s in statuses],
        index=frame.index,
        columns=frame.columns,
    )
    return frame.style.apply(lambda _: styles, axis=None)


def _field_table(review: dict) -> None:
    rows = [row for row in review["fields"] if row["status"] != "empty"]
    frame = pd.DataFrame(
        [
            {
                "Feld": row["label"],
                "Bestand": _format(row["field"], row["existing"]) if review["target"] else "—",
                "Upload": _format(row["field"], row["incoming"]),
                "Ergebnis": FIELD_STATUS[row["status"]] if review["target"] else "🆕 neu",
            }
            for row in rows
        ]
    )
    if review["target"] is None:
        st.dataframe(frame, hide_index=True, width="stretch")
        return
    st.dataframe(_styled(frame, [row["status"] for row in rows]), hide_index=True, width="stretch")


def _people_table(review: dict) -> None:
    if not review["people"]:
        st.caption("Keine Beteiligten im Upload.")
        return
    frame = pd.DataFrame(
        [
            {
                "Person": row["name"],
                "Funktion": ROLES.get(row["role"], row["role"] or "—"),
                "Figur": row["character"] or "",
                "Ergebnis": PEOPLE_STATUS[row["status"]],
            }
            for row in review["people"]
        ]
    )
    st.dataframe(_styled(frame, [row["status"] for row in review["people"]]), hide_index=True, width="stretch")
    st.caption("Personen werden über Name, Quelle und Theater wiedererkannt - es entstehen keine Dubletten.")


def _conflict_choices(review: dict) -> dict[str, str]:
    choices = {}
    rows = {row["field"]: row for row in review["fields"]}
    directors = {
        "existing": ", ".join(p["name"] for p in review["people"] if p["role"] == "director" and p["status"] == "kept"),
        "incoming": ", ".join(p["name"] for p in review["people"] if p["role"] == "director" and p["status"] != "kept"),
    }
    for field in review["conflicts"]:
        if field == "director":
            label, values = "Regie", directors
        else:
            row = rows[field]
            label = row["label"]
            values = {"existing": _format(field, row["existing"]), "incoming": _format(field, row["incoming"])}
        with st.container(border=True):
            st.markdown(f"**⚠️ {label}**")
            left, right = st.columns(2)
            left.caption("Bestand")
            left.write(values["existing"])
            right.caption("Upload")
            right.write(values["incoming"])
            choices[field] = st.radio(
                f"Welcher Wert gilt für {label}?",
                ["existing", "incoming"],
                format_func={"existing": "Bestand behalten", "incoming": "Upload übernehmen"}.__getitem__,
                horizontal=True,
                key=f"choice_{review['case_id']}_{review['target']['id']}_{field}",
            )
    return choices


def _header(review: dict) -> None:
    left, right = st.columns(2)
    with left.container(border=True):
        st.caption("BESTAND (Datenbank)")
        if review["target"]:
            st.markdown(f"#### {review['target']['label']}")
            sources = review["target"]["sources"]
            st.write("Quellen: " + (", ".join(s["source"] for s in sources) if sources else "keine"))
        else:
            st.markdown("#### 🆕 Neue Produktion")
            st.write(f"Wird bei {review['theatre']} angelegt.")
    with right.container(border=True):
        st.caption("UPLOAD")
        st.markdown(f"#### {review['incoming_label']}")
        stand = (
            dt.date.fromisoformat(review["captured_at"]).strftime("%d.%m.%Y") if review["captured_at"] else "unbekannt"
        )
        st.write(f"Quelle: [{review['source']['name']}]({review['source']['url']}) · Stand {stand}")


def _conflict_tab() -> None:
    theatres = _theatre_names()
    cases = list_production_cases()
    if not cases:
        st.success("Keine offenen Produktionskonflikte.")
        return
    theatre_filter = st.selectbox(
        "Theater",
        [None, *sorted({case["organization_id"] for case in cases}, key=lambda key: theatres.get(key, ""))],
        format_func=lambda key: "Alle Theater" if key is None else theatres.get(key, "Unbekanntes Theater"),
        key="conflict_theatre",
    )
    cases = [case for case in cases if theatre_filter is None or case["organization_id"] == theatre_filter]
    by_id = {case["id"]: case for case in cases}
    st.caption(f"{len(cases)} offene Konflikte - nach dem Speichern folgt automatisch der nächste.")
    case_labels = _unique(
        {key: f"{case['name']} · {case['theatre']} - {case['reason']}" for key, case in by_id.items()}
    )
    case_id = st.selectbox(
        "Konflikt",
        list(by_id),
        format_func=case_labels.__getitem__,
        key="conflict_case",
    )
    if case_id is None:
        return
    suggested = production_review(case_id)
    candidates = {row["id"]: row for row in suggested["candidates"]}
    options = [*candidates, NEW_PRODUCTION]
    default = suggested["target"]["id"] if suggested["target"] else NEW_PRODUCTION
    target_labels = _unique(
        {
            **{key: f"{row['label']}  ({row['similarity']:.0%} ähnlich)" for key, row in candidates.items()},
            NEW_PRODUCTION: "🆕 Keine davon - als neue Produktion anlegen",
        }
    )
    choice = st.selectbox(
        "Vergleichen mit (nur Produktionen desselben Theaters; tippen zum Suchen)",
        options,
        index=options.index(default),
        format_func=target_labels.__getitem__,
        key=f"conflict_target_{case_id}",
    )
    create = choice == NEW_PRODUCTION
    review = (
        suggested
        if choice == default
        else production_review(case_id, target_id=None if create else choice, create=create)
    )
    _header(review)
    st.subheader("Felder")
    _field_table(review)
    choices = _conflict_choices(review) if review["target"] else {}
    st.subheader("Beteiligte")
    _people_table(review)
    revision_key = f"conflict_revision_{case_id}_{choice}"
    shown_revision = st.session_state.get(revision_key, review["revision"])
    st.session_state[revision_key] = review["revision"]
    save_col, reject_col = st.columns([3, 1])
    action = "Als neue Produktion anlegen" if create else "Zusammenführen"
    if save_col.button(action, type="primary", width="stretch", key=f"resolve_{case_id}"):
        try:
            resolve_production_case(
                case_id,
                revision=shown_revision,
                actor_id=_actor(),
                target_id=None if create else choice,
                create=create,
                choices=choices,
            )
        except ValueError as exc:
            if "Data changed" not in str(exc):
                raise
            st.error("Daten wurden zwischenzeitlich geändert. Der Vergleich ist aktualisiert - bitte erneut prüfen.")
        else:
            st.session_state["_flash"] = f"Gespeichert: {review['incoming_label']}"
            st.session_state.pop("conflict_case", None)
            st.rerun()
    if reject_col.button("Upload verwerfen", width="stretch", key=f"reject_{case_id}"):
        reject_production_case(case_id, actor_id=_actor())
        st.session_state["_flash"] = f"Verworfen: {review['incoming_label']}"
        st.session_state.pop("conflict_case", None)
        st.rerun()


def _people_merge_review(preview: dict) -> None:
    labels = _unique({state["record"]["id"]: _label(state["record"]) for state in preview["people"]})
    st.subheader("Zusammenführung prüfen")
    st.dataframe(
        [
            {"Person": labels[state["record"]["id"]], **{k: v for k, v in state["record"].items() if k != "id"}}
            for state in preview["people"]
        ],
        hide_index=True,
    )
    choices = {}
    result = dict(preview["result"])
    for field in preview["fields"]:
        if not field["conflict"]:
            continue
        options = {option["person_id"]: option["value"] for option in field["options"]}
        choice = st.selectbox(
            f"{field['field']}: Wert behalten",
            list(options),
            format_func=lambda person_id, options=options: f"{options[person_id]} · {labels[person_id]}",
            key=f"people_choice_{preview['revision']}_{field['field']}",
        )
        choices[field["field"]] = choice
        result[field["field"]] = options[choice]
    with st.expander("Ergebnis und betroffene Verknüpfungen"):
        st.json({"Ergebnis": result, "Personen": preview["people"]})
    st.warning(
        "Die übrigen ausgewählten Personen werden gelöscht. Alle Verknüpfungen, Quellen und alten IDs werden "
        "auf den verbleibenden Datensatz umgeleitet. Nicht gewählte Feldwerte bleiben im Änderungsverlauf erhalten."
    )
    confirmed = st.checkbox(
        "Alle ausgewählten Datensätze sind dieselbe Person; Zusammenführung bestätigen",
        key=f"people_confirm_{preview['revision']}",
    )
    if st.button("Zusammenführung speichern", type="primary", disabled=not confirmed):
        merge_people(
            [state["record"]["id"] for state in preview["people"]],
            target_id=preview["target_id"],
            revision=preview["revision"],
            actor_id=_actor(),
            choices=choices,
        )
        st.session_state.pop("people_merge_preview", None)
        st.session_state["_flash"] = f"{len(preview['people'])} Personen zusammengeführt."
        st.rerun()


def _people_duplicates(query: str) -> list[dict]:
    page = st.number_input("Personenseite", min_value=1, value=1, step=1, key=f"people_page_{query}")
    records = search_people_for_merge(query, offset=(page - 1) * 100)
    st.caption(
        "Bis zu 100 Personen pro Seite, nach Name sortiert. Auswahl gilt für diese Seite. "
        "Produktionen und Theater helfen, Namensgleiche zu unterscheiden."
    )
    if not records:
        st.info("Keine Personen auf dieser Seite.")
        st.session_state.pop("people_merge_preview", None)
        return records
    table = pd.DataFrame(records)
    table["productions"] = table["productions"].map("; ".join)
    table.insert(0, "Auswahl", False)
    edited = st.data_editor(
        table,
        hide_index=True,
        width="stretch",
        column_order=[
            "Auswahl",
            "name",
            "productions",
            *[field for field in records[0] if field not in {"name", "productions"}],
        ],
        disabled=[column for column in table.columns if column != "Auswahl"],
        column_config={
            "id": None,
            "Auswahl": st.column_config.CheckboxColumn("Auswahl", width="small"),
            "name": st.column_config.TextColumn("Name"),
            "productions": st.column_config.TextColumn("Produktionen · Theater", width="large"),
        },
        key=f"people_selection_{stable_digest([query, page, table['id'].tolist()])}",
    )
    selected = edited.loc[edited["Auswahl"], "id"].tolist()
    labels = _unique({row["id"]: _label(row) for row in records if row["id"] in selected})
    target = st.selectbox(
        "Dieser Personendatensatz bleibt erhalten",
        selected,
        format_func=labels.__getitem__,
        key=f"people_survivor_{stable_digest(selected)}",
        disabled=len(selected) < 2,
    )
    if st.button("Personen zusammenführen", disabled=len(selected) < 2):
        st.session_state["people_merge_preview"] = preview_people_merge(selected, target_id=target)
    preview = st.session_state.get("people_merge_preview")
    if preview and (
        {state["record"]["id"] for state in preview["people"]} != set(selected) or preview["target_id"] != target
    ):
        st.session_state.pop("people_merge_preview", None)
        preview = None
    if preview:
        if st.button("Personenvergleich aktualisieren"):
            preview = preview_people_merge(selected, target_id=target)
            st.session_state["people_merge_preview"] = preview
        _people_merge_review(preview)
    return records


def _duplicates_tab() -> None:
    kind = st.selectbox(
        "Bereich",
        ["person", "organization", "text", "production", "award"],
        format_func=KINDS.__getitem__,
        key="duplicate_kind",
    )
    query = st.text_input("Name, Titel oder UUID suchen", key="duplicate_query")
    if kind == "person":
        records = _people_duplicates(query)
        with st.expander("Zwei Personen als Prüffall melden / Ähnlichkeitssuche"):
            _duplicate_pairs(kind, query, records)
    else:
        _duplicate_pairs(kind, query, search_entities(kind, query))


def _duplicate_pairs(kind: str, query: str, records: list[dict]) -> None:
    st.write("Bestehende Duplikate finden oder zwei bekannte Datensätze manuell zur Prüfung einreichen.")
    st.caption("Bei gleichen Namen helfen Ort, Datum und Theater.")
    choices = {row["id"]: row for row in records}
    left_labels = _unique({None: "Bitte wählen", **{key: _label(row) for key, row in choices.items()}})
    left = st.selectbox(
        "Bevorzugter Datensatz",
        [None, *choices],
        format_func=left_labels.__getitem__,
        key="duplicate_left",
    )
    right_query = st.text_input("Zweiten Datensatz suchen (auch anderer Name möglich)", key="duplicate_right_query")
    right_records = search_entities(kind, right_query) if right_query else records
    right_choices = {row["id"]: row for row in right_records if row["id"] != left}
    right_labels = _unique({None: "Bitte wählen", **{key: _label(row) for key, row in right_choices.items()}})
    right = st.selectbox(
        "Mögliches Duplikat",
        [None, *right_choices],
        format_func=right_labels.__getitem__,
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
        labels = _unique({key: _label(records[key]) for key in ids})
        return st.selectbox(
            "Dieser Datensatz bleibt erhalten",
            ids,
            format_func=labels.__getitem__,
            key=f"survivor_{case['id']}",
        )
    query = st.text_input("Zusätzlich nach bestehendem Ziel suchen (Name oder UUID)", key=f"target_query_{case['id']}")
    options = {c["id"]: c["record"] for c in initial["candidates"]}
    if query:
        options.update({row["id"]: row for row in search_entities(case["entity_type"], query)})
    labels = _unique(
        {
            "choose": "Bitte wählen",
            "new": "Neuen Datensatz anlegen (nur nach Zustimmung)",
            **{key: _label(row) for key, row in options.items()},
        }
    )
    choice = st.selectbox(
        "Ziel auswählen",
        ["choose", "new", *options],
        format_func=labels.__getitem__,
        key=f"target_{case['id']}",
    )
    return choice


def _comparison(case: dict, preview: dict, target_id: str | None) -> list[str]:
    case_id = case["id"]
    if preview["candidates"]:
        st.dataframe(
            [
                {
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
        st.caption("Abhängige Prüffälle zuerst bearbeiten. Fehlende Angaben gelten nicht als Widerspruch.")
    if preview["credits"]:
        st.subheader("Beteiligte Personen & Credits")
        st.dataframe(preview["credits"], hide_index=True)
        st.caption(
            "Bestehende Credits bleiben erhalten. Neue Credits werden nur mit genehmigter Personenidentität ergänzt."
        )
    if case["operation"] == "merge":
        st.warning(
            "Beim Zusammenführen wird das Duplikat gelöscht. Alle Verknüpfungen werden auf den gewählten Datensatz "
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
    cases = list_cases(
        status=status,
        kind=None if kind == "all" else kind,
        query=query,
        offset=(page - 1) * 200,
        exclude_operations=[OPERATION],
    )
    if not cases:
        st.info("Keine Prüffälle für diesen Filter.")
        return
    by_id = {case["id"]: case for case in cases}
    st.caption("Bis zu 200 Fälle, älteste zuerst. Voraussetzungen zuerst genehmigen.")
    case_labels = _unique(
        {
            key: f"{KINDS[case['entity_type']]} · "
            f"{case['incoming'].get('name') or case['incoming'].get('title') or case['reason']}"
            for key, case in by_id.items()
        }
    )
    case_id = st.selectbox(
        "Prüffall",
        list(by_id),
        format_func=case_labels.__getitem__,
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


IMPORT_TAB = "Theater-JSON hochladen"


def _render_tabs() -> None:
    init_db()
    open_conflicts = len(list_production_cases())
    conflicts, imports, review, duplicates = st.tabs(
        [
            f"Produktionskonflikte ({open_conflicts})",
            IMPORT_TAB,
            "Weitere Prüffälle",
            "Duplikate suchen / melden",
        ],
        default=IMPORT_TAB if st.session_state.pop("show_import_tab", False) else None,
    )
    with conflicts:
        _conflict_tab()
    with imports:
        _import_tab()
    with review:
        _review_tab()
    with duplicates:
        _duplicates_tab()


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
