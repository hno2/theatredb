from pathlib import Path
from unittest.mock import Mock

import pytest
from watchfiles import PythonFilter

from frontend import dev


def test_env_file_enables_process_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("THEATERDB_DEV_RELOAD=1\n")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THEATERDB_DEV_RELOAD", raising=False)
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--server.port=8502"])
    watch = Mock()
    monkeypatch.setattr("watchfiles.run_process", watch)

    dev.main()

    watch.assert_called_once()
    args, kwargs = watch.call_args
    assert args == (tmp_path / "backend", tmp_path / "frontend")
    assert kwargs["target"] is dev._run_streamlit
    assert kwargs["args"] == (("--server.port=8502", "--server.fileWatcherType=none"),)
    assert isinstance(kwargs["watch_filter"], PythonFilter)


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("THEATERDB_DEV_RELOAD=1\n")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("THEATERDB_DEV_RELOAD", "0")
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--server.port=8502"])
    execute = Mock()
    monkeypatch.setattr(dev.os, "execv", execute)

    dev.main()

    execute.assert_called_once_with(
        dev.sys.executable,
        (dev.sys.executable, "-m", "streamlit", "run", str(tmp_path / "frontend/app.py"), "--server.port=8502"),
    )


def test_invalid_reload_flag_is_explicit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEATERDB_DEV_RELOAD", "yes")
    with pytest.raises(ValueError, match="THEATERDB_DEV_RELOAD must be 0 or 1"):
        dev.main()
