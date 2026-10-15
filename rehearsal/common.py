"""Paths for the isolated, local-only migration rehearsal."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".rehearsal"
SNAPSHOT = STATE / "snapshot"
ARTIFACTS = SNAPSHOT / "artifacts"
DBNAME = "mlflow_rehearsal"
DBUSER = "rehearsal"
TRACKING_URI = "http://127.0.0.1:15000"
COMPOSE = [
    "docker",
    "compose",
    "--project-name",
    "mlflow-rehearsal",
    "--project-directory",
    str(ROOT),
    "-f",
    str(ROOT / "compose.rehearsal.yml"),
]


def prepare_workspace():
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)
    SNAPSHOT.mkdir(mode=0o700, exist_ok=True)
    (STATE / "results").mkdir(mode=0o700, exist_ok=True)


def database_args():
    return ["-U", DBUSER, "-d", DBNAME]


def require_local_docker():
    """Refuse to restore data through a remote Docker endpoint."""
    override = os.environ.get("DOCKER_HOST", "")
    context = subprocess.run(
        ["docker", "context", "inspect"],
        capture_output=True,
        text=True,
        check=True,
    )
    endpoint = json.loads(context.stdout)[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://") or (
        override and not override.startswith("unix://")
    ):
        raise SystemExit("Use a local Unix-socket Docker context for the rehearsal")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
