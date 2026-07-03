from datetime import datetime

import streamlit as st

from backend.entity_management import get_entity_graph, list_entities
from backend.models import Organization
from frontend.views.helpers import entity_link


def render_organization(data: dict):
    # ── Header ────────────────────────────────────────────────────────────────
    is_active = data.get("closed_year") is None

    col_icon, col_title = st.columns([1, 11])
    with col_icon:
        st.markdown("### :material/theater_comedy:")
    with col_title:
        st.markdown(f"## {data['name']}")
        status = ":material/check_circle: Active" if is_active else f":material/cancel: Closed {data['closed_year']}"
        st.markdown(
            f":material/pin_drop: {data.get('address').get('City', 'Unknown')}" if data.get("address") else "",
        )
        st.markdown(
            f"&nbsp;·&nbsp;:material/calendar_today: Founded {data['founded_year']}"
            if data.get("founded_year")
            else "",
            unsafe_allow_html=True,
        )
        st.markdown(f":material/language: [Website]({data['url']})" if data.get("url") else "", unsafe_allow_html=True)

    st.divider()

    # ── Metric cards ──────────────────────────────────────────────────────────
    productions = data.get("productions", [])

    metrics = [
        (":material/movie: Productions", len(productions)),
    ]
    if data.get("founded_year"):
        metrics.append((":material/calendar_today: Founded", data["founded_year"]))
    if data.get("address"):
        metrics.append((":material/location_on: Address", data["address"]))
    if data.get("closed_year"):
        metrics.append((":material/event_busy: Closed", data["closed_year"]))

    for col, (label, value) in zip(st.columns(len(metrics)), metrics, strict=True):
        col.metric(label, value)

    st.divider()

    # ── Details & Productions side-by-side ────────────────────────────────────

    st.markdown("**Details**")

    url = data.get("url")
    st.markdown(
        f":material/language: **Website** &nbsp; [{url}]({url})" if url else ":material/language: **Website** &nbsp; —",
        unsafe_allow_html=True,
    )

    plays_url = data.get("url_plays")
    st.markdown(
        f":material/smart_display: **Plays URL** &nbsp; `{plays_url}`"
        if plays_url
        else ":material/smart_display: **Plays URL** &nbsp; —",
        unsafe_allow_html=True,
    )

    lat = data.get("lat")
    lon = data.get("lon")
    coords = f"`{lat}, {lon}`" if (lat is not None and lon is not None) else "—"
    st.markdown(f":material/my_location: **Coordinates** &nbsp; {coords}", unsafe_allow_html=True)

    sources = data.get("sources", [])
    if sources:
        st.markdown(":material/attach_file: **Sources**")
        for s in sources:
            st.markdown(f"  - {s}")
    else:
        st.markdown(":material/attach_file: **Sources** &nbsp; None", unsafe_allow_html=True)

    st.markdown("**Produktionen**")
    if not productions:
        st.caption("(Noch) keine Produktion gefunden.")  # TODO: Hinzufügen!
    else:
        for prod in productions:
            with st.container(border=True):
                entity_link(prod["name"], "production", prod["id"])

                if prod.get("description"):
                    st.caption(
                        prod["description"][:400] + "..." if len(prod["description"]) > 200 else prod["description"]
                    )

                p_col1, p_col2 = st.columns(2)

                premiere_raw = prod.get("premiere")
                if premiere_raw:
                    try:
                        premiere_dt = datetime.fromisoformat(premiere_raw)
                        premiere_str = premiere_dt.strftime("%-d %b %Y")
                    except ValueError:
                        premiere_str = premiere_raw
                else:
                    premiere_str = "—"
                p_col1.markdown(
                    f":material/event: **Premiere** &nbsp; {premiere_str}",
                    unsafe_allow_html=True,
                )

                duration = prod.get("duration")
                p_col2.markdown(
                    f":material/timer: **Duration** &nbsp; {duration} min"
                    if duration
                    else ":material/timer: **Duration** &nbsp; —",
                    unsafe_allow_html=True,
                )

                age_rating = prod.get("age_rating")
                p_col1.markdown(
                    f":material/person: **Age rating** &nbsp; {age_rating}"
                    if age_rating
                    else ":material/person: **Age rating** &nbsp; —",
                    unsafe_allow_html=True,
                )

                tags = prod.get("tags", [])
                if tags:
                    p_col2.markdown(
                        f":material/label: **Tags** &nbsp; {', '.join(tags)}"
                        if tags
                        else ":material/label: **Tags** &nbsp; None",
                        unsafe_allow_html=True,
                    )
                critics = prod.get("critics", [])
                if critics:
                    with st.expander(":material/rate_review: Kritiken", expanded=False):
                        for c in critics:
                            st.markdown(f"- {c}")

    st.divider()

    # ── UUID ──────────────────────────────────────────────────────────────────
    st.markdown(":material/fingerprint: **ID**")
    st.code(data["id"], language=None)
