import runpy
from pathlib import Path
from unittest.mock import MagicMock, Mock

import dotenv
import pytest
from psycopg.errors import DuplicateDatabase, InsufficientPrivilege
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import SQLModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def load_config(monkeypatch, tmp_path):
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "THEATERDB_DATABASE_URL",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    env_file = tmp_path / ".env"
    original_load_dotenv = dotenv.load_dotenv

    def load_test_env(path, *, override):
        assert path == PROJECT_ROOT / ".env"
        assert override is False
        return original_load_dotenv(env_file, override=override)

    monkeypatch.setattr(dotenv, "load_dotenv", load_test_env)
    monkeypatch.chdir(tmp_path)

    def load(contents):
        env_file.write_text(contents)
        return runpy.run_path(str(PROJECT_ROOT / "backend" / "user_management.py"))

    return load


def test_loads_credentials_from_root_env(load_config) -> None:
    config = load_config(
        "POSTGRES_HOST=db.example\nPOSTGRES_PORT=5433\nPOSTGRES_USER=app\n"
        "POSTGRES_PASSWORD='p@ss:/#word'\nPOSTGRES_DB=theater\n"
    )
    url = config["DATABASE_URL"]
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db.example"
    assert url.port == 5433
    assert url.username == "app"
    assert url.password == "p@ss:/#word"  # ruff: ignore[hardcoded-password-string]
    assert url.database == "theater"
    assert config["_get_engine"]().url == url


def test_environment_overrides_dotenv(load_config, monkeypatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "runtime-password")
    config = load_config("POSTGRES_PASSWORD=file-password\n")
    assert config["DATABASE_URL"].password == "runtime-password"  # ruff: ignore[hardcoded-password-string]


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"])
def test_database_url_from_dotenv(load_config, scheme) -> None:
    config = load_config(f"DATABASE_URL={scheme}://app:secret@db.example/theater\n")
    url = config["DATABASE_URL"]
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "db.example"
    assert url.database == "theater"
    assert config["_get_engine"]().url == url


def test_theaterdb_url_takes_precedence(load_config, monkeypatch) -> None:
    monkeypatch.setenv("THEATERDB_DATABASE_URL", "sqlite://")
    config = load_config("DATABASE_URL=postgresql://app:secret@db.example/theater\n")
    assert str(config["DATABASE_URL"]) == "sqlite://"


def test_runtime_database_url_overrides_dotenv(load_config, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    config = load_config("DATABASE_URL=postgresql://app:secret@db.example/theater\n")
    assert str(config["DATABASE_URL"]) == "sqlite://"


def test_defaults_without_dotenv(load_config) -> None:
    config = load_config("")
    url = config["DATABASE_URL"]
    assert url.host == "localhost"
    assert url.port == 5432
    assert url.username == "postgres"
    assert url.database == "postgres"


@pytest.fixture
def database_setup(load_config, monkeypatch):
    config = load_config("DATABASE_URL=postgresql://app:secret@localhost/theatredb\n")
    engine = config["_get_engine"]()
    admin_engine = MagicMock()
    connection = admin_engine.connect.return_value.__enter__.return_value = Mock()
    connection.dialect = engine.dialect
    connection.execute.return_value.scalar.return_value = None
    create_engine = Mock(return_value=admin_engine)
    monkeypatch.setitem(config["init_db"].__globals__, "create_engine", create_engine)
    create_tables = Mock()
    monkeypatch.setattr(SQLModel.metadata, "create_all", create_tables)
    monkeypatch.setitem(config["init_db"].__globals__, "ensure_queue_schema", Mock())
    return config["init_db"], engine, admin_engine, connection, create_engine, create_tables


def test_existing_database_needs_no_admin_connection(database_setup) -> None:
    init_db, engine, _, _, create_engine, create_tables = database_setup
    init_db()
    create_tables.assert_called_once_with(engine)
    create_engine.assert_not_called()


def test_creates_missing_database_before_tables(database_setup) -> None:
    init_db, engine, admin_engine, connection, create_engine, create_tables = database_setup
    create_tables.side_effect = [OperationalError(None, None, Exception("missing database")), None]
    init_db()
    assert create_engine.call_args.args[0] == engine.url.set(database="postgres")
    assert create_engine.call_args.kwargs == {"isolation_level": "AUTOCOMMIT"}
    assert connection.execute.call_args.args[1] == {"name": "theatredb"}
    connection.exec_driver_sql.assert_called_once_with('CREATE DATABASE "theatredb"')
    assert create_tables.call_count == 2
    admin_engine.dispose.assert_called_once()


def test_database_name_is_quoted(database_setup) -> None:
    init_db, engine, _, connection, _, create_tables = database_setup
    engine.url = engine.url.set(database='theatre"db')
    create_tables.side_effect = [OperationalError(None, None, Exception("missing database")), None]
    init_db()
    connection.exec_driver_sql.assert_called_once_with('CREATE DATABASE "theatre""db"')


def test_existing_database_preserves_connection_error(database_setup) -> None:
    init_db, _, admin_engine, connection, _, create_tables = database_setup
    error = OperationalError(None, None, Exception("connection failed"))
    create_tables.side_effect = error
    connection.execute.return_value.scalar.return_value = 1
    with pytest.raises(OperationalError) as raised:
        init_db()
    assert raised.value is error
    connection.exec_driver_sql.assert_not_called()
    admin_engine.dispose.assert_called_once()


def test_unavailable_server_preserves_connection_error(database_setup) -> None:
    init_db, _, admin_engine, _, _, create_tables = database_setup
    error = OperationalError(None, None, Exception("connection refused"))
    create_tables.side_effect = error
    admin_engine.connect.side_effect = OperationalError(None, None, Exception("admin connection failed"))
    with pytest.raises(OperationalError) as raised:
        init_db()
    assert raised.value is error
    admin_engine.dispose.assert_called_once()


def test_concurrent_database_creation_is_safe(database_setup) -> None:
    init_db, _, _, connection, _, create_tables = database_setup
    create_tables.side_effect = [OperationalError(None, None, Exception("missing database")), None]
    connection.exec_driver_sql.side_effect = ProgrammingError(None, None, DuplicateDatabase())
    init_db()
    assert create_tables.call_count == 2


def test_database_creation_permission_error_is_not_swallowed(database_setup) -> None:
    init_db, _, admin_engine, connection, _, create_tables = database_setup
    create_tables.side_effect = OperationalError(None, None, Exception("missing database"))
    error = ProgrammingError(None, None, InsufficientPrivilege())
    connection.exec_driver_sql.side_effect = error
    with pytest.raises(ProgrammingError) as raised:
        init_db()
    assert raised.value is error
    assert create_tables.call_count == 1
    admin_engine.dispose.assert_called_once()


def test_sqlite_initialization(load_config) -> None:
    config = load_config("DATABASE_URL=sqlite://\n")
    config["init_db"]()
    with config["_get_engine"]().connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM person").scalar() == 0
