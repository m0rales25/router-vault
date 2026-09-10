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
