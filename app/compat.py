import re
import time
from datetime import datetime, timezone

from sqlalchemy import text, select, func

from app.db import SessionLocal
from app.models import Device, Backup


def safe_device_name(name: str) -> str:
    return "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in (name or "")
    )


def backup_prefix_from_filename(filename: str) -> str | None:
    name = (filename or "").strip()
    if not name:
        return None

    run = None
    if name.endswith("__config.rsc"):
        run = name[:-len("__config.rsc")]
    elif name.endswith("__full.backup"):
        run = name[:-len("__full.backup")]

    if run and "__" in run:
        # Split from the right. This remains correct when the sanitized
        # device name itself ends in '_' or contains '__'.
        return run.rsplit("__", 1)[0]

    if name.endswith("_config.rsc"):
        return name[:-len("_config.rsc")]
    if name.endswith("_full.backup"):
        return name[:-len("_full.backup")]
    return None


def ensure_compat_tables():
    with SessionLocal() as db:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS device_name_aliases (
                alias VARCHAR(255) PRIMARY KEY,
                device_id VARCHAR(36) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_device_name_aliases_device
            ON device_name_aliases(device_id)
        """))
        db.commit()


def remember_device_alias(device_id: str, name_or_alias: str):
    alias = safe_device_name(name_or_alias)
    if not alias:
        return

    ensure_compat_tables()
    with SessionLocal() as db:
        # Do not steal an alias that already belongs to another device.
        current = db.execute(
            text("SELECT device_id FROM device_name_aliases WHERE alias=:a"),
            {"a": alias},
        ).first()
        if current and str(current[0]) != str(device_id):
            return

        db.execute(text("""
            INSERT INTO device_name_aliases(alias, device_id)
            VALUES(:a, :d)
            ON CONFLICT(alias)
            DO UPDATE SET device_id=EXCLUDED.device_id
        """), {"a": alias, "d": str(device_id)})
        db.commit()


def backfill_device_aliases():
    ensure_compat_tables()
    added = 0
    with SessionLocal() as db:
        devices = db.scalars(select(Device)).all()
        for device in devices:
            alias = safe_device_name(device.name)
            if alias:
                db.execute(text("""
                    INSERT INTO device_name_aliases(alias, device_id)
                    VALUES(:a, :d)
                    ON CONFLICT(alias) DO NOTHING
                """), {"a": alias, "d": str(device.id)})

        # Existing backup rows are authoritative history. They let us learn
        # old filename prefixes after a device has been renamed, without
        # changing or reinstalling the script on the MikroTik.
        rows = db.execute(
            select(Backup.device_id, Backup.filename)
            .order_by(Backup.created_at.desc())
        ).all()
        for device_id, filename in rows:
            alias = backup_prefix_from_filename(filename)
            if not alias:
                continue
            result = db.execute(text("""
                INSERT INTO device_name_aliases(alias, device_id)
                VALUES(:a, :d)
                ON CONFLICT(alias) DO NOTHING
            """), {"a": alias, "d": str(device_id)})
            if result.rowcount:
                added += result.rowcount
        db.commit()
    return added


def resolve_device_alias(db, filename: str):
    ensure_compat_tables()

    rows = db.execute(
        text("SELECT alias, device_id FROM device_name_aliases")
    ).all()

    matches = []
    for alias, device_id in rows:
        if filename.startswith(str(alias) + "__"):
            matches.append((len(str(alias)), str(device_id), str(alias)))

    if not matches:
        return None, None

    matches.sort(reverse=True)
    best_len = matches[0][0]
    best = [m for m in matches if m[0] == best_len]
    if len(best) != 1:
        return None, None

    _, device_id, alias = best[0]
    device = db.get(Device, device_id)
    if not device or not device.enabled:
        return None, alias
    return device, alias


def reconcile_last_backup_cache():
    with SessionLocal() as db:
        db.execute(text("""
            UPDATE devices d
               SET last_backup_at = x.latest
              FROM (
                    SELECT device_id, MAX(created_at) AS latest
                      FROM backups
                     GROUP BY device_id
                   ) x
             WHERE d.id = x.device_id
               AND d.last_backup_at IS DISTINCT FROM x.latest
        """))
        db.execute(text("""
            UPDATE devices d
               SET last_backup_at = NULL
             WHERE d.last_backup_at IS NOT NULL
               AND NOT EXISTS (
                    SELECT 1 FROM backups b WHERE b.device_id = d.id
               )
        """))
        db.commit()


def cleanup_device_auxiliary(device_id: str):
    ensure_compat_tables()
    with SessionLocal() as db:
        # V2 tables intentionally do not currently have FKs, so remove their
        # records explicitly when a Device is deleted.
        for table in (
            "device_status_samples",
            "device_incidents",
            "device_script_state",
        ):
            try:
                db.execute(
                    text(f"DELETE FROM {table} WHERE device_id=:d"),
                    {"d": str(device_id)},
                )
            except Exception:
                pass
        try:
            db.execute(
                text("DELETE FROM device_name_aliases WHERE device_id=:d"),
                {"d": str(device_id)},
            )
        except Exception:
            pass
        db.commit()


def latest_backup_map(device_ids=None):
    with SessionLocal() as db:
        stmt = select(Backup.device_id, func.max(Backup.created_at)).group_by(Backup.device_id)
        if device_ids:
            stmt = stmt.where(Backup.device_id.in_(list(device_ids)))
        return {str(device_id): value for device_id, value in db.execute(stmt).all()}
