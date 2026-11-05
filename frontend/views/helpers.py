import math
from html import escape
from urllib.parse import urlencode

import streamlit as st

from backend.config import ROLE_LABELS, canonical_role


def role_label(role: str | None) -> str:
    """Translate known canonical roles while retaining source-specific labels."""
    return ROLE_LABELS.get(canonical_role(role) or "", role or "")


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


def entity_link(label: str, kind: str, entity_id: str) -> None:
    """Link to an entity within the current detail page."""
    query = urlencode({"kind": kind, "id": str(entity_id)})
    st.markdown(
        f'<a href="?{escape(query, quote=True)}" target="_self">{escape(label)}</a>',
        unsafe_allow_html=True,
    )


def production_people(production: dict) -> dict[str, dict]:
    """Index credited people by identity, not role or display name."""
    return {
        str(membership["person"]["id"]): membership["person"]
        for membership in production.get("memberships") or []
        if (membership.get("person") or {}).get("id")
    }
