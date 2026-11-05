from datetime import date
from typing import Any
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


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch) -> ProfileFixture:
    source = Source(name="Theaterarchiv")
    monkeypatch.setattr(organization, "list_entities", lambda model: [source])
    links = Mock()
    monkeypatch.setattr(organization, "entity_link", links)
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
    assert "Eintrag im Theaterarchiv" in text
    captions = [element.value for element in app.caption]
    assert "Geprüft · Erfasst am 05.11.2026" in captions
    assert (
        ":material/event: Premiere 01.02.2025 · :material/family_restroom: Ab 0 Jahren · :material/schedule: 90 Minuten"
    ) in captions
    assert not any("source_id" in value or "external_url" in value for value in text)
    assert links.call_args_list[0].args == ("Neu", "production", "new")


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
    data.update(lat=0, lon=0)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    pd.testing.assert_frame_equal(map_widget.call_args.args[0], pd.DataFrame([{"lat": 0, "lon": 0}]))


def test_invalid_coordinates_are_not_mapped(profile: ProfileFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app, data, _ = profile
    data.update(lat=float("nan"), lon=181)
    map_widget = Mock()
    monkeypatch.setattr(organization.st, "map", map_widget)
    app.run()
    assert not app.exception
    map_widget.assert_not_called()
    assert any("Standortkoordinaten" in element.value for element in app.info)


def test_live_search_filters_list_without_hiding_sources(profile: ProfileFixture) -> None:
    app, _, links = profile
    app.run()
    assert app.text_input[0].proto.live_debounce_ms == 250
    assert app.text_input[1].proto.live_debounce_ms == 250
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
        {"person": {"id": "director", "name": "Alex"}, "role": "director"},
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
            {"person": {"id": "actor-1", "name": "Anna"}, "role": "Schauspiel"},
            {"person": {"id": "set", "name": "Nicht anzeigen"}, "role": "Bühnenbild"},
            {"person": {"id": "director-1", "name": "Alex"}, "role": "director"},
            {"person": {"id": "director-2", "name": "Bea"}, "role": "Inszenierung"},
            {"person": {"id": "actor-2", "name": "Chris"}, "role": "actor"},
            {"person": {"id": "actor-1", "name": "Anna"}, "role": "actor", "character": "Hamlet"},
            {"person": {"id": "director-1", "name": "Alex"}, "role": "Regie"},
            {"person": None, "role": "actor"},
        ],
    }
    assert organization.production_credit_line(data) == "Regie: **Alex**, **Bea** · mit Anna, Chris"
    assert not organization.production_credit_line({})
    assert organization.production_credit_line({"memberships": [data["memberships"][0]]}) == "mit Anna"
    assert not organization.production_credit_line({"memberships": [data["memberships"][1]]})


def test_credit_names_are_literal_markdown_not_links() -> None:
    data = {"memberships": [{"person": {"id": "1", "name": "[Alex](https://example.org)"}, "role": "director"}]}
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
        {"person": {"id": "set", "name": "Alex Beispiel"}, "role": "set_designer"},
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
        {"id": str(index), "name": f"Produktion {index:02}", "age_rating": index % 2, "premiere": "2025-01-01"}
        for index in range(25)
    ]
    app.run()
    assert not app.exception
    assert len(links.call_args_list) == 12
    assert app.pagination[0].num_pages == 3
    links.reset_mock()
    app.pagination[0].set_value(3).run()
    assert len(links.call_args_list) == 1
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
