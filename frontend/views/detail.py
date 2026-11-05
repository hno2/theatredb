import streamlit as st

from backend.entity_management import get_entity_graph
from backend.models import Organization, Person, Production
from frontend.views.discovery import render_discovery
from frontend.views.organization import render_organization
from frontend.views.person import render_person
from frontend.views.production import render_production

KIND_CONFIG = {
    "production": ("Produktion", Production, render_production),
    "organization": ("Organisation", Organization, render_organization),
    "person": ("Person", Person, render_person),
}

params = st.query_params
if params.get("id"):
    st.markdown(
        '<a href="detail" target="_self">Zur Übersicht: Theater und Produktionen</a>',
        unsafe_allow_html=True,
    )
    kind = params.get("kind")
    if kind not in KIND_CONFIG:
        st.error("Unknown entity type.")
    else:
        _, _, render_fn = KIND_CONFIG[kind]
        try:
            data = get_entity_graph(KIND_CONFIG[kind][1], params["id"])
        except ValueError:
            st.error("Not found")
            st.stop()
        else:
            render_fn(data)
else:
    render_discovery(params.get("kind", "organization"))
