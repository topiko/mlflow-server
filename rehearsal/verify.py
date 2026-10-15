"""Verify proxy access from a client without the server's artifact filesystem."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import mlflow.sklearn
import numpy as np
from common import SNAPSHOT, STATE, TRACKING_URI, sha256_file
from sklearn.linear_model import LinearRegression

import mlflow
from mlflow import MlflowClient


def download(uri, destination, tracking_uri):
    return Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=uri,
            dst_path=str(destination),
            tracking_uri=tracking_uri,
            registry_uri=tracking_uri,
        )
    )


def verify_history(manifest, tracking_uri):
    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    checksums = {obj["key"]: obj["sha256"] for obj in manifest["objects"]}
    verified = set()
    counts = {"registered_model": 0, "logged_model": 0, "run": 0}
    empty_groups = []
    for group in manifest["groups"]:
        kind = group["kind"]
        if kind == "run":
            assert client.get_run(group["run_id"]).info.artifact_uri == group["uri"]
            uri = f"runs:/{group['run_id']}/"
        elif kind == "registered_model":
            client.get_model_version(group["name"], str(group["version"]))
            uri = f"models:/{group['name']}/{group['version']}"
        else:
            model = client.get_logged_model(group["model_id"])
            assert model.artifact_location == group["uri"]
            uri = f"models:/{group['model_id']}"
        if not group["objects"]:
            empty_groups.append({"kind": kind, "uri": group["uri"]})
            continue
        with TemporaryDirectory() as directory:
            root = download(uri, Path(directory), tracking_uri)
            for obj in group["objects"]:
                relative = obj["key"].removeprefix(group["prefix"])
                assert sha256_file(root / relative) == checksums[obj["key"]], obj["key"]
                verified.add(obj["key"])
        counts[kind] += 1
    assert verified == set(checksums), "Some copied artifacts were not tested"
    return {
        "groups_downloaded": counts,
        "files_verified": len(verified),
        "empty_groups": empty_groups,
    }


def verify_concurrent_reads(manifest, tracking_uri):
    candidates = [obj for obj in manifest["objects"] if 0 < obj["size"] <= 8 * 2**20]
    obj = max(candidates, key=lambda item: item["size"])

    def reader(_):
        with TemporaryDirectory() as directory:
            path = download(
                f"mlflow-artifacts:/{obj['key']}", Path(directory), tracking_uri
            )
            assert sha256_file(path) == obj["sha256"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(reader, range(16)))
    return {"workers": 8, "downloads": 16, "bytes_per_download": obj["size"]}


def verify_concurrent_writes(experiment_id, tracking_uri):
    def writer(index):
        client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
        run = client.create_run(experiment_id)
        try:
            client.log_param(run.info.run_id, "writer", str(index))
            client.log_metric(run.info.run_id, "score", float(index))
            payload = (f"writer={index}; run={run.info.run_id}\n" * 4096).encode()
            with TemporaryDirectory() as directory:
                path = Path(directory) / "shared-name.txt"
                path.write_bytes(payload)
                client.log_artifact(run.info.run_id, str(path))
                destination = Path(directory) / "download"
                destination.mkdir()
                copied = client.download_artifacts(
                    run.info.run_id, path.name, str(destination)
                )
                expected = hashlib.sha256(payload).hexdigest()
                assert sha256_file(Path(copied)) == expected
            stored = client.get_run(run.info.run_id)
            assert stored.data.params["writer"] == str(index)
            assert stored.data.metrics["score"] == float(index)
            assert stored.info.artifact_uri.startswith("mlflow-artifacts:/")
        except Exception:
            client.set_terminated(run.info.run_id, status="FAILED")
            raise
        client.set_terminated(run.info.run_id)
        return run.info.run_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        run_ids = list(pool.map(writer, range(8)))
    return {"workers": 8, "run_ids": run_ids, "filename": "shared-name.txt"}


def verify_linear_predictions(model_uri):
    loaded = mlflow.pyfunc.load_model(model_uri)
    predictions = loaded.predict(np.array([[3.0], [4.0]]))
    assert isinstance(predictions, np.ndarray)
    np.testing.assert_allclose(predictions, [6.0, 8.0])
    return predictions.tolist()


def verify_new_registered_model(experiment_id, tracking_uri):
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_registry_uri(tracking_uri)
    inputs = np.array([[0.0], [1.0], [2.0]])
    outputs = np.array([0.0, 2.0, 4.0])
    model = LinearRegression().fit(inputs, outputs)
    name = "filesystem-rehearsal-" + uuid4().hex[:12]
    with mlflow.start_run(experiment_id=experiment_id):
        info = mlflow.sklearn.log_model(
            model,
            name="linear-model",
            registered_model_name=name,
            signature=mlflow.models.infer_signature(inputs, outputs),
            pip_requirements=["scikit-learn==1.7.2"],
        )
    predictions = verify_linear_predictions(f"models:/{name}/1")
    return {"name": name, "model_uri": info.model_uri, "predictions": predictions}


def verify_previous_report(previous, tracking_uri):
    """On a repeat check, verify the earlier uploads survived a restart."""
    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    runs = previous["concurrent_writes"]["run_ids"]
    for index, run_id in enumerate(runs):
        payload = (f"writer={index}; run={run_id}\n" * 4096).encode()
        with TemporaryDirectory() as directory:
            path = client.download_artifacts(run_id, "shared-name.txt", directory)
            assert sha256_file(Path(path)) == hashlib.sha256(payload).hexdigest()
        assert client.get_run(run_id).info.status == "FINISHED"
    name = previous["new_registered_model"]["name"]
    assert name.startswith("filesystem-rehearsal-")
    verify_linear_predictions(f"models:/{name}/1")
    return {"previous_uploads_verified": len(runs), "previous_model_verified": name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracking-uri", default=TRACKING_URI)
    parser.add_argument("--manifest", type=Path, default=SNAPSHOT / "artifacts.json")
    parser.add_argument("--report", type=Path, default=STATE / "results/report.json")
    args = parser.parse_args()
    target = urlsplit(args.tracking_uri)
    if target.scheme != "http" or target.hostname not in {
        "127.0.0.1", "localhost", "mlflow"
    }:
        raise SystemExit("Verification writes data; use the local rehearsal server")
    # Nested models:/ resolution in MLflow 3.10 also consults the global URIs.
    mlflow.set_tracking_uri(args.tracking_uri)
    mlflow.set_registry_uri(args.tracking_uri)
    manifest = json.loads(args.manifest.read_text())
    report = {
        "started": datetime.now(timezone.utc).isoformat(),
        "tracking_uri": args.tracking_uri,
        "source_artifact_bytes": sum(obj["size"] for obj in manifest["objects"]),
    }
    if args.report.exists():
        previous = json.loads(args.report.read_text())
        report["previous_test_data"] = verify_previous_report(
            previous, args.tracking_uri
        )
        print("Previously uploaded artifacts and model passed", flush=True)
    report["history"] = verify_history(manifest, args.tracking_uri)
    print("Historical artifact and model downloads passed", flush=True)
    report["concurrent_reads"] = verify_concurrent_reads(manifest, args.tracking_uri)
    print("Concurrent downloads of the same file passed", flush=True)
    client = MlflowClient(
        tracking_uri=args.tracking_uri, registry_uri=args.tracking_uri
    )
    experiment = client.create_experiment("filesystem-rehearsal-" + uuid4().hex[:12])
    report["test_experiment_id"] = experiment
    report["concurrent_writes"] = verify_concurrent_writes(
        experiment, args.tracking_uri
    )
    print("Concurrent uploads to separate runs passed", flush=True)
    report["new_registered_model"] = verify_new_registered_model(
        experiment, args.tracking_uri
    )
    print("New model registration, download, and inference passed", flush=True)
    report["finished"] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {args.report}", flush=True)


if __name__ == "__main__":
    main()
