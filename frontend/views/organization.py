from datetime import date, datetime
from itertools import groupby
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

from backend.entity_management import list_entities
from backend.models import Source
from frontend.views.helpers import entity_link, has_coordinates


def parse_date(value: str | None) -> date | None:
    """Read an ISO date or timestamp without shifting its calendar date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def web_url(value: str | None) -> str | None:
    """Allow only absolute HTTP(S) links from source data."""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not any(char.isspace() for char in value):
            return value
    except ValueError:
        return None
    return None


def production_dates(productions: list[dict]) -> tuple[list[tuple[date, dict]], list[dict]]:
    """Separate dated productions from entries without a usable premiere date."""
    dated = []
    undated = []
    for production in productions:
        premiere = parse_date(production.get("premiere"))
        if premiere is None:
            undated.append(production)
        else:
            dated.append((premiere, production))
    return dated, undated


def render_web_link(label: str, value: str | None) -> None:
    """Render a source-provided URL or explicitly flag an unusable one."""
    if url := web_url(value):
        st.link_button(label, url, icon=":material/open_in_new:")
    elif value:
        st.warning(f"{label}: Kein gültiger HTTP(S)-Link hinterlegt.")


def render_location(data: dict) -> None:
    """Show an address and map, keeping missing location data explicit."""
    st.subheader("Standort")
    address_column, map_column = st.columns([1, 2], gap="large")
    with address_column:
        if data.get("street"):
            st.text(data["street"])
        locality = " ".join(str(data[field]) for field in ("zip_code", "city") if data.get(field))
        if locality:
            st.text(locality)
        if data.get("country"):
            st.text(data["country"])
        if not any(data.get(field) for field in ("street", "zip_code", "city", "country")):
            st.caption("Noch keine Adresse hinterlegt.")
        render_web_link("Website", data.get("url"))
        render_web_link("Spielplan", data.get("url_plays"))
    with map_column:
        if has_coordinates(data.get("lat"), data.get("lon")):
            st.map(pd.DataFrame([{"lat": data["lat"], "lon": data["lon"]}]), zoom=12, height=300, color="#8b3a32")
        else:
            st.info("Für dieses Theater sind noch keine gültigen Standortkoordinaten hinterlegt.")
    if data.get("description"):
        st.write(data["description"])


def render_production_card(production: dict) -> None:
    """Keep production links and details available within the timeline."""
    with st.container(border=True):
        st.text(production["name"])
        entity_link("Produktion ansehen", "production", str(production["id"]))
        details = []
        if production.get("duration") is not None:
            details.append(f"{production['duration']} Minuten")
        if production.get("age_rating") is not None:
            details.append(f"Ab {production['age_rating']} Jahren")
        if details:
            st.caption(" · ".join(details))
        if production.get("tags"):
            st.text(" · ".join(production["tags"]))
        if production.get("description"):
            st.write(production["description"])
        if production.get("critics"):
            with st.expander("Kritiken"):
                for critic in production["critics"]:
                    st.write(critic)


def render_timeline(productions: list[dict], organization_id: str) -> None:
    """Render premiere years as a native Streamlit timeline, without inventing dates."""
    st.subheader("Produktionen im Zeitverlauf")
    st.caption("Chronologie nach erfasster Premiere, kein vollständiger Spielplan und keine Laufzeiten.")
    if not productions:
        st.info("Für dieses Theater sind noch keine Produktionen erfasst.")
        return
    dated, undated = production_dates(productions)
    if dated:
        order = st.radio(
            "Reihenfolge",
            ["Neueste zuerst", "Älteste zuerst"],
            horizontal=True,
            key=f"production_order_{organization_id}",
        )
        dated.sort(key=lambda item: (item[0], item[1]["name"].casefold()), reverse=order == "Neueste zuerst")
        for year, entries in groupby(dated, key=lambda item: item[0].year):
            st.markdown(f"#### {year}")
            for premiere, production in entries:
                date_column, production_column = st.columns([1, 5], gap="medium")
                with date_column:
                    st.text(premiere.strftime("%d.%m.%Y"))
                with production_column:
                    render_production_card(production)
    if undated:
        st.markdown("#### Ohne Premierendatum")
        st.caption("Ohne gültiges Premierendatum ist keine zeitliche Einordnung möglich.")
        for production in sorted(undated, key=lambda item: item["name"].casefold()):
            if production.get("premiere"):
                st.warning(f"Ungültiges Premierendatum für „{production['name']}“: {production['premiere']}")
            render_production_card(production)


def render_sources(sources: list[dict]) -> None:
    """Show provenance as readable citations instead of serialized source records."""
    st.subheader("Quellen & Nachweise")
    st.caption("Externe Belege zu diesem Eintrag. „Geprüft“ bezeichnet die Prüfung des verknüpften Quelldatensatzes.")
    if not sources:
        st.info("Für dieses Theater sind noch keine Quellen hinterlegt.")
        return
    names = {str(source.id): source.name for source in list_entities(Source)}
    for source in sources:
        url = web_url(source.get("external_url"))
        name = (
            names.get(str(source.get("source_id"))) or (urlsplit(url).hostname if url else None) or "Unbekannte Quelle"
        )
        with st.container(border=True):
            st.text(name)
            if source.get("title") and source["title"] != name:
                st.text(source["title"])
            metadata = ["Geprüft" if source.get("reviewed") else "Noch nicht geprüft"]
            if source.get("sourced_at"):
                sourced_at = parse_date(source["sourced_at"])
                if sourced_at is not None:
                    metadata.append(f"Erfasst am {sourced_at:%d.%m.%Y}")
                else:
                    st.warning(f"Ungültiges Erfassungsdatum: {source['sourced_at']}")
            st.caption(" · ".join(metadata))
            render_web_link("Quelle öffnen", source.get("external_url"))
            if not source.get("external_url"):
                st.caption("Kein direkter Quellenlink hinterlegt.")


def render_organization(data: dict) -> None:
    """Render a German theatre profile with location, premiere timeline and citations."""
    st.caption("THEATERDB / THEATERPORTRÄT")
    st.title(data["name"])
    context = [str(data[field]) for field in ("city", "country", "type") if data.get(field)]
    if context:
        st.text(" · ".join(context))
    history = []
    if data.get("founded_year") is not None:
        history.append(f"Gegründet {data['founded_year']}")
    if data.get("closed_year") is not None:
        history.append(f"Geschlossen {data['closed_year']}")
    if history:
        st.caption(" · ".join(history))
    productions = data.get("productions") or []
    dated, _ = production_dates(productions)
    sources = data.get("sources") or []
    metrics = [
        ("Produktionen", len(productions)),
        ("Mit Premierendatum", len(dated)),
        ("Quellennachweise", len(sources)),
    ]
    for column, (label, value) in zip(st.columns(3), metrics, strict=True):
        column.metric(label, value, border=True)
    st.space("small")
    render_location(data)
    st.divider()
    production_tab, source_tab = st.tabs(["Produktionen", "Quellen & Nachweise"])
    with production_tab:
        search = st.text_input(
            "Produktionen durchsuchen",
            placeholder="Nach Titel suchen",
            key=f"production_search_{data['id']}",
        ).strip()
        matching = [production for production in productions if search.casefold() in production["name"].casefold()]
        if search and not matching:
            st.info("Keine Produktionen mit diesem Titel gefunden.")
        else:
            if search:
                st.caption(f"{len(matching)} von {len(productions)} Produktionen")
            with st.container(height=650 if len(matching) > 8 else "content", border=False):
                render_timeline(matching, str(data["id"]))
    with source_tab:
        render_sources(sources)
    with st.expander("Technische Angaben"):
        st.caption("Interne Kennung dieses Eintrags")
        st.code(str(data["id"]), language=None)
