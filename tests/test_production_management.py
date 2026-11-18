import json
from collections.abc import Iterator
from datetime import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import Award, EntitySource, Organization, Person, Production, ProductionMembership, Source, Text
from frontend.management.uifactory import _build_fields, _rename_existing_fk_if_changed

PRODUCTION_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "productions.py"
PERSON_APP = PRODUCTION_APP.with_name("persons.py")


@pytest.fixture
def db(monkeypatch, tmp_path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-management.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    yield engine
    engine.dispose()


def test_selected_production_integer_field_is_rendered_without_decimal(db) -> None:
    with Session(db) as session:
        session.add_all([Production(name="With duration", duration=90), Production(name="No duration")])
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception

    dataframe = app.dataframe[0].value
    selected_row = next(index for index, value in enumerate(dataframe["duration"]) if value == 90)
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [selected_row], "columns": []}}
    app.run()

    assert not app.exception
    duration = next(widget for widget in app.text_input if widget.label == "Duration")
    assert duration.value == "90"


def test_production_table_resolves_foreign_keys_to_names(db) -> None:
    organization = Organization(name="Example Theatre")
    text = Text(title="Example Play")
    production = Production(
        name="Example Production",
        organization=organization,
        text=text,
    )
    organization_id = organization.id
    text_id = text.id
    with Session(db) as session:
        session.add(production)
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()

    assert not app.exception
    table = app.dataframe[0].value
    assert "organization" in table.columns
    assert "text" in table.columns
    assert ["Example Theatre"] in table["organization"].values
    assert ["Example Play"] in table["text"].values
    assert str(organization_id) not in str(table)
    assert str(text_id) not in str(table)
    assert list(app.dataframe[0].proto.column_order[:4]) == ["name", "premiere", "organization", "text"]
    config = json.loads(app.dataframe[0].proto.columns)
    assert config["organization"]["type_config"]["type"] == "list"
    assert config["text"]["type_config"]["type"] == "list"


def test_search_covers_productions_beyond_first_page(db) -> None:
    with Session(db) as session:
        session.add_all([Production(name=f"Alpha {i:03}") for i in range(60)] + [Production(name="Zeta 100%")])
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 50
    app.text_input(key="crud_search_production").set_value("100%").run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Zeta 100%"]
    assert app.caption[0].value == "61 Produktionen insgesamt"


def test_filter_section_applies_sql_filters(db) -> None:
    venue = Organization(name="Schauspielhaus")
    with Session(db) as session:
        session.add_all(
            [
                Production(name="Match", organization=venue, duration=90, tags=["Komödie"], premiere=dt(2020, 5, 1)),
                Production(name="Other venue", organization=Organization(name="Oper"), duration=90, tags=["Komödie"]),
                Production(name="Too long", organization=venue, duration=200, tags=["Komödie"]),
                Production(name="Wrong tag", organization=venue, duration=90, tags=["Drama"]),
            ]
            + [Production(name=f"Filler {i:03}") for i in range(60)]
        )
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    assert not app.exception
    assert not app.expander[0].proto.expanded
    app.text_input(key="filter_production_organization_id").set_value("schauspiel")
    app.number_input(key="filter_production_duration_to").set_value(120)
    app.text_input(key="filter_production_tags").set_value("komödie")
    app.run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Match"]
    app.number_input(key="filter_production_premiere_from").set_value(2021).run()
    assert "Keine Einträge gefunden." in [info.value for info in app.info]


def test_person_filter_section_filters_json_types(db) -> None:
    with Session(db) as session:
        session.add_all([Person(name="Ada", type=["director"]), Person(name="Bea", type=["actor"])])
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP.with_name("persons.py"))).run()
    app.text_input(key="filter_person_type").set_value("direct").run()
    assert not app.exception
    assert list(app.dataframe[0].value["name"]) == ["Ada"]


@pytest.mark.parametrize(
    ("model", "label"),
    [
        (Organization, "Organisationen"),
        (Person, "Personen"),
        (Production, "Produktionen"),
        (Text, "Texte"),
        (Award, "Auszeichnungen"),
        (Source, "Quellen"),
    ],
)
def test_management_pages_show_item_count(db, model, label) -> None:
    field = "title" if model is Text else "name"
    with Session(db) as session:
        session.add_all([model(**{field: f"Example {index}"}) for index in range(2)])
        session.commit()

    app = AppTest.from_string(
        f"from backend.models import {model.__name__}\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        f"render_sqlmodel_crud(model_cls={model.__name__})"
    ).run()

    assert not app.exception
    assert app.caption[0].value == f"2 {label} insgesamt"


@pytest.mark.parametrize(
    "model,record_url",
    [
        (Organization, "https://example.org/records/123"),
        (Person, "https://example.org/records/123"),
        (Production, "https://example.org/records/123"),
        (Text, "https://example.org/records/123"),
        (Award, "https://example.org/records/123"),
        (Production, None),
        (Production, "javascript:alert(1)"),
    ],
)
def test_selected_entity_sources_have_clickable_record_urls(db, model, record_url) -> None:
    entity = model(**({"title": "Example"} if model is Text else {"name": "Example"}))
    source = Source(name="Example Source", base_url="https://example.org")
    with Session(db) as session:
        session.add(entity)
        session.add(source)
        session.flush()
        session.add(
            EntitySource(
                source_id=source.id,
                entity_type=model._entity_type,
                entity_id=entity.id,
                external_url=record_url,
            )
        )
        session.commit()

    app = AppTest.from_string(
        f"from backend.models import {model.__name__}\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        f"render_sqlmodel_crud(model_cls={model.__name__})"
    ).run()
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [0], "columns": []}}
    app.run()

    assert not app.exception
    sources = app.dataframe[1]
    if record_url and record_url.startswith("https://"):
        assert sources.value["external_url"].tolist() == [record_url]
    else:
        assert sources.value["external_url"].isna().all()
    assert len(app.warning) == int(bool(record_url and not record_url.startswith("https://")))
    assert sources.value["source"].tolist() == ["Example Source"]
    assert json.loads(sources.proto.columns)["external_url"]["type_config"]["type"] == "link"


@pytest.mark.parametrize("url", [None, "javascript:alert(1)", "https://example.org"])
def test_source_management_links_only_valid_web_urls(db, url) -> None:
    with Session(db) as session:
        session.add(Source(name="Example", base_url=url))
        session.commit()
    app = AppTest.from_string(
        "from backend.models import Source\n"
        "from frontend.management.uifactory import render_sqlmodel_crud\n"
        "render_sqlmodel_crud(model_cls=Source)"
    ).run()

    assert not app.exception
    table = app.dataframe[0]
    assert json.loads(table.proto.columns)["base_url"]["type_config"]["type"] == "link"
    if url and url.startswith("https://"):
        assert table.value["base_url"].tolist() == [url]
    else:
        assert table.value["base_url"].isna().all()
    assert len(app.warning) == int(bool(url and not url.startswith("https://")))


def test_organization_table_starts_with_name_and_clickable_websites(db) -> None:
    with Session(db) as session:
        session.add(
            Organization(name="Example Theatre", url="https://example.org", url_plays="https://example.org/plays")
        )
        session.commit()
    app = AppTest.from_file(str(PRODUCTION_APP.with_name("organizations.py"))).run()

    assert not app.exception
    table = app.dataframe[0]
    assert list(table.proto.column_order[:3]) == ["name", "url", "url_plays"]
    config = json.loads(table.proto.columns)
    assert config["url"]["type_config"]["type"] == "link"
    assert config["url_plays"]["type_config"]["type"] == "link"
    assert config["url"]["label"] == "Website"
    assert config["url_plays"]["label"] == "Spielplan"


def test_person_table_starts_with_name_type_and_life_dates(db) -> None:
    with Session(db) as session:
        session.add(Person(name="Example Person", type=["actor"]))
        session.commit()
    app = AppTest.from_file(str(PERSON_APP)).run()

    assert not app.exception
    table = app.dataframe[0]
    assert list(table.proto.column_order[:5]) == ["Auswahl", "name", "type", "born", "died"]
    assert table.value["type"].tolist() == [["actor"]]
    assert json.loads(table.proto.columns)["type"]["type_config"]["type"] == "list"


def test_cast_and_crew_editor_shows_person_name_without_ids(db) -> None:
    production = Production(name="Example Production")
    person = Person(name="Example Person")
    person_id = person.id
    with Session(db) as session:
        session.add(ProductionMembership(production=production, person=person, role="actor"))
        session.commit()

    app = AppTest.from_file(str(PRODUCTION_APP)).run()
    dataframe_key = app.dataframe[0].proto.id.rsplit("-", 1)[1]
    app.session_state[dataframe_key] = {"selection": {"rows": [0], "columns": []}}
    app.run()

    assert not app.exception
    editor = app.dataframe[1]
    assert "Example Person" in editor.value["person_id"].values
    assert str(person_id) not in str(editor.value)
    column_config = json.loads(editor.proto.columns)
    assert column_config["__id"]["hidden"]
    assert column_config["person_id"]["label"] == "Person"


def test_renaming_cast_and_crew_name_updates_shared_person(db) -> None:
    production = Production(name="Example Production")
    other_production = Production(name="Other Production")
    production_id = production.id
    person = Person(name="Original Name")
    person_id = person.id
    with Session(db) as session:
        session.add_all(
            [
                ProductionMembership(production=production, person=person, role="actor"),
                ProductionMembership(production=other_production, person=person, role="director"),
            ]
        )
        session.commit()
    with Session(db) as session:
        membership = session.exec(
            select(ProductionMembership).where(ProductionMembership.production_id == production_id)
        ).one()
    person_field = next(
        field
        for field in _build_fields(
            ProductionMembership,
            field_overrides={
                "person_id": {"kind": "fk_select", "fk_model": Person, "fk_display": "name"},
                "production_id": {"exclude": True},
            },
        )
        if field.name == "person_id"
    )
    resolved_id = _rename_existing_fk_if_changed(
        person_field,
        "Renamed Person",
        membership,
        {str(person_id): "Original Name"},
        {"payload_field": "name", "rename_existing": True},
        "test",
    )

    assert resolved_id == str(person_id)
    with Session(db) as session:
        saved_person = session.get(Person, person_id)
        memberships = session.exec(select(ProductionMembership)).all()
        assert saved_person is not None and saved_person.name == "Renamed Person"
        assert len(memberships) == 2
        assert {membership.person_id for membership in memberships} == {person_id}
