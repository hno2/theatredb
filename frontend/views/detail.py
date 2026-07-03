# components/detail.py
import streamlit as st

from backend.entity_management import get_entity_graph, list_entities
from backend.models import Organization, Person, Production
from frontend.views.organization import render_organization
from frontend.views.production import render_production
from frontend.views.person import render_person

KIND_CONFIG = {
    "production": ("Produktion", Production, render_production),
    "organization": ("Organisation", Organization, render_organization),
    "person": ("Person", Person, render_person),
}

params = st.query_params
if params.get("id"):
    kind = params.get("kind")
    if kind not in KIND_CONFIG:
        st.error("Unknown entity type.")
    else:
        _, _, render_fn = KIND_CONFIG[kind]
        if render_fn is None:
            st.error(f"{KIND_CONFIG[kind][0]} view not implemented yet.")
        else:
            try:
                data = get_entity_graph(KIND_CONFIG[kind][1], params["id"])
            except ValueError:
                st.error("Not found")
            render_fn(data)
else:
    kind = st.selectbox(
        "Typ",
        options=list(KIND_CONFIG.keys()),
        format_func=lambda k: KIND_CONFIG[k][0],
    )
    entities = list_entities(KIND_CONFIG[kind][1], "name")
    entity = st.selectbox(
        KIND_CONFIG[kind][0],
        options=entities,
        format_func=lambda x: x.name,
    )
    if entity and st.button("Öffnen"):
        st.query_params.update({"kind": kind, "id": entity.id})
        st.rerun()
