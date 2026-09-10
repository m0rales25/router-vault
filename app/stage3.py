import io
import smtplib
import ssl
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import select, text, func

from app.config import settings
from app.db import SessionLocal
from app.models import Device, Backup, Customer, Site
from app.storage import s3
from app.settings_store import get_setting, set_setting, get_all_settings, application_timezone
from app.compat import reconcile_last_backup_cache

ATHENS_TZ = ZoneInfo("Europe/Athens")


def ensure_stage3_tables():
    with SessionLocal() as db:
        db.execute(text('''
            CREATE TABLE IF NOT EXISTS alert_states (
                alert_key VARCHAR(512) PRIMARY KEY,
                active BOOLEAN NOT NULL DEFAULT FALSE,
                first_seen_at TIMESTAMPTZ NULL,
                last_sent_at TIMESTAMPTZ NULL,
                recovered_at TIMESTAMPTZ NULL
            )
        '''))
        db.commit()

    defaults = {
        "down_after_minutes": "5",
        "backup_expected_hour": "3",
        "backup_expected_minute": "0",
        "backup_grace_minutes": "120",
        "automatic_retention_days": "0",
        "last_retention_date": "",
    }

    for key, value in defaults.items():
        if get_setting(key, "__NOT_FOUND__") == "__NOT_FOUND__":
            set_setting(key, value)


def send_notification(subject: str, body: str):
    cfg = get_all_settings()
    recipient = (cfg.get("alert_email") or "").strip()
    host = (cfg.get("smtp_host") or "").strip()
    username = (cfg.get("smtp_username") or "").strip()
    password = cfg.get("smtp_password") or ""
    from_email = (cfg.get("smtp_from_email") or "").strip()
    from_name = (cfg.get("smtp_from_name") or "Router Vault").strip() or "Router Vault"
    security = (cfg.get("smtp_security") or "starttls").strip().lower()

    if not recipient or not host or not from_email:
        return False

    try:
        port = int(cfg.get("smtp_port") or "587")
    except Exception:
        port = 587

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = recipient
    msg.set_content(body)

    context = ssl.create_default_context()
    smtp = None

    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.ehlo()
            if security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()

        if username:
            smtp.login(username, password)

        smtp.send_message(msg)
        return True

    except Exception as exc:
        print(f"Router Vault notification error: {exc}", flush=True)
        return False

    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass


def device_notification_context(db, device):
    """Return stable Customer/Site/Device labels for notification messages."""
    customer_name = "-"
    site_name = "-"

    try:
        customer = db.get(Customer, device.customer_id) if device.customer_id else None
        if customer and customer.name:
            customer_name = customer.name
    except Exception:
        pass

    try:
        site = db.get(Site, device.site_id) if device.site_id else None
        if site and site.name:
            site_name = site.name
    except Exception:
        pass

    device_name = (device.name or "-").strip() or "-"
    model = (device.model or "-").strip() or "-"

    parts = [customer_name]
    if site_name != "-":
        parts.append(site_name)
    parts.append(device_name)

    return {
        "customer": customer_name,
        "site": site_name,
        "device": device_name,
        "model": model,
        "path": " / ".join(parts),
    }


def notification_subject(status: str, context: dict):
    return f"[Router Vault] {status} - {context['path']}"


def notification_identity_block(context: dict, status_label: str):
    return (
        f"Customer: {context['customer']}\n"
        f"Site: {context['site']}\n"
        f"Device: {context['device']}\n"
        f"Model: {context['model']}\n"
        f"Status: {status_label}\n"
    )


def alert_is_active(alert_key: str):
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT active FROM alert_states WHERE alert_key=:key"),
            {"key": alert_key},
        ).first()

        return bool(row[0]) if row else False


def set_alert_state(alert_key: str, active: bool, email_sent: bool):
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        db.execute(
            text('''
                INSERT INTO alert_states(
                    alert_key, active, first_seen_at, last_sent_at, recovered_at
                )
                VALUES(
                    :key,
                    :active,
                    CASE WHEN :active THEN :now ELSE NULL END,
                    CASE WHEN :email_sent THEN :now ELSE NULL END,
                    CASE WHEN :active THEN NULL ELSE :now END
                )
                ON CONFLICT (alert_key)
                DO UPDATE SET
                    active = EXCLUDED.active,
                    first_seen_at = CASE
                        WHEN EXCLUDED.active = TRUE
                         AND alert_states.active = FALSE
                        THEN EXCLUDED.first_seen_at
                        ELSE alert_states.first_seen_at
                    END,
                    last_sent_at = CASE
                        WHEN :email_sent = TRUE THEN :now
                        ELSE alert_states.last_sent_at
                    END,
                    recovered_at = CASE
                        WHEN EXCLUDED.active = TRUE THEN NULL
                        ELSE :now
                    END
            '''),
            {
                "key": alert_key,
                "active": active,
                "email_sent": email_sent,
                "now": now,
            },
        )
        db.commit()


def alert_is_muted(alert_key: str):
    try:
        with SessionLocal() as db:
            row = db.execute(
                text("SELECT muted_until FROM alert_meta WHERE alert_key=:k"),
                {"k": alert_key},
            ).first()
        if not row or not row[0]:
            return False
        until = row[0]
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)
    except Exception:
        return False


def clear_alert_ack(alert_key: str):
    try:
        with SessionLocal() as db:
            db.execute(
                text("""
                    UPDATE alert_meta
                       SET acknowledged_at=NULL,
                           acknowledged_by=NULL
                     WHERE alert_key=:key
                """),
                {"key": alert_key},
            )
            db.commit()
    except Exception:
        pass


def raise_once(alert_key: str, subject: str, body: str):
    if alert_is_active(alert_key):
        return

    # Acknowledge belongs to one incident. A genuinely new incident starts
    # unacknowledged, while an existing mute remains in force.
    clear_alert_ack(alert_key)

    if alert_is_muted(alert_key):
        set_alert_state(alert_key, True, False)
        return

    sent = send_notification(subject, body)
    set_alert_state(alert_key, True, sent)


def recover_once(alert_key: str, subject: str, body: str):
    if not alert_is_active(alert_key):
        return

    sent = send_notification(subject, body)
    set_alert_state(alert_key, False, sent)


def cleanup_alerts_for_device(device_id: str):
    """Delete monitoring alert state/meta belonging to one deleted device."""
    keys = [
        f"device-down:{device_id}",
        f"backup-missing:{device_id}",
    ]

    with SessionLocal() as db:
        for key in keys:
            try:
                db.execute(
                    text("DELETE FROM alert_meta WHERE alert_key=:key"),
                    {"key": key},
                )
            except Exception:
                pass

            db.execute(
                text("DELETE FROM alert_states WHERE alert_key=:key"),
                {"key": key},
            )

        db.commit()


def cleanup_orphan_alerts():
    """Delete alert rows whose referenced device no longer exists."""
    removed = 0

    with SessionLocal() as db:
        device_ids = {
            str(row[0])
            for row in db.execute(select(Device.id)).all()
        }

        rows = db.execute(
            text("""
                SELECT alert_key
                FROM alert_states
                WHERE alert_key LIKE 'device-down:%'
                   OR alert_key LIKE 'backup-missing:%'
            """)
        ).all()

        for row in rows:
            key = row[0]

            if ":" not in key:
                continue

            device_id = key.split(":", 1)[1]

            if device_id in device_ids:
                continue

            try:
                db.execute(
                    text("DELETE FROM alert_meta WHERE alert_key=:key"),
                    {"key": key},
                )
            except Exception:
                pass

            db.execute(
                text("DELETE FROM alert_states WHERE alert_key=:key"),
                {"key": key},
            )
            removed += 1

        db.commit()

    return removed


def check_devices():
    reconcile_last_backup_cache()
    cfg = get_all_settings()

    try:
        down_minutes = max(
            1,
            int(cfg.get("down_after_minutes", "5") or "5"),
        )
    except Exception:
        down_minutes = 5

    try:
        expected_hour = int(
            cfg.get("backup_expected_hour", "3") or "3"
        )
        expected_minute = int(
            cfg.get("backup_expected_minute", "0") or "0"
        )
    except Exception:
        expected_hour, expected_minute = 3, 0

    try:
        grace_minutes = max(
            0,
            int(cfg.get("backup_grace_minutes", "120") or "120"),
        )
    except Exception:
        grace_minutes = 120

    cleanup_orphan_alerts()

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(application_timezone())

    scheduled_today = now_local.replace(
        hour=expected_hour,
        minute=expected_minute,
        second=0,
        microsecond=0,
    )

    deadline_today = (
        scheduled_today
        + timedelta(minutes=grace_minutes)
    )

    if now_local >= deadline_today:
        required_schedule = scheduled_today
        active_deadline = deadline_today
    else:
        required_schedule = (
            scheduled_today - timedelta(days=1)
        )
        active_deadline = (
            required_schedule
            + timedelta(minutes=grace_minutes)
        )

    required_date = required_schedule.date()

    with SessionLocal() as db:
        devices = db.scalars(
            select(Device).where(Device.enabled == True)
        ).all()

        for device in devices:
            down_key = f"device-down:{device.id}"

            if device.last_seen_at:
                last_seen = device.last_seen_at

                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(
                        tzinfo=timezone.utc
                    )

                is_down = (
                    (now_utc - last_seen).total_seconds()
                    > down_minutes * 60
                )

            else:
                created = device.created_at or now_utc

                if created.tzinfo is None:
                    created = created.replace(
                        tzinfo=timezone.utc
                    )

                is_down = (
                    (now_utc - created).total_seconds()
                    > down_minutes * 60
                )

            context = device_notification_context(db, device)

            if is_down:
                raise_once(
                    down_key,
                    notification_subject("DOWN", context),
                    (
                        notification_identity_block(context, "DOWN")
                        + "\n"
                        + f"No heartbeat has been received for more than "
                        + f"{down_minutes} minutes.\n"
                    ),
                )
            else:
                recover_once(
                    down_key,
                    notification_subject("UP", context),
                    (
                        notification_identity_block(context, "UP")
                        + "\nHeartbeat communication has been restored.\n"
                    ),
                )

            backup_key = f"backup-missing:{device.id}"

            created = device.created_at or now_utc

            if created.tzinfo is None:
                created = created.replace(
                    tzinfo=timezone.utc
                )

            created_local = created.astimezone(
                now_local.tzinfo
            )

            # A device added after the last deadline should not
            # immediately produce a false Backup Missing alert.
            if created_local > active_deadline:
                if alert_is_active(backup_key):
                    set_alert_state(
                        backup_key,
                        False,
                        False,
                    )
                continue

            # Source of truth is the backups table, not the cached
            # devices.last_backup_at column.
            latest_backup = db.scalar(
                select(func.max(Backup.created_at))
                .where(Backup.device_id == device.id)
            )

            backup_ok = False

            if latest_backup:
                if latest_backup.tzinfo is None:
                    latest_backup = latest_backup.replace(
                        tzinfo=timezone.utc
                    )

                latest_local = latest_backup.astimezone(
                    now_local.tzinfo
                )

                backup_ok = (
                    latest_local.date() >= required_date
                )

            if not backup_ok:
                raise_once(
                    backup_key,
                    notification_subject("BACKUP MISSING", context),
                    (
                        notification_identity_block(context, "BACKUP MISSING")
                        + "\n"
                        + f"Scheduled backup: "
                        + f"{expected_hour:02d}:{expected_minute:02d}\n"
                        + f"Grace period: {grace_minutes} minutes\n"
                        + f"Deadline: "
                        + f"{active_deadline.strftime('%Y-%m-%d %H:%M %Z')}\n"
                        + "No qualifying backup has been received.\n"
                    ),
                )
            else:
                recover_once(
                    backup_key,
                    notification_subject("BACKUP RECOVERED", context),
                    (
                        notification_identity_block(context, "BACKUP OK")
                        + "\nBackup delivery has been restored.\n"
                    ),
                )

def delete_minio_object(object_key: str):
    s3().delete_object(
        Bucket=settings.s3_bucket,
        Key=object_key,
    )


def cleanup_backups(
    age_value: int,
    age_unit: str,
    customer_id="",
    site_id="",
    device_id="",
):
    factors = {
        "days": 1,
        "weeks": 7,
        "months": 30,
        "years": 365,
    }

    days = max(1, int(age_value)) * factors.get(age_unit, 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    deleted = 0
    affected_devices = set()

    with SessionLocal() as db:
        stmt = (
            select(Backup, Device)
            .join(Device, Backup.device_id == Device.id)
        )

        if device_id:
            stmt = stmt.where(Device.id == device_id)
        elif site_id:
            stmt = stmt.where(Device.site_id == site_id)
        elif customer_id:
            stmt = stmt.where(Device.customer_id == customer_id)

        stmt = stmt.where(Backup.created_at < cutoff)
        rows = db.execute(stmt).all()

        for backup, device in rows:
            try:
                delete_minio_object(backup.object_key)
                db.delete(backup)
                affected_devices.add(device.id)
                deleted += 1
            except Exception as exc:
                print(
                    f"Router Vault cleanup error {backup.object_key}: {exc}",
                    flush=True,
                )

        db.commit()

        for device_id_ in affected_devices:
            device = db.get(Device, device_id_)
            latest = db.scalar(
                select(Backup)
                .where(Backup.device_id == device_id_)
                .order_by(Backup.created_at.desc())
                .limit(1)
            )

            if device:
                device.last_backup_at = (
                    latest.created_at if latest else None
                )

        db.commit()

    return deleted


def run_auto_retention():
    try:
        days = int(
            get_setting("automatic_retention_days", "0")
            or "0"
        )
    except Exception:
        days = 0

    if days <= 0:
        return

    today = datetime.now().astimezone().date().isoformat()

    if get_setting("last_retention_date", "") == today:
        return

    deleted = cleanup_backups(days, "days")
    set_setting("last_retention_date", today)

    print(
        f"Router Vault automatic retention: deleted {deleted} backups",
        flush=True,
    )


def backup_run_key(filename: str):
    if filename.endswith("__config.rsc"):
        return filename[:-len("__config.rsc")]

    if filename.endswith("__full.backup"):
        return filename[:-len("__full.backup")]

    return filename


def select_latest_backup_sets(rows):
    newest_run = {}

    for backup, device in rows:
        if device.id not in newest_run:
            newest_run[device.id] = backup_run_key(
                backup.filename
            )

    selected = []

    for backup, device in rows:
        wanted = newest_run.get(device.id)

        if (
            wanted
            and backup_run_key(backup.filename) == wanted
        ):
            selected.append(backup)

    return selected


def build_zip(backups):
    buffer = io.BytesIO()
    client = s3()

    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        used = set()

        for backup in backups:
            filename = backup.filename

            if filename in used:
                filename = f"{backup.id}-{filename}"

            used.add(filename)

            response = client.get_object(
                Bucket=settings.s3_bucket,
                Key=backup.object_key,
            )

            archive.writestr(
                filename,
                response["Body"].read(),
            )

    buffer.seek(0)
    return buffer


_started = False


def background_loop():
    while True:
        try:
            check_devices()
            run_auto_retention()

        except Exception as exc:
            print(
                f"Router Vault stage3 error: {exc}",
                flush=True,
            )

        time.sleep(60)


def start_background_monitor():
    global _started

    if _started:
        return

    _started = True

    thread = threading.Thread(
        target=background_loop,
        daemon=True,
        name="routervault-monitor",
    )
    thread.start()
