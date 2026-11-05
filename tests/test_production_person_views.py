from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import entity_management
from backend.models import Organization, Person, Production, ProductionMembership
from frontend.views import organization, person, production
from frontend.views.helpers import entity_link, production_people

PRODUCTION_SCRIPT = """
import streamlit as st
from frontend.views.production import render_production
render_production(st.session_state.profile)
"""
PERSON_SCRIPT = """
import streamlit as st
from frontend.views.person import render_person
render_person(st.session_state.profile)
"""


class LinkParser(HTMLParser):
    """Read the rendered anchor without depending on attribute order."""

    def handle_starttag(self, tag, attrs) -> None:
        """Capture link attributes."""
        if tag == "a":
            self.attributes = dict(attrs)


def test_entity_link_replaces_query_and_escapes_source_text(monkeypatch) -> None:
    markdown = Mock()
    monkeypatch.setattr("frontend.views.helpers.st.markdown", markdown)
    entity_id = str(UUID(int=1))
    entity_link('<script>alert("name")</script> & Bühne', "production", entity_id)
    html = markdown.call_args.args[0]
    parser = LinkParser()
    parser.feed(html)
    assert parser.attributes["target"] == "_self"
    assert parse_qs(urlsplit(parser.attributes["href"]).query) == {"kind": ["production"], "id": [entity_id]}
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.fixture
def credits(monkeypatch) -> tuple[dict, dict, dict, Mock]:
    alice = {"id": "alice", "name": "Alex"}
    bob = {"id": "bob", "name": "Alex"}
    theatre: dict[str, str | list[dict[str, str]]] = {"id": "theatre", "name": "Theater"}
    current = {
        "id": "current",
        "name": "Heute",
        "organization_id": "theatre",
        "organization": theatre,
        "premiere": "2026-01-03T22:00:00+02:00",
        "duration": 90,
        "age_rating": 0,
        "description": "Eine Produktion",
        "memberships": [
            {"person": alice, "role": "Regie"},
            {"person": alice, "role": "Schauspiel", "character": "Hamlet"},
            {"person": bob, "role": "Musik"},
            {"person": None},
        ],
    }
    other = {
        "id": "other",
        "name": "Gestern",
        "organization_id": "theatre",
        "organization": {"id": "theatre", "name": "Theater"},
        "memberships": [{"person": alice}, {"person": bob}, {"person": bob}],
    }
    away = {
        "id": "away",
        "name": "Anderswo",
        "organization": {"id": "away-theatre", "name": "Anderes Theater"},
        "organization_id": "away-theatre",
        "memberships": [{"person": alice}],
    }
    theatre["productions"] = [
        {"id": "current", "name": "Heute"},
        {"id": "other", "name": "Gestern"},
        {"id": "no-cast", "name": "Ohne Besetzung"},
    ]
    fetch = Mock(return_value=[current, other, away])
    monkeypatch.setattr(production, "get_people_productions", fetch)
    monkeypatch.setattr(person, "get_people_productions", fetch)
    monkeypatch.setattr(organization, "list_entities", lambda model: [])
    return current, other, away, fetch


def test_production_profile_preserves_details_and_counts_other_credits(credits) -> None:
    current, _, _, fetch = credits
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    fetch.assert_called_once_with(["alice", "bob"])
    assert app.title[0].value == "Heute"
    text = [element.value for element in app.text]
    assert all(value in text for value in ["03.01.2026", "90 Minuten", "Ab 0 Jahren"])
    assert [tab.label for tab in app.tabs] == ["Besetzung & Team", "Verbindungen", "Quellen & Nachweise"]
    assert any(element.value == "Regie / Schauspiel · Hamlet" for element in app.text)
    assert sum("Weitere Produktionen an diesem Theater: 1" in element.value for element in app.caption) == 2
    assert sum("Weitere erfasste Produktionen: 2" in element.value for element in app.caption) == 2
    html = "\n".join(element.value for element in app.markdown)
    assert "?kind=production&amp;id=current" not in html
    assert "?kind=production&amp;id=no-cast" in html
    assert "?kind=person&amp;id=alice" in html


def test_empty_production_and_invalid_data_are_explicit(credits) -> None:
    *_, fetch = credits
    fetch.return_value = []
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = {
        "id": "empty",
        "name": "Leer",
        "premiere": "invalid",
        "url": "javascript:alert(1)",
    }
    app.run()
    assert not app.exception
    assert sum(element.value == "Nicht hinterlegt" for element in app.text) == 3
    assert any("Ungültiges Premierendatum" in element.value for element in app.warning)
    assert any("Kein gültiger HTTP(S)-Link" in element.value for element in app.warning)
    assert any("keine Mitwirkenden" in element.value for element in app.info)
    assert any("kein Theater" in element.value for element in app.info)
    assert not app.get("link_button")


def test_production_preserves_sources_tags_and_critics(credits) -> None:
    current, *_ = credits
    current["organization"]["url"] = "https://theatre.example"
    current.update(
        tags=["Drama"],
        critics=["Eine Kritik"],
        url="https://theatre.example/production",
        sources=[{"title": "Archiv", "external_url": "https://archive.example/production"}],
    )
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    assert any(element.value == "Drama" for element in app.text)
    assert any(element.value == "Eine Kritik" for element in app.markdown)
    assert any(element.value == "Archiv" for element in app.text)
    assert {button.proto.url for button in app.get("link_button")} == {
        "https://theatre.example",
        "https://theatre.example/production",
        "https://archive.example/production",
    }


def test_collaborations_count_unique_productions_and_people_not_roles(credits) -> None:
    current, other, away, _ = credits
    theatres, collaborators = person.collaboration_groups([current, other, other, away], "alice")
    assert len(theatres["theatre"]["productions"]) == 2
    assert len(theatres["away-theatre"]["productions"]) == 1
    assert set(collaborators) == {"bob"}
    assert len(collaborators["bob"]["productions"]) == 2
    assert set(production_people(current)) == {"alice", "bob"}


def test_person_profile_has_linked_collaborations_and_biography(credits) -> None:
    app = AppTest.from_string(PERSON_SCRIPT)
    app.session_state.profile = {
        "id": "alice",
        "name": "Alex",
        "born": "1980-03-02",
        "type": ["Regie"],
        "nationality": "Österreich",
        "urls": ["https://person.example"],
    }
    app.run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["3", "2", "1"]
    assert any(element.value == "Geboren: 02.03.1980" for element in app.caption)
    assert sum("Gemeinsame Produktionen: 2" in element.value for element in app.caption) == 2
    html = "\n".join(element.value for element in app.markdown)
    assert "?kind=person&amp;id=bob" in html
    assert "?kind=person&amp;id=alice" not in html
    assert "?kind=production&amp;id=other" in html


def test_empty_person_profile(credits) -> None:
    *_, fetch = credits
    fetch.return_value = []
    app = AppTest.from_string(PERSON_SCRIPT)
    app.session_state.profile = {"id": "alice", "name": "Alex", "born": "bad-date"}
    app.run()
    assert not app.exception
    assert all(metric.value == "0" for metric in app.metric)
    assert any("keine Produktionen" in element.value for element in app.info)
    assert any("Ungültiges Datum" in element.value for element in app.warning)


def test_people_productions_query_is_scoped_distinct_and_detached(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    alice, bob = Person(name="Alex"), Person(name="Alex")
    theatre = Organization(name="Theater")
    first = Production(name="Erste", organization=theatre)
    second = Production(name="Zweite")
    unrelated = Production(name="Unbeteiligt")
    with Session(engine) as session:
        session.add_all([alice, bob, first, second, unrelated])
        session.add_all(
            [
                ProductionMembership(person=alice, production=first, role="Regie"),
                ProductionMembership(person=alice, production=first, role="Schauspiel"),
                ProductionMembership(person=bob, production=first),
                ProductionMembership(person=bob, production=second),
            ]
        )
        session.commit()
        alice_id, bob_id = str(alice.id), str(bob.id)
        first_id, second_id = str(first.id), str(second.id)
    monkeypatch.setattr(entity_management, "get_session", lambda: Session(engine))
    rows = entity_management.get_people_productions([alice_id, bob_id, alice_id])
    assert [row["id"] for row in rows] == [first_id, second_id]
    assert rows[0]["organization"]["name"] == "Theater"
    assert set(production_people(rows[0])) == {alice_id, bob_id}
    assert len(rows[0]["memberships"]) == 3
    assert rows[1]["organization"] is None
    assert [row["id"] for row in entity_management.get_people_productions([alice_id])] == [first_id]
    assert entity_management.get_people_productions([str(UUID(int=0))]) == []
    assert entity_management.get_people_productions([]) == []
    assert entity_management.get_entity_graph(Production, first_id, max_depth=0)["id"] == first_id
    with pytest.raises(ValueError):
        entity_management.get_people_productions(["not-a-uuid"])
    with pytest.raises(ValueError):
        entity_management.get_entity_graph(Production, "not-a-uuid")
    engine.dispose()


def test_missing_detail_stops_before_render(monkeypatch) -> None:
    monkeypatch.setattr(entity_management, "get_entity_graph", Mock(side_effect=ValueError("not found")))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "frontend" / "views" / "detail.py")
    app.query_params.update(kind="production", id=str(UUID(int=1)))
    app.run()
    assert not app.exception
    assert app.error[0].value == "Not found"
    assert not app.title
