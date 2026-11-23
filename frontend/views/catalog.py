import datetime as dt
from math import ceil
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st
from sqlmodel import col

from backend.config import TEXT_GENRES
from backend.entity_management import (
    CatalogModel,
    catalog_page,
    catalog_suggestions,
    catalog_summary,
    get_unique_field_values,
    verified_organization_filter,
)
from backend.models import Organization, Person, Production, Text
from backend.semantic import search_descriptions
from backend.taxonomy import FACETS, LAYER_LABELS, facet_options
from backend.user_management import get_session
from frontend.management.filters import SqlFilter, render_filter_section
from frontend.management.uifactory import _FieldUI
from frontend.views.helpers import entity_anchor, render_tags, theatre_city, theatre_label
from frontend.views.organization import domain_date_label
from frontend.views.settings import verified_only

PAGE_SIZE = 12

# Sort field -> (label, default descending)
PRODUCTION_SORTS: dict[Literal["name", "premiere"], tuple[str, bool]] = {
    "premiere": ("Premiere", True),
    "name": ("Name", False),
}

CATALOG_ROUTES = {
    "organization": "theater",
    "production": "produktionen",
    "person": "personen",
    "text": "texte",
}
CATALOGS: dict[str, tuple[CatalogModel, str, str, str, list[_FieldUI]]] = {
    "organization": (
        Organization,
        "Theater",
        "Bühnen und Ensembles nach Ort und Profil entdecken.",
        "Theater nach Land",
        [
            _FieldUI("city", "Ort", "str", True),
            _FieldUI("country", "Land", "str", True),
            _FieldUI("founded", "Gründung", "domain_date", True),
        ],
    ),
    "production": (
        Production,
        "Produktionen",
        "Premieren und Inszenierungen im Theaterarchiv finden.",
        "Premieren pro Jahr",
        [
            _FieldUI("organization_id", "Theater", "fk_select", True, fk_model=Organization),
            _FieldUI("premiere", "Premiere", "domain_date", True),
        ],
    ),
    "person": (
        Person,
        "Personen",
        "Menschen auf und hinter der Bühne entdecken.",
        "Personen nach Nationalität",
        [
            _FieldUI("type", "Tätigkeit", "str", True),
            _FieldUI("born", "Geburt", "domain_date", True),
        ],
    ),
    "text": (
        Text,
        "Texte",
        "Stücke und andere Vorlagen für Inszenierungen finden.",
        "Texte nach Gattung",
        [
            _FieldUI("genre", "Gattung", "str", True, options=list(TEXT_GENRES), option_labels=TEXT_GENRES),
            _FieldUI("languages", "Sprachcode (z. B. de)", "str", True),
            _FieldUI("published", "Veröffentlichung", "domain_date", True),
        ],
    ),
}


@st.cache_data(ttl=60, max_entries=128, show_spinner=False)
def _summary(kind: str, query: str, filter_key: str, _filters: list[SqlFilter]) -> list[tuple[str, int]]:
    return catalog_summary(CATALOGS[kind][0], query, _filters)


@st.cache_data(ttl=60, max_entries=2, show_spinner=False)
def _tag_options(kind: str) -> list[str]:
    model = Organization if kind == "organization" else Production
    return get_unique_field_values(model, "tags")


@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def _facet_fields() -> list[_FieldUI]:
    with get_session() as session:
        options = facet_options(session)
    return [
        _FieldUI(
            f"tag_{facet}",
            f"{LAYER_LABELS[FACETS[facet][0]]}: {FACETS[facet][1]}",
            "facet",
            True,
            options=[code for code, _ in values],
            option_labels=dict(values),
        )
        for facet, values in options.items()
    ]


def _extra_filter_fields(kind: str) -> list[_FieldUI]:
    if kind not in {"organization", "production"}:
        return []
    tags = _FieldUI("tags", "Schlagworte", "tags", True, options=_tag_options(kind))
    return [tags, *_facet_fields()] if kind == "production" else [tags]


def _render_entry(kind: str, entry: dict) -> None:
    title = entry["title"] if kind == "text" else entry["name"]
    st.markdown(entity_anchor(title, kind, entry["id"], path="detail"), unsafe_allow_html=True)
    meta: list[str | None]
    if kind == "production":
        org = entry.get("organization") or {}
        meta = [
            domain_date_label(entry, "premiere") if entry.get("premiere") else "Premiere nicht hinterlegt",
            theatre_label(org.get("name") or "", org.get("city")),
        ]
    else:
        count = entry["production_count"]
        meta = [f"{count} {'Produktion' if count == 1 else 'Produktionen'}"]
        if kind == "organization":
            meta.extend((theatre_city(title, entry.get("city")), entry.get("country")))
        elif kind == "person":
            meta.extend((entry.get("nationality"), ", ".join(entry.get("type") or [])))
        else:
            genre = entry.get("genre") or ""
            meta.extend((TEXT_GENRES.get(genre, genre), ", ".join(entry.get("languages") or [])))
    st.text(" · ".join(filter(None, meta)), width="stretch")
    if kind in {"organization", "production"}:
        render_tags(entry.get("tags") or [])


def _render_semantic(kind: Literal["production", "text"], query: str, filters: list) -> None:
    try:
        with get_session() as session, st.spinner("Beschreibungen werden durchsucht …"):
            hits = search_descriptions(session, query, kind=kind, filters=filters)
    except RuntimeError as error:
        st.info(f"Die Beschreibungssuche ist auf diesem Server nicht eingerichtet. {error}")
        return
    if not hits:
        st.info("Keine passenden Beschreibungen gefunden. Bisher sind nur Beschreibungen mit Index durchsuchbar.")
        return
    st.caption(f"{len(hits)} Treffer nach Relevanz")
    with st.container(gap="small"):
        for hit in hits:
            st.markdown(entity_anchor(hit["title"], kind, hit["id"], path="detail"), unsafe_allow_html=True)
            st.caption(hit["snippet"])


def _render_summary(kind: str, chart_title: str, data: list[tuple[str, int]]) -> None:
    if not data:
        return
    with st.container(border=True):
        st.subheader(chart_title, anchor=False)
        st.caption("Alle Treffer mit erfassten Angaben" if kind == "production" else "Häufigste erfasste Angaben")
        if kind == "text":
            data = [(TEXT_GENRES.get(label, label), count) for label, count in data]
        frame = pd.DataFrame(data, columns=["Gruppe", "Anzahl"])
        group = (
            alt.X("Gruppe:O", title=None, sort=None)
            if kind == "production"
            else alt.Y(
                "Gruppe:N",
                title=None,
                sort="-x",
            )
        )
        count = alt.Y("Anzahl:Q") if kind == "production" else alt.X("Anzahl:Q")
        chart = alt.Chart(frame).mark_bar().encode(group, count, tooltip=["Gruppe:N", "Anzahl:Q"])
        st.altair_chart(chart, height=235, alt=chart_title)


def _use_suggestion() -> None:
    if suggestion := st.session_state["catalog_suggestion"]:
        st.session_state["q"] = suggestion


def _render_suggestions(suggestions: list[str]) -> None:
    if suggestions:
        st.pills(
            "Suchvorschläge",
            suggestions,
            key="catalog_suggestion",
            on_change=_use_suggestion,
            wrap=True,
            default=None,
        )


def _upcoming() -> tuple[list[dict], int]:
    today = dt.datetime.now(ZoneInfo("Europe/Berlin")).date()
    return catalog_page(Production, "", [], limit=6, upcoming_from=dt.datetime.combine(today, dt.time()))


def _render_upcoming(upcoming: list[dict], count: int) -> None:
    st.subheader("Kommende Premieren")
    st.caption(
        f"Die nächsten {len(upcoming)} von {count} Premieren mit konkretem Datum"
        if count > len(upcoming)
        else "Premieren mit konkretem Datum, in zeitlicher Reihenfolge"
    )
    for entry in upcoming:
        _render_entry("production", entry)
    st.divider()


def _reset_direction() -> None:
    st.session_state["sort_desc"] = PRODUCTION_SORTS[st.session_state["sort"]][1]
    st.query_params.pop("order", None)


def _flip_direction() -> None:
    descending = not st.session_state["sort_desc"]
    st.session_state["sort_desc"] = descending
    # Buttons cannot bind to the URL, so mirror the direction manually; omit it when it is the default.
    if descending == PRODUCTION_SORTS[st.session_state["sort"]][1]:
        st.query_params.pop("order", None)
    else:
        st.query_params["order"] = "desc" if descending else "asc"


def _render_sort() -> tuple[Literal["name", "premiere"], bool]:
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        sort = st.selectbox(
            "Sortieren nach",
            list(PRODUCTION_SORTS),
            key="sort",
            bind="query-params",
            format_func=lambda value: PRODUCTION_SORTS[value][0],
            on_change=_reset_direction,
            width=200,
        )
        if "sort_desc" not in st.session_state:
            order = st.query_params.get("order")
            st.session_state["sort_desc"] = (
                PRODUCTION_SORTS[sort][1] if order not in {"asc", "desc"} else order == "desc"
            )
        descending = st.session_state["sort_desc"]
        st.button(
            "",
            key="sort_direction",
            icon=":material/arrow_downward:" if descending else ":material/arrow_upward:",
            help="Absteigend sortiert" if descending else "Aufsteigend sortiert",
            on_click=_flip_direction,
        )
    return sort, descending


def render_catalog(kind: str) -> None:
    """Render a public catalog with URL-bound search and filters."""
    model, title, intro, chart_title, fields = CATALOGS[kind]
    st.caption("THEATERDB / ENTDECKEN")
    st.title(title)
    st.caption(intro)
    search_label = {
        "organization": "Name, Ort oder Land",
        "production": "Titel, Theater oder Ort",
        "person": "Name",
        "text": "Titel oder Titelvariante",
    }[kind]
    query = st.text_input(
        "Suchen",
        placeholder=search_label,
        type="search",
        live="500ms",
        key="q",
        bind="query-params",
        max_chars=150,
    ).strip()
    semantic = kind in {"production", "text"} and st.toggle(
        "In Beschreibungen suchen",
        key="semantic",
        bind="query-params",
        help="Findet Inhalte nach Bedeutung, z. B. „Familienkonflikt im Exil“, nicht nur nach Titelwörtern.",
    )
    suggestion_slot = st.container()
    upcoming_slot = st.container()
    results, insights = st.columns([3, 2], gap="large")
    with results:
        heading = st.empty()
        sort_by, descending = (
            _render_sort() if kind == "production" and not (semantic and query) else ("premiere", True)
        )
        fields = [*fields, *_extra_filter_fields(kind)]
        filters, values = render_filter_section(model, fields, key=kind, shareable=True)
    only_verified = kind == "organization" and verified_only()
    if only_verified:
        filters.append(verified_organization_filter())
    if semantic and query:
        with results:
            heading.subheader("Treffer in Beschreibungen", anchor=False)
            _render_semantic("text" if kind == "text" else "production", query, filters)
        return
    with suggestion_slot:
        _render_suggestions(catalog_suggestions(model, query, filters))

    unfiltered = not (query or values.keys() - {"Geplante Premieren ausblenden"})
    page_filters = list(filters)
    upcoming, upcoming_count = _upcoming() if kind == "production" and unfiltered else ([], 0)
    if upcoming:
        # Premieres already previewed above must not repeat in the main list.
        page_filters.append(col(Production.id).not_in([UUID(str(entry["id"])) for entry in upcoming]))

    filter_key = repr((values, only_verified, dt.datetime.now(ZoneInfo("Europe/Berlin")).date()))
    page_key = f"catalog_{kind}_page_{hash((query, filter_key, sort_by, descending))}"
    page = st.session_state.get(page_key, 1)
    entries, total = catalog_page(
        model,
        query,
        page_filters,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        sort_by=sort_by,
        descending=descending,
    )
    pages = max(1, ceil(total / PAGE_SIZE))
    if page > pages:
        page = pages
        entries, _ = catalog_page(
            model,
            query,
            page_filters,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            sort_by=sort_by,
            descending=descending,
        )

    if upcoming and page == 1:
        with upcoming_slot:
            _render_upcoming(upcoming, upcoming_count)

    with results:
        heading.subheader(f"Alle {title}" if unfiltered else "Treffer", anchor=False)
        if not entries:
            st.info(
                "Keine Treffer. Passe die Suche oder die Filter an."
                + (" In den Einstellungen kannst du auch ungeprüfte Theater einblenden." if only_verified else "")
            )
            return
        sort_note = "" if kind == "production" else " · nach Anzahl der Produktionen"
        st.caption(f"{total:,} Treffer{sort_note}".replace(",", "."))
        with st.container(gap="small"):
            for entry in entries:
                _render_entry(kind, entry)
        if pages > 1:
            st.pagination(pages, key=page_key, default=page)
    if summary := _summary(kind, query, filter_key, filters):
        with insights:
            _render_summary(kind, chart_title, summary)
