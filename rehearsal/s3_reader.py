"""Executed via SSH inside production; only lists and reads S3 objects.

Credentials stay in the existing MLflow container. No files are created there.
"""

import hashlib
import io
import json
import os
import sys
import tarfile

import boto3


class HashedReader:
    def __init__(self, body):
        self.body = body
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self.body.read(None if size < 0 else size)
        self.digest.update(chunk)
        return chunk


def inventory(s3, bucket, groups):
    result = []
    for group in groups:
        objects = []
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=group["prefix"]
        )
        for page in pages:
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith("/"):
                    objects.append({
                        "key": obj["Key"], "size": obj["Size"], "etag": obj["ETag"]
                    })
        result.append({**group, "objects": objects})
    return {"bucket": bucket, "groups": result}


def stream_objects(s3, bucket, objects):
    manifest = []
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for obj in objects:
            response = s3.get_object(
                Bucket=bucket, Key=obj["key"], IfMatch=obj["etag"]
            )
            reader = HashedReader(response["Body"])
            info = tarfile.TarInfo(obj["key"])
            info.size = obj["size"]
            info.mode = 0o600
            try:
                if response["ContentLength"] != obj["size"]:
                    raise ValueError(f"Object changed: {obj['key']}")
                archive.addfile(info, reader)
            finally:
                response["Body"].close()
            manifest.append({**obj, "sha256": reader.digest.hexdigest()})
        payload = json.dumps(manifest).encode()
        info = tarfile.TarInfo("__snapshot_manifest__.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))


def main():
    request = json.load(sys.stdin)
    s3 = boto3.client("s3", endpoint_url=os.environ["MLFLOW_S3_ENDPOINT_URL"])
    bucket = os.environ["MINIO_BUCKET_NAME"]
    if request["action"] == "inventory":
        json.dump(inventory(s3, bucket, request["groups"]), sys.stdout)
    elif request["action"] == "copy":
        stream_objects(s3, bucket, request["objects"])
    else:
        raise ValueError("Unsupported snapshot action")


if __name__ == "__main__":
    main()
