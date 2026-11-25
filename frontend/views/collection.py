"""Meine Sammlung: Merkliste, Tagebuch, Bewertungen and Listen of the signed-in user."""

import streamlit as st

from backend import library
from backend.user_management import get_session
from frontend.auth import login_button, principal
from frontend.views.helpers import entity_link
from frontend.views.personal import NUDGE, save
from frontend.views.text import render_stagings, stagings

ICONS = {"text": ":material/menu_book:", "production": ":material/theater_comedy:"}

st.title("Meine Sammlung")
user = principal()
if user is None:
    st.info(NUDGE, icon=":material/lock:")
    login_button("collection_login")
    st.stop()

with get_session() as session:
    entries = library.collection(session, user.id)
    lists = library.playlists(session, user.id)


def heading(entry: dict) -> None:
    """Icon, linked title and theatre for one entry."""
    label = entry["title"] + (f" — {entry['theatre']}" if entry["theatre"] else "")
    st.markdown(ICONS[entry["kind"]], help="Text" if entry["kind"] == "text" else "Produktion")
    entity_link(label, entry["kind"], str(entry["target_id"]))


wish_tab, diary_tab, review_tab, list_tab = st.tabs(
    [
        ":material/bookmark: Merkliste",
        ":material/history_edu: Tagebuch",
        ":material/star: Bewertungen",
        ":material/list: Listen",
    ]
)

with wish_tab:
    wishes = [entry for entry in entries if entry["want"]]
    if not wishes:
        st.caption("Noch nichts gemerkt. Auf Text- oder Produktionsseiten :material/bookmark_border: wählen.")
    for entry in wishes:
        with st.container(border=True):
            heading(entry)
            if entry["kind"] == "text":
                with st.expander("Wo läuft das?", icon=":material/location_on:"):
                    render_stagings(stagings(entry["target_id"]), only_current=True)

with diary_tab:
    visits = sorted(
        ((visit, entry) for entry in entries for visit in entry["visits"]),
        key=lambda pair: (pair[0].visited_on is not None, pair[0].visited_on, pair[0].created_at),
        reverse=True,
    )
    if not visits:
        st.caption("Noch keine Lektüre oder Theaterbesuche eingetragen.")
    for visit, entry in visits:
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            icon = ":material/menu_book:" if visit.kind == "read" else ":material/visibility:"
            st.markdown(f"{icon} {visit.visited_on.strftime('%d.%m.%Y') if visit.visited_on else '—'}")
            heading(entry)

with review_tab:
    rated = [entry for entry in entries if entry["rating"] or entry["review"]]
    if not rated:
        st.caption("Noch keine Sterne oder Rezensionen.")
    for entry in rated:
        with st.container(border=True):
            heading(entry)
            if entry["rating"]:
                st.markdown("★" * entry["rating"] + "☆" * (5 - entry["rating"]), help=f"{entry['rating']} von 5")
            if entry["review"]:
                st.text(entry["review"])

with list_tab:
    with st.form("new_playlist", clear_on_submit=True, border=False):
        name = st.text_input("Neue Liste", placeholder="z. B. Must see")
        if st.form_submit_button("Anlegen", icon=":material/add:"):
            save(library.create_playlist, user.id, name, done="Liste angelegt")
            st.rerun()
    for playlist in lists:
        items = [entry for entry in entries if playlist.id in entry["playlists"]]
        with st.expander(f"{playlist.name} ({len(items)})", icon=":material/list:"):
            for entry in sorted(items, key=lambda e: e["playlists"][playlist.id]):
                with st.container(horizontal=True, vertical_alignment="center"):
                    heading(entry)
                    st.button(
                        "Entfernen",
                        icon=":material/remove:",
                        key=f"rm_{playlist.id}_{entry['entry_id']}",
                        on_click=save,
                        args=(library.remove_from_playlist, user.id, playlist.id, entry["entry_id"]),
                        kwargs={"done": "Entfernt"},
                    )
            with st.popover("Liste löschen", icon=":material/delete:"):
                st.caption("Nur die Liste verschwindet; Merkliste, Tagebuch und Bewertungen bleiben.")
                st.button(
                    "Endgültig löschen",
                    type="primary",
                    key=f"del_list_{playlist.id}",
                    on_click=save,
                    args=(library.delete_playlist, user.id, playlist.id),
                    kwargs={"done": "Liste gelöscht"},
                )
