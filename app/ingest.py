import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Device, Backup, Event
from app.storage import upload_file
from app.compat import (
    safe_device_name,
    resolve_device_alias,
    remember_device_alias,
)
from app.module_system import emit_hook as emit_module_hook


ALLOWED = {".backup", ".rsc", ".cfg", ".conf", ".txt"}
_STABILITY = {}
_STABLE_FOR_SECONDS = 8


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def classify(path: Path):
    if path.suffix.lower() == ".backup":
        return "routeros_binary"
    if path.suffix.lower() == ".rsc":
        return "routeros_export"
    return "config"


def find_device(db, parsed_name: str):
    device = db.scalar(
        select(Device).where(
            Device.name == parsed_name,
            Device.enabled == True,
        )
    )
    if device:
        return device

    candidates = db.scalars(
        select(Device).where(Device.enabled == True)
    ).all()
    matches = [
        d for d in candidates
        if safe_device_name(d.name) == parsed_name
    ]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        print(
            f"ingest: ambiguous normalized device name {parsed_name}: "
            f"{[d.name for d in matches]}",
            flush=True,
        )
    return None


def find_device_from_filename(db, filename: str):
    # 1. Current device names. Match against the COMPLETE sanitized prefix,
    #    avoiding the trailing '_' + '__' separator collision.
    candidates = db.scalars(
        select(Device).where(Device.enabled == True)
    ).all()

    matches = []
    for device in candidates:
        safe_name = safe_device_name(device.name)
        if filename.startswith(safe_name + "__"):
            matches.append((len(safe_name), device, safe_name))

    if matches:
        matches.sort(key=lambda item: item[0], reverse=True)
        best_len = matches[0][0]
        best = [item for item in matches if item[0] == best_len]
        if len(best) == 1:
            _, device, alias = best[0]
            remember_device_alias(device.id, alias)
            return device, alias

    # 2. Historical aliases learned from existing Backup rows and previous
    #    device names. This is what keeps already-installed MikroTik scripts
    #    working after a Device rename.
    device, alias = resolve_device_alias(db, filename)
    if device:
        return device, alias

    # 3. Legacy fallback.
    if "__" in filename:
        parsed_name = filename.split("__", 1)[0]
    elif filename.endswith("_config.rsc"):
        parsed_name = filename[:-len("_config.rsc")]
    elif filename.endswith("_full.backup"):
        parsed_name = filename[:-len("_full.backup")]
    else:
        return None, None

    return find_device(db, parsed_name), parsed_name


def file_is_stable(path: Path):
    """Require two unchanged observations before ingesting an SFTP file."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        _STABILITY.pop(str(path), None)
        return False

    if stat.st_size <= 0:
        return False

    key = str(path)
    signature = (stat.st_size, stat.st_mtime_ns)
    now = time.monotonic()
    previous = _STABILITY.get(key)

    if not previous or previous[0] != signature:
        _STABILITY[key] = (signature, now)
        return False

    return (now - previous[1]) >= _STABLE_FOR_SECONDS


def ingest_once():
    root = Path(settings.ingest_dir)
    root.mkdir(parents=True, exist_ok=True)

    existing = set()

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        existing.add(str(path))
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in ALLOWED:
            continue
        if not file_is_stable(path):
            continue

        name = path.name

        with SessionLocal() as db:
            device, device_name = find_device_from_filename(db, name)

            if not device:
                print(
                    f"ingest: no matching device for {path.name} "
                    f"(parsed device={device_name})",
                    flush=True,
                )
                continue

            # Re-stat after matching, just before hashing/uploading.
            try:
                before = path.stat()
            except FileNotFoundError:
                continue

            now = datetime.now(timezone.utc)
            digest = sha256_file(path)

            try:
                after = path.stat()
            except FileNotFoundError:
                continue

            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                _STABILITY[str(path)] = (
                    (after.st_size, after.st_mtime_ns),
                    time.monotonic(),
                )
                print(
                    f"ingest: deferred changing file {path.name}",
                    flush=True,
                )
                continue

            object_key = (
                f"{device.customer_id}/"
                f"{device.id}/"
                f"{now:%Y/%m/%d}/"
                f"{now:%H%M%S}-{path.name}"
            )

            print(
                f"ingest: {path.name} -> device '{device.name}'",
                flush=True,
            )

            upload_file(str(path), object_key)

            backup = Backup(
                device_id=device.id,
                filename=path.name,
                object_key=object_key,
                size_bytes=after.st_size,
                sha256=digest,
                backup_type=classify(path),
            )
            db.add(backup)
            device.last_backup_at = now
            db.add(
                Event(
                    device_id=device.id,
                    event_type="backup_ingested",
                    message=f"Received {path.name} ({after.st_size} bytes)",
                )
            )
            db.commit()

            try:
                emit_module_hook("backup.ingested", {
                    "backup_id": str(backup.id),
                    "device_id": str(device.id),
                    "customer_id": str(device.customer_id),
                    "site_id": str(device.site_id or ""),
                    "device_name": device.name or "",
                    "filename": path.name,
                    "object_key": object_key,
                    "size_bytes": int(after.st_size),
                    "sha256": digest,
                    "backup_type": backup.backup_type,
                    "created_at": now.isoformat(),
                })
            except Exception:
                # Module hook failures must never break backup ingest.
                pass

            # Remember the actual prefix that arrived from this router so
            # future renames remain compatible with its installed script.
            if device_name:
                remember_device_alias(device.id, device_name)

            path.unlink(missing_ok=True)
            _STABILITY.pop(str(path), None)
            print(f"ingest: completed {path.name}", flush=True)

    # Drop state for files that disappeared between scans.
    for key in list(_STABILITY):
        if key not in existing:
            _STABILITY.pop(key, None)


def loop():
    while True:
        try:
            ingest_once()
        except Exception as exc:
            print(f"ingest error: {exc}", flush=True)
        time.sleep(10)
