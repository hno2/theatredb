import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import Organization, Person, Production, ProductionMembership, Text

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
