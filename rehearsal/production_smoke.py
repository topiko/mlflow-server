"""Verify a migrated server against the export ledger; writes require --write-test."""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

from common import sha256_file

import mlflow
from mlflow import MlflowClient


def verify_models(client, ledger, tracking_uri):
    versions = client.search_model_versions(max_results=1000)
    if versions.token:
        raise ValueError("Model catalog exceeds one page; paginate before verification")
    if not versions:
        raise ValueError("No registered models found; verify the target server")
    verified = []
    for version in versions:
        uri = client.get_model_version_download_uri(version.name, version.version)
        if uri.startswith("models:/m-"):
            model = client.get_logged_model(uri.removeprefix("models:/"))
            uri = model.artifact_location
        parsed = urlsplit(uri)
        if parsed.scheme != "mlflow-artifacts" or parsed.netloc:
            raise ValueError(f"Unexpected model storage URI: {uri}")
        prefix = parsed.path.lstrip("/").rstrip("/") + "/"
        objects = ledger.execute(
            "SELECT key, sha256 FROM objects WHERE substr(key, 1, ?) = ?",
            (len(prefix), prefix),
        ).fetchall()
        if not objects:
            raise ValueError(
                f"Model is missing from export: {version.name}/{version.version}"
            )
        with TemporaryDirectory() as directory:
            root = Path(mlflow.artifacts.download_artifacts(
                artifact_uri=f"models:/{version.name}/{version.version}",
                dst_path=directory,
                tracking_uri=tracking_uri,
                registry_uri=tracking_uri,
            ))
            for obj in objects:
                path = root / obj["key"].removeprefix(prefix)
                if sha256_file(path) != obj["sha256"]:
                    raise ValueError(f"Model checksum mismatch: {obj['key']}")
        verified.append({
            "name": version.name, "version": version.version, "files": len(objects)
        })
    return verified


def write_test(client):
    name = "filesystem-migration-validation"
    experiment = client.get_experiment_by_name(name)
    experiment_id = (
        experiment.experiment_id if experiment else client.create_experiment(name)
    )
    run = client.create_run(
        experiment_id, tags={"purpose": "filesystem migration validation"}
    )
    try:
        client.log_param(run.info.run_id, "artifact_store", "filesystem")
        client.log_metric(run.info.run_id, "verification", 1.0)
        payload = f"Filesystem migration verified: {uuid4().hex}\n".encode()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "migration-check.txt"
            path.write_bytes(payload)
            client.log_artifact(run.info.run_id, str(path))
            destination = Path(directory) / "download"
            destination.mkdir()
            copied = client.download_artifacts(
                run.info.run_id, path.name, str(destination)
            )
            assert sha256_file(Path(copied)) == hashlib.sha256(payload).hexdigest()
        assert client.get_run(run.info.run_id).data.metrics["verification"] == 1.0
        client.set_terminated(run.info.run_id)
    except Exception:
        client.set_terminated(run.info.run_id, status="FAILED")
        raise
    return {
        "experiment_id": experiment_id,
        "run_id": run.info.run_id,
        "artifact_uri": run.info.artifact_uri,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--write-test", action="store_true")
    args = parser.parse_args()
    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_registry_uri(args.tracking_uri)
    client = MlflowClient(
        tracking_uri=args.tracking_uri, registry_uri=args.tracking_uri
    )
    ledger = sqlite3.connect(f"file:{args.state}?mode=ro&immutable=1", uri=True)
    ledger.row_factory = sqlite3.Row
    report = {"started": datetime.now(timezone.utc).isoformat()}
    try:
        report["models"] = verify_models(client, ledger, args.tracking_uri)
    finally:
        ledger.close()
    if args.write_test:
        report["write_test"] = write_test(client)
    report["finished"] = datetime.now(timezone.utc).isoformat()
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "registered_models_verified": len(report["models"]),
        "model_files_verified": sum(model["files"] for model in report["models"]),
        "write_test": report.get("write_test"),
        "report": str(args.report),
    }, indent=2))


if __name__ == "__main__":
    main()
