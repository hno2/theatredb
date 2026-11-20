from types import SimpleNamespace

import streamlit as st
from streamlit.user_info import UserInfoProxy

from frontend.views.settings import render_verified_toggle


def _get_effective_user() -> UserInfoProxy | SimpleNamespace:
    real_user = st.user
    if getattr(real_user, "is_logged_in", False):
        return real_user

    if "mock_user" not in st.session_state:
        st.session_state.mock_user = SimpleNamespace(
            is_logged_in=True,
            name="Simon Mock",
            email="simon@local",
            sub="mock-user-1",
        )
    return st.session_state.mock_user


st.set_page_config(page_title="TheaterDB", layout="wide")

user = _get_effective_user()
st.sidebar.write(f"User: {user.name} ({user.email})")

if "_flash" in st.session_state:
    st.toast(st.session_state.pop("_flash"), icon="✅")
pages = {
    "Entdecken": [
        st.Page("views/overview.py", title="Startseite", icon=":material/map:", default=True),
        st.Page("views/catalog_theater.py", title="Theater", icon=":material/event_seat:", url_path="theater"),
        st.Page(
            "views/catalog_productions.py",
            title="Produktionen",
            icon=":material/local_activity:",
            url_path="produktionen",
        ),
        st.Page("views/catalog_people.py", title="Personen", icon=":material/person:", url_path="personen"),
        st.Page("views/catalog_texts.py", title="Texte", icon=":material/menu_book:", url_path="texte"),
        st.Page("views/detail.py", title="Detailansicht", url_path="detail", visibility="hidden"),
    ],
    "Verwaltung": [
        st.Page("management/reconciliation.py", title="Datenabgleich & Duplikate", icon=":material/merge:"),
        st.Page("management/organizations.py", title="Organisationen", icon=":material/event_seat:"),
        st.Page("management/persons.py", title="Personen", icon=":material/person:"),
        st.Page("management/productions.py", title="Produktionen", icon=":material/local_activity:"),
        st.Page("management/texts.py", title="Texte", icon=":material/menu_book:"),
        st.Page("management/awards.py", title="Auszeichnungen", icon=":material/trophy:"),
        st.Page("management/sources.py", title="Quellen", icon=":material/book:"),
    ],
}
nav = st.navigation(pages)
if nav.url_path in {"", "detail", "theater"}:
    with st.sidebar:
        st.subheader("Einstellungen")
        render_verified_toggle("_sidebar_verified_only")
nav.run()
