"""Resumable S3-to-filesystem export for a staged production migration.

Run inside the existing MLflow image with only the destination and state directory
mounted writable. Source credentials come from its environment. S3 access is read-only.
"""

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import boto3
from common import sha256_file
from s3_reader import HashedReader

RESERVE = 10 * 2**30
BATCH_SIZE = 128


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def inventory(client, bucket):
    objects = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/"):
                if item["Size"]:
                    raise ValueError(f"Nonempty object has a directory key: {key}")
                continue
            path = PurePosixPath(key)
            if path.is_absolute() or ".." in path.parts or str(path) != key:
                raise ValueError(f"Object key is not a safe filesystem path: {key}")
            objects.append({"key": key, "size": item["Size"], "etag": item["ETag"]})
    return objects


def open_state(path, endpoint, bucket):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS source (endpoint TEXT NOT NULL, bucket TEXT NOT NULL)
    """)
    source = connection.execute("SELECT * FROM source").fetchone()
    if source is None:
        connection.execute("INSERT INTO source VALUES (?, ?)", (endpoint, bucket))
    elif (source["endpoint"], source["bucket"]) != (endpoint, bucket):
        raise ValueError("This export state belongs to another S3 source")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS objects (
            key TEXT PRIMARY KEY, size INTEGER NOT NULL, etag TEXT NOT NULL,
            sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL
        )
    """)
    connection.commit()
    return connection


def target_path(destination, key):
    target = destination / key
    if not target.resolve().is_relative_to(destination.resolve()):
        raise ValueError(f"Object escapes destination: {key}")
    return target


def is_current(obj, previous, destination):
    if previous is None or (previous["size"], previous["etag"]) != (
        obj["size"], obj["etag"]
    ):
        return False
    path = target_path(destination, obj["key"])
    if not path.is_file():
        return False
    stat = path.stat()
    return stat.st_size == obj["size"] and stat.st_mtime_ns == previous["mtime_ns"]


def copy_object(client, bucket, destination, obj):
    target = target_path(destination, obj["key"])
    target.parent.mkdir(parents=True, exist_ok=True)
    response = client.get_object(Bucket=bucket, Key=obj["key"], IfMatch=obj["etag"])
    temporary = None
    reader = HashedReader(response["Body"])
    try:
        if response["ContentLength"] != obj["size"]:
            raise ValueError(f"Source object changed: {obj['key']}")
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".__mlflow_export_partial_", delete=False
        ) as output:
            temporary = Path(output.name)
            shutil.copyfileobj(reader, output, length=1024 * 1024)
        if temporary.stat().st_size != obj["size"]:
            raise ValueError(f"Incomplete object: {obj['key']}")
        temporary.replace(target)
        return (
            obj["key"], obj["size"], obj["etag"], reader.digest.hexdigest(),
            target.stat().st_mtime_ns,
        )
    finally:
        response["Body"].close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def copy_pending(client, bucket, destination, connection, objects, workers):
    previous = {row["key"]: row for row in connection.execute("SELECT * FROM objects")}
    pending = [
        obj for obj in objects
        if not is_current(obj, previous.get(obj["key"]), destination)
    ]
    needed = sum(obj["size"] for obj in pending)
    available = shutil.disk_usage(destination).free
    log("copy_plan", objects=len(pending), bytes=needed, free_bytes=available)
    if needed + RESERVE > available:
        raise ValueError("Insufficient space for pending copies plus 10 GiB reserve")
    completed = 0
    copied_bytes = 0
    last_log = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(pending), BATCH_SIZE):
            batch = pending[offset:offset + BATCH_SIZE]
            free = shutil.disk_usage(destination).free
            if sum(obj["size"] for obj in batch) + RESERVE > free:
                raise ValueError("Free space fell below the export reserve")
            results = pool.map(
                lambda obj: copy_object(client, bucket, destination, obj), batch
            )
            for record in results:
                connection.execute(
                    "INSERT OR REPLACE INTO objects VALUES (?, ?, ?, ?, ?)", record
                )
                completed += 1
                copied_bytes += record[1]
            connection.commit()
            if time.monotonic() - last_log >= 10 or completed == len(pending):
                log(
                    "copy_progress", completed=completed,
                    total=len(pending), bytes=copied_bytes,
                )
                last_log = time.monotonic()
    return len(pending)


def remove_deleted(destination, connection, objects):
    """Remove only previously exported keys absent from the current S3 inventory."""
    current = {obj["key"] for obj in objects}
    deleted = 0
    for row in connection.execute("SELECT key FROM objects").fetchall():
        if row["key"] not in current:
            path = target_path(destination, row["key"])
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != destination:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            connection.execute("DELETE FROM objects WHERE key = ?", (row["key"],))
            deleted += 1
    connection.commit()
    return deleted


def verify_files(destination, connection, objects, workers):
    previous = {row["key"]: row for row in connection.execute("SELECT * FROM objects")}

    def verify(obj):
        record = previous.get(obj["key"])
        if record is None or not is_current(obj, record, destination):
            raise ValueError(f"Object needs copying: {obj['key']}")
        if sha256_file(target_path(destination, obj["key"])) != record["sha256"]:
            raise ValueError(f"Checksum mismatch: {obj['key']}")

    completed = 0
    last_log = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(objects), BATCH_SIZE):
            list(pool.map(verify, objects[offset:offset + BATCH_SIZE]))
            completed = min(offset + BATCH_SIZE, len(objects))
            if time.monotonic() - last_log >= 10 or completed == len(objects):
                log("verify_progress", completed=completed, total=len(objects))
                last_log = time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--remove-deleted", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        raise ValueError("workers must be positive")
    if (args.state / "cutover-complete.json").exists():
        raise SystemExit("Cutover is complete; refusing to overwrite live artifacts")
    os.umask(0o077)
    args.destination.mkdir(parents=True, exist_ok=True)
    args.state.mkdir(parents=True, exist_ok=True)
    database = args.state / "export.sqlite3"
    if not database.exists() and any(args.destination.iterdir()):
        raise ValueError("A first export requires an empty destination")
    endpoint = os.environ["MLFLOW_S3_ENDPOINT_URL"]
    bucket = os.environ["MINIO_BUCKET_NAME"]
    client = boto3.client("s3", endpoint_url=endpoint)
    summary = {"started": datetime.now(timezone.utc).isoformat(), "status": "running"}
    status_file = args.state / "status.json"
    status_file.write_text(json.dumps(summary, indent=2) + "\n")
    with open_state(database, endpoint, bucket) as connection:
        try:
            objects = inventory(client, bucket)
            summary.update(
                objects=len(objects), bytes=sum(obj["size"] for obj in objects)
            )
            log("inventory", **summary)
            (args.state / "inventory.json").write_text(json.dumps(objects) + "\n")
            summary["copied"] = copy_pending(
                client, bucket, args.destination, connection, objects, args.workers
            )
            summary["removed"] = (
                remove_deleted(args.destination, connection, objects)
                if args.remove_deleted else 0
            )
            verify_files(args.destination, connection, objects, args.workers)
            summary.update(
                status="verified", finished=datetime.now(timezone.utc).isoformat()
            )
        except Exception as error:
            summary.update(status="failed", error=str(error))
            raise
        finally:
            status_file.write_text(json.dumps(summary, indent=2) + "\n")
            log("result", **summary)


if __name__ == "__main__":
    main()
