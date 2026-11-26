import streamlit as st

from frontend.auth import current_user, login_button
from frontend.views.settings import render_verified_toggle

st.set_page_config(page_title="TheaterDB", layout="wide")

user = current_user()
st.session_state["_principal"] = user
with st.sidebar:
    if user is None:
        st.caption("Merkliste, Sterne und Listen behalten?")
        login_button("sidebar_login")
    else:
        st.caption(f":material/person: {user.name}")
        if user.dev:
            st.caption(":orange-badge[:material/developer_mode: Dev-Zugang]")
        if st.user.get("is_logged_in"):
            st.button("Abmelden", icon=":material/logout:", on_click=st.logout)

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
        st.Page("views/collection.py", title="Meine Sammlung", icon=":material/bookmarks:", url_path="sammlung"),
        st.Page("views/detail.py", title="Detailansicht", url_path="detail", visibility="hidden"),
    ],
    # Pages left out of st.navigation cannot be opened, so this is the access check for management.
    "Verwaltung": [
        st.Page("management/reconciliation.py", title="Datenabgleich & Duplikate", icon=":material/merge:"),
        st.Page("management/authority_bulk.py", title="Normdaten-Abgleich", icon=":material/verified:"),
        st.Page("management/organizations.py", title="Organisationen", icon=":material/event_seat:"),
        st.Page("management/persons.py", title="Personen", icon=":material/person:"),
        st.Page("management/productions.py", title="Produktionen", icon=":material/local_activity:"),
        st.Page("management/texts.py", title="Texte", icon=":material/menu_book:"),
        st.Page("management/awards.py", title="Auszeichnungen", icon=":material/trophy:"),
        st.Page("management/sources.py", title="Quellen", icon=":material/book:"),
    ],
}
if not (user and user.is_admin):
    del pages["Verwaltung"]
nav = st.navigation(pages)
if nav.url_path in {"", "detail", "theater"}:
    with st.sidebar:
        st.subheader("Einstellungen")
        render_verified_toggle("_sidebar_verified_only")
nav.run()
