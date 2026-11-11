"""Native table merge actions and shared reconciliation comparison formatting."""

import pandas as pd
import streamlit as st
from pandas.io.formats.style import Styler
from sqlalchemy.exc import SQLAlchemyError

from backend.config import ENTITY_LABELS, FIELD_STATUS, ROW_COLORS
from backend.date_precision import DatePrecision, format_date, parse_date
from backend.reconciliation import merge_entities, preview_entity_merge

FIELD_LABELS = {
    "name": "Name",
    "title": "Titel",
    "born": "Geboren",
    "died": "Gestorben",
    "published": "Veröffentlicht",
    "language": "Sprache",
    "nationality": "Nationalität",
    "description": "Beschreibung",
    "type": "Typen",
    "urls": "URLs",
}
ERRORS = (ValueError, TypeError, SQLAlchemyError)


def format_value(field: str, value: object, precision: str | None = None) -> str:
    """Format field values consistently across import and entity merge comparisons."""
    if value in (None, "", [], {}):
        return "—"
    if precision or field == "premiere":
        parsed, inferred = parse_date(value)
        return format_date(parsed, DatePrecision(precision or inferred))
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if field == "duration":
        return f"{value} Min."
    if field == "age_rating":
        return f"ab {value} J."
    return str(value)


def styled(frame: pd.DataFrame, statuses: list[str]) -> Styler:
    """Color comparison rows using the existing reconciliation status palette."""
    styles = pd.DataFrame(
        [[f"background-color: {ROW_COLORS[s]}" if s in ROW_COLORS else ""] * len(frame.columns) for s in statuses],
        index=frame.index,
        columns=frame.columns,
    )
    return frame.style.apply(lambda _: styles, axis=None)


def _labels(preview: dict) -> dict[str, str]:
    return {
        state["record"]["id"]: (
            f"{state['record'].get('name') or state['record'].get('title')} · {state['record']['id'][:8]}"
        )
        for state in preview["records"]
    }


def _complete(kind: str, preview: dict, actor: str, choices: dict[str, str]) -> None:
    merge_entities(
        kind,
        [state["record"]["id"] for state in preview["records"]],
        target_id=preview["target_id"],
        revision=preview["revision"],
        actor_id=actor,
        choices=choices,
    )
    st.session_state.pop(f"merge_preview_{kind}", None)
    st.session_state[f"table_epoch_{kind}"] = st.session_state.get(f"table_epoch_{kind}", 0) + 1
    st.session_state["_flash"] = f"{len(preview['records'])} {ENTITY_LABELS[kind]} zusammengeführt."
    st.rerun()


def _comparison(preview: dict, choices: dict[str, str], labels: dict[str, str]) -> None:
    rows, statuses = [], []
    for field in preview["fields"]:
        name = field["field"]
        option = next((o for o in field["options"] if o["entity_id"] == choices.get(name)), None)
        value = option["value"] if option else field["value"]
        precision = option.get("precision") if option else preview["result"].get(f"{name}_precision")
        values = {
            labels[state["record"]["id"]]: format_value(
                name, state["record"][name], state["record"].get(f"{name}_precision")
            )
            for state in preview["records"]
        }
        original = preview["records"][0]["record"][name]
        status = (
            "conflict"
            if field["conflict"]
            else "same"
            if value == original
            else "extended"
            if isinstance(value, list)
            else "new"
        )
        rows.append(
            {
                "Feld": FIELD_LABELS.get(name, name),
                **values,
                "Ergebnis": format_value(name, value, precision),
                "Status": FIELD_STATUS[status],
            }
        )
        statuses.append(status)
    st.dataframe(styled(pd.DataFrame(rows), statuses), hide_index=True, width="stretch")
    counts: dict[str, int] = {}
    for state in preview["records"][1:]:
        for reference, links in state["references"].items():
            counts[reference] = counts.get(reference, 0) + len(links)
    with st.expander("Betroffene Verknüpfungen und Quellen"):
        st.dataframe(
            [{"Verknüpfung": name, "Wird umgehängt": count} for name, count in counts.items()],
            hide_index=True,
        )
        st.caption(
            f"{sum(len(state['sources']) for state in preview['records'])} Quellbelege bleiben erhalten. "
            "Gleiche Verknüpfungen werden vereinigt; alte IDs bleiben als Weiterleitungen gültig."
        )


def _field_choices(preview: dict, labels: dict[str, str]) -> dict[str, str]:
    choices = {}
    for field in preview["fields"]:
        if not field["conflict"]:
            continue
        name = field["field"]
        options = {option["entity_id"]: option for option in field["options"]}
        choice = st.selectbox(
            f"{FIELD_LABELS.get(name, name)}: Wert behalten",
            list(options),
            index=None,
            placeholder="Wert auswählen",
            format_func=lambda entity_id, options=options, name=name: (
                f"{format_value(name, options[entity_id]['value'], options[entity_id].get('precision'))}"
                f" · {labels[entity_id]}"
            ),
            key=f"merge_choice_{preview['revision']}_{name}",
        )
        if choice is not None:
            choices[name] = choice
    return choices


def _merge_dialog(kind: str, actor: str) -> None:
    key = f"merge_preview_{kind}"
    preview = st.session_state[key]
    labels = _labels(preview)
    target = st.selectbox(
        "Dieser Datensatz bleibt erhalten",
        list(labels),
        index=list(labels).index(preview["target_id"]),
        format_func=labels.__getitem__,
        key=f"merge_survivor_{kind}",
    )
    if target != preview["target_id"]:
        try:
            preview = preview_entity_merge(kind, list(labels), target_id=target)
        except ERRORS as exc:
            st.error(f"Zusammenführung fehlgeschlagen: {exc}")
            return
        st.session_state[key] = preview
    choices = _field_choices(preview, labels)
    _comparison(preview, choices, labels)
    st.warning(
        "Die übrigen ausgewählten Datensätze werden gelöscht. Alle Verknüpfungen werden umgehängt; "
        "Quellidentitäten und Änderungsverlauf bleiben erhalten."
    )
    save, refresh, cancel = st.columns(3)
    if save.button(
        "Zusammenführung speichern",
        type="primary",
        disabled=len(choices) != sum(field["conflict"] for field in preview["fields"]),
    ):
        try:
            _complete(kind, preview, actor, choices)
        except ERRORS as exc:
            st.error(f"Zusammenführung fehlgeschlagen: {exc}")
    if refresh.button("Vergleich aktualisieren"):
        try:
            st.session_state[key] = preview_entity_merge(kind, list(labels), target_id=target)
        except ERRORS as exc:
            st.error(f"Zusammenführung fehlgeschlagen: {exc}")
        else:
            st.rerun()
    if cancel.button("Abbrechen"):
        st.session_state.pop(key, None)
        st.rerun()


def render_merge_action(kind: str, rows: list[dict], actor: str | None) -> None:
    """Merge a native table selection immediately or show a conflict dialog."""
    ids = [row["id"] for row in rows]
    key = f"merge_preview_{kind}"
    preview = st.session_state.get(key)
    if preview and {state["record"]["id"] for state in preview["records"]} != set(ids):
        st.session_state.pop(key, None)
    if st.button("Zusammenführen", icon=":material/merge:", disabled=len(ids) < 2, key=f"merge_{kind}"):
        try:
            preview = preview_entity_merge(kind, ids)
        except ERRORS as exc:
            st.error(f"Zusammenführung fehlgeschlagen: {exc}")
        else:
            if any(field["conflict"] for field in preview["fields"]):
                st.session_state[key] = preview
                st.session_state.pop(f"merge_survivor_{kind}", None)
            else:
                try:
                    _complete(kind, preview, actor or "management-ui", {})
                except ERRORS as exc:
                    st.error(f"Zusammenführung fehlgeschlagen: {exc}")
    if key in st.session_state:
        st.dialog(
            "Zusammenführung prüfen",
            width="large",
            on_dismiss=lambda: st.session_state.pop(key, None),
        )(_merge_dialog)(kind, actor or "management-ui")
