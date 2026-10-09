# Changelog

## 2026-10-09 - Downtime reporting and performance optimization

Commit: `83649dc` (`Add downtime reporting and optimize application performance`)

### Added

- dedicated Downtime report
- router outage counts and recorded downtime metrics
- average and maximum outage duration
- availability and last-DOWN reporting
- open / recovered incident visibility
- customer / site / device / date filtering
- CSV / XLSX downtime exports
- incident drill-down
- site-outage correlation
- monitoring-policy baseline support for separating newer data from older heartbeat-flapping history

### Improved

- application timezone handling in user-facing reporting
- date presentation in downtime filters
- availability calculation now supports batch processing
- backup compliance / integrity processing now uses batch data loading
- monitoring worker uses batch lookup of latest status samples and open incidents
- application settings use an in-process cache with update invalidation
- unnecessary request-time backup-cache reconciliation removed from normal page loads
- PostgreSQL indexes / planner statistics updated for report workloads

### Performance validation

Production validation with 37 managed devices after the optimization:

- 100 cached setting reads: approximately `0.05 ms`
- batch availability: approximately `124 ms`
- batch backup compliance: approximately `47 ms`

These values are environment-specific measurements, not performance guarantees.

### Compatibility

The optimization does **not** change:

- generated MikroTik installer body
- generated script version
- existing router API keys
- heartbeat endpoint used by installed routers
- SFTP credentials on routers
- router scheduler configuration

### Validation

- Python application compile check passed
- `/health` returned `{"status":"ok","service":"Router Vault"}`
- API container running
- PostgreSQL container healthy
- production changes pushed to `origin/main`

---

# Router Vault Changelog

## 2.0.1-rc1 - Pre-Lock Candidate

### Added
- shared responsive UI layout
- Administration landing page
- Settings section navigation
- server-side device filename alias table
- alias backfill from historic backup filenames
- SFTP ingest file-stability guard
- common Athens timestamp/byte formatting

### Changed
- user-facing name standardized to `Router Vault`
- Reports and Reports V2 consolidated under `/reports`
- `/reports-v2` retained as a redirect for compatibility
- Add Device uses immutable Customer/Site IDs
- backup compliance reads actual Backup records
- Daily Report timezone set explicitly to Europe/Athens
- default expected backup time aligned to 03:00
- Device deletion cleans auxiliary V2 state
- MinIO Console removed from host port 9001
- Uvicorn normal access log disabled to reduce API-key exposure in path logs

### Compatibility
- generated MikroTik `router_script()` remains byte-identical to the reviewed V2 snapshot
- `CURRENT_SCRIPT_VERSION` remains preserved from the live installation by the installer
- no device API key rotation
- no SFTP credential rotation
- no router scheduler changes
- no existing router needs its script reinstalled for this upgrade

### Fixed
- rename breakage for already-installed name-based backup scripts
- duplicate Customer/Site creation path in Add Device
- stale `last_backup_at` cache drift
- report navigation duplication
- oversized Settings page
- inconsistent `RouterVault` branding outside the intentionally untouched generated router script
