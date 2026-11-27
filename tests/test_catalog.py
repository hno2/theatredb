from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend.entity_management import catalog_page, catalog_suggestions, catalog_summary, tag_filter
from backend.models import Organization, Person, Production, ProductionMembership, Text, TextContribution, TextTitle
from backend.taxonomy import set_production_tags, sync_vocabulary
from backend.user_management import get_session
from frontend.views import catalog
from frontend.views.helpers import theatre_label

VIEWS = Path(__file__).resolve().parents[1] / "frontend" / "views"


@pytest.fixture
def catalog_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("backend.user_management._get_engine", lambda: engine)
    theatre = Organization(name="Z Bühne", address_locality="Wien", address_country="Österreich", tags=["Oper"])
    person = Person(name="Z Person")
    text = Text(title="Z Text", genre="play")
    past = Production(name="Hamlet", premiere="2011-06-01", organization_id=theatre.id, text_id=text.id, tags=["Drama"])
    next_up = Production(name="Hamlet morgen", premiere="2026-11-21", organization_id=theatre.id, text_id=text.id)
    later = Production(
        name="Hamlet später",
        premiere="2026-11-30",
        organization_id=theatre.id,
        text_id=text.id,
        tags=["Schauspiel", "Drama", "Bühne"],
    )
    with Session(engine) as session:
        session.add_all(
            [
                theatre,
                Organization(name="A Bühne"),
                person,
                Person(name="A Person"),
                text,
                Text(title="A Text"),
                TextTitle(text_id=text.id, title="Variante"),
                past,
                next_up,
                later,
                Production(name="Ungefähr", premiere="2027"),
                Production(name="Ohne Datum"),
                ProductionMembership(person_id=person.id, production_id=past.id, role_name="director"),
                ProductionMembership(person_id=person.id, production_id=past.id, role_name="actor"),
            ]
        )
        session.commit()
    catalog._tag_options.clear()
    yield
    catalog._tag_options.clear()
    engine.dispose()


def test_catalog_ranks_before_pagination_and_counts_each_production_once(catalog_db):
    for model, count in ((Organization, 3), (Person, 1), (Text, 3)):
        first, total = catalog_page(model, "", [], limit=1)
        second, _ = catalog_page(model, "", [], limit=1, offset=1)
        assert total == 2
        assert first[0]["production_count"] == count
        assert second[0]["production_count"] == 0
    assert catalog_suggestions(Text, "Vari", []) == ["Variante", "Z Text"]


def test_text_detail_keeps_catalog_metadata_and_personal_actions(catalog_db):
    with get_session() as session:
        text = session.exec(select(Text).where(Text.title == "Z Text")).one()
        person = session.exec(select(Person).where(Person.name == "Z Person")).one()
        session.add(TextContribution(text_id=text.id, person_id=person.id, role="author"))
        session.commit()
        text_id, person_id = str(text.id), str(person.id)

    app = AppTest.from_file(VIEWS / "detail.py")
    app.query_params.update(kind="text", id=text_id)
    app.run()
    assert not app.exception
    assert app.title[0].value == "Z Text"
    assert "Schauspiel" in [element.value for element in app.text]
    assert "Variante" in [element.value for element in app.text]
    assert any("Anmelden, damit" in element.value for element in app.caption)
    assert any(f"detail?kind=person&amp;id={person_id}" in element.value for element in app.markdown)


def test_upcoming_is_nearest_first_and_excludes_imprecise_dates(catalog_db):
    rows, _ = catalog_page(Production, "", [], limit=12, upcoming_from=datetime(2026, 11, 20))
    assert [row["name"] for row in rows] == ["Hamlet morgen", "Hamlet später"]
    rows, _ = catalog_page(Production, "", [], limit=12)
    assert [row["name"] for row in rows] == [
        "Ungefähr",
        "Hamlet später",
        "Hamlet morgen",
        "Hamlet",
        "Ohne Datum",
    ]
    assert catalog_summary(Production, "Wien", []) == [("2011", 1), ("2026", 2)]


def test_catalog_loads_url_filters_and_applies_suggestions(catalog_db):
    app = AppTest.from_file(VIEWS / "catalog_productions.py")
    app.query_params.update(year_from="2012", q="Haml", exclude_planned="false")
    app.run()
    assert not app.exception
    assert app.number_input(key="year_from").value == 2012
    assert "Kommende Premieren" not in [heading.value for heading in app.subheader]
    assert app.pills[0].options == ["Hamlet morgen", "Hamlet später"]
    app.pills[0].set_value("Hamlet morgen").run()
    assert app.query_params["q"] == "Hamlet morgen"
    app.button(key="filter_production__reset").click().run()
    assert not app.exception
    assert "year_from" not in app.query_params


def test_catalog_combines_url_taxonomy_facets(catalog_db):
    with get_session() as session:
        sync_vocabulary(session)
        first = session.exec(select(Production).where(Production.name == "Hamlet")).one()
        second = session.exec(select(Production).where(Production.name == "Hamlet später")).one()
        set_production_tags(session, first.id, {"form.tanz", "premiere.ua"}, "editor")
        set_production_tags(session, second.id, {"form.tanz"}, "editor")
        session.commit()
    catalog._facet_fields.clear()
    try:
        app = AppTest.from_file(VIEWS / "catalog_productions.py")
        app.query_params.update(tag_form=["Tanz"], tag_premiere=["Uraufführung"], exclude_planned="false")
        app.run()
        assert not app.exception
        assert app.multiselect(key="tag_form").value == ["form.tanz"]
        assert app.multiselect(key="tag_premiere").value == ["premiere.ua"]
        results = [element.value for element in app.markdown if "detail?kind=production" in element.value]
        assert len(results) == 1
        assert "Hamlet" in results[0] and "Hamlet später" not in results[0]
        app.button(key="filter_production__reset").click().run()
        assert not app.exception
        assert "tag_form" not in app.query_params and "tag_premiere" not in app.query_params
    finally:
        catalog._facet_fields.clear()


@pytest.mark.parametrize(
    "file", ["catalog_theater.py", "catalog_productions.py", "catalog_people.py", "catalog_texts.py"]
)
def test_catalog_filters_precede_search(catalog_db, file):
    app = AppTest.from_file(VIEWS / file).run()
    assert not app.exception
    elements = list(app.main.children.values())
    filters = next(index for index, element in enumerate(elements) if element.type == "expander")
    search = next(index for index, element in enumerate(elements) if element.type == "text_input")
    assert filters < search


def test_catalog_search_resets_pagination(catalog_db, monkeypatch):
    monkeypatch.setattr(catalog, "PAGE_SIZE", 1)
    app = AppTest.from_file(VIEWS / "catalog_productions.py")
    app.query_params["exclude_planned"] = "false"
    app.run()
    app.pagination[0].set_value(3).run()
    assert app.pagination[0].value == 3
    app.text_input(key="q").set_value("Wien").run()
    assert not app.exception
    assert app.pagination[0].value == 1


@pytest.mark.parametrize(
    ("name", "city", "label"),
    [
        ("Stadttheater Würzburg", " WÜRZBURG ", "Stadttheater Würzburg"),
        ("Thalia Theater", "Hamburg", "Thalia Theater · Hamburg"),
        ("Schauspielhaus Zürich", "Zu\u0308rich", "Schauspielhaus Zürich"),
        ("Burgtheater", None, "Burgtheater"),
    ],
)
def test_theatre_label_only_adds_missing_city(name, city, label):
    assert theatre_label(name, city) == label


@pytest.mark.parametrize("kind", ["organization", "production"])
def test_catalog_does_not_repeat_city_in_results(monkeypatch, kind):
    def entry(name: str, city: str) -> dict:
        theatre = {"name": name, "address_locality": city, "address_country": "Deutschland"}
        if kind == "production":
            return {"id": name, "name": "Hamlet", "premiere": "2022-01-01", "organization": theatre}
        return {"id": name, "production_count": 1, **theatre}

    rows = [entry("Stadttheater Würzburg", "Würzburg"), entry("Thalia Theater", "Hamburg")]
    monkeypatch.setattr(catalog, "catalog_page", lambda *args, **kwargs: (rows, len(rows)))
    monkeypatch.setattr(catalog, "catalog_suggestions", lambda *args: [])
    monkeypatch.setattr(catalog, "_summary", lambda *args: [])
    monkeypatch.setattr(catalog, "_tag_options", lambda *args: [])
    monkeypatch.setattr(catalog, "_facet_fields", list)
    file = "catalog_productions.py" if kind == "production" else "catalog_theater.py"
    app = AppTest.from_file(VIEWS / file)
    app.query_params["q"] = "test"
    app.run()
    assert not app.exception
    context = [item.value for item in app.text]
    assert sum(line.count("Würzburg") for line in context) == (1 if kind == "production" else 0)
    assert sum(line.count("Hamburg") for line in context) == 1


@pytest.mark.parametrize("kind", ["organization", "production"])
def test_tag_filter_uses_own_tags_and_exact_values(catalog_db, kind):
    model = Organization if kind == "organization" else Production
    partial, count = catalog_page(model, "", [tag_filter(model, ["Dram"])], limit=12)
    assert not partial and count == 0
    tags = ["Oper", "Drama"]
    rows, total = catalog_page(model, "", [tag_filter(model, tags)], limit=12)
    assert total == (1 if kind == "organization" else 2)
    assert all(set(row["tags"]).intersection(tags) for row in rows)
    if kind == "organization":
        assert catalog_page(model, "", [tag_filter(model, ["Drama"])], limit=12)[1] == 0
    else:
        assert catalog_page(model, "", [tag_filter(model, ["Bühne"])], limit=12)[1] == 1


@pytest.mark.parametrize(
    ("file", "selected", "options", "badge"),
    [
        ("catalog_theater.py", "Oper", ["Oper"], ":gray-badge[Oper]"),
        ("catalog_productions.py", "Drama", ["Bühne", "Drama", "Schauspiel"], ":gray-badge[Drama]"),
    ],
)
def test_tag_multiselect_is_shareable_and_resettable(catalog_db, file, selected, options, badge):
    app = AppTest.from_file(VIEWS / file)
    app.query_params.update(tags=[selected], verified_only="0")
    app.run()
    assert not app.exception
    widget = app.multiselect(key="tags")
    assert widget.options == options
    assert widget.value == [selected]
    assert any(element.value == badge for element in app.markdown)
    assert "Kommende Premieren" not in [heading.value for heading in app.subheader]
    app.button(key=f"filter_{'production' if 'productions' in file else 'organization'}__reset").click().run()
    assert not app.exception
    assert "tags" not in app.query_params


@pytest.mark.parametrize(
    ("sort_by", "descending", "names"),
    [
        ("name", False, ["Hamlet", "Hamlet morgen", "Hamlet später", "Ohne Datum", "Ungefähr"]),
        ("name", True, ["Ungefähr", "Ohne Datum", "Hamlet später", "Hamlet morgen", "Hamlet"]),
        ("premiere", False, ["Hamlet", "Hamlet morgen", "Hamlet später", "Ungefähr", "Ohne Datum"]),
        ("premiere", True, ["Ungefähr", "Hamlet später", "Hamlet morgen", "Hamlet", "Ohne Datum"]),
    ],
)
def test_production_sort_is_applied_before_pagination(catalog_db, sort_by, descending, names):
    rows, total = catalog_page(Production, "", [], limit=2, offset=1, sort_by=sort_by, descending=descending)
    assert total == 5
    assert [row["name"] for row in rows] == names[1:3]


def test_planned_filter_keeps_today_and_undated_but_not_tomorrow(catalog_db):
    today = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None, hour=23, minute=59, second=0, microsecond=0)
    with get_session() as session:
        session.add_all(
            [
                Production(name="Boundary today", premiere=today),
                Production(name="Boundary tomorrow", premiere=(today + timedelta(days=1)).replace(hour=0, minute=0)),
                Production(name="Boundary undated"),
            ]
        )
        session.commit()
    app = AppTest.from_file(VIEWS / "catalog_productions.py")
    app.query_params["q"] = "Boundary"
    app.run()
    assert not app.exception
    assert app.toggle(key="exclude_planned").value is True
    links = "\n".join(element.value for element in app.markdown)
    assert ">Boundary today</a>" in links
    assert ">Boundary undated</a>" in links
    assert ">Boundary tomorrow</a>" not in links
    app.toggle(key="exclude_planned").set_value(False).run()
    assert not app.exception
    assert ">Boundary tomorrow</a>" in "\n".join(element.value for element in app.markdown)
    assert app.toggle(key="exclude_planned").proto.query_param_key == "exclude_planned"
    app.button(key="filter_production__reset").click().run()
    assert app.toggle(key="exclude_planned").value is True


def test_production_sort_url_direction_button_and_pagination_reset(catalog_db, monkeypatch):
    monkeypatch.setattr(catalog, "PAGE_SIZE", 1)
    app = AppTest.from_file(VIEWS / "catalog_productions.py")
    app.query_params.update(exclude_planned="false", q="e", sort="Name", order="desc")
    app.run()
    assert not app.exception
    assert app.selectbox(key="sort").value == "name"
    assert ">Ungefähr</a>" in "\n".join(element.value for element in app.markdown)
    app.button(key="sort_direction").click().run()
    assert app.query_params.get("order") is None
    assert ">Hamlet</a>" in "\n".join(element.value for element in app.markdown)
    app.pagination[0].set_value(3).run()
    app.selectbox(key="sort").set_value("premiere").run()
    assert not app.exception
    assert app.pagination[0].value == 1
    assert ">Ungefähr</a>" in "\n".join(element.value for element in app.markdown)
    app.button(key="sort_direction").click().run()
    assert app.query_params["order"] == "asc"
    assert ">Hamlet</a>" in "\n".join(element.value for element in app.markdown)


def test_upcoming_preview_is_not_repeated_in_main_list(catalog_db):
    with get_session() as session:
        today = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None, hour=20, minute=0)
        session.add(Production(name="Heute Premiere", premiere=today))
        session.commit()
    app = AppTest.from_file(VIEWS / "catalog_productions.py").run()
    assert not app.exception
    links = "\n".join(element.value for element in app.markdown)
    assert links.count(">Heute Premiere</a>") == 1
    app.toggle(key="exclude_planned").set_value(False).run()
    links = "\n".join(element.value for element in app.markdown)
    assert links.count(">Hamlet morgen</a>") == 1
    assert links.count(">Ungefähr</a>") == 1


def test_planned_filter_does_not_hide_upcoming_preview(catalog_db):
    with get_session() as session:
        tomorrow = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None) + timedelta(days=1)
        session.add(Production(name="Nächste Premiere", premiere=tomorrow))
        session.commit()
    app = AppTest.from_file(VIEWS / "catalog_productions.py").run()
    assert not app.exception
    assert app.toggle(key="exclude_planned").value is True
    assert "Kommende Premieren" in [heading.value for heading in app.subheader]
    assert app.text_input(key="organization_id").proto.placeholder == "Suchbegriff eingeben"
    assert app.multiselect(key="tags").proto.placeholder == "Schlagworte wählen"
    app.toggle(key="exclude_planned").set_value(False).run()
    assert "Kommende Premieren" in [heading.value for heading in app.subheader]
