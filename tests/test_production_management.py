from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine
from streamlit.testing.v1 import AppTest

from backend import user_management
from backend.models import Production

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
