import hashlib
import time
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Device, Backup, Event
from app.storage import upload_file


ALLOWED = {".backup", ".rsc", ".cfg", ".conf", ".txt"}


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


def safe_device_name(name: str):
    """
    Same normalization used by the MikroTik script generator.

    Example:
        BFS CORE -> BFS_CORE
    """
    return "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in name
    )


def find_device(db, parsed_name: str):
    # First try the exact DB name.
    device = db.scalar(
        select(Device).where(
            Device.name == parsed_name,
            Device.enabled == True
        )
    )

    if device:
        return device

    # Then try the sanitized filename representation.
    candidates = db.scalars(
        select(Device).where(
            Device.enabled == True
        )
    ).all()

    matches = [
        d
        for d in candidates
        if safe_device_name(d.name) == parsed_name
    ]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        print(
            f"ingest: ambiguous normalized device name "
            f"{parsed_name}: "
            f"{[d.name for d in matches]}",
            flush=True
        )

    return None


def ingest_once():
    root = Path(settings.ingest_dir)
    root.mkdir(parents=True, exist_ok=True)

    for path in root.rglob("*"):

        if not path.is_file():
            continue

        if path.name.startswith("."):
            continue

        if path.suffix.lower() not in ALLOWED:
            continue

        # Ignore empty/test files.
        if path.stat().st_size == 0:
            continue

        name = path.name

        # New filename format:
        # BFS_CORE__2026-sep-08_19-58-13__config.rsc
        #
        # Legacy formats are also supported.
        if "__" in name:
            device_name = name.split("__", 1)[0]

        elif name.endswith("_config.rsc"):
            device_name = name[:-len("_config.rsc")]

        elif name.endswith("_full.backup"):
            device_name = name[:-len("_full.backup")]

        else:
            continue

        with SessionLocal() as db:

            device = find_device(
                db,
                device_name
            )

            if not device:
                print(
                    f"ingest: no matching device for "
                    f"{path.name} "
                    f"(parsed device={device_name})",
                    flush=True
                )
                continue

            now = datetime.now(timezone.utc)
            digest = sha256_file(path)

            object_key = (
                f"{device.customer_id}/"
                f"{device.id}/"
                f"{now:%Y/%m/%d}/"
                f"{now:%H%M%S}-{path.name}"
            )

            print(
                f"ingest: {path.name} "
                f"-> device '{device.name}'",
                flush=True
            )

            upload_file(
                str(path),
                object_key
            )

            backup = Backup(
                device_id=device.id,
                filename=path.name,
                object_key=object_key,
                size_bytes=path.stat().st_size,
                sha256=digest,
                backup_type=classify(path),
            )

            db.add(backup)

            device.last_backup_at = now

            db.add(
                Event(
                    device_id=device.id,
                    event_type="backup_ingested",
                    message=(
                        f"Received {path.name} "
                        f"({path.stat().st_size} bytes)"
                    )
                )
            )

            db.commit()

            # Delete the SFTP ingress copy only after
            # MinIO upload + DB commit succeeded.
            path.unlink(missing_ok=True)

            print(
                f"ingest: completed {path.name}",
                flush=True
            )


def loop():
    while True:
        try:
            ingest_once()

        except Exception as exc:
            print(
                f"ingest error: {exc}",
                flush=True
            )

        time.sleep(10)
