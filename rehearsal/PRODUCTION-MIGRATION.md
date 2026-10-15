# Production filesystem-artifact migration

## Completed on lefthand: 15 October 2026

**Production now uses `artifacts_data/`, served through MLflow over HTTPS.**
PostgreSQL remains the metadata store. No existing artifact URI rewrites or
database schema upgrades were required.

The running MLflow, PostgreSQL, and Nginx images were reused. MinIO is stopped with
its restart policy set to `no`; its original data and archived images are retained
for rollback. Old MinIO/client containers may appear as stopped Compose orphans.
They are not dependencies of the active stack.

## Backup and export locations

All paths in this section are on `lefthand`.

| Item | Path |
| --- | --- |
| Active artifact directory | `/home/topiko/mlflow-server/artifacts_data/` |
| Original MinIO data | `/home/topiko/mlflow-server/minio_data/` |
| Migration directory | `/home/topiko/mlflow-backups/filesystem-migration-20261015T080226Z/` |
| Final cutover backup directory | Migration directory: `cutover-20261015T082634Z/` |
| Final logical PostgreSQL dump | Cutover directory: `metadata.dump` |
| Dump checksum and metadata | Cutover directory: `metadata.dump.sha256`, `database.json` |
| Archived configuration | Cutover directory: `.env`, `docker-compose.yml`, `nginx/`, `containers.json` |
| Cached-image rollback configuration | Cutover directory: `rollback.compose.yml` |
| MinIO server/client image archive | Migration directory: `minio-images.tar`, plus checksum and image-reference files |
| Export inventory and checksum ledger | Migration directory: `state/inventory.json`, `state/export.sqlite3` |
| Final export log | Migration directory: `final-export.log` |
| Production validation report | Migration directory: `results/production-smoke.json` |
| Completion record | Migration directory: `cutover-complete.json` |

The backup directory is private and contains credentials. The database dump is
approximately 228 MiB. `pg_dump` completed, `pg_restore --list` read the archive,
and its SHA-256 was recorded and checked before deployment. The final dump was
taken with MLflow stopped and client writers paused.

An earlier online checkpoint also exists at
`/home/topiko/mlflow-backups/pre-filesystem-live-20261015T072046Z/`.
The **cutover** checkpoint is the appropriate migration baseline.

These server-local copies protect the migration and allow rollback; off-host
backups are still a separate task. Keep the original MinIO data and image archive
until the retention decision is made.

## Verification performed

- **130,593 objects; 133,417,152,937 bytes (approximately 124.3 GiB)** exported.
- Each file was read back and compared with the SHA-256 of its source stream.
- A final pass after the write pause verified all objects again; zero changed or
  deleted source objects were found in that pass.
- All recorded experiment, run, logged-model, and registered-version storage
  locations used `mlflow-artifacts:` immediately before cutover.
- All **9 registered-model versions**, containing 54 files, downloaded through
  the production MLflow API with matching checksums while MinIO was stopped.
- A new metric and artifact upload/download round trip passed. The validation run
  is `2c566db4a43b4ab6837ef1ec5ad624e0` in experiment `74`.
- The operator confirmed the test artifact through authenticated HTTPS, and
  confirmed existing models and metrics were functional.
- PostgreSQL and MLflow report healthy. Nginx configuration validation and its
  upstream health request passed. MLflow has no published host port and no S3
  credentials in its container environment.

The earlier [local rehearsal](README.md) additionally tested eight concurrent
writers, concurrent downloads, model registration/inference, and container restarts.

## Export implementation and retirement

`export_full.py` uses a SQLite **transfer ledger**, not an MLflow backend or an
artifact store. It records source keys, sizes, ETags, SHA-256 hashes, and destination
modification times. It uses `If-Match` reads, atomic file replacement, and a full
checksum verification pass. The export job was limited to two CPU cores and 1 GiB
of memory, and it never wrote to source S3 objects.

The completed export container has been removed. A `state/cutover-complete.json`
marker makes the exporter refuse further synchronization into the live directory.
**Do not resume the old S3 export after cutover:** MinIO is now stale and doing so
could overwrite newer filesystem artifacts. The ledger describes the cutover
snapshot, not files added afterward.

## Rollback considerations

Normal writes have resumed. A simple switch back to MinIO would strand artifacts
created or changed after cutover. A rollback now requires:

1. Pause writers and preserve the current PostgreSQL metadata and filesystem
   artifacts before making changes.
2. Start the retained MinIO instance and synchronize the active filesystem store
   back through S3, preserving object keys and verifying contents. This is a
   separate reverse-migration operation; the read-only exporter cannot do it.
3. Review and activate the archived `rollback.compose.yml`, using project name
   `mlflow-server`, project directory `/home/topiko/mlflow-server`, and the archived
   environment file. It pins the exact pre-cutover image IDs and disables pulls.
4. Verify historical and post-cutover artifacts through HTTPS before resuming
   clients again.

PostgreSQL did not require a schema migration for this cutover. Do not restore the
old database dump for a routine artifact-storage rollback: that would discard
post-cutover metadata. Database disaster recovery is a separate operation using
an empty target database and a matching artifact snapshot.

The rollback Compose file is intended for the same host's preserved images. The
MinIO archive can be loaded with `docker image load --input minio-images.tar` if
needed; verify its checksum first. Docker image loading may not retain repository
digests, so use the recorded local image IDs/tags. A fresh-host recovery also needs
the remaining MLflow, PostgreSQL, Nginx, and Certbot images or equivalent rebuilt
references.
