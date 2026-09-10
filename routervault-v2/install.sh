#!/usr/bin/env bash
set -euo pipefail

TARGET="/opt/routervault"
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$HERE/VERSION")"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET/version-backups/pre-${VERSION}-${STAMP}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root."
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required before installing RouterVault."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is required."
  exit 1
fi

mkdir -p "$TARGET"

if [ -d "$TARGET/app" ]; then
  mkdir -p "$BACKUP"
  echo "Backing up current installation to $BACKUP"
  cp -a "$TARGET/app" "$BACKUP/app"
  for f in docker-compose.yml docker-compose.https.yml Dockerfile requirements.txt Caddyfile VERSION .env; do
    [ -e "$TARGET/$f" ] && cp -a "$TARGET/$f" "$BACKUP/$f"
  done
fi

# Preserve the live environment. Code/config templates are replaced by the frozen version.
for f in app Dockerfile requirements.txt docker-compose.yml VERSION; do
  rm -rf "$TARGET/$f"
  cp -a "$HERE/$f" "$TARGET/$f"
done

for f in docker-compose.https.yml Caddyfile enable-https.sh; do
  if [ -e "$HERE/$f" ]; then
    cp -a "$HERE/$f" "$TARGET/$f"
  fi
done

if [ ! -f "$TARGET/.env" ]; then
  echo "Creating fresh .env with generated credentials..."
  rand(){ openssl rand -base64 32 | tr -d '\n/=+' | cut -c1-40; }
  APP_SECRET="$(rand)"
  ADMIN_API_KEY="$(rand)"
  ADMIN_PASSWORD="$(rand | cut -c1-20)"
  DB_PASSWORD="$(rand)"
  MINIO_PASSWORD="$(rand)"
  SFTP_PASSWORD="$(rand)"
  cat > "$TARGET/.env" <<ENV
APP_NAME=RouterVault
APP_ENV=production
APP_SECRET=$APP_SECRET
ADMIN_API_KEY=$ADMIN_API_KEY
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASSWORD
POSTGRES_DB=routervault
POSTGRES_USER=routervault
POSTGRES_PASSWORD=$DB_PASSWORD
DATABASE_URL=postgresql+psycopg://routervault:$DB_PASSWORD@db:5432/routervault
MINIO_ROOT_USER=routervault
MINIO_ROOT_PASSWORD=$MINIO_PASSWORD
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=routervault
S3_SECRET_KEY=$MINIO_PASSWORD
S3_BUCKET=routervault
S3_REGION=us-east-1
SFTP_USER=rvbackup
SFTP_PASSWORD=$SFTP_PASSWORD
SFTP_PORT=2222
INGEST_DIR=/ingest
ENV
  chmod 600 "$TARGET/.env"
  echo "Fresh admin username: admin"
  echo "Fresh admin password: $ADMIN_PASSWORD"
fi

cd "$TARGET"
python3 -m py_compile app/*.py

echo "Building RouterVault $VERSION..."
docker compose build api
docker compose up -d --force-recreate
sleep 10

if ! curl -fsS http://127.0.0.1:8000/health >/tmp/routervault-health.txt; then
  echo "Health check failed."
  docker compose logs api --tail=100
  if [ -d "$BACKUP/app" ]; then
    echo "A backup is available at $BACKUP"
  fi
  exit 1
fi
cat /tmp/routervault-health.txt
echo

echo "RouterVault $VERSION installed successfully."
[ -d "$BACKUP" ] && echo "Previous code backup: $BACKUP"
echo "Existing PostgreSQL / MinIO / SFTP Docker volumes were preserved."
