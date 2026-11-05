import streamlit as st

from backend.config import DISCOVERY_KINDS
from backend.entity_management import get_entity_graph
from frontend.views.discovery import render_discovery
from frontend.views.organization import render_organization
from frontend.views.person import render_person
from frontend.views.production import render_production

params = st.query_params
if params.get("id"):
    st.markdown(
        '<a href="detail" target="_self">Zur Übersicht: Theater und Produktionen</a>',
        unsafe_allow_html=True,
    )
    kind = params.get("kind")
    if kind not in DISCOVERY_KINDS:
        st.error("Unknown entity type.")
    else:
        _, model = DISCOVERY_KINDS[kind]
        try:
            data = get_entity_graph(model, params["id"])
        except ValueError:
            st.error("Not found")
            st.stop()
        else:
            if kind == "production":
                render_production(data)
            elif kind == "organization":
                render_organization(data)
            else:
                render_person(data)
else:
    render_discovery(params.get("kind", "organization"))
