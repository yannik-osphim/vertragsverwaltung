from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import database
from .security import create_session_token, read_session_token


SNAPSHOT_NAME = re.compile(r"snapshot-\d{8}T\d{6}Z-[a-zA-Z0-9]{6}")
FILES = {"database.dump": "Datenbank", "uploads.tar.gz": "Dokumente und Logos", "SHA256SUMS": "Pruefsummen", "manifest.txt": "Metadaten"}
DEFAULTS = {"enabled": 1, "interval_seconds": 86400, "retention_days": 14, "retry_seconds": 300, "timeout_seconds": 3600}
STATE_LABELS = {"idle": "Bereit", "running": "Sicherung laeuft", "failed": "Letzte Sicherung fehlgeschlagen"}
REQUEST_LABELS = {"queued": "Angefordert", "running": "In Bearbeitung", "success": "Abgeschlossen", "failed": "Fehlgeschlagen"}


def managed() -> bool:
    return os.environ.get("BACKUP_MANAGEMENT_ENABLED", "0") == "1"


def backup_dir() -> Path:
    return Path(os.environ.get("BACKUP_DIR", "/backups"))


def read_values(connection) -> dict:
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM app_settings WHERE key LIKE 'backup_%'").fetchall()}


def settings_from(values: dict) -> dict:
    result = {}
    for key, default in DEFAULTS.items():
        raw = values.get("backup_" + key, os.environ.get("BACKUP_" + key.upper(), default))
        try:
            value = int(raw)
            if (key == "enabled" and value not in (0, 1)) or (key != "enabled" and not 1 <= value <= 99999999):
                raise ValueError
            result[key] = value
        except (TypeError, ValueError):
            result[key] = default
    return result


def json_value(values: dict, key: str) -> dict:
    try:
        value = json.loads(values.get(key, "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def set_value(connection, key: str, value: str) -> None:
    connection.execute("""INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, datetime.now(timezone.utc).isoformat()))


def time_label(timestamp) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp), timezone.utc).astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return "-"


def size_label(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024


def snapshot_path(name: str) -> Path:
    root = backup_dir().resolve()
    path = root / name
    if not SNAPSHOT_NAME.fullmatch(name) or path.is_symlink() or not path.is_dir():
        raise ValueError("Ungueltige Sicherung.")
    for filename in (*FILES, "completed-at"):
        candidate = path / filename
        if candidate.is_symlink() or not candidate.is_file() or candidate.resolve().parent != path:
            raise ValueError("Unvollstaendige Sicherung.")
    return path


def snapshots() -> tuple[list, str | None]:
    items = []
    try:
        for directory in backup_dir().iterdir():
            if not SNAPSHOT_NAME.fullmatch(directory.name):
                continue
            try:
                path = snapshot_path(directory.name)
                completed = int((path / "completed-at").read_text().strip())
                size = sum((path / filename).stat().st_size for filename in FILES)
                items.append({"name": path.name, "completed": completed, "created": time_label(completed), "bytes": size, "size": size_label(size)})
            except (ValueError, OSError):
                continue
    except FileNotFoundError:
        return [], "Backup-Speicher ist nicht eingebunden."
    except OSError:
        return [], "Backup-Speicher ist nicht lesbar."
    return sorted(items, key=lambda item: item["completed"], reverse=True), None


def status_context(values: dict) -> dict:
    worker = json_value(values, "backup_worker")
    request = json_value(values, "backup_request")
    try:
        online = managed() and 0 <= time.time() - float(worker.get("heartbeat", 0)) < 120
    except (ValueError, TypeError):
        online = False
    return {"worker": worker, "backup_request": request, "worker_online": online,
            "worker_label": STATE_LABELS.get(worker.get("status"), "Bereit") if online else "Nicht erreichbar",
            "request_label": REQUEST_LABELS.get(request.get("status"), "-"),
            "heartbeat_label": time_label(worker.get("heartbeat")),
            "busy": worker.get("status") == "running" and online or request.get("status") in ("queued", "running")}


def page_context(user: dict) -> dict:
    with database.connect() as connection:
        values = read_values(connection)
    config = settings_from(values)
    items, storage_error = snapshots() if managed() else ([], "Backup-Verwaltung ist fuer diese Installation nicht aktiviert.")
    state = status_context(values)
    next_due = items[0]["completed"] + config["interval_seconds"] if items else None
    return {**state, "backup_config": config, "snapshots": items, "storage_error": storage_error, "managed": managed(),
            "latest": items[0]["created"] if items else "Noch keine Sicherung",
            "next_due": (time_label(next_due) if next_due and next_due > time.time() else "Faellig") if config["enabled"] else "Pausiert",
            "total_size": size_label(sum(item["bytes"] for item in items)), "download_files": FILES,
            "form": {"enabled": config["enabled"], "interval_hours": config["interval_seconds"] / 3600,
                     "retention_days": config["retention_days"], "retry_minutes": config["retry_seconds"] / 60,
                     "timeout_minutes": config["timeout_seconds"] / 60},
            "csrf_token": create_session_token({"purpose": "backup-management", "user_id": user["id"]})}


def validate_csrf(token: str, user: dict) -> None:
    payload = read_session_token(token)
    if not payload or payload.get("purpose") != "backup-management" or payload.get("user_id") != user["id"]:
        raise HTTPException(403, "Formular abgelaufen. Bitte Seite neu laden.")


def create_router(render, require_superadmin) -> APIRouter:
    router = APIRouter()

    @router.get("/backups")
    def index(request: Request, user: dict = Depends(require_superadmin)):
        return render(request, "backups.html", {**page_context(user), "saved": request.query_params.get("saved") == "1"})

    @router.get("/backups/status")
    def status(user: dict = Depends(require_superadmin)):
        with database.connect() as connection:
            state = status_context(read_values(connection))
        return JSONResponse({"worker": state["worker"], "request": state["backup_request"], "online": state["worker_online"]}, headers={"Cache-Control": "no-store"})

    @router.post("/backups/settings")
    def update(request: Request, csrf_token: str = Form(""), enabled: str = Form("0"), interval_hours: str = Form(""),
               retention_days: str = Form(""), retry_minutes: str = Form(""), timeout_minutes: str = Form(""),
               user: dict = Depends(require_superadmin)):
        validate_csrf(csrf_token, user)
        if not managed():
            raise HTTPException(503, "Backup-Verwaltung ist nicht aktiviert.")
        form = dict(enabled=enabled, interval_hours=interval_hours, retention_days=retention_days, retry_minutes=retry_minutes, timeout_minutes=timeout_minutes)
        try:
            if enabled not in ("0", "1"):
                raise ValueError
            config = {"enabled": enabled}
            for field, key, upper, factor in (("interval_hours", "interval_seconds", 168, 3600), ("retention_days", "retention_days", 365, 1),
                                               ("retry_minutes", "retry_seconds", 1440, 60), ("timeout_minutes", "timeout_seconds", 1440, 60)):
                value = int(form[field])
                if not 1 <= value <= upper:
                    raise ValueError
                config[key] = str(value * factor)
        except ValueError:
            return render(request, "backups.html", {**page_context(user), "form": form, "error": "Bitte ganze Zahlen innerhalb der angegebenen Grenzen eintragen."}, status_code=400)
        with database.connect() as connection:
            for key, value in config.items():
                set_value(connection, "backup_" + key, value)
        return RedirectResponse("/backups?saved=1", status_code=303)

    @router.post("/backups/run")
    def run(request: Request, csrf_token: str = Form(""), user: dict = Depends(require_superadmin)):
        validate_csrf(csrf_token, user)
        with database.connect() as connection:
            connection.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('backup_request', '{}', ?) ON CONFLICT(key) DO NOTHING",
                               (datetime.now(timezone.utc).isoformat(),))
            connection.execute("SELECT key FROM app_settings WHERE key = 'backup_request'" + (" FOR UPDATE" if database.IS_POSTGRES else ""))
            state = status_context(read_values(connection))
            if not state["worker_online"]:
                error = "Der Backup-Dienst ist nicht erreichbar. Es wurde keine Sicherung angefordert."
            elif state["busy"]:
                error = "Eine Sicherung ist bereits angefordert oder laeuft."
            else:
                error = None
                set_value(connection, "backup_request", json.dumps({"id": secrets.token_hex(16), "status": "queued", "requested_at": int(time.time()), "requested_by": user["id"]}))
        if error:
            return render(request, "backups.html", {**page_context(user), "error": error}, status_code=409)
        return RedirectResponse("/backups", status_code=303)

    @router.get("/backups/{name}/files/{filename}")
    def download(name: str, filename: str, user: dict = Depends(require_superadmin)):
        if not managed() or filename not in FILES:
            raise HTTPException(404, "Sicherung nicht gefunden.")
        try:
            path = snapshot_path(name) / filename
        except (ValueError, OSError):
            raise HTTPException(404, "Sicherung nicht gefunden.")
        return FileResponse(path, filename=filename, media_type="application/octet-stream",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router
