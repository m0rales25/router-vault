# Router Vault

Router Vault is a self-hosted MikroTik monitoring and backup platform.

This tree is based on the **Router Vault V2.0.1 Locked Baseline** and currently includes the **3.0.0-dev Module System foundation**.

## Compatibility promise for this release

This release **does not change the generated MikroTik installer function** and does not require existing routers to be re-added or reconfigured.

The following remain untouched by the upgrade itself:

- generated MikroTik script body
- generated script version
- existing device API keys
- existing router heartbeat endpoint
- existing router SFTP credentials
- existing router scheduler configuration
- existing public API/SFTP settings

Device rename compatibility is handled **server-side** with historical filename aliases learned from existing backup rows.

## Public architecture

- Router API: existing public `:8090` -> Router Vault VM `:8000`
- Admin Web UI: HTTPS `:8443`
- SFTP backup ingress: existing public `:3334`
- MinIO Console: internal only, no host `:9001` publication

The API gateway exposes `/api/...` and `/health`. Login and administration routes are not exposed on the API port.

## V2.0.1 RC1 changes

### UI

- official branding: **Router Vault**
- new shared responsive layout
- simplified top navigation: Dashboard / Alerts / Reports / Administration
- global search in the header
- combined Reports and Reports V2
- Settings split into General / Monitoring / Backups / Notifications / Access & Security / System
- cleaner Device, Customer, Site, Alerts, Users, Audit and Login pages
- compact and consistent Athens timestamps

### Backup reliability

- ingest waits for a file to remain stable before hashing/uploading it
- sanitized-name parsing remains compatible with names such as `protostar(AD122)`
- device-name alias history keeps old installed router scripts working after a Device rename
- aliases are backfilled from existing Backup filenames
- `devices.last_backup_at` is reconciled from the `backups` table
- backup compliance uses the `backups` table and Europe/Athens time

### Monitoring

- expected backup default aligned to 03:00
- Daily Report explicitly uses Europe/Athens
- acknowledgement resets when a genuinely new incident starts
- orphan alert cleanup remains enabled

### Device management

- Add Device now uses Customer/Site IDs for existing records
- case-insensitive duplicate protection for Customers/Sites
- Edit Device keeps old filename aliases when a device is renamed
- Device deletion removes V2 monitoring/script/alias metadata
- Device deletion always removes its backup DB rows and stored MinIO objects, preventing untracked orphan objects

### Security / operations

- Uvicorn access log disabled so device API keys in RouterOS-compatible URL paths are not written to normal access logs
- MinIO Console is no longer published on host port 9001
- Admin HTTPS/IP allowlist remains unchanged
- no secret rotation is performed by this release, specifically to avoid changing already-installed router scripts

## Upgrade

Use the release package installer supplied with this build. The installer:

1. creates rollback files and a PostgreSQL schema/data dump
2. preserves the **live** `router_script()` function byte-for-byte
3. preserves the live `CURRENT_SCRIPT_VERSION`
4. installs the consolidated V2 source/UI
5. changes only the user-facing APP_NAME to `Router Vault`
6. aligns server-side backup alert schedule to 03:00 when still on the old 04:00 default
7. rebuilds Router Vault
8. backfills rename aliases and reconciles Last Backup
9. runs smoke tests

## After validation

The production baseline is:

**Router Vault V2.0.1 – Locked Baseline**

Current development line:

**Router Vault 3.0.0-dev**

Module API:

**v1**
