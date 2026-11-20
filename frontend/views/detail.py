from urllib.parse import urlencode

import streamlit as st

from backend.config import DISCOVERY_KINDS
from backend.entity_management import get_entity_graph
from frontend.views.discovery import render_discovery
from frontend.views.organization import load_organization_profile, render_organization
from frontend.views.person import render_person
from frontend.views.production import render_production
from frontend.views.settings import theatre_filter_link_params, verified_only

params = st.query_params


def _render_directory_link() -> None:
    filter_query = urlencode(theatre_filter_link_params())
    directory_url = f"detail?{filter_query}" if filter_query else "detail"
    st.markdown(
        f'<a href="{directory_url}" target="_self">Zur Übersicht: Theater und Produktionen</a>',
        unsafe_allow_html=True,
    )


if params.get("id"):
    kind = params.get("kind")
    if kind != "production":
        _render_directory_link()
    if kind not in DISCOVERY_KINDS:
        st.error("Unknown entity type.")
    else:
        _, model = DISCOVERY_KINDS[kind]
        try:
            data = (
                load_organization_profile(params["id"], verified_only=verified_only())
                if kind == "organization"
                else get_entity_graph(model, params["id"])
            )
        except ValueError:
            if kind == "production":
                _render_directory_link()
            if kind == "organization" and verified_only():
                st.info("Kein passendes Theater gefunden. Deaktiviere „Nur geprüfte Theater“ in den Einstellungen.")
            else:
                st.error("Not found")
            st.stop()
        else:
            if kind == "production":
                if not (data.get("organization") or {}).get("id"):
                    _render_directory_link()
                render_production(data)
            elif kind == "organization":
                render_organization(data)
            else:
                render_person(data)
else:
    render_discovery(params.get("kind", "organization"))
