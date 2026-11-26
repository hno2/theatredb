import logging
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import entity_management
from backend.models import (
    EntityRedirect,
    EntitySource,
    EntityType,
    Organization,
    OrganizationMembership,
    Person,
    Production,
    ProductionMembership,
    Source,
    Text,
    TextContribution,
)
from frontend.views import organization

PROFILE_SCRIPT = """
import streamlit as st
from frontend.views.organization import render_organization
render_organization(st.session_state.profile)
"""
ProfileFixture = tuple[AppTest, dict[str, Any], Mock]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-03-02", date(2025, 3, 2)),
        ("2025-03-02T23:30:00-03:00", date(2025, 3, 2)),
        ("2025-03-02T00:00:00Z", date(2025, 3, 2)),
        ("2025-02-30", None),
        ("not-a-date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw: str | None, expected: date | None) -> None:
    assert organization.parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" https://example.org/theater?id=1 ", "https://example.org/theater?id=1"),
        ("http://example.org", "http://example.org"),
        ("javascript:alert(1)", None),
        ("https:///missing-host", None),
        ("https://[broken", None),
        ("https://example.org\nunsafe", None),
        ("example.org", None),
        (None, None),
    ],
)
def test_web_url(raw: str | None, expected: str | None) -> None:
    assert organization.web_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://archive.example/about", "https://archive.example/favicon.ico"),
        ("http://[::1]:8000/archive", "http://[::1]:8000/favicon.ico"),
        ("https://user:pass@archive.example/about", "https://archive.example/favicon.ico"),
        ("javascript:alert(1)", None),
        ("https://archive.example:invalid/about", None),
    ],
)
def test_favicon_url(raw: str, expected: str | None) -> None:
    assert organization.favicon_url(raw) == expected


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch) -> ProfileFixture:
    source = Source(name="Theaterarchiv", base_url="https://archive.example/about")
    monkeypatch.setattr(organization, "entities_by_ids", lambda model, ids: [source] if source.id in ids else [])
    links = Mock()
    monkeypatch.setattr(organization, "entity_link", links)
    data = {
        "id": "theater-1",
        "name": "Bühne & Haus",
        "street_address": "Theaterstraße 1",
        "postal_code": "1010",
        "address_locality": "Wien",
        "address_country": "Österreich",
        "latitude": 48.2,
        "longitude": 16.4,
        "founded_year": 1900,
        "url": "https://theater.example",
        "url_plays": "https://theater.example/spielplan",
        "productions": [
            {"id": "old", "name": "Früh", "premiere": "2022-01-02T00:00:00"},
            {
                "id": "new",
                "name": "Neu",
                "premiere": "2025-02-01T22:00:00+02:00",
                "description": "Vollständige Beschreibung",
                "duration": 90,
                "suggested_min_age": 0,
                "tags": ["Drama"],
                "critics": ["Eine Kritik"],
            },
            {"id": "none", "name": "Unbekannt", "premiere": None},
            {"id": "invalid", "name": "Fehlerhaft", "premiere": "unbekannt"},
        ],
        "sources": [
            {
                "source_id": str(source.id),
                "title": "Eintrag im Theaterarchiv",
                "external_url": "https://archive.example/theater",
                "sourced_at": "2026-11-05T11:00:00Z",
                "reviewed": True,
            },
        ],
    }
    app = AppTest.from_string(PROFILE_SCRIPT)
    app.session_state.profile = data
    return app, data, links


def test_profile_has_map_address_and_readable_sources(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert not app.exception
    assert app.title[0].value == "Bühne & Haus"
    assert [metric.value for metric in app.metric] == ["4", "2", "1"]
    assert len(app.get("deck_gl_json_chart")) == 1
    text = [element.value for element in app.text]
    assert "Theaterstraße 1" in text
    assert "1010 Wien" in text
    assert "Theaterarchiv" in text
    captions = [element.value for element in app.caption]
    assert "Eintrag im Theaterarchiv" in captions
    assert "Geprüft · Erfasst am 05.11.2026" in captions
    assert (
        ":material/event: Premiere 01.02.2025 · :material/family_restroom: Ab 0 Jahren · :material/schedule: 90 Minuten"
    ) in captions
    assert not any("source_id" in value or "external_url" in value for value in text)
    assert links.call_args_list[0].args == ("Neu", "production", "new")


def test_theatre_production_tags_are_pills_and_filterable(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert any(element.value == ":gray-badge[Drama]" for element in app.markdown)
    tags = app.multiselect(key="production_tags_theater-1")
    assert tags.options == ["Drama"]
    links.reset_mock()
    tags.set_value(["Drama"]).run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["new"]


def test_sources_render_with_source_favicon(profile: ProfileFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _, _ = profile
    image = Mock()
    load = Mock(return_value=b"favicon")
    monkeypatch.setattr(organization.st, "image", image)
    monkeypatch.setattr(organization, "load_favicon", load)
    app.run()
    assert not app.exception
    load.assert_called_once_with("https://archive.example/favicon.ico")
    image.assert_called_once_with(
        b"favicon",
        width=24,
        alt="Favicon Theaterarchiv",
    )


def test_favicon_download_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    response = MagicMock()
    response.headers.get_content_type.return_value = "image/x-icon"
    response.read.return_value = b"favicon"
    response.__enter__.return_value = response
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(
        organization.socket,
        "getaddrinfo",
        lambda *args: [(organization.socket.AF_INET, organization.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(organization, "build_opener", Mock(return_value=opener))
    organization.load_favicon.clear()
    organization._download_favicon.clear()
    url = "https://favicon-cache-test.example/favicon.ico"
    assert organization.load_favicon(url) == b"favicon"
    organization.load_favicon.clear()
    assert organization.load_favicon(url) == b"favicon"
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == url
    assert request.get_header("User-agent") == "TheatreDB/0.1"
    organization.load_favicon.clear()
    organization._download_favicon.clear()


def test_favicon_failure_is_cached_temporarily_and_can_recover(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    response = MagicMock()
    response.headers.get_content_type.return_value = "image/x-icon"
    response.read.return_value = b"favicon"
    response.__enter__.return_value = response
    opener = Mock()
    opener.open.side_effect = [OSError("HTTP 403"), response]
    monkeypatch.setattr(
        organization.socket,
        "getaddrinfo",
        lambda *args: [(organization.socket.AF_INET, organization.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(organization, "build_opener", Mock(return_value=opener))
    organization.load_favicon.clear()
    organization._download_favicon.clear()
    url = "https://favicon-retry-test.example/favicon.ico"
    with caplog.at_level(logging.WARNING, logger=organization.__name__):
        assert organization.load_favicon(url) is None
        assert organization.load_favicon(url) is None
    opener.open.assert_called_once()
    assert "Cannot load favicon from favicon-retry-test.example: HTTP 403" in caplog.text
    organization.load_favicon.clear()
    assert organization.load_favicon(url) == b"favicon"
    assert opener.open.call_count == 2
    organization.load_favicon.clear()
    organization._download_favicon.clear()


@pytest.mark.parametrize(
    ("content_type", "body", "reason"),
    [
        ("text/html", b"error", "non-image"),
        ("image/x-icon", b"x" * (64 * 1024 + 1), "oversized"),
    ],
)
def test_invalid_favicon_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, content_type: str, body: bytes, reason: str
) -> None:
    response = MagicMock()
    response.headers.get_content_type.return_value = content_type
    response.read.return_value = body
    response.__enter__.return_value = response
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(
        organization.socket,
        "getaddrinfo",
        lambda *args: [(organization.socket.AF_INET, organization.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(organization, "build_opener", Mock(return_value=opener))
    organization.load_favicon.clear()
    organization._download_favicon.clear()
    url = "https://favicon-invalid-test.example/favicon.ico"
    assert organization.load_favicon(url) is None
    assert reason in caplog.text
    response.headers.get_content_type.return_value = "image/x-icon"
    response.read.return_value = b"favicon"
    organization.load_favicon.clear()
    assert organization.load_favicon(url) == b"favicon"
    assert opener.open.call_count == 2
    organization.load_favicon.clear()
    organization._download_favicon.clear()


def test_list_order_and_undated_entries(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert [call.args[2] for call in links.call_args_list] == ["new", "old", "invalid", "none"]
    assert any("Ungültiges Premierendatum" in element.value for element in app.warning)
    links.reset_mock()
    app.selectbox[0].set_value("Älteste zuerst").run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["old", "new", "invalid", "none"]
    assert not [element.value for element in app.markdown if element.value.startswith("####")]
    links.reset_mock()
    app.selectbox[0].set_value("Titel A-Z").run()
    assert [call.args[2] for call in links.call_args_list] == ["invalid", "old", "new", "none"]


def test_empty_profile_has_honest_missing_data_messages(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.session_state.profile = {"id": "empty", "name": "Leeres Haus"}
    app.run()
    assert not app.exception
    assert not app.get("deck_gl_json_chart")
    assert not app.radio
    assert not links.called
    assert all(metric.value == "0" for metric in app.metric)
    messages = [element.value for element in app.info]
    assert any("Standortkoordinaten" in value for value in messages)
    assert any("keine Produktionen" in value for value in messages)
    assert any("keine Quellen" in value for value in messages)


def test_bad_sources_are_visible_without_unsafe_links(profile: ProfileFixture) -> None:
    app, data, _ = profile
    data["sources"] = [
        {"external_url": "javascript:alert(1)", "sourced_at": "bad-date"},
        {"external_url": "https://unknown.example/entry", "title": "Weiterer Beleg"},
        {"title": "Gedrucktes Archiv"},
    ]
    app.run()
    assert not app.exception
    assert any("Kein gültiger HTTP(S)-Link" in element.value for element in app.warning)
    assert any("Ungültiges Erfassungsdatum" in element.value for element in app.warning)
    assert any(element.value == "unknown.example" for element in app.text)
    assert any("Kein direkter Quellenlink" in element.value for element in app.caption)
    assert any(element.value == "Noch nicht geprüft" for element in app.caption)
    buttons = app.get("link_button")
    assert all(not button.proto.url.startswith("javascript:") for button in buttons)


def test_zero_coordinates_are_mapped(profile: ProfileFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app, data, _ = profile
    data.update(latitude=0, longitude=0)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    pd.testing.assert_frame_equal(map_widget.call_args.args[0], pd.DataFrame([{"lat": 0, "lon": 0}]))


def test_invalid_coordinates_are_not_mapped(profile: ProfileFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app, data, _ = profile
    data.update(latitude=float("nan"), longitude=181)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    map_widget.assert_not_called()
    assert any("Standortkoordinaten" in element.value for element in app.info)


def test_live_search_filters_list_without_hiding_sources(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert app.text_input[0].proto.live_debounce_ms == 500
    assert app.text_input[1].proto.live_debounce_ms == 500
    assert not next(expander for expander in app.expander if expander.label == "Filter").proto.expanded
    links.reset_mock()
    app.text_input[0].set_value("NEU").run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["new"]
    assert any(element.value == "1 von 4 Produktionen" for element in app.caption)
    assert any(element.value == "Theaterarchiv" for element in app.text)
    links.reset_mock()
    app.text_input[0].set_value("nicht vorhanden").run()
    assert not app.exception
    links.assert_not_called()
    assert any("Keine Produktionen für diese Suche und Filter" in element.value for element in app.info)


def test_theatre_shows_venues_memberships_and_credit_summary(profile: ProfileFixture) -> None:
    app, data, links = profile
    data["venues"] = [{"Großes Haus": "500"}, {"Studio": ""}]
    data["memberships"] = [
        {
            "person": {"id": "director", "name": "Alex"},
            "role": "director",
            "start_date": "2020-01-02",
            "end_date": "2025-03-04",
        },
        {"person": None, "start_date": "bad-date"},
    ]
    data["productions"][1]["memberships"] = [
        {"person": {"id": "director", "name": "Alex"}, "role_name": "director"},
    ]
    app.run()
    assert not app.exception
    assert "Spielstätten & Menschen" in [tab.label for tab in app.tabs]
    text = [element.value for element in app.text]
    assert "Großes Haus" in text
    assert "Studio" in text
    assert "Regie" in text
    captions = [element.value for element in app.caption]
    assert "Plätze: 500" in captions
    assert "Platzzahl nicht hinterlegt" in captions
    assert "Beginn: 02.01.2020 · Ende: 04.03.2025" in captions
    assert "Regie: **Alex**" in [element.value for element in app.markdown]
    assert any(call.args == ("Alex", "person", "director") for call in links.call_args_list)
    assert any("ohne verknüpfte Person" in element.value for element in app.warning)
    assert any("Ungültiges Datum" in element.value for element in app.warning)


def test_credits_group_directors_first_actors_second_and_hide_other_roles() -> None:
    data = {
        "memberships": [
            {"person": {"id": "actor-1", "name": "Anna"}, "role_name": "Schauspiel"},
            {"person": {"id": "set", "name": "Nicht anzeigen"}, "role_name": "Bühnenbild"},
            {"person": {"id": "director-1", "name": "Alex"}, "role_name": "director"},
            {"person": {"id": "director-2", "name": "Bea"}, "role_name": "Inszenierung"},
            {"person": {"id": "actor-2", "name": "Chris"}, "role_name": "actor"},
            {"person": {"id": "actor-1", "name": "Anna"}, "role_name": "actor", "character_name": "Hamlet"},
            {"person": {"id": "director-1", "name": "Alex"}, "role_name": "Regie"},
            {"person": None, "role_name": "actor"},
        ],
    }
    assert organization.production_credit_line(data) == "Regie: **Alex**, **Bea** · mit Anna, Chris"
    assert not organization.production_credit_line({})
    assert organization.production_credit_line({"memberships": [data["memberships"][0]]}) == "mit Anna"
    assert not organization.production_credit_line({"memberships": [data["memberships"][1]]})


def test_credit_names_are_literal_markdown_not_links() -> None:
    data = {"memberships": [{"person": {"id": "1", "name": "[Alex](https://example.org)"}, "role_name": "director"}]}
    assert organization.production_credit_line(data) == r"Regie: **\[Alex\]\(https://example.org\)**"


def test_preview_is_short_unboxed_and_does_not_replace_full_description(profile: ProfileFixture) -> None:
    app, data, _ = profile
    description = "Eine ausführliche Beschreibung mit vielen Worten. " * 30
    data["productions"][1]["description"] = description
    app.run()
    assert not app.exception
    previews = [element.proto.body for element in app.get("html")]
    assert len(previews) == 1
    assert "-webkit-line-clamp: 2" in previews[0]
    assert previews[0].endswith("…</p>")
    assert len(previews[0].split('margin: 0;">')[1].removesuffix("</p>")) <= 200
    assert description not in [element.value for element in app.markdown]
    assert data["productions"][1]["description"] == description
    assert "Kritiken" not in [expander.label for expander in app.expander]
    facts = [element.value for element in app.caption if ":material/schedule:" in element.value]
    assert len(facts) == 1
    assert all(icon in facts[0] for icon in (":material/event:", ":material/family_restroom:", ":material/schedule:"))


def test_preview_escapes_source_html(profile: ProfileFixture) -> None:
    app, data, _ = profile
    data["productions"][1]["description"] = '<img src=x onerror="alert(1)"> Text & Beschreibung'
    app.run()
    assert not app.exception
    html = app.get("html")[0].proto.body
    assert "<img" not in html
    assert "&lt;img" in html
    assert "Text &amp; Beschreibung" in html


def test_age_and_date_range_filters_combine_and_can_be_cleared(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert app.multiselect[0].options == ["Ab 0 Jahren", "Nicht hinterlegt"]
    app.multiselect[0].set_value(["Ab 0 Jahren"]).run()
    assert not app.exception
    links.reset_mock()
    app.date_input[0].set_value((date(2022, 1, 2), date(2025, 2, 1))).run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["new"]
    links.reset_mock()
    app.multiselect[0].set_value([]).run()
    assert [call.args[2] for call in links.call_args_list] == ["new", "old"]
    links.reset_mock()
    app.date_input[0].set_value(()).run()
    assert [call.args[2] for call in links.call_args_list] == ["new", "old", "invalid", "none"]
    links.reset_mock()
    app.multiselect[0].set_value(["Nicht hinterlegt"]).run()
    assert [call.args[2] for call in links.call_args_list] == ["old", "invalid", "none"]


def test_incomplete_premiere_range_does_not_silently_hide_entries(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    links.reset_mock()
    app.date_input[0].set_value((date(2022, 1, 2),)).run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["new", "old", "invalid", "none"]
    assert any("Enddatum wählen" in element.value for element in app.info)


def test_person_filter_searches_hidden_team_roles_and_text_contributions(profile: ProfileFixture) -> None:
    app, data, links = profile
    data["productions"][0]["memberships"] = [
        {"person": {"id": "set", "name": "Alex Beispiel"}, "role_name": "set_designer"},
    ]
    data["productions"][1]["text"] = {
        "contributions": [{"person": {"id": "author", "name": "Anna Autorin"}, "role": "author"}],
    }
    app.run()
    links.reset_mock()
    app.text_input[1].set_value(" ALEX ").run()
    assert not app.exception
    assert [call.args[2] for call in links.call_args_list] == ["old"]
    links.reset_mock()
    app.text_input[1].set_value("autorIN").run()
    assert [call.args[2] for call in links.call_args_list] == ["new"]
    links.reset_mock()
    app.text_input[0].set_value("Früh").run()
    assert not links.called
    assert any("Keine Produktionen für diese Suche und Filter" in element.value for element in app.info)


def test_list_uses_native_pagination_and_resets_after_filter_changes(profile: ProfileFixture) -> None:
    app, data, links = profile
    data["productions"] = [
        {"id": str(index), "name": f"Produktion {index:02}", "suggested_min_age": index % 2, "premiere": "2025-01-01"}
        for index in range(500)
    ]
    app.run()
    assert not app.exception
    assert len(links.call_args_list) == 12
    assert app.pagination[0].num_pages == 42
    links.reset_mock()
    app.pagination[0].set_value(42).run()
    assert not app.exception
    assert len(links.call_args_list) == 8
    links.reset_mock()
    app.multiselect[0].set_value(["Ab 0 Jahren"]).run()
    assert app.pagination[0].value == 1
    assert len(links.call_args_list) == 12
    links.reset_mock()
    app.text_input[0].set_value("Produktion 00").run()
    assert not app.pagination
    assert [call.args[2] for call in links.call_args_list] == ["0"]


def test_undated_only_theatre_disables_premiere_filter(profile: ProfileFixture) -> None:
    app, data, _ = profile
    data["productions"] = [{"id": "none", "name": "Unbekannt"}]
    app.run()
    assert not app.exception
    assert app.date_input[0].disabled
    assert any("Premiere unbekannt" in element.value for element in app.caption)


@pytest.mark.parametrize("count", [12, 500])
def test_profile_batches_productions_and_credits_without_recursive_queries(monkeypatch, count: int) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    old_id = uuid4()
    with Session(engine) as session:
        theatre = Organization(name="Theater", venues={"Studio": 100})
        source = Source(name="Archiv")
        author = Person(name="Autorin")
        member = Person(name="Hausmitglied")
        session.add_all([theatre, source])
        for index in range(count):
            person = Person(name=f"Person {index}")
            entry = Production(
                name=f"Produktion {index}",
                organization=theatre,
                description=f"Beschreibung {index}",
                suggested_min_age=index % 2,
                text=Text(title=f"Text {index}", contributions=[TextContribution(person=author, role="author")]),
                memberships=[ProductionMembership(person=person, role_name="director")],
            )
            session.add(entry)
        session.add(OrganizationMembership(organization=theatre, person=member, role="director"))
        session.add(Production(name="Anderes Haus", memberships=[ProductionMembership(person=author)]))
        session.flush()
        session.add(EntitySource(source_id=source.id, entity_type=EntityType.ORGANIZATION, entity_id=theatre.id))
        session.add(EntityRedirect(entity_type=EntityType.ORGANIZATION, old_id=old_id, target_id=theatre.id))
        session.commit()
        theatre_id = str(theatre.id)
    monkeypatch.setattr(entity_management, "get_session", lambda: Session(engine))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    try:
        data = entity_management.get_organization_profile(theatre_id)
        assert len(statements) <= 8, statements
        assert len(data["productions"]) == count
        assert data["venues"] == {"Studio": 100}
        assert len(data["sources"]) == 1
        assert data["memberships"][0]["person"]["name"] == "Hausmitglied"
        for production in data["productions"]:
            assert production["description"].startswith("Beschreibung")
            assert production["memberships"][0]["person"]["name"].startswith("Person ")
            assert production["text"]["contributions"][0]["person"]["name"] == "Autorin"
            assert "organization" not in production
            assert "production_memberships" not in production["memberships"][0]["person"]
        assert len(organization.filter_productions(data["productions"], "", "Autorin", [], ())) == count
        assert entity_management.get_organization_profile(str(old_id))["id"] == theatre_id
        with pytest.raises(ValueError, match="Organization not found"):
            entity_management.get_organization_profile(str(uuid4()))
        with pytest.raises(ValueError):
            entity_management.get_organization_profile("invalid")
    finally:
        engine.dispose()


def test_detail_reuses_cached_organization_profile(profile: ProfileFixture, monkeypatch) -> None:
    _, data, _ = profile
    fetch = Mock(return_value=data)
    monkeypatch.setattr(organization, "get_organization_profile", fetch)
    graph = Mock(side_effect=AssertionError("Organization must not load a recursive graph"))
    monkeypatch.setattr(entity_management, "get_entity_graph", graph)
    organization.load_organization_profile.clear()
    try:
        app = AppTest.from_file(Path(__file__).resolve().parents[1] / "frontend" / "views" / "detail.py")
        app.query_params.update(kind="organization", id=data["id"])
        app.run()
        app.run()
        assert not app.exception
        assert app.title[0].value == data["name"]
        fetch.assert_called_once_with(data["id"], verified_only=True)
        cached = organization.load_organization_profile(data["id"], verified_only=True)
        cached["productions"].clear()
        assert len(organization.load_organization_profile(data["id"], verified_only=True)["productions"]) == 4
        organization.load_organization_profile.clear()
        app.run()
        assert not app.exception
        assert fetch.call_count == 2
        graph.assert_not_called()
    finally:
        organization.load_organization_profile.clear()
