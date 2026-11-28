import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - Read the local test checkout.
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from frontend import version


def test_local_commit_comes_from_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THEATERDB_COMMIT", raising=False)
    root = Path(version.__file__).resolve().parents[1]
    git = shutil.which("git")
    assert git is not None
    expected = subprocess.check_output(  # ruff: ignore[subprocess-without-shell-equals-true] - Fixed arguments.
        [git, "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    assert version.current_commit() == expected


def test_sidebar_link_uses_image_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "a" * 40
    monkeypatch.setenv("THEATERDB_COMMIT", commit)
    app = AppTest.from_string(
        "import streamlit as st\nfrom frontend.version import render_github_link\n"
        "with st.sidebar:\n    render_github_link()"
    ).run()
    assert not app.exception
    link = app.sidebar.get("link_button")[0]
    assert link.proto.url == f"https://github.com/hno2/theatredb/commit/{commit}"
    assert commit in link.proto.help


@pytest.mark.parametrize("commit", ["", "main", "a" * 39, "g" * 40])
def test_invalid_image_commit_does_not_link_to_wrong_revision(
    commit: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("THEATERDB_COMMIT", commit)
    app = AppTest.from_string("from frontend.version import render_github_link\nrender_github_link()").run()
    assert not app.exception
    link = app.get("link_button")[0]
    assert link.proto.url == "https://github.com/hno2/theatredb"
    assert link.proto.help == "Commit unbekannt"
    assert "Invalid or missing THEATERDB_COMMIT" in caplog.text


def test_missing_git_is_reported(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("THEATERDB_COMMIT", raising=False)
    monkeypatch.setattr(version.shutil, "which", lambda _: None)
    assert version.current_commit() is None
    assert "Git is unavailable" in caplog.text
