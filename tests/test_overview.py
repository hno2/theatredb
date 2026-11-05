import json
import math
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from streamlit.testing.v1 import AppTest

from backend.models import Organization, Person, Production
from frontend.views import overview

OVERVIEW_SCRIPT = "from frontend.views.overview import render_overview\nrender_overview()"


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (0, 0, True),
        (48.2, 16.4, True),
        (-90, 180, True),
        (None, 16, False),
        (48, None, False),
        (math.nan, 16, False),
        (48, math.inf, False),
        (91, 16, False),
        (48, -181, False),
    ],
)
def test_coordinate_validation(lat, lon, expected):
    assert overview.has_coordinates(lat, lon) is expected


def test_directory_preserves_missing_coordinates_and_links():
    org = Organization(name="Theater & Bühne", city=" Wien ", country=" Österreich ")
    data = overview.organization_data([org])
    assert len(data) == 1
    row = data.iloc[0]
    assert row["city"] == "Wien"
    assert row["country"] == "Österreich"
    url = urlsplit(row["detail"])
    assert url.path == "detail"
    assert parse_qs(url.query) == {"kind": ["organization"], "id": [str(org.id)]}


@pytest.mark.parametrize("positions", [[(0, 0)], [(48, 16), (48, 16)], [(48, 16), (52, 13)]])
def test_map_supports_single_shared_and_multiple_locations(positions):
    data = overview.organization_data(
        [Organization(name=f"Theater {i}", lat=lat, lon=lon) for i, (lat, lon) in enumerate(positions)]
    )
    deck = json.loads(overview.theater_map(data).to_json())
    assert math.isfinite(deck["initialViewState"]["zoom"])
    assert deck["initialViewState"]["zoom"] <= 11
    layer = deck["layers"][0]
    assert layer["id"] == "theaters"
    assert layer["pickable"] is True
    assert {row["id"] for row in layer["data"]} == set(data["id"])
    assert len(layer["data"]) == len(positions)


@pytest.fixture
def homepage(monkeypatch):
    organizations = [
        Organization(name="Bühne [Mitte]", city="Berlin", country="Deutschland", lat=52.5, lon=13.4),
        Organization(name="Burgtheater", city="Wien", country="Österreich", lat=48.2, lon=16.4),
        Organization(name="Ohne Position", city="Berlin", country="Deutschland"),
    ]
    monkeypatch.setattr(overview, "list_entities", lambda *args, **kwargs: organizations)
    monkeypatch.setattr(overview, "count_entities", lambda model: {Production: 1234, Person: 27}.get(model, 0))
    return AppTest.from_string(OVERVIEW_SCRIPT), organizations


def test_homepage_metrics_and_literal_search(homepage):
    app, _ = homepage
    app.run()
    assert not app.exception
    assert app.title[0].value == "Theaterlandschaft entdecken"
    assert [(metric.label, metric.value) for metric in app.metric] == [
        ("Theater & Organisationen", "3"),
        ("Orte", "2"),
        ("Produktionen", "1.234"),
        ("Personen", "27"),
    ]
    assert len(app.dataframe[0].value) == 3
    app.text_input[0].set_value("[Mitte]").run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Bühne [Mitte]"]
    app.text_input[0].set_value("öSTERREICH").run()
    assert list(app.dataframe[0].value["name"]) == ["Burgtheater"]
    app.text_input[0].set_value("Berlin").run()
    assert len(app.dataframe[0].value) == 2
    app.text_input[0].set_value("Ohne Position").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 1
    assert any("Koordinaten" in message.value for message in app.info)
    app.text_input[0].set_value("unbekannt").run()
    assert not app.exception
    assert not app.dataframe
    assert any("Keine Theater gefunden" in message.value for message in app.info)


def test_selected_marker_has_internal_detail_link(homepage, monkeypatch):
    app, organizations = homepage
    selected_id = str(organizations[0].id)
    chart = Mock(return_value={"selection": {"objects": {"theaters": [{"id": selected_id}]}}})
    link = Mock()
    monkeypatch.setattr(overview.st, "pydeck_chart", chart)
    monkeypatch.setattr(overview.st, "page_link", link)
    app.run()
    assert not app.exception
    link.assert_called_once_with(
        "views/detail.py",
        label="Theater ansehen",
        icon=":material/arrow_forward:",
        query_params={"kind": "organization", "id": selected_id},
    )
    assert chart.call_args.kwargs["on_select"] == "rerun"
    link.reset_mock()
    app.text_input[0].set_value("Wien").run()
    assert not app.exception
    link.assert_not_called()


def test_empty_homepage(monkeypatch):
    monkeypatch.setattr(overview, "list_entities", lambda *args, **kwargs: [])
    monkeypatch.setattr(overview, "count_entities", lambda model: 0)
    app = AppTest.from_string(OVERVIEW_SCRIPT).run()
    assert not app.exception
    assert all(metric.value == "0" for metric in app.metric)
    assert not app.dataframe
    assert any("Noch keine Theater erfasst" in message.value for message in app.info)


def test_shared_map_position_links_every_theater(homepage, monkeypatch):
    app, organizations = homepage
    organizations[1].lat = organizations[0].lat
    organizations[1].lon = organizations[0].lon
    monkeypatch.setattr(
        overview.st,
        "pydeck_chart",
        Mock(return_value={"selection": {"objects": {"theaters": [{"id": str(organizations[0].id)}]}}}),
    )
    link = Mock()
    monkeypatch.setattr(overview.st, "page_link", link)
    app.run()
    assert not app.exception
    assert {call.kwargs["query_params"]["id"] for call in link.call_args_list} == {
        str(organizations[0].id),
        str(organizations[1].id),
    }
