import os
import shlex
import subprocess  # ruff: ignore[suspicious-subprocess-import] - Run the deploy script with fake tools.
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy-production"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-production.yaml"
IMAGE = "ghcr.io/hno2/theatredb@sha256:" + "a" * 64


def test_production_workflow_deploys_pushes_to_main() -> None:
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in workflow["on"]
    assert "github.event_name != 'workflow_dispatch' || !inputs.publish_only" in workflow["jobs"]["deploy"]["if"]


def test_public_health_retries_transient_404() -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        requests = 0

        def do_GET(self) -> None:
            type(self).requests += 1
            self.send_response(404 if self.requests == 1 else 200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            curl_line = next(line for line in SCRIPT.read_text().splitlines() if line.startswith("curl "))
            args = shlex.split(curl_line.removesuffix(" \\"))
            result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - Local test server only.
                [*args, f"http://127.0.0.1:{server.server_port}/_stcore/health"],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            server.shutdown()
            thread.join()
    assert result.returncode == 0, result.stderr
    assert HealthHandler.requests == 2


@pytest.mark.parametrize("ssh_status", [0, 1, 127, 255])
def test_deploy_workflow_reports_ssh_status(tmp_path: Path, ssh_status: int) -> None:
    ssh = tmp_path / "ssh"
    ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALLS"\nexit "$SSH_STATUS"\n')
    ssh.chmod(0o755)
    calls = tmp_path / "calls"
    command = textwrap.dedent(WORKFLOW.read_text().rsplit("        run: |\n", 1)[1])
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - Only test-controlled commands and arguments.
        ["/bin/bash", "-e", "-c", command],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SSH_KEY": "test-key",
            "SSH_KNOWN_HOSTS": "test-host-key",
            "SSH_TARGET": "deploy@example.com",
            "SSH_PORT": "22",
            "IMAGE": IMAGE,
            "CALLS": str(calls),
            "SSH_STATUS": str(ssh_status),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == ssh_status
    assert calls.read_text().splitlines()[-2:] == ["deploy@example.com", IMAGE]
    assert ("Check the production deploy key's forced command" in result.stderr) == (ssh_status == 127)


@pytest.mark.parametrize(
    "command,backup_status,health_status",
    [(IMAGE, "0", "0"), (IMAGE, "1", "0"), (IMAGE + " extra", "0", "0"), (IMAGE, "0", "22")],
)
def test_production_deploy(tmp_path: Path, command: str, backup_status: str, health_status: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "flock": "exit 0",
        "curl": 'echo "public-health $*" >> "$CALLS"; exit "$HEALTH_STATUS"',
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
            "HEALTH_STATUS": health_status,
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

    assert "--retry 5 --retry-connrefused --retry-all-errors" in calls_text
    if health_status != "0":
        assert result.returncode == int(health_status)
        assert " ps -q crawl-worker " not in calls_text
        assert len(backups) == 1
        return

    assert result.returncode == 0, result.stderr
    assert len(backups) == 1
    assert backups[0].read_text() == "database-backup\n"
    assert calls_text.index(" exec -T db ") < calls_text.index(" up -d --no-deps ") < calls_text.index("public-health")
    assert "--wait-timeout 180 app crawl-worker" in calls_text
