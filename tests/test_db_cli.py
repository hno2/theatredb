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
from sqlmodel import create_engine

from backend import db, user_management


def test_alembic_config_uses_pyproject_outside_project(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    config = user_management.alembic_config()
    assert ScriptDirectory.from_config(config).get_current_head() == "0013"
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
    assert ScriptDirectory.from_config(config).get_current_head() == "0013"


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


def test_orif_migration_rewrites_postgres_enum_payloads_and_downgrades(postgres) -> None:
    with postgres.begin() as connection:
        config = user_management.alembic_config(connection)
        command.upgrade(config, "0011")
        connection.execute(text("INSERT INTO source (id, name) VALUES (gen_random_uuid(), 'source')"))
        for kind, payload in (
            ("ORGANIZATION", '{"city":"Wien"}'),
            ("PRODUCTION", '{"age_rating":12}'),
            ("PERSON", '{"city":"unchanged"}'),
        ):
            connection.execute(
                text(
                    "INSERT INTO reconciliationcase (id, key, entity_type, operation, status, reason, created_at, incoming) "
                    "VALUES (gen_random_uuid(), :key, CAST(:kind AS entitytype), 'import', 'open', '', now(), "
                    "CAST(:payload AS json))"
                ),
                {"key": kind, "kind": kind, "payload": payload},
            )
            connection.execute(
                text(
                    "INSERT INTO sourcerecord (id, source_id, entity_type, record_key, entity_id, payload) "
                    "SELECT gen_random_uuid(), id, CAST(:kind AS entitytype), :key, gen_random_uuid(), "
                    "json_build_object('incoming', CAST(:payload AS json)) FROM source"
                ),
                {"key": kind, "kind": kind, "payload": payload},
            )

        def payloads(query: str) -> dict:
            return dict(connection.execute(text(query)).all())

        cases = "SELECT CAST(entity_type AS text), incoming FROM reconciliationcase"
        records = "SELECT CAST(entity_type AS text), payload FROM sourcerecord"
        before_cases = payloads(cases)
        before_records = payloads(records)
        command.upgrade(config, "0012")
        assert payloads(cases) == {
            "ORGANIZATION": {"address_locality": "Wien"},
            "PRODUCTION": {"suggested_min_age": 12},
            "PERSON": {"city": "unchanged"},
        }
        assert payloads(records) == {
            "ORGANIZATION": {"incoming": {"address_locality": "Wien"}},
            "PRODUCTION": {"incoming": {"suggested_min_age": 12}},
            "PERSON": {"incoming": {"city": "unchanged"}},
        }
        command.downgrade(config, "0011")
        assert payloads(cases) == before_cases
        assert payloads(records) == before_records


def test_library_migration_keeps_passwords_and_allows_sso(postgres) -> None:
    legacy_id, sso_id = uuid.uuid4(), uuid.uuid4()
    with postgres.begin() as connection:
        config = user_management.alembic_config(connection)
        command.upgrade(config, "0010")
        connection.execute(
            text(
                'INSERT INTO "user" (id, username, password_hash, is_active, is_admin) '
                "VALUES (:id, 'legacy', 'old-hash', true, false)"
            ),
            {"id": legacy_id},
        )
        command.upgrade(config, "head")
        connection.execute(
            text(
                'INSERT INTO "user" (id, username, password_hash, is_active, is_admin) '
                "VALUES (:id, 'oidc:issuer|subject', NULL, true, false)"
            ),
            {"id": sso_id},
        )
        assert (
            connection.execute(text('SELECT password_hash FROM "user" WHERE id = :id'), {"id": legacy_id}).scalar_one()
            == "old-hash"
        )
        assert (
            connection.execute(text('SELECT password_hash FROM "user" WHERE id = :id'), {"id": sso_id}).scalar_one()
            is None
        )


def test_baseline_upgrade_preserves_data_and_indexes_round_trip(postgres) -> None:
    with postgres.begin() as connection:
        command.upgrade(user_management.alembic_config(connection), "0001")
        connection.execute(text("INSERT INTO award (id, name) VALUES (gen_random_uuid(), 'kept')"))
        connection.execute(text("INSERT INTO organization (id, name) VALUES (gen_random_uuid(), 'kept theatre')"))
        connection.execute(text("INSERT INTO person (id, name) VALUES (gen_random_uuid(), 'kept person')"))
    user_management.init_db()
    _assert_at_head_without_drift(postgres)
    with postgres.begin() as connection:
        assert connection.execute(text("SELECT name FROM award")).scalar_one() == "kept"
        assert connection.execute(text("SELECT name, tags FROM organization")).one() == ("kept theatre", [])
        assert connection.execute(text("SELECT name, alt_names FROM person")).one() == ("kept person", [])
        config = user_management.alembic_config(connection)
        command.downgrade(config, "0001")
        command.upgrade(config, "head")
    _assert_at_head_without_drift(postgres)


def test_unversioned_schema_is_stamped_and_keeps_data(postgres) -> None:
    with postgres.begin() as connection:
        command.upgrade(user_management.alembic_config(connection), "0001")
        connection.execute(text("DROP TABLE alembic_version"))
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
