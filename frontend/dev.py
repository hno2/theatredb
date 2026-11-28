"""Local Streamlit launcher with optional whole-process reload."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _run_streamlit(arguments: tuple[str, ...]) -> None:
    os.execv(  # ruff: ignore[start-process-with-no-shell] - Launch the current interpreter without a shell.
        sys.executable, (sys.executable, "-m", "streamlit", "run", str(ROOT / "frontend/app.py"), *arguments)
    )


def main() -> None:
    """Read the local reload flag and launch Streamlit."""
    load_dotenv(ROOT / ".env", override=False)
    reload = os.getenv("THEATERDB_DEV_RELOAD", "0")
    if reload not in {"0", "1"}:
        raise ValueError("THEATERDB_DEV_RELOAD must be 0 or 1")
    os.chdir(ROOT)
    arguments = tuple(sys.argv[1:])
    if reload == "0":
        _run_streamlit(arguments)
    else:
        from watchfiles import PythonFilter, run_process  # ruff: ignore[import-outside-top-level]

        run_process(
            ROOT / "backend",
            ROOT / "frontend",
            target=_run_streamlit,
            args=((*arguments, "--server.fileWatcherType=none"),),
            watch_filter=PythonFilter(),
        )


if __name__ == "__main__":
    main()
