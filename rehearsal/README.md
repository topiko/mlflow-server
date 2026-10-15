# Local filesystem-artifact migration rehearsal

This runs a copy of production metadata with a representative artifact subset.
The local MLflow server stores artifacts in a bind-mounted directory instead of
MinIO. It uses the same PostgreSQL 14.19 and MLflow 3.10.0 versions as production.

**Local UI:** <http://127.0.0.1:15000>

Production has since completed this migration; see the
[production record](PRODUCTION-MIGRATION.md). The existing local snapshot and its
verification commands remain usable. The MinIO acquisition procedure below is
historical: `snapshot.py artifacts` expects the pre-migration S3 environment and
cannot export from today's filesystem-backed production container. A fresh copy
now needs a PostgreSQL dump and files from the active `artifacts_data/` directory.

## Isolation and data layout

- `compose.rehearsal.yml` is a separate Compose file and project,
  `mlflow-rehearsal`. It does not load the production Compose file or credentials.
- PostgreSQL has no published port. MLflow publishes only `127.0.0.1:15000`, with
  HTTP and no authentication for local testing. The database and test client use
  an internal Docker network; MLflow also has a frontend network for host access.
- Persistent rehearsal data is under `.rehearsal/`, which is Git-ignored and
  created with mode `700`. The database copy contains production metadata: keep it
  private.
- The SSH exporter only runs `pg_dump`, lists S3 objects, and reads them. Source
  credentials remain inside the existing containers on `lefthand`.
- `check` is a separate client container. It receives the checksum manifest but
  **does not mount the artifact directory**, so successful downloads demonstrate
  proxied access over HTTP.

```text
.rehearsal/
  snapshot/
    metadata.dump             Complete logical PostgreSQL snapshot
    database.json             Snapshot timestamps, size, and SHA-256
    artifact-inventory.json   Selected metadata groups and object inventory
    artifacts.json            Completed export manifest with source SHA-256 hashes
    artifacts/                Filesystem artifact store, preserving S3 object keys
    pg-dump.log
    pg-restore.log
    artifact-copy.log
  postgres_data/              Restored local PostgreSQL database
  results/report.json         Latest successful client verification report
```

The copied artifact directory also receives **new local test artifacts**. The
dump and the original-object manifest remain available for verification.

## How the pre-migration snapshot was prepared

Run from the repository root. Requirements: Python 3.10+, Docker Engine with
Compose, and SSH/Docker access to `lefthand`. Use a **local Docker context**; check
`docker info --format '{{.Name}}'` before starting. The Python restore helper also
rejects remote Docker endpoints.

If this shell predates your addition to the Docker group, open a new login
session, or wrap Docker-dependent commands with `sg docker -c 'COMMAND'`.

1. Copy the complete database. It is a consistent logical dump while production
   remains online; no production data directory is copied directly:

   ```bash
   python3 rehearsal/snapshot.py database
   ```

2. Start only the local database, then restore the dump:

   ```bash
   docker compose -p mlflow-rehearsal -f compose.rehearsal.yml up -d --wait postgres
   python3 rehearsal/snapshot.py restore
   ```

   Restore verifies the dump checksum, uses local credentials, ignores production
   ownership/ACLs, and refuses to overwrite a nonempty local database.

3. Copy representative artifacts selected from the **restored** metadata:

   ```bash
   python3 rehearsal/snapshot.py artifacts
   ```

   Selection: all ready registered-model versions, six recent finished runs from
   distinct experiments, two old finished runs, and three additional logged models.
   Object keys are preserved under `snapshot/artifacts/`. Downloads use the
   inventoried ETag as an `If-Match` condition and verify SHA-256 against the source
   stream. A changed/deleted object or failed transfer aborts the export.

   The default cap is 5 GiB. If exceeded, inspect `artifact-inventory.json` before
   explicitly increasing `--max-gib`. Increasing this cap does **not** turn the
   representative selection into a full-bucket migration.

4. Build and start the local MLflow server:

   ```bash
   docker compose -p mlflow-rehearsal -f compose.rehearsal.yml build mlflow
   docker compose -p mlflow-rehearsal -f compose.rehearsal.yml up -d --wait --wait-timeout 300
   ```

Copy commands refuse to replace a completed snapshot. Once prepared, use the
commands below rather than repeating the copy/restore steps. A separate checkout
can preserve another data snapshot, but stop this rehearsal first: these helpers
use a fixed Compose project name and are intended for one local rehearsal at a time.

## Verify and use the local copy

Set the client container's file ownership to match your account, then run checks:

```bash
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
docker compose -p mlflow-rehearsal -f compose.rehearsal.yml run --rm check
python3 rehearsal/snapshot.py verify-files
```

The checks cover:

- Original run, logged-model, and registered-model URI resolution and downloads.
- SHA-256 equality for every exported object.
- Sixteen downloads of the same completed artifact with eight concurrent workers.
- Eight concurrent writers using the same filename in **different runs**, with
  metrics, parameters, and upload/download verification.
- Logging and registering a small new model, downloading it, and checking predictions.
- On a repeat check, downloads and predictions using the previous check's uploads
  and model. Each successful check adds a local `filesystem-rehearsal-*` experiment.

Check persistence by restarting the local stack and rerunning verification:

```bash
docker compose -p mlflow-rehearsal -f compose.rehearsal.yml restart
docker compose -p mlflow-rehearsal -f compose.rehearsal.yml up -d --wait --wait-timeout 300
docker compose -p mlflow-rehearsal -f compose.rehearsal.yml run --rm check
```

For other clients on this laptop, set **both** `MLFLOW_TRACKING_URI` and
`MLFLOW_REGISTRY_URI` to `http://127.0.0.1:15000`. Clear production authentication
variables for that session. MLflow 3.10's nested `models:/` resolution consults the
global URIs even when a client or download call supplies explicit URLs.

Most metadata is visible in the UI, but only the selected artifacts were copied.
An artifact download from an unselected run/model is expected to fail locally.

Stop the local stack while retaining the copy:

```bash
docker compose -p mlflow-rehearsal -f compose.rehearsal.yml down
```

## Rehearsal results: 15 October 2026

- Complete database restored successfully; compressed dump approximately 228 MiB.
- 91 source objects, approximately 37 MiB, copied with matching SHA-256 hashes.
- All 20 selected groups downloaded successfully: 9 registered-model versions,
  3 additional logged models, and 8 runs.
- Concurrent read/write checks and new model registration/inference passed.
- The same checks passed after restarting PostgreSQL and MLflow, including the
  previous check's uploaded files and registered model.
- No artifact URI rewrites were required for this selection.

This verifies the filesystem-backed architecture on real copied data. It is not a
full migration of the approximately 124 GiB artifact store, a production load test,
or an inference test for every existing model. The existing models were downloaded
and checksum-verified; prediction testing used a small model created by the check.

Concurrent overwrites of the **same artifact path** and reads during overwrites
are not supported guarantees. Use completed artifacts and distinct run/version paths.

## Before production cutover

The cutover is complete; see the [production record](PRODUCTION-MIGRATION.md).
The considerations below explain how it was performed and apply to future migrations.

The database dump and artifact export were taken at different times while
production remained online. They are useful for rehearsal, not a coordinated
point-in-time backup of the whole stack.

For cutover: export the full bucket through S3, preserve object keys, verify the
complete inventory/checksums, pause writers for a final synchronization and paired
database backup, then change MLflow's artifact destination to the mounted directory.
Validate existing registered models and new client uploads through HTTPS. Retain
the original MinIO state and images for rollback until verification is complete.

## Helper checks

```bash
uv venv --python 3.10 .rehearsal/lint-env
uv pip sync --python .rehearsal/lint-env/bin/python mlflow/requirements.txt
.rehearsal/lint-env/bin/python -m unittest discover -s rehearsal -p 'test_*.py' -v
uvx ruff check rehearsal
uvx ty check --python .rehearsal/lint-env/bin/python rehearsal
```
