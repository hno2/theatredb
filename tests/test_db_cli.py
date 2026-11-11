import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
from alembic import command
from alembic.config import CommandLine
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy import Engine, text
from sqlmodel import SQLModel, create_engine

from backend import db, user_management


def test_alembic_config_uses_pyproject_outside_project(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    config = user_management.alembic_config()
    assert ScriptDirectory.from_config(config).get_current_head() == user_management.BASELINE_REVISION
    with create_engine("sqlite://").connect() as connection:
        assert user_management.alembic_config(connection).attributes["connection"] is connection


def test_alembic_cli_discovers_pyproject(monkeypatch) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    monkeypatch.delenv("ALEMBIC_CONFIG", raising=False)
    cli = CommandLine()
    run_cmd = Mock()
    monkeypatch.setattr(cli, "run_cmd", run_cmd)
    cli.main(["heads"])
    config = run_cmd.call_args.args[0]
    assert ScriptDirectory.from_config(config).get_current_head() == user_management.BASELINE_REVISION


@pytest.fixture
def fake_engine(monkeypatch) -> MagicMock:
    engine = MagicMock()
    engine.init_db = Mock()
    engine.dialect.name = "postgresql"
    engine.url = user_management.DATABASE_URL.set(database="theater")
    monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
    monkeypatch.setattr(user_management, "init_db", engine.init_db)
    return engine


def test_reset_aborts_on_wrong_name(fake_engine, monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "other")
    assert db.main(["reset"]) == 1
    fake_engine.begin.assert_not_called()
    fake_engine.init_db.assert_not_called()


@pytest.mark.parametrize(("argv", "answer"), [(["reset"], "theater"), (["reset", "--yes"], None)])
def test_reset_drops_schema_and_reinitializes(fake_engine, monkeypatch, argv, answer) -> None:
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert db.main(argv) == 0
    statements = [
        str(call.args[0]) for call in fake_engine.begin.return_value.__enter__.return_value.execute.mock_calls
    ]
    assert statements == ["DROP SCHEMA public CASCADE", "CREATE SCHEMA public"]
    fake_engine.init_db.assert_called_once()


def test_reset_refuses_non_postgres(fake_engine) -> None:
    fake_engine.dialect.name = "sqlite"
    assert db.main(["reset", "--yes"]) == 1
    fake_engine.begin.assert_not_called()


@pytest.fixture
def postgres(monkeypatch) -> Iterator[Engine]:
    if os.environ.get("THEATERDB_TEST_POSTGRES") != "1":
        pytest.skip("Set THEATERDB_TEST_POSTGRES=1 for disposable PostgreSQL migration integration")
    name = "theaterdb_migration_test_" + uuid.uuid4().hex
    url = user_management.DATABASE_URL.set(drivername="postgresql", database="postgres")
    with psycopg.connect(url.render_as_string(hide_password=False), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        engine = create_engine(user_management.DATABASE_URL.set(database=name))
        monkeypatch.setattr(user_management, "_get_engine", lambda: engine)
        try:
            yield engine
        finally:
            engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _assert_at_head_without_drift(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() is not None
        assert connection.execute(text("SELECT to_regclass('procrastinate_jobs')")).scalar_one() is not None
        command.check(user_management.alembic_config(connection))


def test_migrations_match_models(postgres) -> None:
    user_management.init_db()
    _assert_at_head_without_drift(postgres)
    assert db.main(["status"]) == 0


def test_unversioned_schema_is_stamped_and_keeps_data(postgres) -> None:
    SQLModel.metadata.create_all(postgres)
    with postgres.begin() as connection:
        connection.execute(text("INSERT INTO award (id, name) VALUES (gen_random_uuid(), 'kept')"))
    user_management.init_db()
    _assert_at_head_without_drift(postgres)
    with postgres.connect() as connection:
        assert connection.execute(text("SELECT name FROM award")).scalar_one() == "kept"


def test_reset_clears_data(postgres) -> None:
    user_management.init_db()
    with postgres.begin() as connection:
        connection.execute(text("INSERT INTO award (id, name) VALUES (gen_random_uuid(), 'gone')"))
    assert db.main(["reset", "--yes"]) == 0
    _assert_at_head_without_drift(postgres)
    with postgres.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM award")).scalar_one() == 0
