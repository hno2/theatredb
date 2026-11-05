import math

import streamlit as st


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
    st.markdown(
        f'<a href="?kind={kind}&id={entity_id}" target="_self">{label}</a>',
        unsafe_allow_html=True,
    )
