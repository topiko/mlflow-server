# MLflow server

Docker Compose deployment of MLflow with PostgreSQL for tracking metadata, a
persistent server directory for artifacts, and Nginx for HTTPS and HTTP Basic
authentication. Certificates are
issued by Let's Encrypt using Certbot and Porkbun DNS validation.

## Maintenance quick reference

Run commands from the repository root. Docker Compose reads `.env` automatically.
Use `sudo docker` if your account does not have Docker access.

| Task | Procedure |
| --- | --- |
| Renew the HTTPS certificate | [Certificate renewal](#certificate-renewal) |
| Replace Porkbun API keys | [Porkbun API credentials](#porkbun-api-credentials) |
| Enable scheduled renewal | [Automatic renewal](#automatic-renewal) |
| Add a user or change a login password | [MLflow login](#mlflow-login) |
| Back up or recover the server | [Backup and restore](#backup-and-restore) |
| Rehearse filesystem-artifact migration locally | [Local rehearsal guide](rehearsal/README.md) |
| Review the production migration and rollback | [Migration record](rehearsal/PRODUCTION-MIGRATION.md) |
| Check service status | `docker compose ps -a` |
| Follow server logs | `docker compose logs -f --tail=100 nginx mlflow_server postgres` |

**Current automation:** Compose waits for healthy PostgreSQL before starting
MLflow, and for healthy MLflow before starting Nginx. A host systemd timer for certificate
renewal is included; it must be installed and enabled once. Scheduled backups,
retention, and restore testing are not implemented.

## Architecture and persistent data

```text
Client --HTTPS / Basic Auth--> Nginx --> MLflow --> PostgreSQL (metadata)
                                          |
                                          +----> artifacts_data/ (files)
Certbot --Porkbun DNS API--> Let's Encrypt certificates --> Nginx
```

MLflow proxies artifact uploads and downloads (`--serve-artifacts`). Clients need
the tracking URL and Basic Auth login, not filesystem access to the server.
`./artifacts_data` is mounted at `/mlflow_artifacts` inside the MLflow container.
Experiments use `mlflow-artifacts:/...` locations so requests travel through MLflow.

Production migrated from MinIO on **15 October 2026**. The full artifact export was
checksum-verified, existing model downloads and new uploads passed, and no artifact
URI rewrites were required. The original MinIO data is retained for rollback; see
the [migration record](rehearsal/PRODUCTION-MIGRATION.md).
On `lefthand`, a copy of that record is also stored at
`/home/topiko/mlflow-backups/filesystem-migration-20261015T080226Z/PRODUCTION-MIGRATION.md`.

MLflow has **no published host port**. Nginx connects to `mlflow_server:5000` over
their shared Docker network; users and software connect to
`https://ml.twohands.dev` with their existing credentials. Nginx's port `80` only
redirects HTTP requests to HTTPS. Internal HTTP between containers is separate
from the public HTTPS connection.

Clients previously using `http://<server-IP>:5000` must switch to the HTTPS URL.
When applying this change to an existing deployment, recreate MLflow and Nginx
together so Nginx resolves the current container IP, then run the client smoke
test. Expect a brief interruption during recreation.

| Host path | Purpose |
| --- | --- |
| `postgres_data/` | PostgreSQL 14 database: experiments, runs, metrics, parameters, registry metadata |
| `artifacts_data/` | Active artifact files, including models and uploaded run files |
| `minio_data/` | Retained pre-migration MinIO data; no longer the active artifact store |
| `mlflow_data/` | MLflow working files, including `tmp/`; **not the active database** |
| `certbot_data/` | Certificate history, private keys, renewal configuration, and `creds.ini` |
| `certbot_log/`, `certbot_work/` | Certbot logs and working state |
| `nginx/htpasswd/users` | Basic Auth usernames and password hashes |
| `.env` | Database credentials and service configuration |

The data directories are **bind mounts**, not Docker-managed named volumes.
An old `mlflow_data/mlflow.db`, if present, is not used by the current configuration.

Source configuration lives in `docker-compose.yml`, `nginx/nginx.conf`,
`mlflow/Dockerfile`, and `certbot/Dockerfile`. The MLflow image uses
Python 3.12 with MLflow 3.17.2, boto3, and psycopg2-binary.
`init-minio.sh` is retained only for the archived rollback configuration.

The current deployment is on **`lefthand`**, in `/home/topiko/mlflow-server`.
It uses Docker Engine 28.2.2 and Compose 2.36.2 (inspected 18 September 2026).
The host's existing `certbot.timer` uses `/etc/letsencrypt/` for the older
`lefthand-ml.duckdns.org` certificate. This stack's `ml.twohands.dev` certificate is
in the separate `certbot_data/` directory and needs the container renewal job below.

## Initial setup

### 1. Prepare the host and configuration

Install Docker Engine with the Compose plugin (2.17 or newer), Git, and `htpasswd` (the
`apache2-utils` package on Debian/Ubuntu). The included Docker installer is named
`get-docker.sh`; review it before running `sudo sh get-docker.sh`.

Point your domain's DNS record to the server. The current domain is
`ml.twohands.dev`. To use another domain, change the domain and certificate paths
in `nginx/nginx.conf`, and the domain and contact email in the Certbot command in
`docker-compose.yml`. Enable API access for the domain in Porkbun.

Create `.env`:

```dotenv
POSTGRES_USER=mlflow
POSTGRES_PASSWORD=replace_with_another_long_random_password
POSTGRES_DB=mlflowdb

# Passed to Certbot by Compose; the configured authenticator uses creds.ini below.
PORKBUN_API_KEY=replace_with_porkbun_key
PORKBUN_API_SECRET=replace_with_porkbun_secret
```

Container addresses and health checks use fixed internal ports. `MLFLOW_PORT` and
the old `MINIO_*` variables are no longer used by the active stack. Keep the old
MinIO credentials with the rollback configuration until that retention period ends.
Credentials are interpolated directly into connection URLs; long random
alphanumeric passwords avoid URL-escaping problems with this configuration.

Prepare directories for a **new installation**:

```bash
chmod 600 .env
mkdir -p artifacts_data postgres_data mlflow_data/tmp \
  certbot_data certbot_log certbot_work nginx/htpasswd nginx/ssl
sudo chown -R 1001:1001 certbot_data certbot_log certbot_work
```

Certbot explicitly runs as UID/GID `1001:1001`. Match ownership to those IDs, or
change its Compose `user` setting to suit your host. MLflow currently runs as root
inside its container and must be able to write `artifacts_data/`. PostgreSQL manages
its own data-directory ownership. Runtime data and the local `.rehearsal/` snapshot
are excluded from Git.

### 2. Configure certificates and authentication

Create `certbot_data/creds.ini` with your Porkbun API credentials:

```ini
dns_porkbun_key=your_api_key
dns_porkbun_secret=your_api_secret
```

```bash
sudo chown 1001:1001 certbot_data/creds.ini
sudo chmod 600 certbot_data/creds.ini
docker compose build certbot mlflow_server
docker compose run --rm certbot
htpasswd -c nginx/htpasswd/users your_username
```

Use `htpasswd -c` **only when creating the file**; it overwrites an existing user
file. Nginx needs the certificate and a readable password file before starting.
DNS validation does not require Nginx to be running.

### 3. Start services

```bash
docker compose up -d --wait --wait-timeout 300
docker compose ps -a
```

MLflow waits for healthy PostgreSQL; Nginx waits for MLflow's `/health` endpoint.
Certbot uses the `tools` profile and runs only when explicitly requested, such as
`docker compose run --rm certbot`. No MinIO service or image pull is required.

To stop the stack without deleting data:

```bash
docker compose down --timeout 120
```

MLflow has `restart: always`; PostgreSQL and Nginx have
`restart: unless-stopped`. Compose also restarts Nginx when it explicitly updates
MLflow, so Nginx resolves the new container IP. Health-based dependency ordering
applies to Compose startup; Docker's reboot restarts do not rerun that ordering.

## Certificates and credentials

### Certificate renewal

The periodically expiring item is the **HTTPS certificate**, not normally the
Porkbun API key. Renewing a certificate does not require generating new API keys.

Inspect the stored certificates and test renewal against Let's Encrypt staging:

```bash
docker compose run --rm certbot certificates
docker compose run --rm certbot renew --dry-run
```

Renew certificates that are due, then reload Nginx so it serves the renewed files:

```bash
docker compose run --rm certbot renew && \
  docker compose exec -T nginx nginx -t && \
  docker compose exec -T nginx nginx -s reload
```

`renew` reuses the saved DNS authenticator and credentials-file path in
`certbot_data/renewal/`. It is safe to run regularly; it skips certificates that
are not due. Avoid routine use of `--force-renewal`. The dry run does not replace
the live certificate. If Nginx is stopped, start it after renewal instead of
reloading it.

Verify the certificate actually served to clients (replace the domain if needed):

```bash
openssl s_client -connect ml.twohands.dev:443 -servername ml.twohands.dev \
  </dev/null 2>/dev/null | openssl x509 -noout -dates -issuer
curl --fail --user your_username https://ml.twohands.dev/health
```

`curl` prompts for the login password and checks certificate validity. A `401`
without credentials is expected; `-k` would hide certificate problems.

### Automatic renewal

The units in `systemd/` check renewal twice daily, with up to one hour of randomized
delay. A missed run is caught up after the host returns. The service runs Certbot,
tests Nginx configuration, and reloads Nginx in sequence; a failed command stops
the sequence and marks the service failed. Reloading also happens when no
certificate was due, which is harmless and retries a previously failed reload.

Install once on the **Docker host**, after initial certificate issuance and stack
startup. Check existing cron jobs and timers first to avoid duplicate renewal jobs.

```bash
sudo install -m 644 systemd/mlflow-certbot-renew.service \
  systemd/mlflow-certbot-renew.timer /etc/systemd/system/
```

The service is configured for `/home/topiko/mlflow-server` on `lefthand` and reads
that checkout's `.env`. On another host, use
`sudo systemctl edit --full mlflow-certbot-renew.service` to change
`WorkingDirectory` to the absolute checkout path. If `command -v docker` is not
`/usr/bin/docker`, update the three `ExecStart` paths too. The service uses the
system Docker daemon as root; rootless Docker needs a user-service setup.

Test and enable it:

```bash
sudo systemctl daemon-reload
docker compose run --rm certbot renew --dry-run
sudo systemctl start mlflow-certbot-renew.service
sudo systemctl enable --now mlflow-certbot-renew.timer
systemctl list-timers mlflow-certbot-renew.timer
```

Check the result or diagnose a failure:

```bash
systemctl status mlflow-certbot-renew.service mlflow-certbot-renew.timer
sudo journalctl -u mlflow-certbot-renew.service -n 50 --no-pager
```

An inactive service after a successful run is normal for a one-shot job; the timer
stays active. Failures are recorded in systemd and the journal. Connect these to
your monitoring if you want email or push alerts. To pause renewal during
maintenance, stop the timer and wait for any active renewal to finish:

```bash
sudo systemctl stop mlflow-certbot-renew.timer
systemctl is-active mlflow-certbot-renew.service
# After maintenance:
sudo systemctl start mlflow-certbot-renew.timer
```

### Porkbun API credentials

When API keys are replaced or revoked:

1. Obtain the replacement key and secret in Porkbun and confirm domain API access.
2. Update both entries in `certbot_data/creds.ini` (use `sudoedit` if necessary).
   This is the credentials file passed to the configured DNS plugin; changing only
   `.env` is insufficient. Keep the duplicate `.env` entries in sync while they
   remain in Compose.
3. Keep the file owned by `1001:1001` with mode `600`.
4. Run `docker compose run --rm certbot renew --dry-run` to verify the replacement.
5. Revoke the old keys after successful validation, if they are still active.

No image rebuild or MLflow restart is needed for a credentials-file change.
See the [Porkbun plugin documentation](https://pypi.org/project/certbot-dns-porkbun/)
for the credentials format and API-access requirements.

### MLflow login

Add a user or change an existing user's password:

```bash
htpasswd nginx/htpasswd/users your_username
```

Remove a user:

```bash
htpasswd -D nginx/htpasswd/users your_username
```

The file is bind-mounted into Nginx; password-file changes apply to subsequent
requests without rebuilding containers. This is a shared Basic Auth gate, not
per-user MLflow permissions: authenticated users share access to the server.

### PostgreSQL credentials

Changing `.env` alone does **not** change the password in an existing database.
Open an interactive database session:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Use `\password` to change the current role's password, then `\q` to exit. Update
`.env` and run `docker compose up -d --force-recreate postgres mlflow_server nginx`.
Keep the existing username and database name. Pause clients during this operation;
avoid putting the new password in shell history.

Include Nginx when recreating MLflow so the proxy resolves its current container
IP. The dependency restart setting also handles explicit Compose restarts.

## Pinned versions and updates

External images are pinned to immutable multi-platform digests in Compose and the
Dockerfiles. MLflow and its Python base were updated on 21 October 2026; the other
image pins match the deployment inspected on 18 September 2026:

| Component | Pinned version |
| --- | --- |
| MLflow / Python | 3.17.2 / 3.12.15 |
| PostgreSQL | 14.19 |
| Nginx | 1.28.0 |
| Certbot / Porkbun plugin | 5.7.0 / 0.11.0 |

MinIO is no longer a runtime dependency. Its old server/client images were archived
on `lefthand` for rollback because upstream anonymous pulls were unavailable.

`mlflow/requirements.txt` and `certbot/requirements.txt` pin direct and transitive
Python dependencies. Their `requirements.in` files hold the direct dependencies.
The Python 3.12 MLflow and Python 3.14 Certbot pins match the deployed dependency
versions. MLflow's input requirement is `mlflow>=3`; the generated requirements
still pin the exact deployed release. The MLflow image refreshes system CA
certificates, bootstraps pip 26.2.2 from a SHA-256-verified wheel, and requires
`certifi>=2026.10.19` to address the eMudhra certificate incident.

To update deliberately:

1. Choose image releases and update their registry manifest digests. Compare the
   deployed versions before applying newer pins to existing data.
2. Edit the direct versions in the relevant `requirements.in`. With
   [uv](https://docs.astral.sh/uv/) installed, regenerate the locks:

   ```bash
   uv pip compile mlflow/requirements.in --python-version 3.12 --universal \
     --upgrade --no-annotate --output-file mlflow/requirements.txt
   uv pip compile certbot/requirements.in --python-version 3.14 --universal \
     --upgrade --no-annotate --output-file certbot/requirements.txt
   ```

   Match the Python targets and Certbot version to their base images when changing
   those images. Commit the input and generated requirements together.
3. Build and check on a test deployment first:

   ```bash
   docker compose config --quiet
   docker compose build --pull mlflow_server certbot
   docker compose run --rm certbot plugins
   docker compose up -d --wait --wait-timeout 300
   docker compose run --rm certbot renew --dry-run
   ```

   For MLflow upgrades requiring a database migration, test `mlflow db upgrade`
   against a restored database copy first. Pause writers, stop Nginx and MLflow,
   and take a fresh database backup before applying the migration to production.
   Recreate MLflow and Nginx together afterward.

   Run the client smoke test, including artifact download. Both Dockerfiles also
   run `pip check` during the build. Refresh pins deliberately for security updates;
   `docker compose pull` alone does not change a digest-pinned version.

## Backup and restore

**Backups are not automated.** The procedure below is a manual, full offline
backup. It trades downtime for a simple, consistent snapshot of PostgreSQL and
`artifacts_data/`. Downtime grows with the size of the artifact store.

PostgreSQL metadata and artifact files must be restored as a matching pair.
Backing up only the database does not preserve models and other artifacts.

### Create a backup

Pause client jobs and other writers first. Do not run Certbot or change
credentials while archiving. If installed, pause the renewal timer as described
above and wait for its service to finish before backup or restore; resume it after
maintenance. Choose an existing backup destination with enough free space for the
database and artifacts, preferably on another disk or off-host-mounted storage.
During MinIO rollback retention, `lefthand` has insufficient free space for another
full uncompressed copy; do not rely on compression to make it fit.

Set the destination below before running this Bash block. It stops on failure
without exiting your interactive shell:

```bash
(
  set -eu
  umask 077
  backup_root="/path/to/backup-storage/mlflow"
  test -d "$backup_root"
  backup="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$backup"
  printf 'Backup directory: %s\n' "$backup"

  # Record the deployment needed to read this physical backup.
  git rev-parse HEAD > "$backup/revision.txt"
  git diff HEAD > "$backup/local-changes.patch"
  docker compose images > "$backup/images.txt"

  docker compose down --timeout 120
  sudo tar --numeric-owner -czf - \
    postgres_data artifacts_data mlflow_data certbot_data .env nginx/htpasswd \
    > "$backup/state.tgz"
  tar -tzf "$backup/state.tgz" >/dev/null
  sha256sum < "$backup/state.tgz" > "$backup/state.tgz.sha256"
)
```

Bring services back up using the [startup procedure](#3-start-services), including
its readiness check. Do this even if archiving failed; treat a failed archive as
incomplete. Do not copy a running `postgres_data/` with `tar`. Include the whole
`artifacts_data/`, including hidden files, and the whole `certbot_data/`,
since `live/` contains symlinks into `archive/`.

Store an encrypted copy off the server: the archive contains database contents,
passwords, and TLS private keys. The checksum detects corruption; it does not
prove that a restore works. `images.txt` records image identifiers, not the images
themselves: retain the deployed images in a registry or with `docker image save`
if you need to recover the exact versions later. Keep any untracked deployment
configuration separately as well.

### Restore a backup

The `state.tgz` recipe above produces a **physical PostgreSQL backup**. Migration
checkpoints named `metadata.dump` are logical `pg_dump` archives; see the
[migration record](rehearsal/PRODUCTION-MIGRATION.md) for those checkpoints.

Restore a physical backup with PostgreSQL **14** and the
same MLflow version first; do upgrades separately. A physical backup
cannot be restored directly into a different PostgreSQL major version. Use a
logical dump (`pg_dump` / `pg_restore`) for that kind of migration.

1. Prepare Docker and the repository at the recorded revision. Review and apply
   any saved local configuration changes. On a new host, provide `.env` from the
   backup for Compose, and prepare the Certbot working/log directories and
   `nginx/ssl` as described in initial setup. Use an isolated host for a restore
   rehearsal, since the backup contains production credentials and certificates.
2. Set the archive path and verify it:

   ```bash
   backup="/path/to/backup-storage/mlflow/REPLACE_WITH_BACKUP_TIMESTAMP"
   sha256sum --check "$backup/state.tgz.sha256" < "$backup/state.tgz"
   tar -tzf "$backup/state.tgz"
   ```

   The checksum commands read the archive from standard input, so they also work
   after moving the backup to another host. On a new host, extract only `.env` with
   `tar -xzf "$backup/state.tgz" .env`, then `chmod 600 .env`.

3. Stop services, then move existing state aside instead of deleting it:

   ```bash
   (
     set -eu
     docker compose down --timeout 120
     rollback="$HOME/mlflow-before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
     mkdir -m 700 "$rollback"
     mkdir "$rollback/nginx"
     printf 'Previous state: %s\n' "$rollback"
     for path in postgres_data artifacts_data mlflow_data certbot_data .env nginx/htpasswd; do
       if [ -e "$path" ]; then
         sudo mv "$path" "$rollback/$path"
       fi
     done
     sudo tar --numeric-owner -xzpf "$backup/state.tgz" -C .
   )
   ```

   Stop if any command fails. Extract into empty paths, not over an existing
   database. Root extraction preserves the numeric ownership needed by the
   containers. If `.env` was restored with another host user's UID, change only
   its ownership to your current user and retain mode `600`.

4. Start PostgreSQL, verify readiness, and inspect startup logs:

   ```bash
   docker compose up -d --wait postgres
   docker compose exec postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
   docker compose logs --tail=100 postgres
   ```

   Proceed once PostgreSQL accepts connections. The artifact directory must have
   been restored alongside the database. If the restored certificate has expired,
   renew it before starting Nginx.

5. Start MLflow and Nginx with `docker compose up -d mlflow_server nginx`. Verify
   an **existing** experiment, run, and artifact download, then run the smoke test
   below. Keep the moved-aside state until recovery is confirmed. If rollback is
   needed, stop the stack, move the restored paths aside, and put all the original
   paths back as a matching set before restarting.

## Client smoke test

Install the Python MLflow client (preferably the server's version). In Bash,
configure authentication without recording the password in shell history:

```bash
export MLFLOW_TRACKING_URI=https://ml.twohands.dev
export MLFLOW_TRACKING_USERNAME=your_username
read -rsp 'MLflow password: ' MLFLOW_TRACKING_PASSWORD
export MLFLOW_TRACKING_PASSWORD
```

Run this Python snippet to test metadata, artifact upload, and artifact download:

```python
from pathlib import Path
from tempfile import TemporaryDirectory

import mlflow

mlflow.set_experiment("smoke-test")
with mlflow.start_run() as run:
    mlflow.log_param("source", "readme-smoke-test")
    mlflow.log_metric("accuracy", 0.95)
    mlflow.log_text("Hello MLflow!", "hello.txt")

with TemporaryDirectory() as directory:
    artifact = mlflow.artifacts.download_artifacts(
        run_id=run.info.run_id, artifact_path="hello.txt", dst_path=directory
    )
    assert Path(artifact).read_text() == "Hello MLflow!"

print(f"Metadata and artifact round trip succeeded: {run.info.run_id}")
```

Check the run in the MLflow UI. Clear the client password afterward with
`unset MLFLOW_TRACKING_PASSWORD`.

## Remaining improvements

These are review findings, not features already implemented:

1. **Limit the PostgreSQL host port.** Compose still publishes PostgreSQL (`5432`)
   on all host interfaces. Internet access depends on router and firewall rules.
   Remove the mapping if external administration does not need it, or bind it to
   localhost; MLflow reaches PostgreSQL over Docker's internal network.
2. **Automate paired backups and rehearse recovery (deferred).** Schedule backups with
   retention and encrypted off-host storage, and periodically restore them on an
   isolated host. If offline copies become too slow, use PostgreSQL logical dumps
   plus a coordinated artifact-directory backup, pausing writes to preserve consistency.
3. **Simplify configuration.** Remove duplicate Porkbun environment variables,
   add a checked-in `.env.example`, and remove stale Nginx HTTP-challenge settings
   (this setup uses DNS validation). Parameterize the domain, email, and UID/GID.
