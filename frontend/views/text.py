"""Public text page: version details, personal actions, and where the work is staged."""

from __future__ import annotations

import uuid

import streamlit as st

from backend import library
from backend.config import LINEAGE_LABELS, TEXT_GENRES, role_label
from backend.user_management import get_session
from frontend.views.helpers import entity_link
from frontend.views.organization import domain_date_label
from frontend.views.personal import render_actions

STATUS = {
    "upcoming": (":material/event_upcoming:", "Kommende Premiere"),
    "recent": (":material/new_releases:", "Kürzlich Premiere"),
    "past": (":material/history:", "Frühere Produktion"),
    "unknown": (":material/help:", "Datum unbekannt"),
}


def stagings(text_id: str | uuid.UUID) -> list[dict]:
    """Productions of the whole work family (translations/adaptations, not "based on")."""
    with get_session() as session:
        return library.stagings(session, uuid.UUID(str(text_id)))


def render_stagings(rows: list[dict], *, only_current: bool = False) -> None:
    """Premiere-based hints; never claims an actual performance date."""
    shown = [row for row in rows if row["status"] in {"upcoming", "recent"}] if only_current else rows
    if not shown:
        st.caption(
            "Keine kommenden oder kürzlichen Premieren erfasst." if only_current else "Keine Produktionen erfasst."
        )
        return
    for row in shown:
        icon, label = STATUS[row["status"]]
        where = " · ".join(filter(None, (row["theatre"], row["city"])))
        st.markdown(f"{icon} **{label}** {row['premiere'] if row['status'] != 'unknown' else ''}", help=label)
        entity_link(f"{row['name']}" + (f" — {where}" if where else ""), "production", str(row["id"]))
    st.caption("Premierendaten, keine bestätigten Aufführungstermine. Spielplan beim Theater prüfen.")


def render_text_page(data: dict) -> None:
    """Show one text version publicly."""
    st.caption("THEATERDB / TEXT")
    st.title(data["title"])
    genre = data.get("genre") or ""
    facts = [
        TEXT_GENRES.get(genre, genre),
        ", ".join(data.get("languages") or []),
        f"entstanden {domain_date_label(data, 'created')}" if data.get("created") else None,
        domain_date_label(data, "published") if data.get("published") else None,
    ]
    if metadata := " · ".join(filter(None, facts)):
        st.text(metadata)
    rows = stagings(data["id"])
    render_actions(
        "text",
        data["id"],
        {str(row["id"]): " · ".join(filter(None, (row["name"], row["theatre"], row["premiere"]))) for row in rows},
    )
    if data.get("description"):
        st.subheader("Beschreibung")
        st.markdown(data["description"])
    if variants := sorted({row["title"] for row in data.get("titles") or [] if row["title"] != data["title"]}):
        st.subheader("Weitere Titel")
        for variant in variants:
            st.text(variant)
    for contribution in data.get("contributions") or []:
        person = contribution.get("person") or {}
        if person.get("id"):
            st.caption(role_label(contribution.get("role")) or "Mitwirkung")
            entity_link(person["name"], "person", person["id"])
    for lineage in data.get("lineages_as_derived") or []:
        if source := lineage.get("source_text"):
            st.caption(LINEAGE_LABELS.get(lineage.get("relation") or "", "Bezug") + " von")
            entity_link(source["title"], "text", source["id"])
    st.subheader("Wo wird der Text inszeniert?")
    render_stagings(rows)
