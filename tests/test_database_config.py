import runpy
from pathlib import Path

import dotenv
import pytest

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
