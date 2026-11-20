"""Reusable collapsible filter section that turns Streamlit inputs into SQL WHERE clauses."""

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import streamlit as st
from sqlalchemy import JSON, String, cast, or_
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import SQLModel, col, select

if TYPE_CHECKING:
    from frontend.management.uifactory import _FieldUI

SqlFilter = ColumnElement[bool]


def _contains(column: Any, value: str) -> SqlFilter:  # ruff: ignore[any-type]
    return col(column).icontains(value, autoescape=True)


def _json_contains(column: Any, value: str) -> SqlFilter:  # ruff: ignore[any-type]
    # JSON is serialized with ensure_ascii, so also match the escaped form of non-ASCII input.
    text = cast(column, String)
    return or_(text.icontains(value, autoescape=True), text.icontains(json.dumps(value)[1:-1], autoescape=True))


def _year_range(column: Any, start: int | None, end: int | None) -> list[SqlFilter]:  # ruff: ignore[any-type]
    clauses = []
    if start is not None:
        clauses.append(col(column) >= dt.datetime(int(start), 1, 1))
    if end is not None:
        clauses.append(col(column) < dt.datetime(int(end) + 1, 1, 1))
    return clauses


def _number_range(column: Any, low: float | None, high: float | None) -> list[SqlFilter]:  # ruff: ignore[any-type]
    clauses = []
    if low is not None:
        clauses.append(col(column) >= low)
    if high is not None:
        clauses.append(col(column) <= high)
    return clauses


def _render_range(field: _FieldUI, key: str, *, years: bool) -> tuple[Any, Any]:
    low, high = st.columns(2)
    step = 1 if years or field.kind == "int" else 0.1
    suffix = " (Jahr)" if years else ""
    return (
        low.number_input(f"{field.label} von{suffix}", value=None, step=step, key=f"{key}_from"),
        high.number_input(f"{field.label} bis{suffix}", value=None, step=step, key=f"{key}_to"),
    )


def _render_field_filter(model_cls: type[SQLModel], field: _FieldUI, key: str) -> tuple[list[SqlFilter], Any]:
    column = getattr(model_cls, field.name, None)
    if column is None or not hasattr(column, "type"):
        return [], None
    if field.kind == "fk_select" and field.fk_model is not None:
        value = st.text_input(f"{field.label} enthält", key=key, type="search", live="500ms").strip()
        if not value:
            return [], None
        target = field.fk_model
        related = select(sa_inspect(target).primary_key[0]).where(_contains(getattr(target, field.fk_display), value))
        return [col(column).in_(related)], value
    if field.kind == "bool":
        value = st.selectbox(
            field.label, [True, False], index=None, format_func=lambda v: "Ja" if v else "Nein", key=key
        )
        return ([] if value is None else [col(column) == value]), value
    if field.kind in {"domain_date", "datetime"}:
        start, end = _render_range(field, key, years=True)
        return _year_range(column, start, end), [start, end] if start is not None or end is not None else None
    if field.kind in {"int", "float"}:
        low, high = _render_range(field, key, years=False)
        return _number_range(column, low, high), [low, high] if low is not None or high is not None else None
    if field.kind == "uuid":
        return [], None
    if field.options and not isinstance(column.type, JSON):
        labels = field.option_labels or {}
        values = st.multiselect(field.label, field.options, format_func=lambda v: labels.get(v, v), key=key)
        return ([col(column).in_(values)] if values else []), values or None
    value = st.text_input(f"{field.label} enthält", key=key, type="search", live="500ms").strip()
    if not value:
        return [], None
    return [_json_contains(column, value) if isinstance(column.type, JSON) else _contains(column, value)], value


def render_filter_section(
    model_cls: type[SQLModel], fields: list[_FieldUI], *, key: str, label: str = "Filter"
) -> tuple[list[SqlFilter], dict[str, Any]]:
    """Render a collapsed expander with one filter per field.

    Returns the SQL WHERE clauses (AND-combined by the caller) and the active filter values,
    which callers can hash to reset pagination when filters change.
    """
    prefix = f"filter_{key}"
    clauses: list[SqlFilter] = []
    values: dict[str, Any] = {}
    with st.expander(label, expanded=False, icon=":material/filter_list:", key=f"{prefix}__section"):
        grid = st.columns(3)
        for index, field in enumerate(fields):
            with grid[index % 3]:
                field_clauses, value = _render_field_filter(model_cls, field, f"{prefix}_{field.name}")
            clauses.extend(field_clauses)
            if value is not None:
                values[field.label] = value
        if st.button("Filter zurücksetzen", key=f"{prefix}__reset", disabled=not values, icon=":material/clear:"):
            for name in [name for name in st.session_state if isinstance(name, str) and name.startswith(f"{prefix}_")]:
                del st.session_state[name]
            st.rerun()
    if values:
        st.caption(f"{len(values)} Filter aktiv: {', '.join(values)}")
    return clauses, values
