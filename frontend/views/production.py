# components/production_detail.py
import streamlit as st
from backend.entity_management import get_entity_graph, list_entities
from backend.models import Organization, Person, Production
from backend.models import Production
from datetime import date
from frontend.views.helpers import entity_link


def render_production(data: dict) -> None:
    # ── Header ────────────────────────────────────────────────────────────────
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

    st.divider()

    # ── Description ───────────────────────────────────────────────────────────
    if data.get("description"):
        with st.expander("Über diese Production", expanded=True):
            st.markdown(data["description"])

    # ── Cast ──────────────────────────────────────────────────────────────────
    memberships = data.get("memberships") or []
    if memberships:
        st.subheader("Besetzung")
        for m in memberships:
            person = m.get("person") or {}
            name = person.get("name", "Unknown")
            role = m.get("role") or ""
            character = m.get("character") or ""

            label = name
            caption_parts = [x for x in [role, character] if x]
            caption = " · ".join(caption_parts) if caption_parts else "—"

            col_name, col_role = st.columns([2, 3])
            col_name = entity_link(label, "person", person.get("id", ""))
            col_role.caption(caption)

    # ── Organization ──────────────────────────────────────────────────────────
    org = data.get("organization")
    if org:
        st.divider()
        st.subheader("Theater")
        entity_link(org["name"], "organization", org["id"])
        if org.get("url"):
            st.link_button("🔗 Theater-Website", org["url"])

    # ── Tags ──────────────────────────────────────────────────────────────────
    tags = data.get("tags") or []
    if tags:
        st.divider()
        st.markdown(" ".join(f"`{t}`" for t in tags))
