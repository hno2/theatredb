import datetime as dt
import importlib

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import library
from backend.models import (
    LibraryEntry,
    LibraryVisit,
    LineageRelation,
    Organization,
    PlaylistItem,
    Production,
    Text,
    TextLineage,
)
from frontend import auth
from frontend.views import personal

TODAY = dt.date(2026, 11, 21)


def test_oidc_server_dependencies_are_available():
    assert callable(importlib.import_module("authlib.integrations.starlette_client").OAuth)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _user(session, key="oidc:issuer|a"):
    return library.find_or_create_user(session, key, "Reader").id


def test_identity_is_stable_and_not_admin(session):
    first = library.find_or_create_user(session, "oidc:issuer|a", "A")
    assert library.find_or_create_user(session, "oidc:issuer|a", "renamed").id == first.id
    assert library.find_or_create_user(session, "oidc:other|a", "A").id != first.id
    assert not first.is_admin
    assert first.password_hash is None


def test_read_watched_want_and_opinion_are_independent(session):
    user = _user(session)
    text = Text(title="Woyzeck")
    session.add(text)
    session.commit()
    library.add_visit(session, user, "text", text.id, "read", None, today=TODAY)
    library.add_visit(session, user, "text", text.id, "read", dt.date(2020, 5, 1), today=TODAY)
    library.set_opinion(session, user, "text", text.id, 5, "  ")
    state = library.status(session, user, "text", text.id)
    assert len(state["reads"]) == 2
    assert not state["watches"]
    assert not state["want"]
    assert (state["rating"], state["review"]) == (5, None)
    library.set_want(session, user, "text", text.id, True)
    library.set_want(session, user, "text", text.id, True)
    library.set_opinion(session, user, "text", text.id, None, "Stark")
    state = library.status(session, user, "text", text.id)
    assert state["want"]
    assert (state["rating"], state["review"]) == (None, "Stark")
    assert len(state["reads"]) == 2


@pytest.mark.parametrize(
    ("kind", "visit_kind", "day", "message"),
    [
        ("production", "read", None, "Nur Texte"),
        ("text", "watched", TODAY + dt.timedelta(days=1), "Zukunft"),
    ],
)
def test_invalid_visits_are_rejected(session, kind, visit_kind, day, message):
    text = Text(title="Text")
    production = Production(name="Staging", text=text)
    session.add_all([text, production])
    session.commit()
    target = text.id if kind == "text" else production.id
    with pytest.raises(ValueError, match=message):
        library.add_visit(session, _user(session), kind, target, visit_kind, day, today=TODAY)


def test_rating_out_of_range_is_rejected(session):
    text = Text(title="Text")
    session.add(text)
    session.commit()
    with pytest.raises(ValueError, match="Sternen"):
        library.set_opinion(session, _user(session), "text", text.id, 6, None)


def test_production_visit_counts_for_text_but_is_not_a_reading(session):
    user = _user(session)
    text = Text(title="Faust")
    production = Production(name="Faust I", text=text)
    session.add_all([text, production])
    session.commit()
    library.add_visit(session, user, "production", production.id, "watched", None, today=TODAY)
    state = library.status(session, user, "text", text.id)
    assert state["production_watches"] == 1
    assert not state["reads"]
    assert state["rating"] is None


def test_other_users_cannot_touch_private_rows(session):
    owner, other = _user(session), _user(session, "oidc:issuer|b")
    text = Text(title="Text")
    session.add(text)
    session.commit()
    library.add_visit(session, owner, "text", text.id, "read", None, today=TODAY)
    playlist = library.create_playlist(session, owner, "Must see")
    visit = session.exec(select(LibraryVisit)).one()
    with pytest.raises(ValueError, match="nicht gefunden"):
        library.delete_visit(session, other, visit.id)
    with pytest.raises(ValueError, match="nicht gefunden"):
        library.add_to_playlist(session, other, playlist.id, "text", text.id)
    with pytest.raises(ValueError, match="nicht gefunden"):
        library.delete_playlist(session, other, playlist.id)
    assert library.status(session, other, "text", text.id)["reads"] == []
    assert library.collection(session, other) == []


def test_mixed_playlist_is_ordered_idempotent_and_deletion_keeps_entries(session):
    user = _user(session)
    text = Text(title="Text")
    production = Production(name="Staging")
    session.add_all([text, production])
    session.commit()
    playlist = library.create_playlist(session, user, "  Must   see ")
    assert playlist.name == "Must see"
    assert library.create_playlist(session, user, "Must see").id == playlist.id
    library.add_to_playlist(session, user, playlist.id, "production", production.id)
    library.add_to_playlist(session, user, playlist.id, "text", text.id)
    library.add_to_playlist(session, user, playlist.id, "production", production.id)
    library.set_opinion(session, user, "text", text.id, 3, None)
    entries = {row["kind"]: row for row in library.collection(session, user)}
    assert entries["production"]["playlists"] == {playlist.id: 0}
    assert entries["text"]["playlists"] == {playlist.id: 1}
    library.delete_playlist(session, user, playlist.id)
    session.flush()
    assert session.exec(select(PlaylistItem)).all() == []
    assert library.status(session, user, "text", text.id)["rating"] == 3


def test_stagings_cover_family_and_label_premieres(session):
    original = Text(title="Original")
    translation = Text(title="Übersetzung")
    based_on = Text(title="Nach Motiven")
    theatre = Organization(name="Schauspiel", address_locality="Köln")
    session.add_all([original, translation, based_on, theatre])
    session.flush()
    session.add_all(
        [
            TextLineage(
                source_text_id=original.id, derived_text_id=translation.id, relation=LineageRelation.TRANSLATION
            ),
            TextLineage(source_text_id=original.id, derived_text_id=based_on.id, relation=LineageRelation.BASED_ON),
            Production(name="Soon", text_id=translation.id, organization_id=theatre.id, premiere="2027-01-10"),
            Production(name="Recent", text_id=original.id, premiere="2026-10-01"),
            Production(name="Year", text_id=original.id, premiere="2026"),
            Production(name="Old", text_id=original.id, premiere="2019-03-01"),
            Production(name="Undated", text_id=original.id),
            Production(name="Other work", text_id=based_on.id, premiere="2027-01-01"),
        ]
    )
    session.commit()
    rows = library.stagings(session, original.id, today=TODAY)
    assert [(row["name"], row["status"]) for row in rows] == [
        ("Soon", "upcoming"),
        ("Recent", "recent"),
        ("Year", "recent"),
        ("Old", "past"),
        ("Undated", "unknown"),
    ]
    assert rows[0]["theatre"] == "Schauspiel"


def test_merge_target_coalesces_without_losing_private_data(session):
    user = _user(session)
    keep, duplicate = Text(title="Keep"), Text(title="Duplicate")
    session.add_all([keep, duplicate])
    session.commit()
    playlist = library.create_playlist(session, user, "Liste")
    library.set_opinion(session, user, "text", keep.id, 4, "Gut")
    library.set_want(session, user, "text", duplicate.id, True)
    library.set_opinion(session, user, "text", duplicate.id, 2, "Anders")
    library.add_visit(session, user, "text", duplicate.id, "read", None, today=TODAY)
    library.add_to_playlist(session, user, playlist.id, "text", duplicate.id)
    library.merge_target(session, "text", duplicate.id, keep.id)
    session.commit()
    (entry,) = session.exec(select(LibraryEntry)).all()
    assert (entry.text_id, entry.want_to_see, entry.rating, entry.review) == (keep.id, True, 4, "Gut\n\nAnders")
    assert len(library.status(session, user, "text", keep.id)["reads"]) == 1
    assert session.exec(select(PlaylistItem.entry_id)).all() == [entry.id]


@pytest.mark.parametrize(
    ("env", "value", "flag"),
    [("development", "0", False), ("development", "1", True), ("production", "0", False)],
)
def test_dev_auth_flag(monkeypatch, env, value, flag):
    monkeypatch.setenv("THEATERDB_ENV", env)
    monkeypatch.setenv("THEATERDB_DEV_AUTH", value)
    assert auth.dev_auth_enabled() is flag


@pytest.mark.parametrize(("env", "value"), [("production", "1"), ("development", "yes")])
def test_dev_auth_refuses_production_and_garbage(monkeypatch, env, value):
    monkeypatch.setenv("THEATERDB_ENV", env)
    monkeypatch.setenv("THEATERDB_DEV_AUTH", value)
    with pytest.raises(RuntimeError):
        auth.dev_auth_enabled()


ACTIONS = """
import streamlit as st
from frontend.views.personal import render_actions
st.session_state["_principal"] = st.session_state.get("who")
render_actions("text", st.session_state.target)
"""


@pytest.fixture
def app_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(personal, "get_session", lambda: Session(engine))
    yield engine
    engine.dispose()


def test_anonymous_actions_nudge_and_write_nothing(app_db):
    with Session(app_db) as session:
        text = Text(title="Text")
        session.add(text)
        session.commit()
        target = text.id
    app = AppTest.from_string(ACTIONS)
    app.session_state.target = target
    app.run()
    assert not app.exception
    assert any("Anmelden, damit" in caption.value for caption in app.caption)
    with Session(app_db) as session:
        assert session.exec(select(LibraryEntry)).all() == []


def test_signed_in_actions_save_wish_and_stars(app_db):
    with Session(app_db) as session:
        text = Text(title="Text")
        session.add(text)
        session.commit()
        target = text.id
        user = _user(session)
    app = AppTest.from_string(ACTIONS)
    app.session_state.target = target
    app.session_state.who = auth.Principal(user, "Reader", is_admin=False)
    app.run()
    app.button(key=f"want_{user}_{target}").click().run()
    app.feedback[0].set_value(3)
    app.text_area[0].input("Großartig")
    app.button[-2].click().run()  # opinion form submit (playlist form is last)
    assert not app.exception
    with Session(app_db) as session:
        entry = session.exec(select(LibraryEntry)).one()
    assert (entry.want_to_see, entry.rating, entry.review) == (True, 4, "Großartig")


WHO = """
import streamlit as st
from frontend.auth import current_user
st.session_state.who = current_user()
"""


@pytest.mark.parametrize(("flag", "admin"), [("0", None), ("1", True)])
def test_dev_flag_controls_local_admin(app_db, monkeypatch, flag, admin):
    monkeypatch.setattr(auth, "get_session", lambda: Session(app_db))
    monkeypatch.setenv("THEATERDB_ENV", "development")
    monkeypatch.setenv("THEATERDB_DEV_AUTH", flag)
    app = AppTest.from_string(WHO).run()
    assert not app.exception
    who = app.session_state["who"]
    assert (who and who.is_admin) == admin
