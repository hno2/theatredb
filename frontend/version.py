"""Commit information shown in the sidebar."""

import logging
import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - Run Git with fixed arguments.
from pathlib import Path

import streamlit as st

REPOSITORY_URL = "https://github.com/hno2/theatredb"
logger = logging.getLogger(__name__)


def current_commit() -> str | None:
    """Return the image's build commit or the local checkout's HEAD."""
    commit = os.environ.get("THEATERDB_COMMIT")
    if commit is None:
        git = shutil.which("git")
        if git is None:
            logger.warning("Cannot determine Git commit: Git is unavailable")
            return None
        try:
            commit = subprocess.check_output(  # ruff: ignore[subprocess-without-shell-equals-true] - Fixed arguments.
                [git, "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except OSError, subprocess.CalledProcessError:
            logger.warning("Cannot determine Git commit for sidebar link")
            return None
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        logger.warning("Invalid or missing THEATERDB_COMMIT for sidebar link")
        return None
    return commit


def render_github_link() -> None:
    """Link to the current commit when its SHA is available."""
    commit = current_commit()
    st.link_button(
        "GitHub",
        f"{REPOSITORY_URL}/commit/{commit}" if commit else REPOSITORY_URL,
        help=f"Commit: {commit}" if commit else "Commit unbekannt",
        icon=":material/code:",
        type="tertiary",
    )
