"""Read production through SSH and write a separate local snapshot."""

import argparse
import hashlib
import json
import shlex
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from common import (
    ARTIFACTS,
    COMPOSE,
    SNAPSHOT,
    database_args,
    prepare_workspace,
    require_local_docker,
    sha256_file,
)


def ssh_command(host, command):
    return [
        "ssh", "-T", "-C", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        host, shlex.join(command),
    ]


def copy_database(host):
    destination = SNAPSHOT / "metadata.dump"
    if destination.exists():
        raise SystemExit(f"Snapshot already exists: {destination}")
    command = [
        "docker", "exec", "mlflow-server-postgres-1", "sh", "-c",
        'exec pg_dump --format=custom --compress=3 --no-owner --no-acl '
        '--lock-wait-timeout=10s --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"',
    ]
    started = datetime.now(timezone.utc).isoformat()
    temporary = destination.with_suffix(".partial")
    print("Copying a consistent PostgreSQL dump; production stays online", flush=True)
    with temporary.open("wb") as output, (SNAPSHOT / "pg-dump.log").open("w") as log:
        subprocess.run(
            ssh_command(host, command), stdout=output, stderr=log, check=True
        )
    temporary.replace(destination)
    metadata = {
        "host": host,
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
        "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }
    (SNAPSHOT / "database.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Database snapshot: {metadata['bytes'] / 2**30:.2f} GiB", flush=True)


def query(sql):
    result = subprocess.run(
        [
            *COMPOSE, "exec", "-T", "postgres", "psql", "-X",
            "-v", "ON_ERROR_STOP=1", *database_args(), "-At", "-c", sql,
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def restore_database():
    destination = SNAPSHOT / "metadata.dump"
    metadata = json.loads((SNAPSHOT / "database.json").read_text())
    if sha256_file(destination) != metadata["sha256"]:
        raise ValueError("Database snapshot checksum mismatch")
    count = query(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )
    if count:
        raise SystemExit("Refusing to overwrite a nonempty local rehearsal database")
    print("Restoring the local rehearsal database; see pg-restore.log", flush=True)
    log_path = SNAPSHOT / "pg-restore.log"
    with destination.open("rb") as source, log_path.open("w") as log:
        subprocess.run(
            [
                *COMPOSE, "exec", "-T", "postgres", "pg_restore",
                "--exit-on-error", "--no-owner", "--no-acl", *database_args(),
            ],
            stdin=source, stdout=log, stderr=subprocess.STDOUT, check=True,
        )
    print("Local database restored", flush=True)


def representative_groups():
    groups = []
    versions = query("""
        SELECT coalesce(json_agg(row_to_json(v)), '[]') FROM
        (SELECT name, version, storage_location AS uri FROM model_versions
         WHERE status = 'READY' ORDER BY name, version) v
    """)
    for version in versions:
        groups.append({"kind": "registered_model", **version})
    runs = query("""
        SELECT coalesce(json_agg(row_to_json(r)), '[]') FROM (
          (SELECT run_uuid AS run_id, artifact_uri AS uri FROM (
            SELECT DISTINCT ON (experiment_id) run_uuid, artifact_uri, start_time
            FROM runs WHERE status = 'FINISHED' AND lifecycle_stage = 'active'
            ORDER BY experiment_id, start_time DESC
          ) recent ORDER BY start_time DESC LIMIT 6)
          UNION
          (SELECT run_uuid AS run_id, artifact_uri AS uri FROM runs
           WHERE status = 'FINISHED' AND lifecycle_stage = 'active'
           ORDER BY start_time ASC LIMIT 2)
        ) r
    """)
    groups.extend({"kind": "run", **run} for run in runs)
    models = query("""
        SELECT coalesce(json_agg(row_to_json(m)), '[]') FROM
        (SELECT model_id, artifact_location AS uri FROM logged_models
         ORDER BY model_id LIMIT 3) m
    """)
    groups.extend({"kind": "logged_model", **model} for model in models)
    for group in groups:
        uri = urlsplit(group["uri"])
        if uri.scheme != "mlflow-artifacts" or uri.netloc:
            raise ValueError(f"Needs explicit URI migration: {group['uri']}")
        prefix = uri.path.lstrip("/").rstrip("/")
        if not prefix or ".." in PurePosixPath(prefix).parts:
            raise ValueError(f"Unsafe artifact prefix: {prefix}")
        group["prefix"] = prefix + "/"
    return groups


def save_artifact(member, body, objects):
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or member.name not in objects:
        raise ValueError(f"Unexpected artifact path: {member.name}")
    if member.size != objects[member.name]["size"]:
        raise ValueError(f"Artifact size mismatch: {member.name}")
    target = ARTIFACTS.joinpath(*path.parts)
    if not target.resolve().is_relative_to(ARTIFACTS.resolve()):
        raise ValueError(f"Artifact escapes the snapshot: {member.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with target.open("wb") as output:
        while chunk := body.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def receive_artifacts(stream, objects):
    digests = {}
    remote_manifest = None
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile():
                raise ValueError("The artifact stream must contain only regular files")
            body = archive.extractfile(member)
            assert body is not None
            with body:
                if member.name == "__snapshot_manifest__.json":
                    remote_manifest = json.load(body)
                else:
                    digests[member.name] = save_artifact(member, body, objects)
    if remote_manifest is None or set(digests) != set(objects):
        raise ValueError("Incomplete artifact stream")
    if {obj["key"] for obj in remote_manifest} != set(objects):
        raise ValueError("Incomplete artifact manifest")
    for obj in remote_manifest:
        if digests[obj["key"]] != obj["sha256"]:
            raise ValueError(f"Artifact checksum mismatch: {obj['key']}")
    return remote_manifest


def copy_artifacts(host, max_gib):
    manifest_path = SNAPSHOT / "artifacts.json"
    if manifest_path.exists():
        raise SystemExit(f"Artifact snapshot already exists: {manifest_path}")
    reader = Path(__file__).with_name("s3_reader.py").read_text()
    command = ssh_command(
        host, ["docker", "exec", "-i", "mlflow_server", "python", "-B", "-c", reader]
    )
    inventory = subprocess.run(
        command,
        input=json.dumps({"action": "inventory", "groups": representative_groups()}),
        capture_output=True, text=True, check=True,
    )
    plan = json.loads(inventory.stdout)
    (SNAPSHOT / "artifact-inventory.json").write_text(json.dumps(plan, indent=2) + "\n")
    objects = {obj["key"]: obj for group in plan["groups"] for obj in group["objects"]}
    size = sum(obj["size"] for obj in objects.values())
    print(
        f"Selected {len(plan['groups'])} groups, {len(objects)} objects, "
        f"{size / 2**30:.2f} GiB", flush=True,
    )
    if size > max_gib * 2**30:
        raise SystemExit(
            f"Above {max_gib} GiB; inspect artifact-inventory.json before increasing it"
        )
    if not objects:
        raise SystemExit("No artifacts were found for the selected metadata")
    ARTIFACTS.mkdir(mode=0o700, exist_ok=True)
    with (SNAPSHOT / "artifact-copy.log").open("w") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log
        )
        assert process.stdin is not None and process.stdout is not None
        request = {"action": "copy", "objects": list(objects.values())}
        process.stdin.write(json.dumps(request).encode())
        process.stdin.close()
        try:
            remote_manifest = receive_artifacts(process.stdout, objects)
            if process.wait() != 0:
                raise RuntimeError("Remote artifact copy failed; see artifact-copy.log")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                process.wait()
    plan["objects"] = remote_manifest
    plan["copied_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(plan, indent=2) + "\n")
    print(f"Copied and verified SHA-256 for all {len(objects)} objects", flush=True)


def verify_local_artifacts():
    manifest = json.loads((SNAPSHOT / "artifacts.json").read_text())
    for obj in manifest["objects"]:
        if sha256_file(ARTIFACTS / obj["key"]) != obj["sha256"]:
            raise ValueError(f"Original artifact changed: {obj['key']}")
    print(f"All {len(manifest['objects'])} original artifact checksums match")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["database", "restore", "artifacts", "verify-files"]
    )
    parser.add_argument("--host", default="lefthand")
    parser.add_argument("--max-gib", type=float, default=5)
    args = parser.parse_args()
    prepare_workspace()
    if args.action == "database":
        copy_database(args.host)
    elif args.action == "verify-files":
        verify_local_artifacts()
    else:
        require_local_docker()
        if args.action == "restore":
            restore_database()
        else:
            copy_artifacts(args.host, args.max_gib)


if __name__ == "__main__":
    main()
