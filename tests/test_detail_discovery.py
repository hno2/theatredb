from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import entity_management
from backend.models import Organization, Person, Production
from frontend.views import discovery

DETAIL_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "views" / "detail.py"


@pytest.fixture
def directory(monkeypatch) -> Mock:
    browse = Mock(
        return_value=(
            [{"id": "theatre", "name": "Bühne", "city": "Wien", "country": "Österreich"}],
            1,
        )
    )
    monkeypatch.setattr(discovery, "browse_entities", browse)
    return browse


def test_detail_entry_shows_links_without_dropdowns_or_open_button(directory) -> None:
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    directory.assert_called_once_with(Organization, "", limit=12, offset=0)
    assert not app.selectbox
    assert not app.button
    assert any("?kind=organization&amp;id=theatre" in element.value for element in app.markdown)
    assert any(element.value == "Wien · Österreich" for element in app.text)


def test_missing_detail_keeps_same_tab_directory_link(monkeypatch) -> None:
    monkeypatch.setattr(entity_management, "get_entity_graph", Mock(side_effect=ValueError("not found")))
    app = AppTest.from_file(DETAIL_PAGE)
    app.query_params.update(kind="production", id="missing")
    app.run()
    assert not app.exception
    assert app.error[0].value == "Not found"
    assert 'href="detail" target="_self"' in app.markdown[0].value


def test_production_directory_has_direct_links_and_theatre_context(directory) -> None:
    directory.return_value = (
        [
            {
                "id": "production",
                "name": "Hamlet",
                "premiere": "2026-03-04",
                "organization": {"id": "theatre", "name": "Bühne", "city": "Wien"},
            },
        ],
        1,
    )
    app = AppTest.from_file(DETAIL_PAGE)
    app.query_params["kind"] = "production"
    app.run()
    assert not app.exception
    directory.assert_called_once_with(Production, "", limit=12, offset=0)
    html = "\n".join(element.value for element in app.markdown)
    assert "?kind=production&amp;id=production" in html
    assert "?kind=organization&amp;id=theatre" in html
    assert any(element.value == "Premiere: 04.03.2026" for element in app.caption)


def test_directory_search_pagination_and_changed_search_reset(directory) -> None:
    directory.return_value = (directory.return_value[0], 25)
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.number_input
    assert not app.button
    assert [widget.value for widget in app.pagination] == [1, 1]
    app.pagination[0].set_value(3).run()
    assert not app.exception
    directory.assert_called_with(Organization, "", limit=12, offset=24)
    assert [widget.value for widget in app.pagination] == [3, 3]
    app.pagination[1].set_value(2).run()
    directory.assert_called_with(Organization, "", limit=12, offset=12)
    assert [widget.value for widget in app.pagination] == [2, 2]
    app.pagination[1].set_value(3).run()
    directory.assert_called_with(Organization, "", limit=12, offset=24)
    app.text_input[0].set_value("  Wien  ").run()
    directory.assert_called_with(Organization, "Wien", limit=12, offset=0)
    assert [widget.value for widget in app.pagination] == [1, 1]
    app.pagination[0].set_value(3).run()
    directory.return_value = (directory.return_value[0], 1)
    app.run()
    assert not app.exception
    directory.assert_called_with(Organization, "Wien", limit=12, offset=0)
    assert not app.pagination


def test_large_directory_keeps_pagination_bounded_and_last_page_reachable(directory) -> None:
    directory.return_value = (directory.return_value[0], 1200)
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    assert not app.button
    assert all(widget.num_pages == 100 for widget in app.pagination)
    assert all(widget.proto.max_visible_pages == 7 for widget in app.pagination)
    app.pagination[0].set_value(100).run()
    directory.assert_called_with(Organization, "", limit=12, offset=1188)
    assert [widget.value for widget in app.pagination] == [100, 100]
    app.pagination[0].set_value(99).run()
    directory.assert_called_with(Organization, "", limit=12, offset=1176)
    app.pagination[1].set_value(1).run()
    directory.assert_called_with(Organization, "", limit=12, offset=0)
    assert [widget.value for widget in app.pagination] == [1, 1]


def test_empty_directory_and_search_are_explicit(directory) -> None:
    directory.return_value = ([], 0)
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    assert app.info[0].value == "Noch keine Einträge erfasst."
    app.text_input[0].set_value("Hamlet").run()
    assert "Keine Einträge gefunden" in app.info[0].value
    app.radio[0].set_value("person").run()
    directory.assert_called_with(Person, "", limit=12, offset=0)


def test_browse_query_is_bounded_searchable_literal_and_detached(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        theatre = Organization(name="Bühne", city="Wien", country="Österreich")
        other = Organization(name="Other", city="Berlin")
        productions = [
            Production(name="Old", organization=theatre, premiere=datetime(2020, 1, 1, tzinfo=UTC)),
            Production(name="New", organization=theatre, premiere=datetime(2026, 1, 1, tzinfo=UTC)),
            Production(name="100%_literal"),
            Production(name="Elsewhere", organization=other),
        ]
        session.add_all([theatre, other, *productions, Person(name="Alex"), Person(name="Alex")])
        session.commit()
        newest_id = str(productions[1].id)
    monkeypatch.setattr(entity_management, "get_session", lambda: Session(engine))
    rows, total = entity_management.browse_entities(Production, "wien", limit=1)
    assert total == 2
    assert [row["id"] for row in rows] == [newest_id]
    assert rows[0]["organization"]["city"] == "Wien"
    rows, total = entity_management.browse_entities(Production, "Bühne", limit=1, offset=1)
    assert total == 2
    assert [row["name"] for row in rows] == ["Old"]
    rows, total = entity_management.browse_entities(Production, "%_")
    assert total == 1
    assert rows[0]["name"] == "100%_literal"
    assert rows[0]["organization"] is None
    assert entity_management.browse_entities(Organization, "wien")[1] == 1
    assert entity_management.browse_entities(Organization, "Österreich")[1] == 1
    rows, total = entity_management.browse_entities(Person, "Alex")
    assert total == 2
    assert len({row["id"] for row in rows}) == 2
    assert entity_management.browse_entities(Production, "missing") == ([], 0)
    assert entity_management.browse_entities(Production, offset=100) == ([], 4)
    for limit, offset in ((0, 0), (101, 0), (12, -1)):
        with pytest.raises(ValueError, match="bounds"):
            entity_management.browse_entities(Production, limit=limit, offset=offset)
    engine.dispose()
