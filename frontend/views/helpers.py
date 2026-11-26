import math
from html import escape
from unicodedata import normalize
from urllib.parse import urlencode

import streamlit as st

from backend.config import PRESERVED_CREDIT_LABELS, ROLE_LABELS, canonical_role
from frontend.views.settings import theatre_filter_link_params


def role_label(role: str | None) -> str:
    """Translate known canonical roles while retaining source-specific labels."""
    if role and (label := PRESERVED_CREDIT_LABELS.get(role.strip().casefold())):
        return label
    return ROLE_LABELS.get(canonical_role(role) or "", role or "")


def theatre_city(name: str, city: str | None) -> str:
    """Return the city only when it adds information to the theatre name."""
    city = (city or "").strip()
    normalized_city = normalize("NFC", " ".join(city.split())).casefold()
    normalized_name = normalize("NFC", " ".join(name.split())).casefold()
    return "" if normalized_city in normalized_name else city


def theatre_label(name: str, city: str | None) -> str:
    """Format a theatre name with nonredundant city context."""
    return " · ".join(filter(None, (name, theatre_city(name, city))))


def render_tags(tags: list[str]) -> None:
    """Show source tags as wrapping, noninteractive pills."""
    if tags:
        with st.container(horizontal=True, gap="xsmall"):
            for tag in dict.fromkeys(tags):
                st.badge(tag, color="gray")


def has_coordinates(lat: float | None, lon: float | None) -> bool:
    """Accept only finite coordinates within geographic bounds, including zero."""
    return (
        lat is not None
        and lon is not None
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def entity_anchor(label: str, kind: str, entity_id: str, *, include_unverified: bool = False, path: str = "") -> str:
    """Build a same-tab entity link for use within a line of text."""
    filters = {"verified_only": "0"} if include_unverified else theatre_filter_link_params()
    query = urlencode({"kind": kind, "id": entity_id, **filters})
    return f'<a href="{escape(path, quote=True)}?{escape(query, quote=True)}" target="_self">{escape(label)}</a>'


def entity_link(label: str, kind: str, entity_id: str) -> None:
    """Link to an entity on the detail page, from any page."""
    st.markdown(entity_anchor(label, kind, entity_id, path="detail"), unsafe_allow_html=True)


def production_people(production: dict) -> dict[str, dict]:
    """Index credited people by identity, not role or display name."""
    return {
        str(membership["person"]["id"]): membership["person"]
        for membership in production.get("memberships") or []
        if (membership.get("person") or {}).get("id")
    }
