import json
import math
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.data_quality import apply_quality_action
from backend.models import Organization, Person, Production
from frontend.views import overview

OVERVIEW_SCRIPT = "from frontend.views.overview import render_overview\nrender_overview()"
APP = Path(__file__).resolve().parents[1] / "frontend" / "app.py"
HomepageFixture = tuple[AppTest, list[Organization]]


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
def test_coordinate_validation(lat: float | None, lon: float | None, expected: bool) -> None:
    assert overview.has_coordinates(lat, lon) is expected


def test_directory_preserves_missing_coordinates_and_links() -> None:
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
def test_map_supports_single_shared_and_multiple_locations(positions: list[tuple[float, float]]) -> None:
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
def homepage(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[HomepageFixture]:
    organizations = [
        Organization(name="Bühne [Mitte]", city="Berlin", country="Deutschland", lat=52.5, lon=13.4),
        Organization(name="Burgtheater", city="Wien", country="Österreich", lat=48.2, lon=16.4),
        Organization(name="Ohne Position", city="Berlin", country="Deutschland"),
    ]

    engine = create_engine(f"sqlite:///{tmp_path / 'homepage.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(organizations)
        session.commit()
    monkeypatch.setattr(
        overview,
        "count_entities_many",
        lambda *models: {model: {Production: 1234, Person: 27}.get(model, 0) for model in models},
    )
    app = AppTest.from_string(OVERVIEW_SCRIPT)
    app.query_params["verified_only"] = "0"
    yield app, organizations
    engine.dispose()


def test_homepage_metrics_and_literal_search(homepage: HomepageFixture) -> None:
    app, _ = homepage
    app.run()
    assert not app.exception
    assert app.text_input[0].proto.live_debounce_ms == 500
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
    app.text_input[0].set_value("ÖSTERREICH").run()
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


def test_selected_marker_has_internal_detail_link(homepage: HomepageFixture, monkeypatch: pytest.MonkeyPatch) -> None:
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
        query_params={"kind": "organization", "id": selected_id, "verified_only": "0"},
    )
    assert chart.call_args.kwargs["on_select"] == "rerun"
    link.reset_mock()
    app.text_input[0].set_value("Wien").run()
    assert not app.exception
    link.assert_not_called()


def test_empty_homepage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(overview, "list_matching_entities", lambda *args, **kwargs: [])
    monkeypatch.setattr(overview, "organization_coverage", lambda **kwargs: (0, 0, 0, []))
    monkeypatch.setattr(overview, "count_entities_many", lambda *models: dict.fromkeys(models, 0))
    app = AppTest.from_string(OVERVIEW_SCRIPT).run()
    assert not app.exception
    assert all(metric.value == "0" for metric in app.metric)
    assert not app.dataframe
    assert any("Keine geprüften Theater gefunden" in message.value for message in app.info)


def test_homepage_map_and_directory_are_unpaginated(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'overview.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    with Session(engine) as session:
        session.add_all(
            [Organization(name=f"Alpha {i:03}", city="Berlin", lat=52.5, lon=13.4) for i in range(60)]
            + [
                Organization(name="Ohne Position", city="Berlin"),
                Organization(name="Zeta", city="Wien [AT]", lat=48.2, lon=16.4),
            ]
        )
        session.commit()
    app = AppTest.from_string(OVERVIEW_SCRIPT)
    app.query_params["verified_only"] = "0"
    app.run()
    assert not app.exception
    assert not app.pagination
    names = list(app.dataframe[0].value["name"])
    assert len(names) == 62
    assert app.metric[0].value == "62"
    assert app.metric[1].value == "2"
    layer = json.loads(app.get("deck_gl_json_chart")[0].proto.json)["layers"][0]
    assert {row["name"] for row in layer["data"]} == set(names) - {"Ohne Position"}
    app.text_input[0].set_value("[at]").run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Zeta"]
    layer = json.loads(app.get("deck_gl_json_chart")[0].proto.json)["layers"][0]
    assert [row["name"] for row in layer["data"]] == ["Zeta"]
    engine.dispose()


def test_shared_map_position_links_every_theater(homepage: HomepageFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app, organizations = homepage
    with user_management.get_session() as session:
        theatre = session.get(Organization, organizations[1].id)
        assert theatre is not None
        theatre.lat, theatre.lon = organizations[0].lat, organizations[0].lon
        session.commit()
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


def test_quality_filter_keeps_map_directory_and_missing_positions_consistent(
    homepage: HomepageFixture,
) -> None:
    _, organizations = homepage
    for index in (0, 2):
        apply_quality_action(organizations[index].id, "verify_general", actor_id="editor", evidence="Source")
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert len(app.toggle) == 2
    assert all(toggle.value for toggle in app.toggle)
    assert app.metric[0].value == "2"
    assert set(app.dataframe[0].value["name"]) == {"Bühne [Mitte]", "Ohne Position"}
    assert list(app.dataframe[0].value.columns) == ["name", "city", "country", "detail"]
    assert not app.get("badge")
    layer = json.loads(app.get("deck_gl_json_chart")[0].proto.json)["layers"][0]
    assert layer["getFillColor"] == [139, 58, 50, 200]
    assert [row["name"] for row in layer["data"]] == ["Bühne [Mitte]"]
    app.text_input[0].set_value("Wien").run()
    assert not app.exception
    assert not app.dataframe


def test_toggles_sync_and_carry_filter_to_public_directory(homepage: HomepageFixture) -> None:
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert all(toggle.value for toggle in app.toggle)
    assert not app.dataframe
    app.toggle(key="_map_verified_only").set_value(False).run()
    assert not app.exception
    assert not app.toggle(key="_sidebar_verified_only").value
    assert len(app.dataframe[0].value) == 3
    assert all("verified_only=0" in link for link in app.dataframe[0].value["detail"])
    app.switch_page("views/detail.py").run()
    assert not app.exception
    assert not app.toggle(key="_sidebar_verified_only").value
    assert any("Burgtheater" in element.value for element in app.markdown)
    app.toggle(key="_sidebar_verified_only").set_value(True).run()
    assert any("Keine geprüften Theater" in message.value for message in app.info)
    app.switch_page("views/overview.py").run()
    assert not app.exception
    assert all(toggle.value for toggle in app.toggle)
    assert not app.dataframe
    app.toggle(key="_sidebar_verified_only").set_value(False).run()
    assert not app.toggle(key="_map_verified_only").value
