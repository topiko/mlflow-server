# MLflow Server with Remote Access (via MinIO + Nginx + Certbot)

This repo contains a full setup of MLflow with artifact storage in MinIO, exposed securely over HTTPS with Nginx and Let’s Encrypt certificates (via Certbot).

## 📂 Project Layout

```
.
├── certbot/ # Dockerfile for Certbot
├── certbot_data/ # Persistent data for Let's Encrypt
│ └── creds.ini # Porkbun DNS credentials
├── docker-compose.yml # Main orchestration file
├── minio_data/ # MinIO artifact store (mounted volume)
├── mlflow/ # Dockerfile for custom MLflow image (with boto3)
├── mlflow_data/ # SQLite DB (MLflow backend store)
├── nginx/ # Nginx config and SSL storage
│ ├── nginx.conf
│ ├── htpasswd/
│ ├── ssl/
│ └── www/
├── README.md # This file
```

## 🌐 DNS & Exposure

1. Point your domain (e.g. ml.twohands.dev) to the server’s IP.
2. Example: using Porkbun

## 🔐 Certificates (Let's Encrypt via Certbot)

1. Certbot is set up as a service in docker-compose.yml.
2. Credentials for Porkbun DNS validation must be in certbot_data/creds.ini:

```
dns_porkbun_key=<your_api_key>
dns_porkbun_secret=<your_api_secret>
```

To obtain / renew certificates:

```
docker compose build certbot
docker compose run --rm certbot
```

Certificates will be stored under certbot_data/ and mounted into the Nginx container.

## 👤 Users & Authentication

`htpasswd nginx/htpasswd/users <username>`

## 📦 MLflow

The MLflow container is built from mlflow/Dockerfile.

This Dockerfile extends the official MLflow image and adds boto3 (needed for MinIO access).

Build once:

```
docker compose build mlflow_server
```

## 📦 MinIO (Artifact Store)

minio_data/ is mounted to persist artifacts.

Ensure the container runs with correct UID/GID (match your local user: id -u, id -g). Check the ownership of `minio_data/` the user needs to match this owner.

Initialize bucket -> see the service in `docker-compose.yml`

You should see a mlflow-bucket created in `minio_data/`. Verify MinIO bucket:
`docker exec -it mlflow_minio ls -R /data`
->

```
/data:
mlflow-bucket
```

## ⚙️ Backend Store

Metrics & metadata are stored in mlflow_data/mlflow.db (SQLite).

For production, you may want to swap SQLite with Postgres.

HTTP Basic Auth is enabled in Nginx. To add a user:

## 🚀 Running the Stack

Start:

```
sudo docker compose --env-file .env up -d
```

Stop:

```
sudo docker compose down
```

Services are configured with restart: always, so they come back automatically after a reboot.

## 🔧 Environment Variables

Create a .env file in the project root. Example:

```
# MLflow
MLFLOW_PORT=5000
MINIO_BUCKET_NAME=mlflow-bucket

# MinIO
MINIO_ROOT_USER=<your_minio_user>
MINIO_ROOT_PASSWORD=<your_minio_password>
MINIO_ADDRESS=:9000
MINIO_STORAGE_USE_HTTPS=False
MINIO_CONSOLE_ADDRESS=:9001
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001

# Postgres
POSTGRES_USER=<postgres_user>
POSTGRES_PASSWORD=<postgres_pass>
POSTGRES_DB=mlflowdb

# Certbot / Porkbun
PORKBUN_API_KEY=<your_porkbun_api_key>
PORKBUN_API_SECRET=<your_porkbun_api_secret>
```

## 🛠 Debugging

Check logs:

```
docker logs -f mlflow_server
docker logs -f mlflow_minio
docker logs -f mlflow_nginx
```

Check MLFlow is reachable:
`curl -k https://<your-domain>/`

Verify MinIO bucket:
`docker exec -it mlflow_minio ls -R /data`
->

```
/data:
mlflow-bucket
```

## ✅ Smoke Test

From a client (with Python + mlflow installed):

```python3
import mlflow

[mlflow.set_tracking_uri](mlflow.set_tracking_uri)("https://<your-domain>")
mlflow.set_experiment("smoke-test")

with mlflow.start_run():
    mlflow.log_metric("accuracy", 0.95)
    with open("hello.txt", "w") as f:
        f.write("Hello MLflow!")
    mlflow.log_artifact("hello.txt")

print("✅ metric and artifact logged!")
```

Check in the MLflow UI: https://<your-domain>.

## Backups & Restore

MLflow in this stack stores:

- Metadata (experiments, runs, registry) → in Postgres
- Artifacts (models, logs, metrics files) → in MinIO

Bak:

```
docker compose down
tar czf backup_postgres.tgz -C postgres_data .
tar czf backup_minio.tgz    -C minio_data .
```

Restore:

```
docker compose down
rm -rf postgres_data/* minio_data/*
tar xzf backup_postgres.tgz -C postgres_data
tar xzf backup_minio.tgz    -C minio_data
docker compose up -d
```

### Wipe:

```
docker compose down
sudo rm -rf postgres_data/* minio_data/* mlflow_data/*
docker compose --env-file .env up -d
# WAIT for the minio to be healhy
docker compose run --rm init-minio
```

### 📋 Migration Checklist

When deploying on a fresh server:

1. **Install** Docker & Compose: `./get_docker.sh`
2. **Clone** repo & prepare `.env`.
3. **Configure DNS** point domain (e.g. ml.twohands.dev) to server IP, see e.g., [here](https://github.com/topiko/dns).
4. **Build custom images** (only once, or push to a registry for reuse):
   ```
   docker compose build mlflow_server
   dockre compose build certbot
   ```
5. **Run certbot** (issue TLS certs): `docker compose run --rm certbot`
6. **Start stack**: `docker compose --env-file .env up -d`
7. **Verify health**
   ```
   docker compose ps
   ```
8. **Access** https://<your-domain> -> MLFlow UI loads.
9. **Run** smoke test --> metrics + artifacts visible.
