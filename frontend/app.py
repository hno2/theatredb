from types import SimpleNamespace

import streamlit as st


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

user = _get_effective_user()
st.sidebar.write(f"User: {user.name} ({user.email})")

if "_flash" in st.session_state:
    st.toast(st.session_state.pop("_flash"), icon="✅")
pages = {
    "Views": [
        st.Page("views/overview.py", title="Overview", icon=":material/dashboard:"),
        st.Page("views/organization.py", title="Organization View", icon=":material/event_seat:"),
        # st.Page("views/people.py", title="People", icon=":material/person:"),
        # st.Page("views/productions.py", title="Productions", icon=":material/local_activity:"),
        # st.Page("views/texts.py", title="Texts", icon=":material/menu_book:"),
        # st.Page("views/awards.py", title="Awards", icon=":material/trophy:"),
    ],
    "Management": [
        st.Page("management/organizations.py", title="Organizations", icon=":material/event_seat:"),
        st.Page("management/persons.py", title="Persons", icon=":material/person:"),
        st.Page("management/productions.py", title="Productions", icon=":material/local_activity:"),
        st.Page("management/texts.py", title="Texts", icon=":material/menu_book:"),
        st.Page("management/awards.py", title="Awards", icon=":material/trophy:"),
        st.Page("management/sources.py", title="Sources", icon=":material/book:"),
    ],
}
nav = st.navigation(pages)
nav.run()
