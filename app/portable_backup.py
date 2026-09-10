import io
import json
import os
import tarfile
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import MetaData, Table, delete, inspect, select, text
from sqlalchemy.sql.sqltypes import DateTime, Date, Boolean, Integer, BigInteger, Float, Numeric

from app.db import SessionLocal, engine
from app.models import Backup
from app.settings_store import get_all_settings, application_timezone_name
from app.storage import s3
from app.config import settings

BACKUP_MAGIC = "router-vault-portable-backup"
FORMAT_VERSION = 1
def current_app_version():
    for candidate in (Path("VERSION"), Path("/srv/app/VERSION"), Path("/opt/routervault/VERSION")):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass
    return "unknown"


CURRENT_APP_VERSION = current_app_version()

# Stable logical format. Future Router Vault versions should keep a migration
# reader for format_version=1 so backups created by V2 can be imported later.
TABLES = [
    "customers",
    "sites",
    "devices",
    "backups",
    "events",
    "app_settings",
    "app_users",
    "audit_log",
    "device_status_samples",
    "device_incidents",
    "device_script_state",
    "alert_meta",
    "scheduled_reports",
    "alert_states",
    "device_name_aliases",
]

DELETE_ORDER = [
    "alert_meta",
    "alert_states",
    "device_status_samples",
    "device_incidents",
    "device_script_state",
    "device_name_aliases",
    "backups",
    "events",
    "app_users",
    "sites",
    "devices",  # deleted again safely after sites ordering is corrected below
    "customers",
    "audit_log",
    "scheduled_reports",
    "app_settings",
]

# Actual FK-safe delete order. Devices must be before Sites/Customers.
DELETE_ORDER = [
    "alert_meta",
    "alert_states",
    "device_status_samples",
    "device_incidents",
    "device_script_state",
    "device_name_aliases",
    "backups",
    "events",
    "app_users",
    "devices",
    "sites",
    "customers",
    "audit_log",
    "scheduled_reports",
    "app_settings",
]

INSERT_ORDER = [
    "customers",
    "sites",
    "devices",
    "backups",
    "events",
    "app_settings",
    "app_users",
    "audit_log",
    "device_status_samples",
    "device_incidents",
    "device_script_state",
    "alert_meta",
    "scheduled_reports",
    "alert_states",
    "device_name_aliases",
]


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    return value


def _existing_tables():
    inspector = inspect(engine)
    return set(inspector.get_table_names())


def _export_database(include_backup_history: bool):
    existing = _existing_tables()
    data = {}

    with SessionLocal() as db:
        for table_name in TABLES:
            if table_name not in existing:
                continue

            if table_name == "backups" and not include_backup_history:
                data[table_name] = []
                continue

            rows = db.execute(text(f'SELECT * FROM "{table_name}"')).mappings().all()
            data[table_name] = [
                {key: _json_value(value) for key, value in dict(row).items()}
                for row in rows
            ]

    return data


def _manifest(mode: str, database_data: dict):
    return {
        "magic": BACKUP_MAGIC,
        "format_version": FORMAT_VERSION,
        "source_version": CURRENT_APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "includes_backup_objects": mode == "full",
        "database_tables": sorted(database_data.keys()),
        "compatibility": {
            "minimum_reader_format": 1,
            "note": "Future Router Vault versions should migrate Portable Backup Format v1 during import.",
        },
    }


def _add_json(tf: tarfile.TarFile, name: str, value):
    raw = json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    info.mtime = int(datetime.now().timestamp())
    tf.addfile(info, io.BytesIO(raw))


def create_portable_backup(mode: str = "config_database", destination: str | None = None):
    if mode not in {"config_database", "full"}:
        raise ValueError("Invalid backup mode")

    include_objects = mode == "full"
    database_data = _export_database(include_backup_history=include_objects)
    manifest = _manifest(mode, database_data)

    if destination:
        output = destination
    else:
        fd, output = tempfile.mkstemp(prefix="router-vault-", suffix=".rvbackup")
        os.close(fd)

    object_errors = []
    object_count = 0

    with tarfile.open(output, "w:gz") as tf:
        _add_json(tf, "manifest.json", manifest)
        _add_json(tf, "database.json", database_data)
        _add_json(tf, "config.json", {
            "application_settings": get_all_settings(),
            "public_architecture": {
                "api_public_port": get_all_settings().get("public_app_port", "8090"),
                "admin_https_port": "8443",
                "sftp_public_port": get_all_settings().get("public_sftp_port", "3334"),
                "timezone": application_timezone_name(),
            },
            "note": "Environment-level secrets (.env) are intentionally not included. Existing MikroTik credentials/scripts are not changed by import.",
        })

        if include_objects:
            client = s3()
            for row in database_data.get("backups", []):
                backup_id = str(row.get("id") or "")
                object_key = str(row.get("object_key") or "")
                if not backup_id or not object_key:
                    continue
                try:
                    response = client.get_object(Bucket=settings.s3_bucket, Key=object_key)
                    body = response["Body"]
                    size = int(response.get("ContentLength") or row.get("size_bytes") or 0)
                    info = tarfile.TarInfo(f"objects/{backup_id}.blob")
                    info.size = size
                    info.mtime = int(datetime.now().timestamp())
                    tf.addfile(info, body)
                    object_count += 1
                except Exception as exc:
                    object_errors.append({"backup_id": backup_id, "object_key": object_key, "error": str(exc)})

            _add_json(tf, "object-export-report.json", {
                "exported_objects": object_count,
                "errors": object_errors,
            })

    if object_errors:
        # A "full" archive must be complete. Do not hand the user an archive
        # that claims to be portable while silently missing backup objects.
        try:
            os.unlink(output)
        except Exception:
            pass
        raise RuntimeError(f"Full export failed for {len(object_errors)} MinIO object(s)")

    return output, manifest


def inspect_portable_backup(path: str):
    with tarfile.open(path, "r:gz") as tf:
        try:
            manifest_member = tf.getmember("manifest.json")
            database_member = tf.getmember("database.json")
        except KeyError as exc:
            raise ValueError("Not a Router Vault portable backup") from exc

        manifest = json.loads(tf.extractfile(manifest_member).read().decode("utf-8"))
        database_data = json.loads(tf.extractfile(database_member).read().decode("utf-8"))

    if manifest.get("magic") != BACKUP_MAGIC:
        raise ValueError("Invalid Router Vault backup signature")

    version = int(manifest.get("format_version") or 0)
    database_data = migrate_payload(database_data, version)
    return manifest, database_data


def migrate_payload(database_data: dict, format_version: int):
    # Versioned migration entry point. V2 establishes format v1. A later V3
    # should continue accepting v1 here and transform it to the newer schema.
    if format_version == 1:
        return database_data
    raise ValueError(
        f"Unsupported backup format v{format_version}. This Router Vault release supports Portable Backup Format v1."
    )


def _decode_row(table: Table, row: dict):
    result = {}
    for column in table.columns:
        if column.name not in row:
            continue
        value = row[column.name]
        if value is None:
            result[column.name] = None
            continue
        if isinstance(value, dict) and "__bytes_hex__" in value:
            result[column.name] = bytes.fromhex(value["__bytes_hex__"])
            continue
        if isinstance(column.type, DateTime) and isinstance(value, str):
            result[column.name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(column.type, Date) and isinstance(value, str):
            result[column.name] = date.fromisoformat(value)
        elif isinstance(column.type, Boolean):
            result[column.name] = bool(value)
        elif isinstance(column.type, (Integer, BigInteger)):
            result[column.name] = int(value)
        elif isinstance(column.type, (Float, Numeric)):
            result[column.name] = float(value)
        else:
            result[column.name] = value
    return result


def _delete_current_minio_objects():
    client = s3()
    deleted = 0
    continuation = None

    while True:
        kwargs = {"Bucket": settings.s3_bucket, "MaxKeys": 1000}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        response = client.list_objects_v2(**kwargs)
        objects = response.get("Contents") or []
        if objects:
            client.delete_objects(
                Bucket=settings.s3_bucket,
                Delete={"Objects": [{"Key": item["Key"]} for item in objects], "Quiet": True},
            )
            deleted += len(objects)
        if not response.get("IsTruncated"):
            break
        continuation = response.get("NextContinuationToken")

    return deleted


def _reset_sequences(db):
    # Tables with BIGSERIAL IDs. setval handles both empty and populated tables.
    for table_name in ("audit_log", "device_status_samples", "device_incidents", "scheduled_reports"):
        try:
            db.execute(text(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table_name}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                    (SELECT COUNT(*) > 0 FROM {table_name})
                )
            """))
        except Exception:
            pass


def restore_portable_backup(path: str):
    manifest, database_data = inspect_portable_backup(path)
    mode = manifest.get("mode") or "config_database"
    includes_objects = bool(manifest.get("includes_backup_objects"))

    # Create a server-side safety copy before touching data. If the incoming
    # archive is full, the safety copy is full too, so MinIO can be recovered.
    safety_dir = Path(settings.ingest_dir) / "_router_vault_safety"
    safety_dir.mkdir(parents=True, exist_ok=True)
    safety_name = datetime.now().strftime("pre-import-%Y%m%d-%H%M%S.rvbackup")
    safety_path = safety_dir / safety_name
    safety_mode = "full" if includes_objects else "config_database"
    create_portable_backup(safety_mode, str(safety_path))

    existing = _existing_tables()
    metadata = MetaData()
    metadata.reflect(bind=engine, only=[t for t in TABLES if t in existing])

    removed_objects = 0

    # Restore the logical database transactionally first. Existing MinIO
    # objects are left untouched until the database commit succeeds.
    with SessionLocal() as db:
        try:
            for table_name in DELETE_ORDER:
                table = metadata.tables.get(table_name)
                if table is not None:
                    db.execute(delete(table))

            for table_name in INSERT_ORDER:
                table = metadata.tables.get(table_name)
                if table is None:
                    continue
                rows = database_data.get(table_name) or []
                if table_name == "backups" and not includes_objects:
                    rows = []
                if not rows:
                    continue
                decoded = [_decode_row(table, row) for row in rows]
                db.execute(table.insert(), decoded)

            if includes_objects:
                db.execute(text("""
                    UPDATE devices d
                       SET last_backup_at = x.latest
                      FROM (
                            SELECT device_id, MAX(created_at) AS latest
                              FROM backups
                             GROUP BY device_id
                           ) x
                     WHERE d.id = x.device_id
                """))
                db.execute(text("""
                    UPDATE devices
                       SET last_backup_at = NULL
                     WHERE NOT EXISTS (
                         SELECT 1 FROM backups b WHERE b.device_id = devices.id
                     )
                """))
            else:
                db.execute(text("UPDATE devices SET last_backup_at=NULL"))

            _reset_sequences(db)
            db.commit()
        except Exception:
            db.rollback()
            raise

    # Only after the DB commit succeeded do we replace object storage.
    removed_objects = _delete_current_minio_objects()
    restored_objects = 0
    object_errors = []

    if includes_objects:
        client = s3()
        backup_rows = database_data.get("backups") or []
        by_id = {str(row.get("id")): row for row in backup_rows}
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.startswith("objects/") or not member.name.endswith(".blob"):
                    continue
                backup_id = Path(member.name).name[:-5]
                row = by_id.get(backup_id)
                if not row:
                    continue
                object_key = row.get("object_key")
                try:
                    client.upload_fileobj(
                        tf.extractfile(member),
                        settings.s3_bucket,
                        object_key,
                    )
                    restored_objects += 1
                except Exception as exc:
                    object_errors.append({"backup_id": backup_id, "object_key": object_key, "error": str(exc)})

        if object_errors:
            raise RuntimeError(
                f"Database restored, but {len(object_errors)} backup object(s) failed to restore. Safety backup: {safety_path}"
            )

    return {
        "source_version": manifest.get("source_version"),
        "format_version": manifest.get("format_version"),
        "mode": mode,
        "removed_current_objects": removed_objects,
        "restored_objects": restored_objects,
        "safety_backup": str(safety_path),
    }
