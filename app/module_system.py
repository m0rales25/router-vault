"""Router Vault Module System API v1.

Modules are trusted Python extensions installed into a persistent Docker volume.
They are intentionally isolated from the locked MikroTik script generator contract,
but they execute inside the Router Vault API process and therefore MUST be treated
as trusted code.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect as pyinspect
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import ChoiceLoader, FileSystemLoader
from sqlalchemy import text

from app.db import SessionLocal
from app.settings_store import get_setting as core_get_setting, set_setting as core_set_setting

MODULE_API_VERSION = 1
MODULES_ROOT = Path(os.environ.get("ROUTER_VAULT_MODULES_DIR", "/srv/routervault-modules"))
HISTORY_ROOT = MODULES_ROOT / ".history"
TMP_ROOT = MODULES_ROOT / ".tmp"
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_UNPACKED_BYTES = 60 * 1024 * 1024
MAX_PACKAGE_FILES = 500
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
HANDLER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

ALLOWED_PERMISSIONS = {
    "devices.read",
    "devices.write",
    "customers.read",
    "sites.read",
    "backups.read",
    "settings.read",
    "settings.write",
    "network.generate",
    "module.storage",
}
ALLOWED_ACCESS_ROLES = {"admin", "read_only", "customer"}

_core_templates = None
_import_cache: dict[tuple[str, str, int, int], Any] = {}
_import_lock = threading.RLock()
_tables_lock = threading.RLock()
_tables_ready = False


class ModuleError(RuntimeError):
    pass


class ModuleValidationError(ModuleError):
    pass


class ModuleDependencyError(ModuleError):
    pass


class ModuleStateError(ModuleError):
    pass


def configure_core_templates(templates):
    """Attach the Router Vault Jinja environment for module templates."""
    global _core_templates
    _core_templates = templates


def _utcnow():
    return datetime.now(timezone.utc)


def current_app_version() -> str:
    env = (os.environ.get("ROUTER_VAULT_VERSION") or "").strip()
    if env:
        return env
    for candidate in (Path("VERSION"), Path("/srv/app/VERSION"), Path("/opt/routervault/VERSION")):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass
    return "0.0.0"


def ensure_module_tables():
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        MODULES_ROOT.mkdir(parents=True, exist_ok=True)
        HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
        TMP_ROOT.mkdir(parents=True, exist_ok=True)

        with SessionLocal() as db:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS rv_modules (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'installed',
                    manifest_json TEXT NOT NULL,
                    package_sha256 TEXT NOT NULL DEFAULT '',
                    installed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_error TEXT NOT NULL DEFAULT ''
                )
            """))
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS rv_module_settings (
                    module_slug TEXT NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (module_slug, setting_key)
                )
            """))
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS rv_module_migrations (
                    module_slug TEXT NOT NULL,
                    migration_id TEXT NOT NULL,
                    module_version TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    down_path TEXT NOT NULL DEFAULT '',
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (module_slug, migration_id)
                )
            """))
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS rv_module_events (
                    id BIGSERIAL PRIMARY KEY,
                    module_slug TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS idx_rv_module_events_slug_created ON rv_module_events(module_slug, created_at DESC)"))
            db.commit()
        _tables_ready = True


def _row_to_dict(row):
    if not row:
        return None
    data = dict(row)
    try:
        data["manifest"] = json.loads(data.pop("manifest_json") or "{}")
    except Exception:
        data["manifest"] = {}
    return data


def list_modules() -> list[dict]:
    ensure_module_tables()
    with SessionLocal() as db:
        rows = db.execute(text("SELECT * FROM rv_modules ORDER BY LOWER(name), slug")).mappings().all()
    modules = [_row_to_dict(row) for row in rows]
    for module in modules:
        module["history"] = history_versions(module["slug"])
    return modules


def get_module(slug: str) -> dict | None:
    ensure_module_tables()
    with SessionLocal() as db:
        row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
    result = _row_to_dict(row)
    if result:
        result["history"] = history_versions(slug)
    return result


def module_events(slug: str, limit: int = 100) -> list[dict]:
    ensure_module_tables()
    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT id,module_slug,level,event_type,message,created_at
            FROM rv_module_events
            WHERE module_slug=:s
            ORDER BY created_at DESC, id DESC
            LIMIT :n
        """), {"s": slug, "n": max(1, min(int(limit), 500))}).mappings().all()
    return [dict(r) for r in rows]


def _record_event(slug: str, event_type: str, message: str = "", level: str = "info", db=None):
    owns = db is None
    if owns:
        db = SessionLocal()
    try:
        db.execute(text("""
            INSERT INTO rv_module_events(module_slug,level,event_type,message,created_at)
            VALUES (:s,:l,:e,:m,NOW())
        """), {"s": slug, "l": level[:20], "e": event_type[:80], "m": (message or "")[:4000]})
        if owns:
            db.commit()
    finally:
        if owns:
            db.close()


def _safe_relpath(value: str, *, suffix: str | None = None) -> str:
    raw = (value or "").replace("\\", "/").strip()
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or ".." in p.parts or raw.startswith("/"):
        raise ModuleValidationError(f"Unsafe module path: {value!r}")
    if suffix and not raw.lower().endswith(suffix.lower()):
        raise ModuleValidationError(f"Expected {suffix} file: {value!r}")
    return raw


def _version_key(value: str):
    """Small dependency-free SemVer-ish ordering with dev/alpha/beta/rc support."""
    raw = (value or "0").strip().lower()
    main, sep, pre = raw.partition("-")
    nums = []
    for part in main.split(".")[:4]:
        m = re.match(r"(\d+)", part)
        nums.append(int(m.group(1)) if m else 0)
    while len(nums) < 4:
        nums.append(0)
    if not sep:
        rank, pre_num = 100, 0
    else:
        m = re.match(r"([a-z]+)[.-]?(\d*)", pre)
        tag = m.group(1) if m else pre
        pre_num = int(m.group(2) or 0) if m else 0
        rank = {"dev": 10, "alpha": 20, "a": 20, "beta": 30, "b": 30, "rc": 40}.get(tag, 15)
    return (*nums, rank, pre_num, raw)


def _satisfies(version: str, constraint: str) -> bool:
    constraint = (constraint or "").strip()
    if not constraint or constraint == "*":
        return True
    actual = _version_key(version)
    for token in [x.strip() for x in constraint.split(",") if x.strip()]:
        op = "=="
        target = token
        for candidate in (">=", "<=", "!=", "==", ">", "<", "="):
            if token.startswith(candidate):
                op = "==" if candidate == "=" else candidate
                target = token[len(candidate):].strip()
                break
        expected = _version_key(target)
        if op == ">=" and not (actual >= expected):
            return False
        if op == "<=" and not (actual <= expected):
            return False
        if op == ">" and not (actual > expected):
            return False
        if op == "<" and not (actual < expected):
            return False
        if op == "==" and not (actual == expected):
            return False
        if op == "!=" and not (actual != expected):
            return False
    return True


def _normalize_manifest(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ModuleValidationError("manifest.json must contain a JSON object")

    schema = int(raw.get("schema_version") or 1)
    if schema != 1:
        raise ModuleValidationError(f"Unsupported manifest schema_version {schema}")

    api = int(raw.get("module_api") or 1)
    if api != MODULE_API_VERSION:
        raise ModuleValidationError(f"Module requires unsupported Module API v{api}")

    slug = str(raw.get("id") or "").strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise ModuleValidationError("Module id must match: [a-z][a-z0-9-]{1,63}")

    name = str(raw.get("name") or "").strip()
    version = str(raw.get("version") or "").strip()
    if not name or len(name) > 120:
        raise ModuleValidationError("Module name is required and must be <= 120 characters")
    if not version or len(version) > 64:
        raise ModuleValidationError("Module version is required")

    entrypoint = _safe_relpath(str(raw.get("entrypoint") or "module.py"), suffix=".py")
    rv_constraint = str(raw.get("router_vault") or ">=3.0.0-dev,<4.0.0").strip()

    permissions = raw.get("permissions") or []
    if not isinstance(permissions, list) or any(str(p) not in ALLOWED_PERMISSIONS for p in permissions):
        unknown = [str(p) for p in permissions if str(p) not in ALLOWED_PERMISSIONS] if isinstance(permissions, list) else []
        raise ModuleValidationError(f"Invalid/unknown module permissions: {unknown}")
    permissions = list(dict.fromkeys(str(p) for p in permissions))

    access = raw.get("access") or ["admin"]
    if not isinstance(access, list) or not access:
        raise ModuleValidationError("access must be a non-empty list")
    access = list(dict.fromkeys(str(r) for r in access))
    if any(r not in ALLOWED_ACCESS_ROLES for r in access):
        raise ModuleValidationError(f"Invalid access role(s): {access}")

    dependencies = raw.get("dependencies") or {}
    if isinstance(dependencies, dict) and "modules" in dependencies:
        dependencies = dependencies.get("modules") or {}
    if not isinstance(dependencies, dict):
        raise ModuleValidationError("dependencies must be an object")
    normalized_dependencies = {}
    for dep, spec in dependencies.items():
        dep = str(dep).strip().lower()
        if not SLUG_RE.fullmatch(dep):
            raise ModuleValidationError(f"Invalid dependency id: {dep}")
        normalized_dependencies[dep] = str(spec or "*").strip()

    routes = raw.get("routes") or []
    if not isinstance(routes, list):
        raise ModuleValidationError("routes must be a list")
    normalized_routes = []
    seen_routes = set()
    for item in routes:
        if not isinstance(item, dict):
            raise ModuleValidationError("Each route must be an object")
        method = str(item.get("method") or "GET").upper().strip()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ModuleValidationError(f"Unsupported module route method: {method}")
        path = str(item.get("path") or "/").strip()
        if not path.startswith("/") or ".." in PurePosixPath(path).parts or "//" in path:
            raise ModuleValidationError(f"Invalid module route path: {path}")
        handler = str(item.get("handler") or "").strip()
        if not HANDLER_RE.fullmatch(handler):
            raise ModuleValidationError(f"Invalid route handler: {handler}")
        key = (method, path.rstrip("/") or "/")
        if key in seen_routes:
            raise ModuleValidationError(f"Duplicate module route: {method} {path}")
        seen_routes.add(key)
        normalized_routes.append({"method": method, "path": key[1], "handler": handler})

    hooks = raw.get("hooks") or {}
    if not isinstance(hooks, dict):
        raise ModuleValidationError("hooks must be an object")
    normalized_hooks = {}
    for hook_name, handler in hooks.items():
        hook_name = str(hook_name).strip()
        handler = str(handler).strip()
        if not re.fullmatch(r"[a-z0-9_.:-]{2,100}", hook_name):
            raise ModuleValidationError(f"Invalid hook name: {hook_name}")
        if not HANDLER_RE.fullmatch(handler):
            raise ModuleValidationError(f"Invalid hook handler: {handler}")
        normalized_hooks[hook_name] = handler

    migrations = raw.get("migrations") or []
    if not isinstance(migrations, list):
        raise ModuleValidationError("migrations must be a list")
    normalized_migrations = []
    migration_ids = set()
    for item in migrations:
        if not isinstance(item, dict):
            raise ModuleValidationError("Each migration must be an object")
        migration_id = str(item.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", migration_id):
            raise ModuleValidationError(f"Invalid migration id: {migration_id}")
        if migration_id in migration_ids:
            raise ModuleValidationError(f"Duplicate migration id: {migration_id}")
        migration_ids.add(migration_id)
        up = _safe_relpath(str(item.get("up") or ""), suffix=".sql")
        down_raw = str(item.get("down") or "").strip()
        down = _safe_relpath(down_raw, suffix=".sql") if down_raw else ""
        normalized_migrations.append({"id": migration_id, "up": up, "down": down})

    navigation = raw.get("navigation") or {}
    if not isinstance(navigation, dict):
        raise ModuleValidationError("navigation must be an object")
    nav_label = str(navigation.get("label") or name).strip()[:80]
    try:
        nav_order = int(navigation.get("order") or 100)
    except Exception:
        nav_order = 100
    navigation = {
        "show": bool(navigation.get("show", True)),
        "label": nav_label or name,
        "order": max(-10000, min(nav_order, 10000)),
    }

    checksums = raw.get("checksums") or {}
    if not isinstance(checksums, dict):
        raise ModuleValidationError("checksums must be an object")
    normalized_checksums = {}
    for rel, digest in checksums.items():
        rel = _safe_relpath(str(rel))
        digest = str(digest).lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ModuleValidationError(f"Invalid SHA256 for {rel}")
        normalized_checksums[rel] = digest

    return {
        "schema_version": schema,
        "module_api": api,
        "id": slug,
        "name": name,
        "version": version,
        "description": str(raw.get("description") or "").strip()[:1000],
        "author": str(raw.get("author") or "").strip()[:200],
        "router_vault": rv_constraint,
        "entrypoint": entrypoint,
        "permissions": permissions,
        "access": access,
        "dependencies": {"modules": normalized_dependencies},
        "navigation": navigation,
        "routes": normalized_routes,
        "hooks": normalized_hooks,
        "migrations": normalized_migrations,
        "checksums": normalized_checksums,
    }


def _validate_zip_member(info: zipfile.ZipInfo):
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or name.startswith("/"):
        raise ModuleValidationError(f"Unsafe archive entry: {info.filename}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise ModuleValidationError(f"Symlinks are not allowed in module packages: {info.filename}")


def inspect_module_package(package_path: str | Path, *, extract_to: str | Path | None = None) -> dict:
    path = Path(package_path)
    if not path.is_file():
        raise ModuleValidationError("Module package not found")
    if path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ModuleValidationError(f"Module package exceeds {MAX_PACKAGE_BYTES // (1024*1024)} MB")

    package_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with zipfile.ZipFile(path, "r") as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_PACKAGE_FILES:
            raise ModuleValidationError(f"Module package contains more than {MAX_PACKAGE_FILES} files")
        total = 0
        names = set()
        for info in infos:
            _validate_zip_member(info)
            total += int(info.file_size or 0)
            if total > MAX_UNPACKED_BYTES:
                raise ModuleValidationError(f"Expanded module exceeds {MAX_UNPACKED_BYTES // (1024*1024)} MB")
            names.add(info.filename.replace("\\", "/"))

        if "manifest.json" not in names:
            raise ModuleValidationError("manifest.json must exist at the root of the .rvm package")
        try:
            raw_manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise ModuleValidationError(f"Invalid manifest.json: {exc}") from exc
        manifest = _normalize_manifest(raw_manifest)

        required_paths = {manifest["entrypoint"]}
        required_paths.update(m["up"] for m in manifest["migrations"])
        required_paths.update(m["down"] for m in manifest["migrations"] if m["down"])
        missing = sorted(p for p in required_paths if p not in names)
        if missing:
            raise ModuleValidationError("Module package is missing: " + ", ".join(missing))

        for rel, expected in manifest["checksums"].items():
            if rel not in names:
                raise ModuleValidationError(f"Checksummed file missing: {rel}")
            actual = hashlib.sha256(zf.read(rel)).hexdigest()
            if actual != expected:
                raise ModuleValidationError(f"Checksum mismatch: {rel}")

        # Syntax-check the entrypoint without executing the module.
        try:
            ast.parse(zf.read(manifest["entrypoint"]).decode("utf-8"), filename=manifest["entrypoint"])
        except Exception as exc:
            raise ModuleValidationError(f"Entrypoint Python syntax error: {exc}") from exc

        if not _satisfies(current_app_version(), manifest["router_vault"]):
            raise ModuleValidationError(
                f"Module requires Router Vault {manifest['router_vault']}; current version is {current_app_version()}"
            )

        if extract_to is not None:
            destination = Path(extract_to)
            destination.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                _validate_zip_member(info)
                target = destination / info.filename
                resolved = target.resolve()
                if destination.resolve() not in resolved.parents and resolved != destination.resolve():
                    raise ModuleValidationError(f"Archive path escaped destination: {info.filename}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)

    return {
        "manifest": manifest,
        "package_sha256": package_sha,
        "package_bytes": path.stat().st_size,
        "unpacked_bytes": total,
        "file_count": len(infos),
    }


def _module_dir(slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ModuleValidationError("Invalid module id")
    return MODULES_ROOT / slug


def _load_entrypoint_from_dir(module_dir: Path, manifest: dict):
    entrypoint = module_dir / manifest["entrypoint"]
    if not entrypoint.is_file():
        raise ModuleStateError(f"Module entrypoint missing: {entrypoint.name}")
    stat = entrypoint.stat()
    key = (manifest["id"], manifest["version"], stat.st_mtime_ns, stat.st_size)
    with _import_lock:
        if key in _import_cache:
            return _import_cache[key]
        # Clear older imports for this slug.
        for old in [k for k in _import_cache if k[0] == manifest["id"]]:
            _import_cache.pop(old, None)
        module_name = f"routervault_module_{manifest['id'].replace('-', '_')}_{hashlib.sha1(str(key).encode()).hexdigest()[:10]}"
        spec = importlib.util.spec_from_file_location(module_name, entrypoint)
        if not spec or not spec.loader:
            raise ModuleStateError("Could not load module entrypoint")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _import_cache[key] = module
        return module


def _load_installed_entrypoint(slug: str, manifest: dict | None = None):
    if manifest is None:
        record = get_module(slug)
        if not record:
            raise ModuleStateError("Module is not installed")
        manifest = record["manifest"]
    return _load_entrypoint_from_dir(_module_dir(slug), manifest)


def _check_dependencies(manifest: dict, db, *, require_enabled: bool = True):
    for dep_slug, constraint in manifest.get("dependencies", {}).get("modules", {}).items():
        row = db.execute(text("SELECT version,enabled,status FROM rv_modules WHERE slug=:s"), {"s": dep_slug}).mappings().first()
        if not row:
            raise ModuleDependencyError(f"Required module '{dep_slug}' is not installed")
        if require_enabled and not row["enabled"]:
            raise ModuleDependencyError(f"Required module '{dep_slug}' is disabled")
        if not _satisfies(str(row["version"]), constraint):
            raise ModuleDependencyError(
                f"Module '{dep_slug}' version {row['version']} does not satisfy {constraint}"
            )


def dependent_modules(slug: str, *, enabled_only: bool = False) -> list[str]:
    result = []
    for module in list_modules():
        if module["slug"] == slug:
            continue
        if enabled_only and not module["enabled"]:
            continue
        if slug in module.get("manifest", {}).get("dependencies", {}).get("modules", {}):
            result.append(module["slug"])
    return sorted(result)


def _migration_map(manifest: dict):
    return {m["id"]: m for m in manifest.get("migrations", [])}


def _sql_file(root: Path, rel: str) -> str:
    path = root / rel
    resolved = path.resolve()
    if root.resolve() not in resolved.parents:
        raise ModuleValidationError(f"Migration escaped module directory: {rel}")
    return path.read_text(encoding="utf-8")


def _apply_pending_migrations(db, root: Path, manifest: dict):
    slug = manifest["id"]
    existing = {
        r["migration_id"]: dict(r)
        for r in db.execute(text("SELECT * FROM rv_module_migrations WHERE module_slug=:s"), {"s": slug}).mappings().all()
    }
    for migration in manifest.get("migrations", []):
        mid = migration["id"]
        sql = _sql_file(root, migration["up"])
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if mid in existing:
            if existing[mid]["checksum"] != checksum:
                raise ModuleValidationError(
                    f"Migration {mid} was already applied with a different checksum. Create a new migration id."
                )
            continue
        db.execute(text(sql))
        db.execute(text("""
            INSERT INTO rv_module_migrations(module_slug,migration_id,module_version,checksum,down_path,applied_at)
            VALUES (:s,:m,:v,:c,:d,NOW())
        """), {
            "s": slug,
            "m": mid,
            "v": manifest["version"],
            "c": checksum,
            "d": migration.get("down") or "",
        })


def _rollback_removed_migrations(db, current_root: Path, current_manifest: dict, target_manifest: dict):
    slug = current_manifest["id"]
    current_map = _migration_map(current_manifest)
    target_ids = set(_migration_map(target_manifest))
    applied = db.execute(text("""
        SELECT migration_id FROM rv_module_migrations
        WHERE module_slug=:s
        ORDER BY applied_at DESC
    """), {"s": slug}).mappings().all()
    for row in applied:
        mid = row["migration_id"]
        if mid in target_ids:
            continue
        migration = current_map.get(mid)
        if not migration or not migration.get("down"):
            raise ModuleStateError(
                f"Cannot rollback migration {mid}: current module package has no down migration"
            )
        sql = _sql_file(current_root, migration["down"])
        db.execute(text(sql))
        db.execute(text("DELETE FROM rv_module_migrations WHERE module_slug=:s AND migration_id=:m"), {"s": slug, "m": mid})


def _archive_dir(module_dir: Path, slug: str, version: str) -> Path:
    HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    target_dir = HISTORY_ROOT / slug
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().strftime("%Y%m%d-%H%M%S-%f")
    archive = target_dir / f"{stamp}__{version}.rvm"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(module_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(module_dir).as_posix())
    return archive


def history_versions(slug: str) -> list[dict]:
    if not SLUG_RE.fullmatch(slug or ""):
        return []
    root = HISTORY_ROOT / slug
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*.rvm"), key=lambda p: p.stat().st_mtime, reverse=True):
        version = "unknown"
        try:
            with zipfile.ZipFile(path, "r") as zf:
                manifest = _normalize_manifest(json.loads(zf.read("manifest.json").decode("utf-8")))
                version = manifest["version"]
        except Exception:
            pass
        items.append({
            "filename": path.name,
            "version": version,
            "size_bytes": path.stat().st_size,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        })
    return items


@dataclass
class ModuleContext:
    slug: str
    manifest: dict
    db: Any
    request: Request | None = None
    user: str = "system"
    role: str = "admin"
    root_override: Path | None = None

    @property
    def module_dir(self) -> Path:
        return self.root_override or _module_dir(self.slug)

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute(text("""
            SELECT setting_value FROM rv_module_settings
            WHERE module_slug=:s AND setting_key=:k
        """), {"s": self.slug, "k": str(key)}).mappings().first()
        return str(row["setting_value"]) if row else default

    def set_setting(self, key: str, value: Any):
        self.db.execute(text("""
            INSERT INTO rv_module_settings(module_slug,setting_key,setting_value,updated_at)
            VALUES (:s,:k,:v,NOW())
            ON CONFLICT(module_slug,setting_key)
            DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=NOW()
        """), {"s": self.slug, "k": str(key), "v": str(value)})

    def core_setting(self, key: str, default: str = "") -> str:
        if "settings.read" not in self.manifest.get("permissions", []):
            raise ModuleStateError("Module did not declare settings.read permission")
        return core_get_setting(key, default)

    def set_core_setting(self, key: str, value: Any):
        if "settings.write" not in self.manifest.get("permissions", []):
            raise ModuleStateError("Module did not declare settings.write permission")
        core_set_setting(key, str(value))

    def log(self, event_type: str, message: str = "", level: str = "info"):
        _record_event(self.slug, event_type, message, level, db=self.db)

    def url(self, path: str = "/") -> str:
        suffix = "/" + str(path or "").lstrip("/")
        if suffix == "/":
            return f"/module/{self.slug}"
        return f"/module/{self.slug}{suffix}"

    def render(self, template_name: str, context: dict | None = None, status_code: int = 200) -> HTMLResponse:
        if self.request is None:
            raise ModuleStateError("render() requires a web request")
        if _core_templates is None:
            raise ModuleStateError("Core templates are not configured")
        template_name = _safe_relpath(template_name)
        module_templates = self.module_dir / "templates"
        env = _core_templates.env.overlay()
        env.loader = ChoiceLoader([
            FileSystemLoader(str(module_templates)),
            _core_templates.env.loader,
        ])
        template = env.get_template(template_name)
        data = dict(context or {})
        data.setdefault("request", self.request)
        data.setdefault("module", self.manifest)
        data.setdefault("module_context", self)
        data.setdefault("app_name", "Router Vault")
        return HTMLResponse(template.render(**data), status_code=status_code)

    def redirect(self, path: str = "/", status_code: int = 303) -> RedirectResponse:
        return RedirectResponse(self.url(path), status_code=status_code)


def _invoke_lifecycle(root: Path, manifest: dict, hook: str, db, *args):
    module = _load_entrypoint_from_dir(root, manifest)
    handler = getattr(module, hook, None)
    if handler is None:
        return None
    if not callable(handler):
        raise ModuleValidationError(f"Lifecycle handler {hook} is not callable")
    ctx = ModuleContext(manifest["id"], manifest, db, user="system", role="admin", root_override=root)
    result = handler(ctx, *args)
    if pyinspect.isawaitable(result):
        raise ModuleValidationError(f"Lifecycle handler {hook} must be synchronous")
    return result


def install_module_package(package_path: str | Path, actor: str = "admin") -> dict:
    ensure_module_tables()
    temp_dir = Path(tempfile.mkdtemp(prefix="module-install-", dir=TMP_ROOT))
    replaced_live = False
    old_archive = None
    live_dir = None
    old_record = None
    try:
        info = inspect_module_package(package_path, extract_to=temp_dir)
        manifest = info["manifest"]
        slug = manifest["id"]
        live_dir = _module_dir(slug)

        with SessionLocal() as db:
            old_row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
            old_record = _row_to_dict(old_row)
            if old_record:
                if manifest["version"] == old_record["version"]:
                    if info["package_sha256"] == old_record["package_sha256"]:
                        return old_record
                    raise ModuleStateError(
                        f"Module {slug} {manifest['version']} is already installed with different package content. Bump the version."
                    )
                if _version_key(manifest["version"]) < _version_key(old_record["version"]):
                    raise ModuleStateError(
                        f"Refusing downgrade {old_record['version']} -> {manifest['version']}. Use Rollback instead."
                    )

            _check_dependencies(manifest, db, require_enabled=False)
            _apply_pending_migrations(db, temp_dir, manifest)

            if old_record:
                _invoke_lifecycle(temp_dir, manifest, "on_upgrade", db, old_record["version"], manifest["version"])
            else:
                _invoke_lifecycle(temp_dir, manifest, "on_install", db)

            if live_dir.exists():
                old_archive = _archive_dir(live_dir, slug, old_record["version"] if old_record else "unknown")
                shutil.rmtree(live_dir)
            os.replace(temp_dir, live_dir)
            replaced_live = True

            enabled = bool(old_record["enabled"]) if old_record else False
            installed_at = old_record["installed_at"] if old_record else _utcnow()
            db.execute(text("""
                INSERT INTO rv_modules(
                    slug,name,version,description,author,enabled,status,manifest_json,
                    package_sha256,installed_at,updated_at,last_error
                ) VALUES (
                    :slug,:name,:version,:description,:author,:enabled,:status,:manifest_json,
                    :package_sha256,:installed_at,NOW(),''
                )
                ON CONFLICT(slug) DO UPDATE SET
                    name=EXCLUDED.name,
                    version=EXCLUDED.version,
                    description=EXCLUDED.description,
                    author=EXCLUDED.author,
                    enabled=EXCLUDED.enabled,
                    status=EXCLUDED.status,
                    manifest_json=EXCLUDED.manifest_json,
                    package_sha256=EXCLUDED.package_sha256,
                    updated_at=NOW(),
                    last_error=''
            """), {
                "slug": slug,
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "author": manifest["author"],
                "enabled": enabled,
                "status": "enabled" if enabled else "installed",
                "manifest_json": json.dumps(manifest, ensure_ascii=False),
                "package_sha256": info["package_sha256"],
                "installed_at": installed_at,
            })
            _record_event(
                slug,
                "updated" if old_record else "installed",
                f"actor={actor}; version={manifest['version']}; sha256={info['package_sha256']}",
                db=db,
            )
            db.commit()

        with _import_lock:
            for key in [k for k in _import_cache if k[0] == slug]:
                _import_cache.pop(key, None)
        return get_module(slug)
    except Exception:
        # Restore previous module code when an update had already swapped it.
        if replaced_live and live_dir is not None:
            try:
                shutil.rmtree(live_dir, ignore_errors=True)
                if old_archive and Path(old_archive).exists():
                    restore_tmp = Path(tempfile.mkdtemp(prefix="module-restore-", dir=TMP_ROOT))
                    with zipfile.ZipFile(old_archive, "r") as zf:
                        zf.extractall(restore_tmp)
                    os.replace(restore_tmp, live_dir)
            except Exception:
                pass
        raise
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def enable_module(slug: str, actor: str = "admin") -> dict:
    ensure_module_tables()
    with SessionLocal() as db:
        row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
        record = _row_to_dict(row)
        if not record:
            raise ModuleStateError("Module is not installed")
        if record["enabled"]:
            return record
        manifest = record["manifest"]
        _check_dependencies(manifest, db, require_enabled=True)
        root = _module_dir(slug)
        if not root.is_dir():
            raise ModuleStateError("Module code directory is missing")
        _invoke_lifecycle(root, manifest, "on_enable", db)
        db.execute(text("UPDATE rv_modules SET enabled=TRUE,status='enabled',last_error='',updated_at=NOW() WHERE slug=:s"), {"s": slug})
        _record_event(slug, "enabled", f"actor={actor}", db=db)
        db.commit()
    return get_module(slug)


def disable_module(slug: str, actor: str = "admin") -> dict:
    ensure_module_tables()
    deps = dependent_modules(slug, enabled_only=True)
    if deps:
        raise ModuleDependencyError("Disable dependent module(s) first: " + ", ".join(deps))
    with SessionLocal() as db:
        row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
        record = _row_to_dict(row)
        if not record:
            raise ModuleStateError("Module is not installed")
        if not record["enabled"]:
            return record
        root = _module_dir(slug)
        if root.is_dir():
            _invoke_lifecycle(root, record["manifest"], "on_disable", db)
        db.execute(text("UPDATE rv_modules SET enabled=FALSE,status='disabled',updated_at=NOW() WHERE slug=:s"), {"s": slug})
        _record_event(slug, "disabled", f"actor={actor}", db=db)
        db.commit()
    return get_module(slug)


def uninstall_module(slug: str, actor: str = "admin", *, purge_data: bool = False) -> None:
    ensure_module_tables()
    deps = dependent_modules(slug, enabled_only=False)
    if deps:
        raise ModuleDependencyError("Uninstall dependent module(s) first: " + ", ".join(deps))

    live_dir = _module_dir(slug)
    with SessionLocal() as db:
        row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
        record = _row_to_dict(row)
        if not record:
            raise ModuleStateError("Module is not installed")
        if record["enabled"]:
            raise ModuleStateError("Disable the module before uninstalling it")

        manifest = record["manifest"]
        if live_dir.is_dir():
            _invoke_lifecycle(live_dir, manifest, "on_uninstall", db, purge_data)

        if purge_data:
            migration_map = _migration_map(manifest)
            applied = db.execute(text("""
                SELECT migration_id FROM rv_module_migrations
                WHERE module_slug=:s ORDER BY applied_at DESC
            """), {"s": slug}).mappings().all()
            for row in applied:
                mid = row["migration_id"]
                mig = migration_map.get(mid)
                if not mig or not mig.get("down"):
                    raise ModuleStateError(f"Cannot purge: migration {mid} has no down migration")
                db.execute(text(_sql_file(live_dir, mig["down"])))
                db.execute(text("DELETE FROM rv_module_migrations WHERE module_slug=:s AND migration_id=:m"), {"s": slug, "m": mid})
            db.execute(text("DELETE FROM rv_module_settings WHERE module_slug=:s"), {"s": slug})

        if live_dir.is_dir():
            _archive_dir(live_dir, slug, record["version"])
        _record_event(slug, "uninstalled", f"actor={actor}; purge_data={purge_data}", db=db)
        db.execute(text("DELETE FROM rv_modules WHERE slug=:s"), {"s": slug})
        db.commit()

    shutil.rmtree(live_dir, ignore_errors=True)
    with _import_lock:
        for key in [k for k in _import_cache if k[0] == slug]:
            _import_cache.pop(key, None)


def rollback_module(slug: str, actor: str = "admin") -> dict:
    ensure_module_tables()
    history = history_versions(slug)
    if not history:
        raise ModuleStateError("No previous module version is available")

    with SessionLocal() as db:
        row = db.execute(text("SELECT * FROM rv_modules WHERE slug=:s"), {"s": slug}).mappings().first()
        current = _row_to_dict(row)
        if not current:
            raise ModuleStateError("Module is not installed")
        if current["enabled"]:
            raise ModuleStateError("Disable the module before rollback")

        target_archive = HISTORY_ROOT / slug / history[0]["filename"]
        temp_dir = Path(tempfile.mkdtemp(prefix="module-rollback-", dir=TMP_ROOT))
        try:
            info = inspect_module_package(target_archive, extract_to=temp_dir)
            target_manifest = info["manifest"]
            if target_manifest["id"] != slug:
                raise ModuleValidationError("Rollback archive module id mismatch")
            _check_dependencies(target_manifest, db, require_enabled=False)

            current_root = _module_dir(slug)
            _rollback_removed_migrations(db, current_root, current["manifest"], target_manifest)
            _archive_dir(current_root, slug, current["version"])
            shutil.rmtree(current_root)
            os.replace(temp_dir, current_root)

            db.execute(text("""
                UPDATE rv_modules SET
                    name=:name,version=:version,description=:description,author=:author,
                    manifest_json=:manifest,package_sha256=:sha,status='disabled',
                    last_error='',updated_at=NOW()
                WHERE slug=:slug
            """), {
                "name": target_manifest["name"],
                "version": target_manifest["version"],
                "description": target_manifest["description"],
                "author": target_manifest["author"],
                "manifest": json.dumps(target_manifest, ensure_ascii=False),
                "sha": info["package_sha256"],
                "slug": slug,
            })
            _record_event(slug, "rollback", f"actor={actor}; {current['version']} -> {target_manifest['version']}", db=db)
            db.commit()
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    with _import_lock:
        for key in [k for k in _import_cache if k[0] == slug]:
            _import_cache.pop(key, None)
    return get_module(slug)


def module_nav_items(role: str = "admin") -> list[dict]:
    items = []
    for record in list_modules():
        if not record["enabled"]:
            continue
        manifest = record["manifest"]
        if role not in manifest.get("access", ["admin"]):
            continue
        nav = manifest.get("navigation", {})
        if not nav.get("show", True):
            continue
        items.append({
            "slug": record["slug"],
            "name": record["name"],
            "label": nav.get("label") or record["name"],
            "order": int(nav.get("order") or 100),
            "href": f"/module/{record['slug']}",
        })
    return sorted(items, key=lambda x: (x["order"], x["label"].lower()))


def module_stats() -> dict:
    modules = list_modules()
    return {
        "installed": len(modules),
        "enabled": sum(1 for m in modules if m["enabled"]),
        "disabled": sum(1 for m in modules if not m["enabled"]),
        "errors": sum(1 for m in modules if m.get("last_error")),
    }


def _route_match(manifest: dict, method: str, module_path: str):
    normalized_path = "/" + (module_path or "").strip("/")
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    method = method.upper()
    for route in manifest.get("routes", []):
        if route["method"] == method and route["path"] == normalized_path:
            return route
    return None


async def dispatch_module_request(slug: str, module_path: str, request: Request, db, user: str, role: str):
    record = get_module(slug)
    if not record or not record["enabled"]:
        raise HTTPException(404, "Module not found or disabled")
    manifest = record["manifest"]
    if role not in manifest.get("access", ["admin"]):
        raise HTTPException(403, "Module access denied")
    route = _route_match(manifest, request.method, module_path)
    if not route:
        raise HTTPException(404, "Module route not found")

    try:
        module = _load_installed_entrypoint(slug, manifest)
        handler = getattr(module, route["handler"], None)
        if not callable(handler):
            raise ModuleStateError(f"Handler {route['handler']} is missing")
        ctx = ModuleContext(slug, manifest, db, request=request, user=user, role=role)
        result = handler(ctx, request)
        if pyinspect.isawaitable(result):
            result = await result
        db.commit()
        if isinstance(result, Response):
            return result
        if isinstance(result, dict):
            return ctx.render("index.html", result)
        if result is None:
            return Response(status_code=204)
        return HTMLResponse(str(result))
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        try:
            with SessionLocal() as log_db:
                log_db.execute(text("UPDATE rv_modules SET last_error=:e,status='error',updated_at=NOW() WHERE slug=:s"), {"e": str(exc)[:2000], "s": slug})
                _record_event(slug, "route_error", f"{request.method} /{module_path}: {exc}", "error", db=log_db)
                log_db.commit()
        except Exception:
            pass
        raise HTTPException(500, "Module execution failed") from exc


def module_static_file(slug: str, asset_path: str) -> Path:
    record = get_module(slug)
    if not record or not record["enabled"]:
        raise HTTPException(404, "Module not found or disabled")
    root = (_module_dir(slug) / "static").resolve()
    candidate = (root / asset_path).resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise HTTPException(404, "Module asset not found")
    return candidate


def emit_hook(hook_name: str, payload: dict | None = None):
    """Emit an event to enabled modules. Hook failures never break core work."""
    payload = dict(payload or {})
    for record in list_modules():
        if not record["enabled"]:
            continue
        handler_name = record["manifest"].get("hooks", {}).get(hook_name)
        if not handler_name:
            continue
        slug = record["slug"]
        try:
            module = _load_installed_entrypoint(slug, record["manifest"])
            handler = getattr(module, handler_name, None)
            if not callable(handler):
                raise ModuleStateError(f"Hook handler {handler_name} is missing")
            with SessionLocal() as db:
                ctx = ModuleContext(slug, record["manifest"], db, user="system", role="admin")
                result = handler(ctx, payload)
                if pyinspect.isawaitable(result):
                    raise ModuleStateError("Event hooks must be synchronous")
                db.commit()
                _record_event(slug, "hook", f"{hook_name}", db=db)
                db.commit()
        except Exception as exc:
            try:
                with SessionLocal() as db:
                    db.execute(text("UPDATE rv_modules SET last_error=:e,updated_at=NOW() WHERE slug=:s"), {"e": f"hook {hook_name}: {exc}"[:2000], "s": slug})
                    _record_event(slug, "hook_error", f"{hook_name}: {exc}", "error", db=db)
                    db.commit()
            except Exception:
                pass


def clear_module_error(slug: str):
    with SessionLocal() as db:
        db.execute(text("UPDATE rv_modules SET last_error='',status=CASE WHEN enabled THEN 'enabled' ELSE 'disabled' END,updated_at=NOW() WHERE slug=:s"), {"s": slug})
        db.commit()


def admin_redirect(message: str = "", *, error: str = "", slug: str = ""):
    query = []
    if message:
        query.append("message=" + quote_plus(message[:180]))
    if error:
        query.append("error=" + quote_plus(error[:180]))
    suffix = ("?" + "&".join(query)) if query else ""
    anchor = f"#{slug}" if slug else ""
    return RedirectResponse("/administration/modules" + suffix + anchor, status_code=303)
