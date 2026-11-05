# components/production_detail.py
from datetime import date

import streamlit as st

from frontend.views.helpers import entity_link


def _render_header(data: dict) -> None:
    st.title(data["name"])

    col1, col2, col3 = st.columns(3)
    if data.get("duration"):
        col1.metric("Dauer", f"{data['duration']} min")
    if data.get("age_rating"):
        col2.metric("Altersempfehlung", f"{data['age_rating']}+")
    if data.get("premiere"):
        premiere = date.fromisoformat(data["premiere"][:10])
        col3.metric("Premiere", premiere.strftime("%-d. %B %Y"))

    if data.get("url"):
        st.link_button("🔗 Offizielle Seite", data["url"])


def _render_memberships(memberships: list[dict]) -> None:
    if not memberships:
        return
    st.subheader("Besetzung")
    for membership in memberships:
        person = membership.get("person") or {}
        role = membership.get("role") or ""
        character = membership.get("character") or ""
        caption_parts = [value for value in (role, character) if value]
        caption = " · ".join(caption_parts) if caption_parts else "—"

        _col_name, col_role = st.columns([2, 3])
        entity_link(person.get("name", "Unknown"), "person", person.get("id", ""))
        col_role.caption(caption)


def _render_organization(org: dict | None) -> None:
    if not org:
        return
    st.divider()
    st.subheader("Theater")
    entity_link(org["name"], "organization", org["id"])
    if org.get("url"):
        st.link_button("🔗 Theater-Website", org["url"])


def render_production(data: dict) -> None:
    """Render production details, cast, organization, and tags."""
    _render_header(data)
    st.divider()

    if data.get("description"):
        with st.expander("Über diese Production", expanded=True):
            st.markdown(data["description"])

    _render_memberships(data.get("memberships") or [])
    _render_organization(data.get("organization"))
    tags = data.get("tags") or []
    if tags:
        st.divider()
        st.markdown(" ".join(f"`{t}`" for t in tags))
