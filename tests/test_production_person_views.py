import json
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pyarrow as pa
import pytest
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import entity_management
from backend.models import Organization, Person, Production, ProductionMembership, Text, TextContribution
from frontend.views import organization, person, production
from frontend.views.helpers import entity_link, production_people, role_label

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


@pytest.mark.parametrize("role", ["Vermittlung", "VERMITTLUNG", "theatre_educator"])
def test_educator_role_label(role: str) -> None:
    assert role_label(role) == "Theaterpädagogik"


def test_unknown_role_label_preserves_source_text() -> None:
    assert role_label("Example character") == "Example character"
    assert not role_label(None)


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
        "memberships": [{"person": alice, "role": "director"}, {"person": bob}, {"person": bob}],
    }
    away = {
        "id": "away",
        "name": "Anderswo",
        "organization": {"id": "away-theatre", "name": "Anderes Theater"},
        "organization_id": "away-theatre",
        "memberships": [{"person": alice, "role": "Schauspiel"}],
    }
    theatre["productions"] = [
        {"id": "current", "name": "Heute"},
        {"id": "other", "name": "Gestern"},
        {"id": "no-cast", "name": "Ohne Besetzung"},
    ]
    fetch = Mock(return_value=[current, other, away])
    monkeypatch.setattr(production, "get_people_productions", fetch)
    monkeypatch.setattr(person, "get_people_productions", fetch)
    monkeypatch.setattr(organization, "entities_by_ids", lambda model, ids: [])
    return current, other, away, fetch


def test_production_profile_shows_theatre_navigation_facts_and_cast_without_tabs(credits) -> None:
    current, _, _, fetch = credits
    current["organization"]["city"] = "Wien"
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    fetch.assert_called_once_with(["alice"])
    assert app.title[0].value == "Heute"
    assert not app.tabs
    assert not any("weitere Produktion" in element.label for element in app.expander)
    back = app.markdown[0].value
    assert "← Theater" in back
    assert "Wien" not in back
    assert "?kind=organization&amp;id=theatre&amp;verified_only=0" in back
    html = "\n".join(element.value for element in app.markdown)
    assert "Premiere: 03.01.2026 · 90 Minuten · Ab 0 Jahren" in html
    assert "<strong>Regie, Schauspiel (Hamlet)</strong>" in html
    assert "Eine Produktion" in html
    assert "Gleiche Textgrundlage" not in html
    assert "?kind=production&amp;id=current" not in html
    assert "?kind=production&amp;id=away" not in html
    assert "?kind=production&amp;id=no-cast" in html
    assert "?kind=person&amp;id=alice" in html
    assert json.loads(app.json[0].value) == current


def test_empty_production_omits_empty_sections_but_flags_invalid_data(credits) -> None:
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
    fetch.assert_not_called()
    assert not app.markdown
    assert not app.header and not app.subheader
    assert not app.metric
    assert any("Ungültiges Premierendatum" in element.value for element in app.warning)
    assert any("Kein gültiger HTTP(S)-Link" in element.value for element in app.warning)
    assert not app.get("link_button")


def test_year_only_premiere_and_legacy_theapolis_links_are_not_official(credits) -> None:
    current, *_ = credits
    current.update(premiere=None, premiere_year=2026, url="https://www2.theapolis.de/de/produktionen/123")
    current["organization"]["url"] = "https://www.theapolis.de/de/organization/theater"
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    html = "\n".join(element.value for element in app.markdown)
    assert "Premiere: 2026" in html
    assert "01.01.2026" not in html
    assert html.count(">Theapolis-Quelle</a>") == 2
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
    assert any(element.value == "Drama" for element in app.caption)
    assert any(element.value == "Eine Kritik" for element in app.markdown)
    assert app.subheader[-1].value == "Quellen & Nachweise"
    assert app.markdown[-1].value == (
        '<p><a href="https://archive.example/production" target="_blank" rel="noopener noreferrer">'
        "Archiv</a> · archive.example · Noch nicht geprüft</p>"
    )
    assert {button.proto.url for button in app.get("link_button")} == {
        "https://theatre.example",
        "https://theatre.example/production",
    }


def test_related_production_previews_are_distinct_sorted_and_limited(credits) -> None:
    current, _, away, fetch = credits
    entries = [
        {
            "id": f"entry-{year}",
            "name": f"Stück {year}",
            "premiere": str(year) if year % 2 else None,
            "premiere_year": year if year % 2 == 0 else None,
            "organization": {"city": "Wien", "name": "Bühne am Fluss"},
            "memberships": [{"person_id": "alice", "role": " Regie "}],
        }
        for year in (2018, *range(2020, 2026))
    ]
    candidates = [*entries, entries[-1], {"id": "current", "name": "Heute"}, {"id": "undated", "name": "Unbekannt"}]
    current["text"] = {"title": "Die Vorlage", "productions": candidates}
    current["organization"]["productions"] = candidates
    fetch.return_value = [*candidates, current, away]
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    overflow = next(element for element in app.expander if element.label == "3 weitere Produktionen")
    assert not overflow.proto.expanded
    overflow_rows = overflow.markdown[0].value
    assert overflow_rows.count('target="_self"') == 3
    assert [overflow_rows.index(f"id={entry}") for entry in ("entry-2020", "entry-2018", "undated")] == sorted(
        overflow_rows.index(f"id={entry}") for entry in ("entry-2020", "entry-2018", "undated")
    )
    rows = [
        element.value for element in app.markdown if "id=entry-" in element.value and element.value != overflow_rows
    ]
    assert len(rows) == 3
    for row in rows:
        assert row.count('target="_self"') == 5
        indices = [row.index(f"id=entry-{year}") for year in range(2025, 2020, -1)]
        assert indices == sorted(indices)
    assert ">Stück 2025 · Wien · 2025</a>" in rows[0]
    assert ">Stück 2025 · 2025</a>" in rows[1]
    assert ">Stück 2025 · Bühne am Fluss · 2025</a>" in rows[2]
    footers = "\n".join(element.value for element in app.caption)
    assert 'id=theatre&amp;verified_only=0" target="_self">Mehr vom Theater →</a>' in footers
    assert 'id=alice" target="_self">Mehr von Alex →</a>' in footers
    assert sum(caption.value == "8 erfasste Premieren" for caption in app.caption) == 2
    assert not any(heading.value == "Premieren im Zeitverlauf" for heading in app.subheader)
    charts = app.get("vega_lite_chart")
    assert len(charts) == 2
    for chart in charts:
        spec = json.loads(chart.proto.spec)
        encoding = spec["encoding"]
        assert (encoding["x"]["field"], encoding["x"]["title"]) == ("year", "Premierenjahr")
        assert (encoding["y"]["field"], encoding["y"]["title"]) == ("count", "Premieren")
        assert [tooltip["field"] for tooltip in encoding["tooltip"]] == ["year", "count", "titles"]
        dataset = next(dataset for dataset in chart.proto.datasets if dataset.name == spec["data"]["name"])
        assert pa.ipc.open_stream(dataset.data.data).read_all().to_pylist() == [
            {
                "year": year,
                "count": int(year != 2019),
                "titles": "—" if year == 2019 else ("Heute" if year == 2026 else f"Stück {year}"),
            }
            for year in range(2018, 2027)
        ]


def test_related_text_chart_groups_publications_and_lists_titles(credits) -> None:
    current, *_ = credits
    version = {"id": "version", "title": "Bearbeitung", "published": "2021"}
    current["text"] = {
        "title": "Vorlage",
        "lineages_as_source": [
            {"relation": "adaptation", "derived_text": version},
            {"relation": "translation", "derived_text": version},
            {"relation": "translation", "derived_text": {"id": "undated", "title": "Ohne Datum"}},
            {"relation": "based_on", "derived_text": {"id": "excluded", "title": "Nicht enthalten"}},
        ],
        "lineages_as_derived": [
            {"relation": "translation", "source_text": {"id": "source", "title": "Original", "published": "2021"}},
        ],
    }
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    chart = app.get("vega_lite_chart")[-1]
    spec = json.loads(chart.proto.spec)
    assert spec["encoding"]["x"]["title"] == "Erscheinungsjahr"
    dataset = next(dataset for dataset in chart.proto.datasets if dataset.name == spec["data"]["name"])
    assert pa.ipc.open_stream(dataset.data.data).read_all().to_pylist() == [
        {"year": 2021, "count": 2, "titles": "Bearbeitung · Original"}
    ]
    html = "\n".join(element.value for element in app.markdown)
    assert "Ohne Datum · Erscheinungsjahr unbekannt" in html
    assert "Nicht enthalten" not in html


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


@pytest.mark.parametrize(("title", "lead"), [("Die Vorlage", "Nach „Die Vorlage“ von"), ("Heute", "Von")])
def test_production_shows_text_credits_inline(credits, title: str, lead: str) -> None:
    current, *_ = credits
    current["memberships"][0]["role"] = "director"
    current["text"] = {
        "title": title,
        "language": "de",
        "published_year": 1920,
        "contributions": [
            {"person": {"id": "author", "name": "Autorin"}, "role": "author"},
            {"person": {"id": "translator", "name": "Übersetzer"}, "role": "translator"},
            {"person": None, "role": "adapter"},
        ],
    }
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    assert not any(element.value == "Textgrundlage" for element in app.subheader)
    credit = app.caption[0]
    assert credit.value.startswith(f"<p>{lead} <a")
    assert "Übersetzung:" in credit.value
    assert "?kind=person&amp;id=author" in credit.value
    assert "?kind=person&amp;id=translator" in credit.value
    assert credit.proto.help == "Sprache: de\n\nErschienen: 1920\n\nBearbeitung: Person nicht verknüpft"


def test_production_combines_text_and_cast_credits_by_person(credits) -> None:
    current, *_ = credits
    alice = current["memberships"][0]["person"]
    current["memberships"].append({"person": alice, "role": "Regie"})
    current["text"] = {
        "title": "Die Vorlage",
        "contributions": [
            {"person": alice, "role": "author"},
            {"person": {"id": "translator", "name": "Alex"}, "role": "translator"},
        ],
    }
    app = AppTest.from_string(PRODUCTION_SCRIPT)
    app.session_state.profile = current
    app.run()
    assert not app.exception
    cast = next(element.value for element in app.markdown if "<strong>Text, Regie" in element.value)
    assert "<strong>Text, Regie, Schauspiel (Hamlet)</strong>:" in cast
    assert cast.count("id=alice") == 1
    assert "<strong>Musik</strong>:" in cast and "id=bob" in cast
    assert "Nach „Die Vorlage“" in app.caption[0].value
    assert "id=alice" not in app.caption[0].value
    assert "id=translator" in app.caption[0].value


def test_production_graph_includes_text_contributor_names(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        author = Person(name="Autorin")
        text = Text(title="Vorlage", contributions=[TextContribution(person=author, role="author")])
        entry = Production(name="Produktion", text=text)
        session.add(entry)
        session.commit()
        entry_id = str(entry.id)
    monkeypatch.setattr(entity_management, "get_session", lambda: Session(engine))
    graph = entity_management.get_entity_graph(Production, entry_id)
    assert graph["text"]["contributions"][0]["person"]["name"] == "Autorin"
    engine.dispose()
