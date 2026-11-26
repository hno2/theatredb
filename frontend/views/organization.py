import ipaddress
import logging
import re
import socket
from datetime import date, datetime, time, timedelta
from html import escape
from textwrap import shorten
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.response import addinfourl
from uuid import UUID

import pandas as pd
import streamlit as st

from backend.config import PERFORMER_ROLES, canonical_role
from backend.data_quality import public_summary
from backend.date_precision import DatePrecision, date_interval, format_date, intervals_overlap
from backend.date_precision import parse_date as parse_domain_date
from backend.entity_management import entities_by_ids, get_organization_profile
from backend.models import Source
from frontend.views.helpers import entity_link, has_coordinates, render_tags, role_label, theatre_city

LOG = logging.getLogger(__name__)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> Request | None:
        return None


@st.cache_data(ttl=30, max_entries=64)
def load_organization_profile(entity_id: str, *, verified_only: bool = False) -> dict:
    """Reuse detached public profile data across reruns, for at most 30 seconds."""
    return get_organization_profile(entity_id, verified_only=verified_only)


def parse_date(value: str | None) -> date | None:
    """Read an ISO date or timestamp without shifting its calendar date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def domain_date_interval(record: dict, field: str) -> tuple[datetime, datetime | None] | None:
    """Resolve a domain date without treating its start as an exact date."""
    value = record.get(field)
    if not value:
        return None
    try:
        parsed, inferred = parse_domain_date(value)
        return date_interval(parsed, DatePrecision(record.get(f"{field}_precision") or inferred))
    except ValueError:
        return None


def domain_date_label(record: dict, field: str) -> str:
    """Display only the precision recorded for a date."""
    value = record.get(field)
    if not value:
        return "Nicht hinterlegt"
    try:
        parsed, inferred = parse_domain_date(value)
        if f"{field}_precision" not in record and inferred == DatePrecision.MINUTE:
            inferred = DatePrecision.DAY
        return format_date(parsed, DatePrecision(record.get(f"{field}_precision") or inferred))
    except ValueError:
        return "Ungültiges Datum"


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


def favicon_url(value: str | None) -> str | None:
    """Build a favicon URL from a valid source URL without carrying its path or credentials."""
    if not (url := web_url(value)):
        return None
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}/favicon.ico"


@st.cache_data(ttl=3600, max_entries=256)
def load_favicon(url: str) -> bytes | None:
    """Cache failures temporarily while keeping successful downloads on disk."""
    try:
        return _download_favicon(url)
    except OSError as error:
        LOG.warning("%s", error)
        return None


@st.cache_data(persist="disk", max_entries=256)
def _download_favicon(url: str) -> bytes | None:
    """Fetch a public favicon and persist successful downloads in Streamlit's disk cache."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path != "/favicon.ico":
        return None
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        for result in socket.getaddrinfo(parsed.hostname, port):
            address = result[4][0]
            if not isinstance(address, str):
                raise TypeError(f"Unexpected socket address for {parsed.hostname}")
            addresses.add(ipaddress.ip_address(address.split("%", maxsplit=1)[0]))
    except (OSError, TypeError, ValueError) as error:
        raise OSError(f"Cannot resolve favicon host {parsed.hostname}") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise OSError(f"Skipping favicon from non-public host {parsed.hostname}")
    opener = build_opener(_NoRedirectHandler(), ProxyHandler({}))
    try:
        # ruff: ignore[suspicious-url-open-usage] - scheme validated above
        request = Request(url, headers={"User-Agent": "TheatreDB/0.1"})
        with opener.open(request, timeout=5) as response:
            return _favicon_image(response, parsed.hostname)
    except OSError as error:
        raise OSError(f"Cannot load favicon from {parsed.hostname}: {error}") from error


def _favicon_image(response: addinfourl, host: str) -> bytes:
    if not response.headers.get_content_type().startswith("image/"):
        raise OSError(f"Ignoring non-image favicon response from {host}")
    image = response.read(64 * 1024 + 1)
    if len(image) > 64 * 1024:
        raise OSError(f"Ignoring oversized favicon response from {host}")
    return image


def production_dates(productions: list[dict]) -> tuple[list[tuple[date, dict]], list[dict]]:
    """Separate dated productions from entries without a usable premiere date."""
    dated = []
    undated = []
    for production in productions:
        premiere = parse_date(production.get("premiere"))
        if premiere is None and production.get("premiere_year"):
            premiere = date(production["premiere_year"], 1, 1)
        if premiere is None:
            undated.append(production)
        else:
            dated.append((premiere, production))
    return dated, undated


def premiere_label(production: dict) -> str:
    """Display actual precision, never a made-up day for a year-only premiere."""
    if production.get("premiere"):
        label = domain_date_label(production, "premiere")
        return "Nicht hinterlegt" if label == "Ungültiges Datum" else label
    return str(production["premiere_year"]) if production.get("premiere_year") else "Nicht hinterlegt"


def is_theapolis_url(url: str) -> bool:
    """Identify archive links that should not be labeled as official theatre sites."""
    host = urlsplit(url).hostname or ""
    return host == "theapolis.de" or host.endswith(".theapolis.de")


def render_web_link(label: str, value: str | None, *, official: bool = False) -> None:
    """Render a source-provided URL or explicitly flag an unusable one."""
    if url := web_url(value):
        if official and is_theapolis_url(url):
            st.link_button("Theapolis-Quelle", url, icon=":material/open_in_new:")
            return
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
        if data.get("region") and data["region"] != data.get("city"):
            st.text(data["region"])
        if data.get("country"):
            st.text(data["country"])
        if not any(data.get(field) for field in ("street", "zip_code", "city", "region", "country")):
            st.caption("Noch keine Adresse hinterlegt.")
        render_web_link("Website", data.get("url"), official=True)
        render_web_link("Spielplan", data.get("url_plays"), official=True)
    with map_column:
        if has_coordinates(data.get("lat"), data.get("lon")):
            st.map(pd.DataFrame([{"lat": data["lat"], "lon": data["lon"]}]), zoom=12, height=300, color="#8b3a32")
        else:
            st.info("Für dieses Theater sind noch keine gültigen Standortkoordinaten hinterlegt.")
    if data.get("description"):
        st.write(data["description"])


def render_production_card(production: dict, *, compact: bool = False, show_organization: bool = True) -> None:
    """Keep production links and details available within the timeline."""
    with st.container(border=True):
        st.text(production["name"])
        entity_link("Produktion ansehen", "production", str(production["id"]))
        organization = production.get("organization") or {}
        if organization and show_organization:
            entity_link(organization["name"], "organization", organization["id"])
            if city := theatre_city(organization["name"], organization.get("city")):
                st.caption(city)
        render_production_summary(production)
        if production.get("description"):
            if compact:
                with st.expander("Beschreibung"):
                    st.write(production["description"])
            else:
                st.write(production["description"])
        if production.get("critics"):
            with st.expander("Kritiken"):
                for critic in production["critics"]:
                    st.write(critic)


def render_production_summary(production: dict) -> None:
    """Make recorded dates, guidance and credits visible before opening a profile."""
    premiere = parse_date(production.get("premiere"))
    if premiere or production.get("premiere_year"):
        st.caption(f"Premiere: {premiere_label(production)}")
    elif production.get("premiere"):
        st.warning(f"Ungültiges Premierendatum: {production['premiere']}")
    else:
        st.caption("Premiere nicht hinterlegt")
    details = []
    if production.get("duration") is not None:
        details.append(f"{production['duration']} Minuten")
    if production.get("age_rating") is not None:
        details.append(f"Ab {production['age_rating']} Jahren")
    if details:
        st.caption(" · ".join(details))
    render_tags(production.get("tags") or [])
    text = production.get("text") or {}
    if text.get("title"):
        st.caption(f"Textgrundlage: {text['title']}")
    credits = dict.fromkeys(
        (role_label(membership.get("role")), membership["person"]["name"])
        for membership in production.get("memberships") or []
        if (membership.get("person") or {}).get("name") and membership.get("role")
    )
    if credits:
        st.caption(" · ".join(f"{role}: {name}" for role, name in credits))


def production_credit_line(production: dict) -> str:
    """Group distinct directors first, followed by performers, preserving source order."""
    groups: dict[str, dict[str, str]] = {"director": {}, "actor": {}}
    for membership in production.get("memberships") or []:
        role = canonical_role(membership.get("role"))
        if role in PERFORMER_ROLES:
            role = "actor"
        person = membership.get("person") or {}
        if role in groups and person.get("name"):
            name = re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", person["name"])
            groups[role].setdefault(str(person.get("id") or person["name"]), name)
    parts = []
    if groups["director"]:
        parts.append("Regie: " + ", ".join(f"**{name}**" for name in groups["director"].values()))
    if groups["actor"]:
        parts.append("mit " + ", ".join(groups["actor"].values()))
    return " · ".join(parts)


def render_theatre_production(production: dict) -> None:
    """Show an unboxed production preview; keep complete details on its linked profile."""
    entity_link(production["name"], "production", str(production["id"]))
    if credits := production_credit_line(production):
        st.markdown(credits)
    premiere = parse_date(production.get("premiere"))
    facts = [
        f":material/event: Premiere {premiere_label(production)}"
        if premiere or production.get("premiere_year")
        else ":material/event: Premiere unbekannt"
    ]
    if production.get("age_rating") is not None:
        facts.append(f":material/family_restroom: Ab {production['age_rating']} Jahren")
    if production.get("duration") is not None:
        facts.append(f":material/schedule: {production['duration']} Minuten")
    st.caption(" · ".join(facts))
    render_tags(production.get("tags") or [])
    if production.get("premiere") and premiere is None:
        st.warning(f"Ungültiges Premierendatum: {production['premiere']}")
    if production.get("description"):
        preview = escape(shorten(production["description"], width=200, placeholder="…"))
        st.html(
            '<p style="display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; '
            f'overflow: hidden; margin: 0;">{preview}</p>'
        )


def age_label(production: dict) -> str:
    """Label missing age guidance distinctly from an age-zero recommendation."""
    age = production.get("age_rating")
    return f"Ab {age} Jahren" if age is not None else "Nicht hinterlegt"


def filter_productions(
    productions: list[dict],
    search: str,
    person: str,
    ages: list[str],
    premiere_range: tuple[date, ...],
    tags: list[str] | None = None,
) -> list[dict]:
    """Combine literal title, credited-person, age and inclusive premiere filters."""
    matching = []
    for production in productions:
        if search.casefold() not in production["name"].casefold():
            continue
        if (ages and age_label(production) not in ages) or (
            tags and not set(tags).intersection(production.get("tags") or [])
        ):
            continue
        contributors = [
            *(production.get("memberships") or []),
            *((production.get("text") or {}).get("contributions") or []),
        ]
        if person and not any(
            person.casefold() in (contribution.get("person") or {}).get("name", "").casefold()
            for contribution in contributors
        ):
            continue
        premiere = domain_date_interval(production, "premiere")
        if len(premiere_range) == 2:
            if premiere is not None:
                start = datetime.combine(premiere_range[0], time.min)
                end = (
                    datetime.combine(premiere_range[1] + timedelta(days=1), time.min)
                    if premiere_range[1] < date.max
                    else None
                )
                if not intervals_overlap(premiere, (start, end)):
                    continue
            elif year := production.get("premiere_year"):
                if not premiere_range[0].year <= year <= premiere_range[1].year:
                    continue
            else:
                continue
        matching.append(production)
    return matching


@st.fragment
def render_production_list(productions: list[dict], organization_id: str) -> None:
    """Filter while typing without rerendering the theatre's map and other tabs."""
    st.subheader("Produktionen")
    st.caption(
        "Erfasste Produktionen, kein vollständiger Spielplan. Premieren sind keine aktuellen Aufführungstermine."
    )
    if not productions:
        st.info("Für dieses Theater sind noch keine Produktionen erfasst.")
        return
    search = st.text_input(
        "Produktionen durchsuchen",
        placeholder="Nach Titel suchen",
        key=f"production_search_{organization_id}",
        type="search",
        live="500ms",
    ).strip()
    with st.expander("Filter", expanded=False):
        ratings = sorted({row["age_rating"] for row in productions if row.get("age_rating") is not None})
        age_options = [f"Ab {age} Jahren" for age in ratings]
        if any(row.get("age_rating") is None for row in productions):
            age_options.append("Nicht hinterlegt")
        ages = st.multiselect("Altersempfehlung", age_options, key=f"production_ages_{organization_id}")
        tags = st.multiselect(
            "Schlagworte",
            sorted({tag for row in productions for tag in row.get("tags") or []}),
            key=f"production_tags_{organization_id}",
            help="Ein Treffer muss mindestens eines der gewählten Schlagworte haben.",
        )
        dated, _ = production_dates(productions)
        dates = [premiere for premiere, _ in dated]
        dates.extend(
            (interval[1] - timedelta(microseconds=1)).date() if interval[1] else date.max
            for row in productions
            if (interval := domain_date_interval(row, "premiere"))
        )
        dates.extend(
            date(row["premiere_year"], 12, 31)
            for row in productions
            if row.get("premiere_year") and parse_date(row.get("premiere")) is None
        )
        premiere_range = st.date_input(
            "Premiere im Zeitraum",
            value=[],
            min_value=min(dates) if dates else None,
            max_value=max(dates) if dates else None,
            format="DD.MM.YYYY",
            key=f"production_dates_{organization_id}",
            disabled=not dates,
            help="Start- und Enddatum einschließen. Ohne Auswahl bleiben auch undatierte Produktionen sichtbar.",
        )
        person = st.text_input(
            "Person suchen",
            placeholder="Name aus Besetzung, Team oder Textbeiträgen",
            key=f"production_person_{organization_id}",
            type="search",
            live="500ms",
        ).strip()
    if len(premiere_range) == 1:
        st.info("Bitte auch das Enddatum wählen. Der Zeitraumfilter ist noch nicht aktiv.")
    order = st.selectbox(
        "Sortierung",
        ["Neueste zuerst", "Älteste zuerst", "Titel A-Z"],
        key=f"production_order_{organization_id}",
    )
    matching = filter_productions(productions, search, person, ages, premiere_range, tags)
    dated, undated = production_dates(matching)
    dated.sort(key=lambda item: (item[0], item[1]["name"].casefold()), reverse=order == "Neueste zuerst")
    ordered = [row for _, row in dated] + sorted(undated, key=lambda row: row["name"].casefold())
    if order == "Titel A-Z":
        ordered.sort(key=lambda row: row["name"].casefold())
    st.caption(f"{len(matching)} von {len(productions)} Produktionen")
    if not matching:
        st.info("Keine Produktionen für diese Suche und Filter gefunden.")
        return
    page_key = f"theatre_page_{organization_id}_{search}_{person}_{ages}_{premiere_range}_{tags}_{order}"
    page = st.pagination((len(ordered) + 11) // 12, key=page_key) if len(ordered) > 12 else 1
    with st.container(gap="medium"):
        for production in ordered[(page - 1) * 12 : page * 12]:
            with st.container(gap="xsmall"):
                render_theatre_production(production)


def _render_compact_source(source: dict, name: str, url: str | None, metadata: list[str]) -> None:
    title = source.get("title") or name
    details = [] if source.get("link_only") else metadata
    if title != name and not source.get("link_only"):
        details = [name, *details]
    citation = escape(title)
    if url:
        citation = f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{citation}</a>'
    elif source.get("external_url"):
        details.append("Kein gültiger HTTP(S)-Link hinterlegt.")
    else:
        details.append("Kein direkter Quellenlink hinterlegt.")
    line = " · ".join([citation, *(escape(item) for item in details)])
    st.markdown(f"<p>{line}</p>", unsafe_allow_html=True)


def _render_source_with_icon(
    source: dict, name: str, icon: str | None, url: str | None, metadata: list[str], date_warning: str | None
) -> None:
    with st.container():
        icon_column, content_column = st.columns([1, 20], vertical_alignment="center")
        with icon_column:
            if icon:
                image = load_favicon(icon)
                if image:
                    st.image(image, width=24, alt=f"Favicon {name}")
        with content_column:
            st.text(name)
            if source.get("title") and source["title"] != name:
                st.caption(source["title"])
            if date_warning:
                st.warning(date_warning)
            st.caption(" · ".join(metadata))
            if url:
                st.link_button("Quelle öffnen", url, type="tertiary")
            else:
                render_web_link("Quelle öffnen", source.get("external_url"))
            if not source.get("external_url"):
                st.caption("Kein direkter Quellenlink hinterlegt.")


def _render_source(source: dict, source_record: Source | None, *, compact: bool) -> None:
    url = web_url(source.get("external_url"))
    name = (
        (source_record.name if source_record else None)
        or (urlsplit(url).hostname if url else None)
        or "Unbekannte Quelle"
    )
    metadata = ["Geprüft" if source.get("reviewed") else "Noch nicht geprüft"]
    date_warning = None
    if source.get("sourced_at"):
        sourced_at = parse_date(source["sourced_at"])
        if sourced_at is not None:
            metadata.append(f"Erfasst am {sourced_at:%d.%m.%Y}")
        else:
            date_warning = f"Ungültiges Erfassungsdatum: {source['sourced_at']}"
    if compact:
        _render_compact_source(source, name, url, [*metadata, date_warning] if date_warning else metadata)
    else:
        icon = favicon_url(source_record.base_url if source_record else None) or favicon_url(url)
        _render_source_with_icon(source, name, icon, url, metadata, date_warning)


def render_sources(sources: list[dict], *, compact: bool = False) -> None:
    """Show provenance as readable citations instead of serialized source records."""
    st.subheader("Quellen & Nachweise")
    if not compact:
        st.caption(
            "Externe Belege zu diesem Eintrag. „Geprüft“ bezeichnet die Prüfung des verknüpften Quelldatensatzes."
        )
    if not sources:
        st.info("Für diesen Eintrag sind noch keine Quellen hinterlegt.")
        return
    source_records = {
        str(source.id): source
        for source in entities_by_ids(
            Source, {UUID(str(item["source_id"])) for item in sources if item.get("source_id")}
        )
    }
    with st.container(gap="xsmall" if compact else "small"):
        for source in sources:
            _render_source(source, source_records.get(str(source.get("source_id"))), compact=compact)


def render_organization(data: dict) -> None:
    """Render a German theatre profile with location, premiere timeline and citations."""
    st.caption("THEATERDB / THEATERPORTRÄT")
    st.title(data["name"])
    context = list(filter(None, (theatre_city(data["name"], data.get("city")), data.get("country"), data.get("type"))))
    if context:
        st.text(" · ".join(context))
    render_tags(data.get("tags") or [])
    history = []
    if data.get("founded"):
        history.append(f"Gegründet {domain_date_label(data, 'founded')}")
    elif data.get("founded_year") is not None:
        history.append(f"Gegründet {data['founded_year']}")
    if data.get("closed"):
        history.append(f"Geschlossen {domain_date_label(data, 'closed')}")
    elif data.get("closed_year") is not None:
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
    production_tab, house_tab, source_tab = st.tabs(["Produktionen", "Spielstätten & Menschen", "Quellen & Nachweise"])
    with production_tab:
        render_production_list(productions, str(data["id"]))
    with source_tab:
        render_sources(sources)
    with house_tab:
        render_house(data)
    with st.expander("Technische Angaben"):
        st.caption("Interne Kennung dieses Eintrags")
        st.code(str(data["id"]), language=None)


def render_quality_summary(data: dict) -> None:
    """Show editorial coverage separately from programme freshness and source review."""
    summary = public_summary(data, data.get("season_checks") or [])
    with st.container(border=True):
        st.subheader("Datenqualität")
        st.text(summary["general_label"])
        st.text(summary["history_label"])
        st.text(summary["season_label"])
        if summary["update_reason"]:
            st.warning(summary["update_reason"])
        if summary["evidence"]:
            with st.expander("Belege zur Prüfung"):
                for evidence in dict.fromkeys(summary["evidence"]):
                    if url := web_url(evidence):
                        st.link_button("Prüfquelle öffnen", url)
                    else:
                        st.text(evidence)


def render_house(data: dict) -> None:
    """Show recorded venues and memberships without claiming current employment."""
    st.subheader("Spielstätten")
    venues = data.get("venues") or []
    if not venues:
        st.info("Noch keine Spielstätten hinterlegt.")
    for venue in venues:
        for name, seats in venue.items():
            with st.container(border=True):
                st.text(name)
                st.caption(f"Plätze: {seats}" if seats else "Platzzahl nicht hinterlegt")
    st.subheader("Menschen am Theater")
    st.caption("Erfasste Zugehörigkeiten, keine vollständige oder automatisch aktuelle Ensembleliste.")
    memberships = data.get("memberships") or []
    if not memberships:
        st.info("Noch keine Zugehörigkeiten hinterlegt.")
    for membership in memberships:
        with st.container(border=True):
            render_membership(membership)


def render_membership(membership: dict) -> None:
    """Link one recorded theatre membership and retain its documented dates."""
    person = membership.get("person") or {}
    if person.get("id"):
        entity_link(person["name"], "person", person["id"])
    else:
        st.warning("Zugehörigkeit ohne verknüpfte Person.")
    if membership.get("role"):
        st.text(role_label(membership["role"]))
    dates = []
    for field, label in (("start_date", "Beginn"), ("end_date", "Ende")):
        if membership.get(field):
            value = parse_date(membership[field])
            if value:
                dates.append(f"{label}: {domain_date_label(membership, field)}")
            else:
                st.warning(f"Ungültiges Datum: {membership[field]}")
    if dates:
        st.caption(" · ".join(dates))
