import streamlit as st

from backend.config import DISCOVERY_KINDS
from backend.entity_management import browse_entities
from frontend.views.helpers import entity_link
from frontend.views.organization import render_production_card

PAGE_SIZE = 12


def _sync_page(source: str, target: str) -> None:
    st.session_state[target] = st.session_state[source]


def render_pagination(page: int, pages: int, page_key: str, position: str) -> None:
    """Keep native pagination above and below results in sync."""
    if pages <= 1:
        return
    bottom_key = f"{page_key}_bottom"
    key, other_key = (page_key, bottom_key) if position == "top" else (bottom_key, page_key)
    if st.session_state.get(key) != page:
        st.session_state[key] = page
    st.pagination(pages, key=key, on_change=_sync_page, args=(key, other_key))
    st.caption(f"Seite {page} von {pages}")


def render_discovery(initial_kind: str = "organization") -> None:
    """Offer direct detail links without requiring an entity selection first."""
    st.caption("THEATERDB / ENTDECKEN")
    st.title("Theater und Produktionen entdecken")
    st.write("Einträge direkt öffnen oder nach Titel, Theater und Ort suchen.")
    kinds = list(DISCOVERY_KINDS)
    kind = st.radio(
        "Entdecken",
        kinds,
        index=kinds.index(initial_kind) if initial_kind in kinds else 0,
        format_func=lambda value: DISCOVERY_KINDS[value][0],
        horizontal=True,
    )
    label, model = DISCOVERY_KINDS[kind]
    query = st.text_input(
        "Person suchen" if kind == "person" else "Titel, Theater oder Ort suchen",
        key=f"discovery_search_{kind}",
    ).strip()
    page_key = f"discovery_page_{kind}_{query}"
    page = st.session_state.get(page_key, 1)
    entries, total = browse_entities(model, query, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        st.session_state[page_key] = pages
        st.rerun()
    st.caption(
        f"{total} Treffer · {label}. "
        + ("Neueste Premieren zuerst." if kind == "production" else "Nach Name sortiert.")
    )
    render_pagination(page, pages, page_key, "top")
    if not entries:
        st.info(
            "Keine Einträge gefunden. Versuche einen anderen Suchbegriff." if query else "Noch keine Einträge erfasst."
        )
    for entry in entries:
        if kind == "production":
            render_production_card(entry, compact=True)
            continue
        with st.container(border=True):
            entity_link(entry["name"], kind, entry["id"])
            if kind == "organization":
                context = [entry.get(field) for field in ("city", "country", "type")]
            else:
                context = [*(entry.get("type") or []), entry.get("nationality")]
            if any(context):
                st.text(" · ".join(filter(None, context)))
            if entry.get("description"):
                with st.expander("Beschreibung"):
                    st.write(entry["description"])
    render_pagination(page, pages, page_key, "bottom")
