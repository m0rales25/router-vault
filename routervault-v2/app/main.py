import csv
import io
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Form
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine, get_db
from app.models import Customer, Site, Device, Backup, Event
from app.schemas import Heartbeat
from app.storage import ensure_bucket, presigned_download, delete_object, get_object
from app.ingest import loop as ingest_loop
from app.settings_store import (
    get_all_settings as rv_get_all_settings,
    get_setting as rv_get_setting,
    set_setting as rv_set_setting,
    public_api_base as rv_public_api_base,
    public_host as rv_public_host,
    public_sftp_port as rv_public_sftp_port,
    set_admin_password as rv_set_admin_password,
    verify_admin_password as rv_verify_admin_password,
    send_test_email as rv_send_test_email,
)
from app.stage3 import (
    ensure_stage3_tables,
    start_background_monitor,
    cleanup_backups as rv_cleanup_backups,
    build_zip as rv_build_zip,
    select_latest_backup_sets as rv_select_latest_backup_sets,
)
from app.v2 import (
    CURRENT_SCRIPT_VERSION, ensure_v2_tables, start_v2_worker, authenticate_user,
    create_user as v2_create_user, list_users as v2_list_users, set_user_enabled as v2_set_user_enabled,
    audit as v2_audit, recent_audit as v2_recent_audit, record_script_version, get_script_state,
    availability as v2_availability, device_history as v2_device_history, backup_integrity as v2_backup_integrity,
    backup_compliance as v2_backup_compliance, alert_rows as v2_alert_rows,
    acknowledge_alert as v2_ack_alert, mute_alert as v2_mute_alert, xlsx_bytes as v2_xlsx_bytes,
    get_scheduled_report as v2_get_scheduled_report, save_scheduled_report as v2_save_scheduled_report,
)


templates = Jinja2Templates(directory="app/templates")
ATHENS_TZ = ZoneInfo("Europe/Athens")


def athens_time(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ATHENS_TZ)


templates.env.filters["athens_time"] = athens_time


def logged_in(request: Request):
    return bool(request.session.get("user"))


def current_role(request: Request):
    return request.session.get("role", "admin" if request.session.get("user") == settings.admin_username else "read_only")


def current_customer_scope(request: Request):
    return request.session.get("customer_id") or ""


def require_web_admin(request: Request):
    if not logged_in(request):
        raise HTTPException(401, "Login required")
    if current_role(request) != "admin":
        raise HTTPException(403, "Admin permission required")


def can_access_device(request: Request, device: Device):
    if current_role(request) != "customer":
        return True
    return device.customer_id == current_customer_scope(request)


def require_admin(x_admin_key: str = Header(...)):
    if not secrets.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


def require_device(x_device_key: str = Header(...), db: Session = Depends(get_db)):
    device = db.scalar(select(Device).where(Device.api_key == x_device_key, Device.enabled == True))
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device key")
    return device


def down_after_minutes():
    try:
        return max(1, int(rv_get_setting("down_after_minutes", "5") or "5"))
    except Exception:
        return 5


def heartbeat_interval_minutes():
    try:
        return max(1, int(rv_get_setting("heartbeat_interval_minutes", "5") or "5"))
    except Exception:
        return 5


def device_status(device: Device, now=None):
    now = now or datetime.now(timezone.utc)
    if not device.last_seen_at:
        return "WAITING"
    seen = device.last_seen_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return "UP" if (now - seen).total_seconds() <= down_after_minutes() * 60 else "DOWN"


def parse_local_date_start(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value).replace(tzinfo=ATHENS_TZ)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_local_date_end(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value).replace(hour=23, minute=59, second=59, tzinfo=ATHENS_TZ)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_bucket()
    ensure_stage3_tables()
    start_background_monitor()
    ensure_v2_tables()
    start_v2_worker()

    db = next(get_db())
    try:
        default = db.scalar(select(Customer).where(Customer.name == "Default"))
        if not default:
            db.add(Customer(name="Default"))
            db.commit()
    finally:
        db.close()

    threading.Thread(target=ingest_loop, daemon=True, name="routervault-ingest").start()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret, same_site="lax", https_only=False)


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "app_name": settings.app_name},
    )


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if secrets.compare_digest(username, settings.admin_username) and rv_verify_admin_password(password):
        request.session["user"] = username
        request.session["role"] = "admin"
        request.session["customer_id"] = ""
        v2_audit(username, "login", "session", "", "Legacy admin login")
        return RedirectResponse("/", status_code=303)

    user = authenticate_user(username, password)
    if user:
        request.session["user"] = user["username"]
        request.session["role"] = user["role"]
        request.session["customer_id"] = user.get("customer_id") or ""
        v2_audit(user["username"], "login", "session", user["id"], f"role={user['role']}")
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Invalid username or password", "app_name": settings.app_name},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    user = request.session.get("user") or "unknown"
    v2_audit(user, "logout", "session")
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    customer: str = "",
    site: str = "",
    device: str = "",
    status: str = "",
    routeros: str = "",
    model: str = "",
    q: str = "",
    db: Session = Depends(get_db),
):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)

    now = datetime.now(timezone.utc)
    customers_list = db.scalars(select(Customer).order_by(Customer.name)).all()
    sites_list = db.scalars(select(Site).order_by(Site.name)).all()
    devices_all = db.scalars(select(Device).order_by(Device.name)).all()

    scope_customer = current_customer_scope(request)
    if current_role(request) == "customer" and scope_customer:
        customers_list = [c for c in customers_list if c.id == scope_customer]
        sites_list = [st for st in sites_list if st.customer_id == scope_customer]
        devices_all = [d for d in devices_all if d.customer_id == scope_customer]
        customer = scope_customer

    customer_map = {c.id: c for c in customers_list}
    site_map = {s.id: s for s in sites_list}

    rows = []
    for d in devices_all:
        state = device_status(d, now)
        if customer and d.customer_id != customer:
            continue
        if site and (d.site_id or "") != site:
            continue
        if device and d.id != device:
            continue
        if status and state != status:
            continue
        if routeros and routeros.lower() not in (d.routeros_version or "").lower():
            continue
        if model and model.lower() not in (d.model or "").lower():
            continue
        if q:
            hay = " ".join([
                d.name or "",
                d.model or "",
                (customer_map.get(d.customer_id).name if customer_map.get(d.customer_id) else ""),
                (site_map.get(d.site_id).name if d.site_id and site_map.get(d.site_id) else ""),
            ]).lower()
            if q.lower() not in hay:
                continue

        rows.append({
            "device": d,
            "customer": customer_map.get(d.customer_id),
            "site": site_map.get(d.site_id) if d.site_id else None,
            "status": state,
        })

    total_up = sum(1 for d in devices_all if device_status(d, now) == "UP")
    total_down = sum(1 for d in devices_all if device_status(d, now) == "DOWN")
    total_waiting = sum(1 for d in devices_all if device_status(d, now) == "WAITING")
    compliance = v2_backup_compliance(devices_all)
    missing_backup_count = sum(1 for x in compliance if not (x["today"] and x["integrity"]["valid"]))
    all_alerts = v2_alert_rows()
    if scope_customer:
        allowed_ids = {d.id for d in devices_all}
        active_alert_count = sum(1 for a in all_alerts if a["active"] and any(a["alert_key"].endswith(i) for i in allowed_ids))
    else:
        active_alert_count = sum(1 for a in all_alerts if a["active"])
    if scope_customer:
        backup_count = db.scalar(
            select(func.count()).select_from(Backup).join(Device, Backup.device_id == Device.id).where(Device.customer_id == scope_customer)
        ) or 0
    else:
        backup_count = db.scalar(select(func.count()).select_from(Backup)) or 0

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "app_name": settings.app_name,
            "customers": len(customers_list),
            "devices": len(devices_all),
            "backups": backup_count,
            "up_count": total_up,
            "down_count": total_down,
            "waiting_count": total_waiting,
            "missing_backup_count": missing_backup_count,
            "active_alert_count": active_alert_count,
            "device_rows": rows,
            "customers_list": customers_list,
            "sites_list": sites_list,
            "devices_list": devices_all,
            "filters": {
                "customer": customer,
                "site": site,
                "device": device,
                "status": status,
                "routeros": routeros,
                "model": model,
                "q": q,
            },
            "now": now,
            "down_after": down_after_minutes(),
            "role": current_role(request),
            "current_user": request.session.get("user"),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def routervault_settings_page(request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "app_name": settings.app_name,
            "cfg": rv_get_all_settings(),
            "customers_list": db.scalars(select(Customer).order_by(Customer.name)).all(),
            "sites_list": db.scalars(select(Site).order_by(Site.name)).all(),
            "devices_list": db.scalars(select(Device).order_by(Device.name)).all(),
            "scheduled_report": v2_get_scheduled_report(),
            "role": current_role(request),
        },
    )


@app.post("/settings")
def routervault_save_settings(
    request: Request,
    public_scheme: str = Form("http"),
    public_host: str = Form(""),
    public_app_port: str = Form(""),
    public_sftp_port: str = Form(""),
    alert_email: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_security: str = Form("starttls"),
    smtp_from_email: str = Form(""),
    smtp_from_name: str = Form("RouterVault"),
    heartbeat_interval_minutes: str = Form("5"),
    down_after_minutes: str = Form("5"),
    backup_expected_hour: str = Form("4"),
    backup_expected_minute: str = Form("0"),
    automatic_retention_days: str = Form("0"),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)

    old = rv_get_all_settings()
    values = {
        "public_scheme": public_scheme.strip(),
        "public_host": public_host.strip(),
        "public_app_port": public_app_port.strip(),
        "public_sftp_port": public_sftp_port.strip(),
        "alert_email": alert_email.strip(),
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port.strip(),
        "smtp_username": smtp_username.strip(),
        "smtp_security": smtp_security.strip(),
        "smtp_from_email": smtp_from_email.strip(),
        "smtp_from_name": smtp_from_name.strip(),
        "heartbeat_interval_minutes": heartbeat_interval_minutes.strip(),
        "down_after_minutes": down_after_minutes.strip(),
        "backup_expected_hour": backup_expected_hour.strip(),
        "backup_expected_minute": backup_expected_minute.strip(),
        "automatic_retention_days": automatic_retention_days.strip(),
    }

    # Validate the values that directly affect monitoring/schedulers.
    try:
        hb = int(values["heartbeat_interval_minutes"] or "5")
        down = int(values["down_after_minutes"] or "5")
        bh = int(values["backup_expected_hour"] or "4")
        bm = int(values["backup_expected_minute"] or "0")
        retention = int(values["automatic_retention_days"] or "0")
        if hb < 1 or hb > 60 or down < 1 or down > 1440 or not 0 <= bh <= 23 or not 0 <= bm <= 59 or retention < 0:
            raise ValueError
    except Exception:
        raise HTTPException(400, "Invalid monitoring settings")

    for key, value in values.items():
        rv_set_setting(key, value)

    if smtp_password:
        rv_set_setting("smtp_password", smtp_password)

    generator_keys = {
        "public_scheme", "public_host", "public_app_port", "public_sftp_port",
        "heartbeat_interval_minutes",
    }
    if any((old.get(k) or "") != (values.get(k) or "") for k in generator_keys):
        rv_set_setting("script_config_updated_at", datetime.now(timezone.utc).isoformat())

    v2_audit(request.session.get("user") or "admin", "save_settings", "settings", "global")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/change-password")
def routervault_change_password(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if new_password != confirm_password:
        raise HTTPException(400, "Passwords do not match")
    rv_set_admin_password(new_password)
    return RedirectResponse("/settings?password_changed=1", status_code=303)


@app.post("/settings/test-email")
def routervault_test_email(request: Request):
    require_web_admin(request)
    ok, message = rv_send_test_email()
    if not ok:
        raise HTTPException(500, f"SMTP test failed: {message}")
    return RedirectResponse("/settings?email_sent=1", status_code=303)


@app.post("/settings/cleanup")
def routervault_cleanup(
    request: Request,
    age_value: int = Form(...),
    age_unit: str = Form("days"),
    customer_id: str = Form(""),
    site_id: str = Form(""),
    device_id: str = Form(""),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    deleted = rv_cleanup_backups(
        age_value,
        age_unit,
        customer_id=customer_id,
        site_id=site_id,
        device_id=device_id,
    )
    v2_audit(request.session.get("user") or "admin", "backup_cleanup", "backup", device_id or site_id or customer_id or "global", f"deleted={deleted};age={age_value} {age_unit}")
    return RedirectResponse(f"/settings?deleted={deleted}", status_code=303)


@app.post("/settings/scheduled-report")
def scheduled_report_save(
    request: Request,
    enabled: str = Form(""),
    recipient: str = Form(""),
    report_hour: int = Form(8),
):
    require_web_admin(request)
    v2_save_scheduled_report(enabled == "1", recipient, report_hour)
    v2_audit(request.session.get("user") or "admin", "scheduled_report_settings", "settings", "daily", f"enabled={enabled};hour={report_hour}")
    return RedirectResponse("/settings?report_saved=1", status_code=303)


@app.get("/backups/bulk-download")
def routervault_bulk_download(
    request: Request,
    customer: str = "",
    site: str = "",
    device: str = "",
    latest_only: int = 0,
    db: Session = Depends(get_db),
):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)

    scope_customer = current_customer_scope(request) if current_role(request) == "customer" else ""
    if scope_customer:
        customer = scope_customer
        site = "" if site and not db.get(Site, site) else site
        device_obj = db.get(Device, device) if device else None
        if device_obj and device_obj.customer_id != scope_customer:
            raise HTTPException(403, "Forbidden")

    stmt = select(Backup, Device).join(Device, Backup.device_id == Device.id)
    if scope_customer:
        stmt = stmt.where(Device.customer_id == scope_customer)
    if device:
        stmt = stmt.where(Device.id == device)
    elif site:
        stmt = stmt.where(Device.site_id == site)
    elif customer:
        stmt = stmt.where(Device.customer_id == customer)

    rows = db.execute(stmt.order_by(Device.name, Backup.created_at.desc())).all()
    backups = rv_select_latest_backup_sets(rows) if latest_only else [b for b, _ in rows]
    if not backups:
        raise HTTPException(404, "No backups found")

    data = rv_build_zip(backups)
    filename = "routervault-backups-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip"
    return StreamingResponse(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def report_data(
    db: Session,
    report_type: str,
    customer: str = "",
    site: str = "",
    device: str = "",
    status: str = "",
    routeros: str = "",
    model: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
):
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    customer_map = {c.id: c for c in customers}
    site_map = {s.id: s for s in sites}
    device_map = {d.id: d for d in devices}
    now = datetime.now(timezone.utc)

    if report_type == "backups":
        stmt = select(Backup).order_by(Backup.created_at.desc())
        start = parse_local_date_start(date_from)
        end = parse_local_date_end(date_to)
        if start:
            stmt = stmt.where(Backup.created_at >= start)
        if end:
            stmt = stmt.where(Backup.created_at <= end)

        backup_rows = db.scalars(stmt).all()
        rows = []
        for b in backup_rows:
            d = device_map.get(b.device_id)
            if not d:
                continue
            if customer and d.customer_id != customer:
                continue
            if site and (d.site_id or "") != site:
                continue
            if device and d.id != device:
                continue
            if q and q.lower() not in (b.filename + " " + d.name).lower():
                continue
            rows.append({
                "backup": b,
                "device": d,
                "customer": customer_map.get(d.customer_id),
                "site": site_map.get(d.site_id) if d.site_id else None,
            })
        return rows, customers, sites, devices

    rows = []
    for d in devices:
        state = device_status(d, now)
        if customer and d.customer_id != customer:
            continue
        if site and (d.site_id or "") != site:
            continue
        if device and d.id != device:
            continue
        if status and state != status:
            continue
        if routeros and routeros.lower() not in (d.routeros_version or "").lower():
            continue
        if model and model.lower() not in (d.model or "").lower():
            continue
        if q and q.lower() not in (d.name + " " + (d.model or "")).lower():
            continue
        rows.append({
            "device": d,
            "customer": customer_map.get(d.customer_id),
            "site": site_map.get(d.site_id) if d.site_id else None,
            "status": state,
        })
    return rows, customers, sites, devices


@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    type: str = "devices",
    customer: str = "",
    site: str = "",
    device: str = "",
    status: str = "",
    routeros: str = "",
    model: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    report_type = "backups" if type == "backups" else "devices"
    if current_role(request) == "customer":
        customer = current_customer_scope(request)
    rows, customers, sites, devices = report_data(
        db, report_type, customer, site, device, status, routeros, model, q, date_from, date_to
    )
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "app_name": settings.app_name,
            "report_type": report_type,
            "rows": rows,
            "customers_list": customers,
            "sites_list": sites,
            "devices_list": devices,
            "filters": {
                "customer": customer, "site": site, "device": device,
                "status": status, "routeros": routeros, "model": model, "q": q,
                "date_from": date_from, "date_to": date_to,
            },
            "now": datetime.now(timezone.utc),
        },
    )


@app.get("/reports.csv")
def reports_csv(
    request: Request,
    type: str = "devices",
    customer: str = "",
    site: str = "",
    device: str = "",
    status: str = "",
    routeros: str = "",
    model: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)

    report_type = "backups" if type == "backups" else "devices"
    if current_role(request) == "customer":
        customer = current_customer_scope(request)
    rows, _, _, _ = report_data(
        db, report_type, customer, site, device, status, routeros, model, q, date_from, date_to
    )

    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)

    if report_type == "backups":
        writer.writerow(["Customer", "Site", "Device", "Filename", "Type", "Size bytes", "Created at"])
        for row in rows:
            b, d = row["backup"], row["device"]
            writer.writerow([
                row["customer"].name if row["customer"] else "",
                row["site"].name if row["site"] else "",
                d.name,
                b.filename,
                b.backup_type,
                b.size_bytes,
                athens_time(b.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            ])
        filename = "routervault-backups-report.csv"
    else:
        writer.writerow(["Customer", "Site", "Device", "Model", "RouterOS", "Status", "Uptime", "CPU", "Free RAM", "Last heartbeat", "Last backup"])
        for row in rows:
            d = row["device"]
            writer.writerow([
                row["customer"].name if row["customer"] else "",
                row["site"].name if row["site"] else "",
                d.name,
                d.model or "",
                d.routeros_version or "",
                row["status"],
                d.uptime or "",
                d.cpu_load or "",
                d.free_memory or "",
                athens_time(d.last_seen_at).strftime("%Y-%m-%d %H:%M:%S") if d.last_seen_at else "",
                athens_time(d.last_backup_at).strftime("%Y-%m-%d %H:%M:%S") if d.last_backup_at else "",
            ])
        filename = "routervault-device-report.csv"

    return Response(
        content=out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/devices/new", response_class=HTMLResponse)
def new_device_page(request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    return templates.TemplateResponse(
        request=request,
        name="device_new.html",
        context={
            "app_name": settings.app_name,
            "customers": customers,
            "sites": sites,
        },
    )


@app.post("/devices/new")
def create_device_web(
    request: Request,
    device_name: str = Form(...),
    customer_name: str = Form("Default"),
    site_name: str = Form(""),
    model: str = Form(""),
    db: Session = Depends(get_db),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)

    device_name = device_name.strip()
    customer_name = customer_name.strip() or "Default"
    site_name = site_name.strip()

    existing = db.scalar(select(Device).where(Device.name == device_name))
    if existing:
        return RedirectResponse(f"/devices/{existing.id}", status_code=303)

    customer = db.scalar(select(Customer).where(Customer.name == customer_name))
    if not customer:
        customer = Customer(name=customer_name)
        db.add(customer)
        db.commit()
        db.refresh(customer)

    site = None
    if site_name:
        site = db.scalar(select(Site).where(Site.customer_id == customer.id, Site.name == site_name))
        if not site:
            site = Site(customer_id=customer.id, name=site_name)
            db.add(site)
            db.commit()
            db.refresh(site)

    device_obj = Device(
        customer_id=customer.id,
        site_id=site.id if site else None,
        name=device_name,
        vendor="MikroTik",
        model=model.strip() or None,
        api_key="rv_" + secrets.token_urlsafe(32),
    )
    db.add(device_obj)
    db.commit()
    db.refresh(device_obj)
    v2_audit(request.session.get("user") or "admin", "create_device", "device", device_obj.id, device_obj.name)
    return RedirectResponse(f"/devices/{device_obj.id}", status_code=303)


def router_script(request: Request, device: Device):
    api_base = rv_public_api_base()
    host = rv_public_host()
    sftp_public_port = rv_public_sftp_port()
    hb_interval = heartbeat_interval_minutes()

    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in device.name)

    return f"""# ============================================================
# RouterVault MikroTik Installer
# Script version: {CURRENT_SCRIPT_VERSION}
# Device: {device.name}
# RouterOS: 6 / 7
# Heartbeat: every {hb_interval} minute(s)
# Backup: daily 03:00
# SFTP: {host}:{sftp_public_port}
# ============================================================

:if ([:len [/system scheduler find name="routervault-heartbeat"]] > 0) do={{
    /system scheduler remove [find name="routervault-heartbeat"]
}}
:if ([:len [/system scheduler find name="routervault-backup"]] > 0) do={{
    /system scheduler remove [find name="routervault-backup"]
}}
:if ([:len [/system script find name="routervault-heartbeat"]] > 0) do={{
    /system script remove [find name="routervault-heartbeat"]
}}
:if ([:len [/system script find name="routervault-backup"]] > 0) do={{
    /system script remove [find name="routervault-backup"]
}}

/system script add name="routervault-heartbeat" policy=read,test source={{
    :local ver [/system resource get version]
    :local uptime [/system resource get uptime]
    :local cpu [/system resource get cpu-load]
    :local mem [/system resource get free-memory]
    :local sp [:find $ver " "]
    :if ($sp != nil) do={{ :set ver [:pick $ver 0 $sp] }}
    :local heartbeatUrl ("{api_base}/api/v2/device/{device.api_key}/heartbeat-metrics/" . $ver . "/" . $uptime . "/" . $cpu . "/" . $mem . "/{CURRENT_SCRIPT_VERSION}")
    /tool fetch url=$heartbeatUrl keep-result=no
}}

/system scheduler add name="routervault-heartbeat" interval={hb_interval}m start-time=startup policy=read,test on-event="/system script run routervault-heartbeat"

/system script add name="routervault-backup" policy=ftp,reboot,read,write,policy,test,password,sniff,sensitive source={{
    :local d [/system clock get date]
    :local t [/system clock get time]
    :local stamp ""

    :if ([:find $d "/"] != nil) do={{
        :local mon [:pick $d 0 3]
        :local day [:pick $d 4 6]
        :local year [:pick $d 7 11]
        :set stamp ($year . "-" . $mon . "-" . $day . "_" . [:pick $t 0 2] . "-" . [:pick $t 3 5] . "-" . [:pick $t 6 8])
    }} else={{
        :set stamp ($d . "_" . [:pick $t 0 2] . "-" . [:pick $t 3 5] . "-" . [:pick $t 6 8])
    }}

    :local exportBase ("{safe_name}__" . $stamp . "__config")
    :local backupBase ("{safe_name}__" . $stamp . "__full")
    :local exportFile ($exportBase . ".rsc")
    :local backupFile ($backupBase . ".backup")
    :local exportOK false
    :local backupOK false

    /export file=$exportBase
    /system backup save name=$backupBase
    :delay 5s

    :do {{
        /tool fetch upload=yes url=("sftp://{host}:{sftp_public_port}/upload/" . $exportFile) user="{settings.sftp_user}" password="{settings.sftp_password}" src-path=$exportFile keep-result=no
        :set exportOK true
    }} on-error={{ :log error "RouterVault: RSC upload failed" }}

    :delay 2s

    :do {{
        /tool fetch upload=yes url=("sftp://{host}:{sftp_public_port}/upload/" . $backupFile) user="{settings.sftp_user}" password="{settings.sftp_password}" src-path=$backupFile keep-result=no
        :set backupOK true
    }} on-error={{ :log error "RouterVault: BACKUP upload failed" }}

    :delay 2s

    :if ($exportOK = true) do={{
        :if ([:len [/file find name=$exportFile]] > 0) do={{ /file remove [find name=$exportFile] }}
    }}
    :if ($backupOK = true) do={{
        :if ([:len [/file find name=$backupFile]] > 0) do={{ /file remove [find name=$backupFile] }}
    }}
}}

/system scheduler add name="routervault-backup" interval=1d start-time=03:00:00 policy=ftp,reboot,read,write,policy,test,password,sniff,sensitive on-event="/system script run routervault-backup"
/system script run routervault-heartbeat
"""


@app.get("/devices/{device_id}/edit", response_class=HTMLResponse)
def edit_device_page(device_id: str, request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if not can_access_device(request, device):
        raise HTTPException(403, "Forbidden")
    customer = db.get(Customer, device.customer_id) if device.customer_id else None
    site = db.get(Site, device.site_id) if device.site_id else None
    return templates.TemplateResponse(
        request=request,
        name="device_edit.html",
        context={
            "app_name": settings.app_name,
            "device": device,
            "customer_name": customer.name if customer else "Default",
            "site_name": site.name if site else "",
        },
    )


@app.post("/devices/{device_id}/edit")
def edit_device_save(
    device_id: str,
    request: Request,
    device_name: str = Form(...),
    customer_name: str = Form("Default"),
    site_name: str = Form(""),
    model: str = Form(""),
    db: Session = Depends(get_db),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    device_name = device_name.strip()
    customer_name = customer_name.strip() or "Default"
    site_name = site_name.strip()

    duplicate = db.scalar(select(Device).where(Device.name == device_name, Device.id != device.id))
    if duplicate:
        raise HTTPException(400, "Another device already uses this name")

    customer = db.scalar(select(Customer).where(Customer.name == customer_name))
    if not customer:
        customer = Customer(name=customer_name)
        db.add(customer)
        db.commit()
        db.refresh(customer)

    site = None
    if site_name:
        site = db.scalar(select(Site).where(Site.customer_id == customer.id, Site.name == site_name))
        if not site:
            site = Site(customer_id=customer.id, name=site_name)
            db.add(site)
            db.commit()
            db.refresh(site)

    device.name = device_name
    device.customer_id = customer.id
    device.site_id = site.id if site else None
    device.model = model.strip() or None
    db.commit()
    v2_audit(request.session.get("user") or "admin", "edit_device", "device", device.id, device.name)
    return RedirectResponse(f"/devices/{device.id}", status_code=303)


@app.post("/devices/{device_id}/delete/execute")
def delete_device(
    device_id: str,
    request: Request,
    confirm_name: str = Form(...),
    delete_backups: str = Form("yes"),
    db: Session = Depends(get_db),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")

    if confirm_name.strip() != device.name:
        raise HTTPException(400, "Device name confirmation does not match")

    backups = db.scalars(select(Backup).where(Backup.device_id == device.id)).all()
    if delete_backups == "yes":
        for backup in backups:
            try:
                delete_object(backup.object_key)
            except Exception as exc:
                print(f"device delete storage warning {backup.object_key}: {exc}", flush=True)
            db.delete(backup)
    else:
        # Preserve stored backup objects. Remove DB backup rows before device deletion to avoid stale ownership.
        for backup in backups:
            db.delete(backup)

    for event in db.scalars(select(Event).where(Event.device_id == device.id)).all():
        db.delete(event)

    v2_audit(request.session.get("user") or "admin", "delete_device", "device", device.id, f"name={device.name};delete_backups={delete_backups}")
    db.delete(device)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/devices/{device_id}/backups/{backup_id}/download")
def download_device_backup(device_id: str, backup_id: str, request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if not can_access_device(request, device):
        raise HTTPException(403, "Forbidden")
    backup = db.get(Backup, backup_id)
    if not backup or backup.device_id != device.id:
        raise HTTPException(404, "Backup not found")

    try:
        obj = get_object(backup.object_key)
    except Exception as exc:
        print(f"download error {backup.object_key}: {exc}", flush=True)
        raise HTTPException(500, "Could not read backup from storage")

    return StreamingResponse(
        obj["Body"].iter_chunks(chunk_size=1024 * 1024),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{backup.filename}"'},
    )


@app.post("/devices/{device_id}/backups/delete-selected")
def delete_selected_backups(
    device_id: str,
    request: Request,
    backup_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    require_web_admin(request)
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if not backup_ids:
        return RedirectResponse(f"/devices/{device.id}", status_code=303)

    backups = db.scalars(
        select(Backup).where(Backup.device_id == device.id, Backup.id.in_(backup_ids))
    ).all()

    for backup in backups:
        try:
            delete_object(backup.object_key)
        except Exception as exc:
            print(f"backup delete object error {backup.object_key}: {exc}", flush=True)
            raise HTTPException(500, f"Could not delete {backup.filename} from storage")
        db.delete(backup)
        db.add(Event(device_id=device.id, event_type="backup_deleted", message=f"Deleted backup {backup.filename}"))

    db.commit()
    v2_audit(request.session.get("user") or "admin", "delete_backups", "device", device.id, f"count={len(backups)}")
    latest = db.scalar(
        select(Backup).where(Backup.device_id == device.id).order_by(Backup.created_at.desc()).limit(1)
    )
    device.last_backup_at = latest.created_at if latest else None
    db.commit()
    return RedirectResponse(f"/devices/{device.id}", status_code=303)


@app.get("/devices/{device_id}", response_class=HTMLResponse)
def device_page(device_id: str, request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if not can_access_device(request, device):
        raise HTTPException(403, "Forbidden")
    backups = db.scalars(
        select(Backup).where(Backup.device_id == device.id).order_by(Backup.created_at.desc()).limit(50)
    ).all()
    customer = db.get(Customer, device.customer_id) if device.customer_id else None
    site = db.get(Site, device.site_id) if device.site_id else None
    return templates.TemplateResponse(
        request=request,
        name="device.html",
        context={
            "app_name": settings.app_name,
            "device": device,
            "customer": customer,
            "site": site,
            "script": router_script(request, device),
            "backups": backups,
            "now": datetime.now(timezone.utc),
            "status": device_status(device),
            "heartbeat_interval": heartbeat_interval_minutes(),
            "down_after": down_after_minutes(),
            "script_config_updated_at": rv_get_setting("script_config_updated_at", ""),
            "script_state": get_script_state(device.id),
            "script_version": CURRENT_SCRIPT_VERSION,
            "availability_1d": v2_availability(device.id, 1),
            "availability_7d": v2_availability(device.id, 7),
            "availability_30d": v2_availability(device.id, 30),
            "history": v2_device_history(device.id, 24),
            "backup_integrity": v2_backup_integrity(device.id),
            "role": current_role(request),
        },
    )


@app.get("/devices/{device_id}/script", response_class=PlainTextResponse)
def device_script(device_id: str, request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        raise HTTPException(401, "Login required")
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    if not can_access_device(request, device):
        raise HTTPException(403, "Forbidden")
    return router_script(request, device)



@app.get("/api/v2/device/heartbeat-secure/{version}/{uptime}/{cpu}/{free_memory}/{script_version}")
def heartbeat_metrics_secure(
    version: str, uptime: str, cpu: int, free_memory: int, script_version: str,
    x_device_key: str = Header(...), db: Session = Depends(get_db),
):
    device = db.scalar(select(Device).where(Device.api_key == x_device_key, Device.enabled == True))
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device key")
    device.last_seen_at = datetime.now(timezone.utc)
    device.routeros_version = version
    device.uptime = uptime
    device.cpu_load = str(cpu)
    device.free_memory = str(free_memory)
    db.commit()
    record_script_version(device.id, script_version)
    return {"ok": True, "device": device.name, "script_version": script_version}


@app.get("/api/v2/device/{device_key}/heartbeat-metrics/{version}/{uptime}/{cpu}/{free_memory}/{script_version}")
def heartbeat_metrics_v2(
    device_key: str,
    version: str,
    uptime: str,
    cpu: int,
    free_memory: int,
    script_version: str,
    db: Session = Depends(get_db),
):
    device = db.scalar(select(Device).where(Device.api_key == device_key, Device.enabled == True))
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device key")
    device.last_seen_at = datetime.now(timezone.utc)
    device.routeros_version = version
    device.uptime = uptime
    device.cpu_load = str(cpu)
    device.free_memory = str(free_memory)
    db.commit()
    record_script_version(device.id, script_version)
    return {"ok": True, "device": device.name, "script_version": script_version}


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_page(customer_id: str, request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    if current_role(request) == "customer" and current_customer_scope(request) != customer_id:
        raise HTTPException(403, "Forbidden")
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404, "Customer not found")
    sites = db.scalars(select(Site).where(Site.customer_id == customer.id).order_by(Site.name)).all()
    devices = db.scalars(select(Device).where(Device.customer_id == customer.id).order_by(Device.name)).all()
    compliance = v2_backup_compliance(devices)
    return templates.TemplateResponse(request=request, name="customer.html", context={
        "app_name": settings.app_name, "customer": customer, "sites": sites, "devices": devices,
        "compliance": compliance, "now": datetime.now(timezone.utc), "device_status": device_status,
    })


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_page(site_id: str, request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    site = db.get(Site, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    if current_role(request) == "customer" and current_customer_scope(request) != site.customer_id:
        raise HTTPException(403, "Forbidden")
    customer = db.get(Customer, site.customer_id)
    devices = db.scalars(select(Device).where(Device.site_id == site.id).order_by(Device.name)).all()
    return templates.TemplateResponse(request=request, name="site.html", context={
        "app_name": settings.app_name, "site": site, "customer": customer, "devices": devices,
        "now": datetime.now(timezone.utc), "device_status": device_status,
    })


@app.get("/search", response_class=HTMLResponse)
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    term = q.strip().lower()
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    scope = current_customer_scope(request) if current_role(request) == "customer" else ""
    if scope:
        customers = [x for x in customers if x.id == scope]
        sites = [x for x in sites if x.customer_id == scope]
        devices = [x for x in devices if x.customer_id == scope]
    if term:
        customers = [x for x in customers if term in x.name.lower()]
        sites = [x for x in sites if term in x.name.lower()]
        devices = [x for x in devices if term in " ".join([x.name or "",x.model or "",x.routeros_version or "",x.wan_ip or ""]).lower()]
    return templates.TemplateResponse(request=request, name="search.html", context={"app_name":settings.app_name,"q":q,"customers":customers,"sites":sites,"devices":devices})


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=302)
    alerts = v2_alert_rows()
    if current_role(request) == "customer":
        scope = current_customer_scope(request)
        ids = {d.id for d in db.scalars(select(Device).where(Device.customer_id == scope)).all()}
        alerts = [a for a in alerts if any(a["alert_key"].endswith(i) for i in ids)]
    return templates.TemplateResponse(request=request, name="alerts.html", context={"app_name":settings.app_name,"alerts":alerts,"role":current_role(request)})


@app.post("/alerts/ack")
def alerts_ack(request: Request, alert_key: str = Form(...)):
    require_web_admin(request)
    v2_ack_alert(alert_key, request.session.get("user") or "admin")
    v2_audit(request.session.get("user") or "admin", "ack_alert", "alert", alert_key)
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/mute")
def alerts_mute(request: Request, alert_key: str = Form(...), hours: int = Form(24)):
    require_web_admin(request)
    v2_mute_alert(alert_key, hours)
    v2_audit(request.session.get("user") or "admin", "mute_alert", "alert", alert_key, f"hours={hours}")
    return RedirectResponse("/alerts", status_code=303)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)
    return templates.TemplateResponse(request=request, name="users.html", context={
        "app_name":settings.app_name,"users":v2_list_users(),"customers":db.scalars(select(Customer).order_by(Customer.name)).all()
    })


@app.post("/users/new")
def user_create(request: Request, username: str = Form(...), password: str = Form(...), role: str = Form(...), customer_id: str = Form("")):
    require_web_admin(request)
    try:
        v2_create_user(username, password, role, customer_id or None)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    v2_audit(request.session.get("user") or "admin", "create_user", "user", username, f"role={role}")
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/toggle")
def user_toggle(user_id: str, request: Request, enabled: int = Form(...)):
    require_web_admin(request)
    v2_set_user_enabled(user_id, bool(enabled))
    v2_audit(request.session.get("user") or "admin", "toggle_user", "user", user_id, f"enabled={bool(enabled)}")
    return RedirectResponse("/users", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    require_web_admin(request)
    return templates.TemplateResponse(request=request, name="audit.html", context={"app_name":settings.app_name,"rows":v2_recent_audit(500)})


@app.get("/devices/{device_id}/delete-confirm", response_class=HTMLResponse)
def delete_confirm_page(device_id: str, request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)
    device = db.get(Device, device_id)
    if not device: raise HTTPException(404, "Device not found")
    count = db.scalar(select(func.count()).select_from(Backup).where(Backup.device_id == device.id)) or 0
    return templates.TemplateResponse(request=request, name="delete_confirm.html", context={"app_name":settings.app_name,"device":device,"backup_count":count})


@app.post("/devices/{device_id}/rotate-token")
def rotate_device_token(device_id: str, request: Request, db: Session = Depends(get_db)):
    require_web_admin(request)
    device = db.get(Device, device_id)
    if not device: raise HTTPException(404, "Device not found")
    device.api_key = "rv_" + secrets.token_urlsafe(32)
    db.commit()
    rv_set_setting("script_config_updated_at", datetime.now(timezone.utc).isoformat())
    v2_audit(request.session.get("user") or "admin", "rotate_device_token", "device", device.id, device.name)
    return RedirectResponse(f"/devices/{device.id}?token_rotated=1", status_code=303)


@app.get("/reports.xlsx")
def reports_xlsx(request: Request, report: str = "devices", db: Session = Depends(get_db)):
    if not logged_in(request): raise HTTPException(401, "Login required")
    scope = current_customer_scope(request) if current_role(request)=="customer" else ""
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    if scope: devices=[d for d in devices if d.customer_id==scope]
    customers={c.id:c for c in db.scalars(select(Customer)).all()}
    sites={st.id:st for st in db.scalars(select(Site)).all()}
    if report == "backup_compliance":
        comp=v2_backup_compliance(devices)
        headers=["Customer","Site","Device","Backup today","Integrity","Last backup"]
        rows=[]
        for x in comp:
            d=x["device"]
            rows.append([customers.get(d.customer_id).name if customers.get(d.customer_id) else "", sites.get(d.site_id).name if d.site_id and sites.get(d.site_id) else "", d.name, "YES" if x["today"] else "NO", x["integrity"]["status"], str(d.last_backup_at or "")])
        data=v2_xlsx_bytes("Backup Compliance",headers,rows)
        filename="routervault-backup-compliance.xlsx"
    elif report == "availability":
        headers=["Customer","Site","Device","1d %","7d %","30d %"]
        rows=[[customers.get(d.customer_id).name if customers.get(d.customer_id) else "", sites.get(d.site_id).name if d.site_id and sites.get(d.site_id) else "", d.name, v2_availability(d.id,1), v2_availability(d.id,7), v2_availability(d.id,30)] for d in devices]
        data=v2_xlsx_bytes("Availability",headers,rows); filename="routervault-availability.xlsx"
    else:
        headers=["Customer","Site","Device","Model","RouterOS","Status","CPU","RAM","WAN IP","Last heartbeat","Last backup"]
        rows=[[customers.get(d.customer_id).name if customers.get(d.customer_id) else "", sites.get(d.site_id).name if d.site_id and sites.get(d.site_id) else "", d.name,d.model or "",d.routeros_version or "",device_status(d),d.cpu_load or "",d.free_memory or "",d.wan_ip or "",str(d.last_seen_at or ""),str(d.last_backup_at or "")] for d in devices]
        data=v2_xlsx_bytes("Devices",headers,rows); filename="routervault-devices.xlsx"
    return StreamingResponse(data,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/reports-v2", response_class=HTMLResponse)
def reports_v2_page(request: Request, db: Session = Depends(get_db)):
    if not logged_in(request): return RedirectResponse("/login",302)
    scope = current_customer_scope(request) if current_role(request)=="customer" else ""
    devices=db.scalars(select(Device).order_by(Device.name)).all()
    if scope: devices=[d for d in devices if d.customer_id==scope]
    customers={c.id:c for c in db.scalars(select(Customer)).all()}; sites={st.id:st for st in db.scalars(select(Site)).all()}
    compliance=v2_backup_compliance(devices)
    versions={}
    for d in devices: versions[d.routeros_version or "Unknown"]=versions.get(d.routeros_version or "Unknown",0)+1
    high_cpu=[d for d in devices if (int(d.cpu_load) if str(d.cpu_load or '').isdigit() else 0)>=80]
    return templates.TemplateResponse(request=request,name="reports_v2.html",context={"app_name":settings.app_name,"devices":devices,"customers":customers,"sites":sites,"compliance":compliance,"versions":versions,"high_cpu":high_cpu,"device_status":device_status,"availability":v2_availability})


@app.post("/api/v1/device/heartbeat")
def heartbeat(payload: Heartbeat, device: Device = Depends(require_device), db: Session = Depends(get_db)):
    device.last_seen_at = datetime.now(timezone.utc)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(device, field, value)
    db.add(Event(device_id=device.id, event_type="heartbeat", message="Heartbeat received"))
    db.commit()
    return {"ok": True, "device": device.name}


@app.get("/api/v1/admin/backups/{backup_id}/download", dependencies=[Depends(require_admin)])
def download_backup(backup_id: str, db: Session = Depends(get_db)):
    b = db.get(Backup, backup_id)
    if not b:
        raise HTTPException(404, "Backup not found")
    return {"url": presigned_download(b.object_key)}


@app.get("/api/v1/device/{device_key}/heartbeat")
def heartbeat_simple(device_key: str, request: Request, db: Session = Depends(get_db)):
    device = db.scalar(select(Device).where(Device.api_key == device_key, Device.enabled == True))
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device key")

    q = request.query_params
    device.last_seen_at = datetime.now(timezone.utc)
    if q.get("version"):
        device.routeros_version = q.get("version")
    if q.get("uptime"):
        device.uptime = q.get("uptime")
    if q.get("cpu"):
        device.cpu_load = q.get("cpu")
    if q.get("free_memory"):
        device.free_memory = q.get("free_memory")
    db.commit()
    return {
        "ok": True,
        "device": device.name,
        "version": device.routeros_version,
        "uptime": device.uptime,
        "cpu": device.cpu_load,
        "free_memory": device.free_memory,
    }


@app.get("/api/v1/device/{device_key}/heartbeat-metrics/{version}/{uptime}/{cpu}/{free_memory}")
def heartbeat_metrics_path(
    device_key: str,
    version: str,
    uptime: str,
    cpu: int,
    free_memory: int,
    db: Session = Depends(get_db),
):
    device = db.scalar(select(Device).where(Device.api_key == device_key, Device.enabled == True))
    if not device:
        raise HTTPException(status_code=401, detail="Invalid device key")

    device.last_seen_at = datetime.now(timezone.utc)
    device.routeros_version = version
    device.uptime = uptime
    device.cpu_load = str(cpu)
    device.free_memory = str(free_memory)
    db.commit()
    return {
        "ok": True,
        "device": device.name,
        "version": version,
        "uptime": uptime,
        "cpu": cpu,
        "free_memory": free_memory,
    }
