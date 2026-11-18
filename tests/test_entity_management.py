from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from backend import entity_management
from backend.models import Person, Production, Source


def test_count_entities_many_uses_one_query(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Person(name="Alex"), Person(name="Sam"), Production(name="Hamlet"), Source(name="Archive")])
        session.commit()

    queries = []
    event.listen(engine, "before_cursor_execute", lambda *args: queries.append(args[2]))
    monkeypatch.setattr(entity_management, "get_session", lambda: Session(engine))

    assert entity_management.count_entities_many(Person, Production, Source) == {
        Person: 2,
        Production: 1,
        Source: 1,
    }
    assert len(queries) == 1
    engine.dispose()
