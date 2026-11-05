from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from backend.models import Source
from frontend.views import organization

PROFILE_SCRIPT = """
import streamlit as st
from frontend.views.organization import render_organization
render_organization(st.session_state.profile)
"""


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
def test_parse_date(raw, expected):
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
def test_web_url(raw, expected):
    assert organization.web_url(raw) == expected


@pytest.fixture
def profile(monkeypatch):
    source = Source(name="Theaterarchiv")
    monkeypatch.setattr(organization, "list_entities", lambda model: [source])
    links = Mock()
    monkeypatch.setattr(organization.st, "page_link", links)
    data = {
        "id": "theater-1",
        "name": "Bühne & Haus",
        "street": "Theaterstraße 1",
        "zip_code": "1010",
        "city": "Wien",
        "country": "Österreich",
        "lat": 48.2,
        "lon": 16.4,
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
                "age_rating": 0,
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


def test_profile_has_map_address_and_readable_sources(profile):
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
    assert "Eintrag im Theaterarchiv" in text
    captions = [element.value for element in app.caption]
    assert "Geprüft · Erfasst am 05.11.2026" in captions
    assert "90 Minuten · Ab 0 Jahren" in captions
    assert not any("source_id" in value or "external_url" in value for value in text)
    assert links.call_args_list[0].kwargs["query_params"] == {"kind": "production", "id": "new"}


def test_timeline_order_and_undated_entries(profile):
    app, _, links = profile
    app.run()
    assert [call.kwargs["query_params"]["id"] for call in links.call_args_list] == ["new", "old", "invalid", "none"]
    assert any("Ungültiges Premierendatum" in element.value for element in app.warning)
    links.reset_mock()
    app.radio[0].set_value("Älteste zuerst").run()
    assert not app.exception
    assert [call.kwargs["query_params"]["id"] for call in links.call_args_list] == ["old", "new", "invalid", "none"]
    assert [element.value for element in app.markdown if element.value.startswith("####")] == [
        "#### 2022",
        "#### 2025",
        "#### Ohne Premierendatum",
    ]


def test_empty_profile_has_honest_missing_data_messages(profile):
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


def test_bad_sources_are_visible_without_unsafe_links(profile):
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


def test_zero_coordinates_are_mapped(profile, monkeypatch):
    app, data, _ = profile
    data.update(lat=0, lon=0)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    pd.testing.assert_frame_equal(map_widget.call_args.args[0], pd.DataFrame([{"lat": 0, "lon": 0}]))


def test_invalid_coordinates_are_not_mapped(profile, monkeypatch):
    app, data, _ = profile
    data.update(lat=float("nan"), lon=181)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    map_widget.assert_not_called()
    assert any("Standortkoordinaten" in element.value for element in app.info)


def test_search_filters_timeline_without_hiding_sources(profile):
    app, _, links = profile
    app.run()
    links.reset_mock()
    app.text_input[0].set_value("NEU").run()
    assert not app.exception
    assert [call.kwargs["query_params"]["id"] for call in links.call_args_list] == ["new"]
    assert any(element.value == "1 von 4 Produktionen" for element in app.caption)
    assert any(element.value == "Theaterarchiv" for element in app.text)
    links.reset_mock()
    app.text_input[0].set_value("nicht vorhanden").run()
    assert not app.exception
    links.assert_not_called()
    assert any("Keine Produktionen mit diesem Titel" in element.value for element in app.info)
