import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine, select
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import Organization, Person, Production, ProductionMembership, Text
from frontend.management.uifactory import _build_fields, _rename_existing_fk_if_changed

PRODUCTION_APP = Path(__file__).resolve().parents[1] / "frontend" / "management" / "productions.py"


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
    assert "Example Theatre" in table["organization"].values
    assert "Example Play" in table["text"].values
    assert str(organization_id) not in str(table)
    assert str(text_id) not in str(table)


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
