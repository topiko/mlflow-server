#!/bin/sh
set -eu

# mc config to a writable place
export HOME=/tmp
export MC_CONFIG_DIR=/tmp/.mc
mkdir -p "$MC_CONFIG_DIR"

# Use env-based alias (no ~/.mc writes needed)
# Requires MC_HOST_local env in compose
echo "Ensuring bucket: ${MINIO_BUCKET_NAME}"
mc ls local || true
mc mb -p "local/${MINIO_BUCKET_NAME}" || true
mc ls "local/${MINIO_BUCKET_NAME}"
echo "Done."

