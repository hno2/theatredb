"""Compact icon row to remember, log, rate and list a text or production."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable

import streamlit as st
from sqlmodel import Session

from backend import library
from backend.user_management import get_session
from frontend.auth import Principal, login_button, principal

NUDGE = "Anmelden, damit Merkliste, Sterne und Rezensionen erhalten bleiben."


def save(action: Callable, *args: object, done: str) -> None:
    """Run one owner-scoped write in its own transaction; toast only after commit."""
    try:
        with get_session() as session:
            action(session, *args)
            session.commit()
    except ValueError as error:
        st.toast(str(error), icon=":material/error:")
    else:
        st.toast(done, icon=":material/check:")


def _nudge(key: str) -> None:
    st.caption(NUDGE)
    login_button(key)


def _visit_form(user: Principal, kind: str, target_id: uuid.UUID, visit_kind: str, options: dict[str, str]) -> None:
    key = f"{visit_kind}_{user.id}_{target_id}"

    def submit() -> None:
        chosen = st.session_state.get(f"{key}_production")
        target_kind, target = ("production", uuid.UUID(chosen)) if chosen else (kind, target_id)
        save(
            library.add_visit,
            user.id,
            target_kind,
            target,
            visit_kind,
            st.session_state[f"{key}_date"],
            done="Gespeichert",
        )

    with st.form(key, clear_on_submit=True, border=False):
        if options:
            st.selectbox(
                "Welche Produktion?",
                list(options),
                format_func=options.__getitem__,
                index=None,
                placeholder="Unbekannt / andere",
                key=f"{key}_production",
            )
        st.date_input("Wann?", value=None, max_value=dt.date.today(), key=f"{key}_date", format="DD.MM.YYYY")
        st.form_submit_button("Speichern", icon=":material/add:", on_click=submit)


def _visits(user: Principal, visits: list, label: str) -> None:
    for visit in visits:
        left, right = st.columns([4, 1], vertical_alignment="center")
        left.caption(f"{label} {visit.visited_on.strftime('%d.%m.%Y') if visit.visited_on else '(ohne Datum)'}")
        right.button(
            "Löschen",
            icon=":material/delete:",
            key=f"del_{visit.id}",
            help="Eintrag löschen",
            on_click=save,
            args=(library.delete_visit, user.id, visit.id),
            kwargs={"done": "Gelöscht"},
        )


def _opinion_form(user: Principal, kind: str, target_id: uuid.UUID, state: dict) -> None:
    key = f"opinion_{user.id}_{target_id}"

    def submit() -> None:
        stars = st.session_state[f"{key}_stars"]
        rating = None if stars is None else stars + 1
        save(
            library.set_opinion, user.id, kind, target_id, rating, st.session_state[f"{key}_review"], done="Gespeichert"
        )

    with st.form(key, border=False):
        st.feedback("stars", key=f"{key}_stars", default=None if state["rating"] is None else state["rating"] - 1)
        st.text_area("Rezension (privat)", value=state["review"] or "", key=f"{key}_review", max_chars=10_000)
        st.form_submit_button("Speichern", icon=":material/save:", on_click=submit)


def _playlist_form(user: Principal, kind: str, target_id: uuid.UUID, state: dict) -> None:
    key = f"list_{user.id}_{target_id}"
    with get_session() as session:
        lists = {str(item.id): item.name for item in library.playlists(session, user.id)}
    for playlist_id in state["playlists"]:
        st.caption(f":material/check: {lists.get(str(playlist_id), '')}")

    def submit() -> None:
        def add(session: Session, name: str, chosen: str | None) -> None:
            playlist_id = uuid.UUID(chosen) if chosen else library.create_playlist(session, user.id, name).id
            library.add_to_playlist(session, user.id, playlist_id, kind, target_id)

        save(
            add,
            st.session_state.get(f"{key}_new", ""),
            st.session_state.get(f"{key}_pick"),
            done="Zur Liste hinzugefügt",
        )

    with st.form(key, clear_on_submit=True, border=False):
        if lists:
            st.selectbox("Liste", list(lists), format_func=lists.__getitem__, index=None, key=f"{key}_pick")
        st.text_input("Neue Liste", placeholder="z. B. Must see", key=f"{key}_new")
        st.form_submit_button("Hinzufügen", icon=":material/playlist_add:", on_click=submit)


def render_actions(kind: str, target_id: str | uuid.UUID, productions: dict[str, str] | None = None) -> None:
    """Merken / Gelesen / Gesehen / Bewerten / Liste; anonymous visitors get a gentle login nudge instead."""
    user = principal()
    row = st.container(horizontal=True)
    if user is None:
        for label, icon in [
            ("Merken", ":material/bookmark_border:"),
            *([("Gelesen", ":material/menu_book:")] if kind == "text" else []),
            ("Gesehen", ":material/visibility:"),
            ("Bewerten", ":material/star_border:"),
            ("Liste", ":material/playlist_add:"),
        ]:
            with row.popover(label, icon=icon, help=label):
                _nudge(f"nudge_{label}_{target_id}")
        return
    target_id = uuid.UUID(str(target_id))
    with get_session() as session:
        state = library.status(session, user.id, kind, target_id)

    want = state["want"]
    row.button(
        "Gemerkt" if want else "Merken",
        icon=":material/bookmark:" if want else ":material/bookmark_border:",
        type="primary" if want else "secondary",
        help="Von der Merkliste nehmen" if want else "Auf die Merkliste: will ich sehen",
        key=f"want_{user.id}_{target_id}",
        on_click=save,
        args=(library.set_want, user.id, kind, target_id, not want),
        kwargs={"done": "Merkliste aktualisiert"},
    )
    if kind == "text":
        reads = state["reads"]
        with row.popover(
            f"Gelesen {len(reads)}" if reads else "Gelesen",
            icon=":material/menu_book:",
            type="primary" if reads else "secondary",
            help="Diese Fassung gelesen",
        ):
            _visits(user, reads, "Gelesen")
            _visit_form(user, kind, target_id, "read", {})
    watches = state["watches"]
    seen = len(watches) + state["production_watches"]
    with row.popover(
        f"Gesehen {seen}" if seen else "Gesehen",
        icon=":material/visibility:",
        type="primary" if seen else "secondary",
        help="Im Theater gesehen",
    ):
        if state["production_watches"]:
            st.caption(f"{state['production_watches']} mal über eine Produktion dieses Textes")
        _visits(user, watches, "Gesehen")
        _visit_form(user, kind, target_id, "watched", productions or {})
    rating = state["rating"]
    with row.popover(
        "★" * rating if rating else "Bewerten",
        icon=None if rating else ":material/star_border:",
        type="primary" if rating or state["review"] else "secondary",
        help=f"{rating} von 5 Sternen" if rating else "Sterne und Rezension",
    ):
        _opinion_form(user, kind, target_id, state)
    lists = state["playlists"]
    with row.popover(
        f"Liste {len(lists)}" if lists else "Liste",
        icon=":material/playlist_add_check:" if lists else ":material/playlist_add:",
        type="primary" if lists else "secondary",
        help="Zu einer eigenen Liste hinzufügen",
    ):
        _playlist_form(user, kind, target_id, state)
