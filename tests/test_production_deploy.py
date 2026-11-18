import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - Run the deploy script with fake tools.
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy-production"
IMAGE = "ghcr.io/hno2/theatredb@sha256:" + "a" * 64


@pytest.mark.parametrize("command,backup_status", [(IMAGE, "0"), (IMAGE, "1"), (IMAGE + " extra", "0")])
def test_production_deploy(tmp_path: Path, command: str, backup_status: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "flock": "exit 0",
        "curl": 'echo public-health >> "$CALLS"',
        "docker": """
echo "$*" >> "$CALLS"
case " $* " in
    *" ps --status running -q db "*) echo database ;;
    *" exec -T db "*) test "$BACKUP_STATUS" = 0 && echo database-backup ;;
    *" ps -q crawl-worker "*) echo worker ;;
    *" inspect "*) echo 'true 0' ;;
esac
""",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    calls = tmp_path / "calls"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - Only test-controlled paths and arguments.
        ["/bin/bash", str(SCRIPT), str(tmp_path), "production"],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "CALLS": str(calls),
            "SSH_ORIGINAL_COMMAND": command,
            "BACKUP_STATUS": backup_status,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if command != IMAGE:
        assert result.returncode != 0
        assert "Expected an exact TheatreDB GHCR image digest" in result.stderr
        assert not calls.exists()
        return

    calls_text = calls.read_text()
    backups = list((tmp_path / ".local/state/theatredb/production").glob("*.dump"))
    if backup_status != "0":
        assert result.returncode != 0
        assert " up -d " not in calls_text
        assert not backups
        return

    assert result.returncode == 0, result.stderr
    assert len(backups) == 1
    assert backups[0].read_text() == "database-backup\n"
    assert calls_text.index(" exec -T db ") < calls_text.index(" up -d --no-deps ") < calls_text.index("public-health")
    assert "--wait-timeout 180 app crawl-worker" in calls_text
