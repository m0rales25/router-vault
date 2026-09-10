import hashlib
import hmac
import os
import smtplib
import ssl
from zoneinfo import ZoneInfo

from email.message import EmailMessage
from sqlalchemy import text

from app.db import SessionLocal
from app.config import settings


DEFAULTS = {
    "public_scheme": "http",
    "public_host": "",
    "public_app_port": "8090",
    "public_sftp_port": "3334",
    "alert_email": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "smtp_security": "starttls",
    "smtp_from_email": "",
    "smtp_from_name": "Router Vault",
    "heartbeat_interval_minutes": "5",
    "down_after_minutes": "5",
    "backup_expected_hour": "3",
    "backup_expected_minute": "0",
    "automatic_retention_days": "0",
    "last_retention_date": "",
    "script_config_updated_at": "",
    "application_timezone": "Europe/Athens",
}


def ensure_settings_table():
    with SessionLocal() as db:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR(128) PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """))
        db.commit()

        for key, value in DEFAULTS.items():
            db.execute(
                text("""
                    INSERT INTO app_settings (key, value)
                    VALUES (:key, :value)
                    ON CONFLICT (key) DO NOTHING
                """),
                {"key": key, "value": value},
            )
        db.commit()


def get_setting(key: str, default: str = "") -> str:
    ensure_settings_table()
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT value FROM app_settings WHERE key=:key"),
            {"key": key},
        ).first()
        if row:
            return row[0]
    return DEFAULTS.get(key, default)


def set_setting(key: str, value: str):
    ensure_settings_table()
    with SessionLocal() as db:
        db.execute(
            text("""
                INSERT INTO app_settings (key, value)
                VALUES (:key, :value)
                ON CONFLICT (key)
                DO UPDATE SET value=EXCLUDED.value
            """),
            {"key": key, "value": value or ""},
        )
        db.commit()


def get_all_settings():
    ensure_settings_table()
    result = dict(DEFAULTS)
    with SessionLocal() as db:
        for key, value in db.execute(text("SELECT key, value FROM app_settings")).all():
            result[key] = value
    return result



def application_timezone_name():
    name = (get_setting("application_timezone", "Europe/Athens") or "Europe/Athens").strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "Europe/Athens"


def application_timezone():
    return ZoneInfo(application_timezone_name())

def public_api_base():
    cfg = get_all_settings()
    scheme = (cfg.get("public_scheme") or "http").strip()
    host = (cfg.get("public_host") or "").strip()
    port = (cfg.get("public_app_port") or "").strip()
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def public_host():
    return (get_setting("public_host", "") or "").strip()


def public_sftp_port():
    try:
        return int(get_setting("public_sftp_port", "3334"))
    except Exception:
        return 3334


def _make_hash(password: str, salt: str | None = None):
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        250000,
    ).hex()
    return salt, digest


def set_admin_password(password: str):
    salt, digest = _make_hash(password)
    set_setting("admin_password_salt", salt)
    set_setting("admin_password_hash", digest)


def verify_admin_password(password: str):
    salt = get_setting("admin_password_salt", "")
    stored_hash = get_setting("admin_password_hash", "")

    if not salt or not stored_hash:
        return hmac.compare_digest(password, settings.admin_password)

    _, calculated_hash = _make_hash(password, salt)
    return hmac.compare_digest(calculated_hash, stored_hash)


def send_test_email():
    cfg = get_all_settings()

    recipient = (cfg.get("alert_email") or "").strip()
    host = (cfg.get("smtp_host") or "").strip()
    username = (cfg.get("smtp_username") or "").strip()
    password = cfg.get("smtp_password") or ""
    security = (cfg.get("smtp_security") or "starttls").strip().lower()
    from_email = (cfg.get("smtp_from_email") or "").strip()
    from_name = (cfg.get("smtp_from_name") or "Router Vault").strip() or "Router Vault"

    if not recipient:
        return False, "Alert email is empty"
    if not host:
        return False, "SMTP host is empty"
    if not from_email:
        return False, "From email is empty"

    try:
        port = int(cfg.get("smtp_port") or "587")
    except Exception:
        return False, "Invalid SMTP port"

    msg = EmailMessage()
    msg["Subject"] = "[Router Vault] Test notification"
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = recipient
    msg.set_content("Router Vault SMTP configuration is working correctly.")

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
        return True, "Email sent"
    except Exception as exc:
        return False, str(exc)
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass
