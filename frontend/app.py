from types import SimpleNamespace

import streamlit as st

from backend.user_management import init_db


@st.cache_resource
def _initialize_database() -> None:
    init_db()


def _get_effective_user() -> SimpleNamespace:
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
_initialize_database()

user = _get_effective_user()
st.sidebar.write(f"User: {user.name} ({user.email})")

if "_flash" in st.session_state:
    st.toast(st.session_state.pop("_flash"), icon="✅")
pages = {
    "Entdecken": [
        st.Page("views/overview.py", title="Startseite", icon=":material/map:", default=True),
        st.Page("views/detail.py", title="Details", icon=":material/visibility:", url_path="detail"),
        # st.Page("views/people.py", title="People", icon=":material/person:"),
        # st.Page("views/productions.py", title="Productions", icon=":material/local_activity:"),
        # st.Page("views/texts.py", title="Texts", icon=":material/menu_book:"),
        # st.Page("views/awards.py", title="Awards", icon=":material/trophy:"),
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
nav.run()
