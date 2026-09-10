#!/usr/bin/env bash
set -euo pipefail

APP="/opt/routervault"
EXPECTED_ROUTER_HASH="e0d67dda0dc67ac760674a463fbe0d80f620843a609bb75967da9b2f2922e8bb"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$APP/upgrade-backups/stage3-timezone-import-fix-$STAMP"

cd "$APP"

echo "============================================================"
echo "Router Vault - Stage3 application_timezone emergency fix"
echo "============================================================"
echo "Version: $(tr -d '[:space:]' < VERSION 2>/dev/null || echo unknown)"
echo

router_hash() {
python3 - <<'PY'
from pathlib import Path
import ast, hashlib
src = Path("app/main.py").read_text()
tree = ast.parse(src)
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "router_script":
        segment = ast.get_source_segment(src, node) or ""
        print(hashlib.sha256(segment.encode()).hexdigest())
        break
else:
    raise SystemExit("router_script() not found")
PY
}

BEFORE_HASH="$(router_hash)"
echo "router_script before: $BEFORE_HASH"

if [[ "$BEFORE_HASH" != "$EXPECTED_ROUTER_HASH" ]]; then
    echo "ERROR: Locked MikroTik generator hash is unexpected."
    echo "Expected: $EXPECTED_ROUTER_HASH"
    echo "Actual  : $BEFORE_HASH"
    echo "Nothing was changed."
    exit 1
fi

if ! grep -q '^def application_timezone()' app/settings_store.py; then
    echo "ERROR: app/settings_store.py does not contain application_timezone()."
    echo "Nothing was changed."
    exit 1
fi

mkdir -p "$BACKUP_DIR"
cp -a app/stage3.py "$BACKUP_DIR/stage3.py.before"
echo "Backup: $BACKUP_DIR/stage3.py.before"

python3 - <<'PY'
from pathlib import Path
import ast

p = Path("app/stage3.py")
src = p.read_text()
tree = ast.parse(src)

target = None
has_import = False

for node in tree.body:
    if isinstance(node, ast.ImportFrom) and node.module == "app.settings_store":
        target = node
        if any(alias.name == "application_timezone" for alias in node.names):
            has_import = True
        break

if has_import:
    print("OK: application_timezone already imported; no source edit required.")
elif target is None:
    raise SystemExit("ERROR: Could not find 'from app.settings_store import ...' in stage3.py")
else:
    lines = src.splitlines()

    # One-line import.
    if target.lineno == target.end_lineno:
        idx = target.lineno - 1
        line = lines[idx]
        if "#" in line:
            code, comment = line.split("#", 1)
            lines[idx] = code.rstrip() + ", application_timezone  #" + comment
        else:
            lines[idx] = line.rstrip() + ", application_timezone"
    else:
        # Parenthesized/multiline import: insert before the closing line.
        insert_at = target.end_lineno - 1
        indent = "    "
        lines.insert(insert_at, indent + "application_timezone,")

    fixed = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
    ast.parse(fixed)
    p.write_text(fixed)
    print("FIXED: imported application_timezone from app.settings_store")
PY

echo
echo "[1/6] Syntax checks"
python3 -m py_compile app/stage3.py app/settings_store.py app/main.py
echo "OK"

AFTER_EDIT_HASH="$(router_hash)"
if [[ "$AFTER_EDIT_HASH" != "$EXPECTED_ROUTER_HASH" ]]; then
    echo "ERROR: router_script hash changed unexpectedly after stage3 edit."
    cp -a "$BACKUP_DIR/stage3.py.before" app/stage3.py
    exit 1
fi

echo
echo "[2/6] Rebuild API only"
if ! docker compose build api; then
    echo "ERROR: API build failed. Restoring stage3.py."
    cp -a "$BACKUP_DIR/stage3.py.before" app/stage3.py
    docker compose build api >/dev/null 2>&1 || true
    docker compose up -d --force-recreate api >/dev/null 2>&1 || true
    exit 1
fi

echo
echo "[3/6] Recreate API"
docker compose up -d --force-recreate api

echo
echo "[4/6] Wait for health"
OK=0
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        OK=1
        break
    fi
    sleep 2
done

if [[ "$OK" != "1" ]]; then
    echo "ERROR: API did not become healthy. Rolling back stage3.py."
    cp -a "$BACKUP_DIR/stage3.py.before" app/stage3.py
    docker compose build api
    docker compose up -d --force-recreate api
    exit 1
fi
curl -fsS http://127.0.0.1:8000/health
echo

echo
echo "[5/6] Runtime verification + immediate alert check"
docker compose exec -T api python - <<'PY'
from app.settings_store import application_timezone, application_timezone_name
import app.stage3 as stage3

print("Application timezone:", application_timezone_name())
print("Timezone object     :", application_timezone())
print("stage3 import       : OK")

# Run one check immediately instead of waiting for the background interval.
stage3.check_devices()
print("stage3.check_devices: OK")
PY

echo
echo "[6/6] Final locked generator verification"
FINAL_HASH="$(router_hash)"
echo "router_script after : $FINAL_HASH"

if [[ "$FINAL_HASH" != "$EXPECTED_ROUTER_HASH" ]]; then
    echo "ERROR: Locked MikroTik generator hash mismatch."
    exit 1
fi

echo
echo "Recent Stage3 / notification logs:"
docker compose logs --since=5m api 2>&1 | grep -Ei 'stage3|notification|smtp|mail|PATAXOS1|DOWN|UP|error|exception' | tail -80 || true

echo
echo "PATAXOS1 alert state (if present):"
docker compose exec -T db psql -U routervault -d routervault -c \
"SELECT alert_key, active, first_seen_at, last_sent_at, recovered_at
 FROM alert_states
 WHERE alert_key ILIKE '%PATAXOS1%'
 ORDER BY first_seen_at DESC;" || true

echo
echo "============================================================"
echo "FIX COMPLETE"
echo "The missing application_timezone import in app/stage3.py is repaired."
echo "Locked MikroTik router_script SHA256:"
echo "$FINAL_HASH"
echo "Backup of previous stage3.py:"
echo "$BACKUP_DIR/stage3.py.before"
echo "============================================================"
