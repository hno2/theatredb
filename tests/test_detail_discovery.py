from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import entity_management
from backend.data_quality import apply_quality_action
from backend.models import Organization, Person, Production
from frontend.views import discovery, organization

DETAIL_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "views" / "detail.py"


def discovery_page(model, query: str = "", *, limit: int = 12, offset: int = 0, verified_only: bool = False):
    return entity_management.catalog_page(
        model,
        query,
        [entity_management.verified_organization_filter()] if verified_only else [],
        limit=limit,
        offset=offset,
        sort_by="premiere" if model is Production else "name",
        descending=model is Production,
    )


@pytest.fixture
def directory(monkeypatch) -> Mock:
    page = Mock(
        return_value=(
            [{"id": "theatre", "name": "Bühne", "address_locality": "Wien", "address_country": "Österreich"}],
            1,
        )
    )
    monkeypatch.setattr(discovery, "catalog_page", page)
    return page


def assert_page_call(page: Mock, model, query: str, offset: int, *, verified: bool = False) -> None:
    args, kwargs = page.call_args
    assert args[:2] == (model, query)
    assert len(args[2]) == int(verified)
    assert kwargs == {
        "limit": 12,
        "offset": offset,
        "sort_by": "premiere" if model is Production else "name",
        "descending": model is Production,
    }


def test_detail_entry_shows_links_without_dropdowns_or_open_button(directory) -> None:
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    assert app.text_input[0].proto.live_debounce_ms == 500
    assert_page_call(directory, Organization, "", 0, verified=True)
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
    assert 'href="produktionen" target="_self"' in app.markdown[0].value


def test_production_directory_has_direct_links_and_theatre_context(directory) -> None:
    directory.return_value = (
        [
            {
                "id": "production",
                "name": "Hamlet",
                "premiere": "2026-03-04",
                "organization": {"id": "theatre", "name": "Bühne", "address_locality": "Wien"},
            },
        ],
        1,
    )
    app = AppTest.from_file(DETAIL_PAGE)
    app.query_params["kind"] = "production"
    app.run()
    assert not app.exception
    assert_page_call(directory, Production, "", 0)
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
    assert_page_call(directory, Organization, "", 24, verified=True)
    assert [widget.value for widget in app.pagination] == [3, 3]
    app.pagination[1].set_value(2).run()
    assert_page_call(directory, Organization, "", 12, verified=True)
    assert [widget.value for widget in app.pagination] == [2, 2]
    app.pagination[1].set_value(3).run()
    assert_page_call(directory, Organization, "", 24, verified=True)
    app.text_input[0].set_value("  Wien  ").run()
    assert_page_call(directory, Organization, "Wien", 0, verified=True)
    assert [widget.value for widget in app.pagination] == [1, 1]
    app.pagination[0].set_value(3).run()
    directory.return_value = (directory.return_value[0], 1)
    app.run()
    assert not app.exception
    assert_page_call(directory, Organization, "Wien", 0, verified=True)
    assert not app.pagination


def test_large_directory_keeps_pagination_bounded_and_last_page_reachable(directory) -> None:
    directory.return_value = (directory.return_value[0], 1200)
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    assert not app.button
    assert all(widget.num_pages == 100 for widget in app.pagination)
    assert all(widget.proto.max_visible_pages == 7 for widget in app.pagination)
    app.pagination[0].set_value(100).run()
    assert_page_call(directory, Organization, "", 1188, verified=True)
    assert [widget.value for widget in app.pagination] == [100, 100]
    app.pagination[0].set_value(99).run()
    assert_page_call(directory, Organization, "", 1176, verified=True)
    app.pagination[1].set_value(1).run()
    assert_page_call(directory, Organization, "", 0, verified=True)
    assert [widget.value for widget in app.pagination] == [1, 1]


def test_empty_directory_and_search_are_explicit(directory) -> None:
    directory.return_value = ([], 0)
    app = AppTest.from_file(DETAIL_PAGE).run()
    assert not app.exception
    assert "Keine geprüften Theater gefunden" in app.info[0].value
    app.text_input[0].set_value("Hamlet").run()
    assert "Ändere die Suche" in app.info[0].value
    app.radio[0].set_value("person").run()
    assert_page_call(directory, Person, "", 0)


def test_direct_theatre_link_respects_filter_without_exposing_quality(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'profiles.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("backend.user_management._get_engine", lambda: engine)
    with Session(engine, expire_on_commit=False) as session:
        theatres = [Organization(name=name) for name in ("Unverified", "Verified", "General update")]
        session.add_all(theatres)
        session.commit()
    unchecked, checked, flagged = theatres
    for theatre, scope in ((checked, "productions"), (flagged, "general")):
        apply_quality_action(theatre.id, "verify_general", actor_id="editor", evidence="Audit citation")
        apply_quality_action(
            theatre.id, "flag_update", actor_id="editor", update_scope=scope, update_reason="Audit note"
        )
    try:
        rows, total = discovery_page(Organization, limit=1, verified_only=True)
        assert total == 1
        assert [row["name"] for row in rows] == [checked.name]
        for theatre in (unchecked, flagged, checked):
            app = AppTest.from_file(DETAIL_PAGE)
            app.query_params.update(kind="organization", id=str(theatre.id))
            app.run()
            assert not app.exception
            if theatre is checked:
                assert app.title[0].value == checked.name
                assert not any(item.value == "Datenqualität" for item in app.subheader)
            else:
                assert not app.title
                assert "Kein passendes Theater" in app.info[0].value
        app = AppTest.from_file(DETAIL_PAGE)
        app.query_params.update(kind="organization", id=str(unchecked.id), verified_only="0")
        app.run()
        assert not app.exception
        assert app.title[0].value == unchecked.name
        assert 'href="theater?verified_only=0"' in app.markdown[0].value
    finally:
        organization.load_organization_profile.clear()
        engine.dispose()


def test_catalog_search_is_bounded_searchable_literal_and_detached(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        theatre = Organization(name="Bühne", address_locality="Wien", address_country="Österreich")
        other = Organization(name="Other", address_locality="Berlin")
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
    rows, total = discovery_page(Production, "wien", limit=1)
    assert total == 2
    assert [row["id"] for row in rows] == [newest_id]
    assert rows[0]["organization"]["address_locality"] == "Wien"
    rows, total = discovery_page(Production, "Bühne", limit=1, offset=1)
    assert total == 2
    assert [row["name"] for row in rows] == ["Old"]
    rows, total = discovery_page(Production, "%_")
    assert total == 1
    assert rows[0]["name"] == "100%_literal"
    assert rows[0]["organization"] is None
    assert discovery_page(Organization, "wien")[1] == 1
    assert discovery_page(Organization, "Österreich")[1] == 1
    rows, total = discovery_page(Person, "Alex")
    assert total == 2
    assert len({row["id"] for row in rows}) == 2
    assert discovery_page(Production, "missing") == ([], 0)
    assert discovery_page(Production, offset=100) == ([], 4)
    for limit, offset in ((0, 0), (101, 0), (12, -1)):
        with pytest.raises(ValueError, match="bounds"):
            discovery_page(Production, limit=limit, offset=offset)
    engine.dispose()
