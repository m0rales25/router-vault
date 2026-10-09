# Router Vault

Router Vault is a self-hosted MikroTik monitoring, backup and administration platform built for multi-customer / multi-site environments.

## Current status

- **Production baseline:** Router Vault V2.0.1 - Locked Baseline
- **Current development line:** 3.0.0-dev
- **Module API:** v1
- **Primary runtime:** FastAPI + PostgreSQL 17 + MinIO + SFTP + Nginx
- **Target routers:** MikroTik RouterOS

The current tree includes the locked V2 production foundation, the 3.0.0-dev module-system foundation, downtime reporting, configurable application timezone support and the October 2026 performance work.

## Compatibility promise

Current server-side upgrades do **not** require existing MikroTik devices to be re-added or reconfigured unless a change explicitly says otherwise.

The following remain protected compatibility points:

- generated MikroTik installer / script body
- generated script version
- existing device API keys
- heartbeat endpoint used by installed routers
- SFTP credentials already installed on routers
- router scheduler configuration
- public Router API / SFTP settings

Device rename compatibility is handled server-side through historical filename aliases learned from existing backup rows.

## Architecture

### Public services

- **Router API:** public `:8090` -> Router Vault API `:8000`
- **Admin Web UI:** HTTPS `:8443`
- **SFTP backup ingress:** public `:3334`
- **MinIO Console:** internal only

The API gateway exposes router-facing API routes and `/health`. Administrative access remains behind the HTTPS admin endpoint and its source-IP allowlist.

### Core services

- `api` - FastAPI application and background workers
- `db` - PostgreSQL 17
- `minio` - backup object storage
- `sftp` - MikroTik backup ingress
- `gateway` - Nginx reverse proxy / TLS endpoint

Persistent application modules are stored under `/srv/routervault-modules`.

## Main features

### Device monitoring

- heartbeat-based UP / DOWN monitoring
- configurable heartbeat and DOWN thresholds
- CPU, free-memory, RouterOS version and uptime collection
- status history
- device incidents
- alert acknowledgement and mute controls
- customer and site scoping

### Downtime reporting

Reports include a dedicated **Downtime** view with:

- outage count per router
- recorded downtime
- average outage duration
- maximum outage duration
- availability percentage
- last DOWN time
- open / recovered incident state
- customer, site, device and date filters
- CSV / XLSX export
- site-outage correlation for devices that fail together
- incident drill-down

Historical monitoring data is retained. Reports can distinguish current-policy data from older monitoring periods so previous heartbeat-flapping data does not need to be destructively deleted.

### Backups

- SFTP backup ingestion
- file-stability check before hash/upload
- RSC + binary backup-pair integrity checks
- MinIO object storage
- latest-backup tracking
- bulk download
- retention support
- portable application backup / restore
- device-name alias compatibility after router rename

### Reports

- overview
- device inventory
- backup history
- backup compliance
- availability
- downtime / incidents
- CSV and XLSX exports

### Administration

- Customers / Sites / Devices
- Users and roles
- customer-restricted access
- global search
- audit log
- monitoring settings
- backup settings
- notifications / SMTP
- admin IP allowlist
- application timezone
- module management

## Application timezone

Router Vault uses the configured application timezone for user-facing dates and scheduled reporting.

The default is:

```text
Europe/Athens
```

Templates keep some legacy filter names for compatibility, but conversion is based on the configured application timezone rather than a permanently hardcoded timezone.

## Performance

The October 2026 optimization removed several N+1 query patterns and unnecessary request-time work.

Current optimizations include:

- batch 1d / 7d / 30d availability calculation
- batch backup-compliance / integrity processing
- batch status-sample / open-incident lookups in the monitoring worker
- lightweight application-setting cache with invalidation on update
- database indexes for backup and incident reporting
- planner statistics refresh
- removal of unnecessary `last_backup_at` reconciliation from ordinary page views

`devices.last_backup_at` continues to be maintained when backups are ingested, deleted, restored or cleaned up.

## Module system

The development line includes **Module API v1**.

Modules are trusted Python extensions loaded into the API process. They can register UI/routes/hooks according to the module-system contract and declared permissions.

Current permission vocabulary includes:

- `devices.read`
- `devices.write`
- `customers.read`
- `sites.read`
- `backups.read`
- `settings.read`
- `settings.write`
- `network.generate`
- `module.storage`

Supported module access roles:

- `admin`
- `read_only`
- `customer`

## Security notes

- Repository is intended to remain private.
- Do not commit `.env`, passwords, API keys, certificates, private keys or database dumps.
- Uvicorn access logging is disabled for the API container so RouterOS-compatible token-bearing URL paths are not written to the normal access log.
- MinIO Console is not publicly published.
- Admin HTTPS access remains protected by the configured IP allowlist.
- Existing router secrets are not automatically rotated during normal application upgrades.

## Repository layout

```text
app/                FastAPI application, templates and application logic
deploy/             Debian deployment helpers
gateway/            Nginx gateway configuration
mikrotik/           MikroTik-related project material
routervault-v2/     historical/reference V2 tree
docker-compose.yml  production service definition
Dockerfile          API image build
requirements.txt    Python dependencies
VERSION             application version marker
CHANGELOG.md        development/change history
```

## Deployment

Typical production rebuild:

```bash
cd /opt/routervault
docker compose build api
docker compose up -d api
docker compose ps
```

Health check:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok","service":"Router Vault"}
```

## Git workflow

Before committing production changes:

```bash
cd /opt/routervault
git status
git diff --check
docker compose exec -T api python -m compileall -q app
```

Then stage only the intended files, commit and push:

```bash
git add <files>
git commit -m "Describe the change"
git push origin main
```

Do not use production-only manual edits as a substitute for source control.

## Production baseline

The locked production foundation remains:

**Router Vault V2.0.1 - Locked Baseline**

Current development continues on:

**Router Vault 3.0.0-dev**

Module API:

**v1**
