#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then echo "Usage: $0 routervault.example.com"; exit 1; fi
if grep -q '^ROUTERVAULT_DOMAIN=' .env 2>/dev/null; then
  sed -i "s/^ROUTERVAULT_DOMAIN=.*/ROUTERVAULT_DOMAIN=$DOMAIN/" .env
else
  echo "ROUTERVAULT_DOMAIN=$DOMAIN" >> .env
fi
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d caddy
cat <<MSG
HTTPS profile started for: $DOMAIN
Make sure DNS points $DOMAIN to this server and TCP 80/443 reach it.
After TLS is valid, set RouterVault Settings -> Public protocol=https and Public host=$DOMAIN.
Also firewall direct public access to the API port.
MSG
