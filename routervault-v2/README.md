# RouterVault 2.0.0

Version 2 builds on the frozen 1.0.0 release and preserves the Version 1 PostgreSQL, MinIO and SFTP volumes during upgrade.

## New in V2

### Monitoring / availability
- Persistent device health samples
- DOWN incident history with open/close time and duration
- 1-day, 7-day and 30-day availability percentages
- Richer Device page with health history and incidents

### Backup compliance / verification
- Daily backup compliance view
- Latest backup-pair verification (`.rsc` + `.backup`)
- Size/hash checks for latest backup set
- Missing / partial / OK states

### Alerts
- Global Alerts page
- Active / recovered state
- Acknowledge alerts
- Mute alerts for N hours
- Customer-scoped alert visibility for customer users

### Customer / Site navigation
- Customer detail page
- Site detail page
- Clickable Customer and Site names from the dashboard

### Script / device security
- RouterVault script version 2.0.0
- Installed vs expected script version on the Device page
- Update Required / Up to date state
- V2 heartbeat reports script version
- Device API-token rotation
- Header-token heartbeat endpoint for clients that can use headers
- RouterOS 6 compatibility path heartbeat remains available

### Search / reports
- Global search across devices, models, RouterOS, WAN IP, customers and sites
- V2 Reports page
- Availability report
- Backup Compliance report
- RouterOS version summary
- High CPU report
- Excel `.xlsx` exports
- Existing CSV and Print/PDF reports remain available

### Users / roles
- Admin
- Read-only
- Customer-restricted user
- Customer users are scoped to their customer for dashboard, search, alerts, customer/site/device views and downloads

### Audit / safer operations
- Audit log for logins, settings, device create/edit/delete, alert actions, cleanup, user actions and token rotation
- Safer device deletion confirmation page
- Requires typing the device name
- Explicit choice whether MinIO backup objects are deleted

### Scheduled reports
- Daily email summary using the configured SMTP settings
- Configurable recipient and delivery hour

### HTTPS
- Optional Caddy HTTPS profile
- `enable-https.sh <domain>` helper
- Security headers in the Caddy profile

## Install / upgrade

```bash
cd routervault-v2.0.0
chmod +x install.sh
./install.sh
```

On first V2 startup the new V2 tables are created automatically. Existing V1 tables and Docker volumes remain in place.

## HTTPS

After DNS points to the RouterVault server:

```bash
cd /opt/routervault
./enable-https.sh routervault.example.com
```

Then update RouterVault Settings to use `https` and the same hostname. Firewall direct public access to the old API port after confirming HTTPS works.

## Router scripts after V2

Existing routers continue to work through the legacy heartbeat endpoint, but they will show `Installed version: Unknown` / `Update required` until the V2 MikroTik installer is copied to them. Re-copy the generated script to each device when convenient.
