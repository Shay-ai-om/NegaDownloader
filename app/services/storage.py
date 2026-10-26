from __future__ import annotations

import os
import re
import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("NEGADOWNLOADER_CONFIG_DIR", os.environ.get("YOULOGGER_CONFIG_DIR", str(PROJECT_DIR / "data")))).resolve()
DOWNLOAD_DIR = Path(os.environ.get("NEGADOWNLOADER_DOWNLOAD_DIR", os.environ.get("YOULOGGER_DOWNLOAD_DIR", str(PROJECT_DIR / "download")))).resolve()
AUTH_DIR = CONFIG_DIR / "auth"
TEMP_DIR = AUTH_DIR / "temporary"
DB_PATH = CONFIG_DIR / "youlogger.sqlite3"
TOKEN_SCOPES = frozenset({"jobs:create", "jobs:read", "jobs:cancel", "files:read"})


class ApiLimitError(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


def normalize_download_subdir(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value:
        return ""
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("請輸入 /app/download 掛載目錄底下的相對路徑。")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("下載子目錄不可離開 /app/download 掛載目錄。")
    normalized = "/".join(parts)
    candidate = (DOWNLOAD_DIR / normalized).resolve()
    if not candidate.is_relative_to(DOWNLOAD_DIR):
        raise ValueError("下載子目錄不可離開 /app/download 掛載目錄。")
    return normalized


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def prepare_storage() -> None:
    for directory in (CONFIG_DIR, DOWNLOAD_DIR, AUTH_DIR, TEMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700 if directory in (CONFIG_DIR, AUTH_DIR, TEMP_DIR) else 0o755)
        except OSError:
            pass


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def init_db() -> None:
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                file_path TEXT,
                error TEXT,
                cookie_path TEXT,
                user_agent TEXT,
                download_subdir TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "download_subdir" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN download_subdir TEXT NOT NULL DEFAULT ''")
        if "api_token_id" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN api_token_id TEXT")
        if "idempotency_key" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
        if "request_hash" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN request_hash TEXT")
        db.execute(
            """CREATE TABLE IF NOT EXISTS api_tokens (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                scopes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                last_used_at TEXT,
                revoked_at TEXT
            )"""
        )
        db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        initial_subdir = os.environ.get("NEGADOWNLOADER_DOWNLOAD_SUBDIR", os.environ.get("YOULOGGER_DOWNLOAD_SUBDIR", ""))
        try:
            initial_subdir = normalize_download_subdir(initial_subdir)
        except ValueError:
            initial_subdir = ""
        db.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('download_subdir', ?)",
            (initial_subdir,),
        )
        db.execute("CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS jobs_token_created ON jobs(api_token_id, created_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_token_idempotency ON jobs(api_token_id, idempotency_key)")
        # Resume interrupted jobs after a container restart; yt-dlp can continue partial files.
        db.execute("UPDATE jobs SET status='QUEUED', updated_at=? WHERE status='RUNNING'", (utc_now(),))


def create_job(url: str) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex,
        "url": url,
        "status": "QUEUED",
        "progress": 0.0,
        "file_path": None,
        "error": None,
        "cookie_path": None,
        "user_agent": None,
        "download_subdir": "",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    with connect() as db:
        setting = db.execute("SELECT value FROM app_settings WHERE key='download_subdir'").fetchone()
        try:
            job["download_subdir"] = normalize_download_subdir(setting["value"] if setting else "")
        except ValueError:
            job["download_subdir"] = ""
        db.execute(
            """INSERT INTO jobs (id,url,status,progress,file_path,error,cookie_path,user_agent,download_subdir,created_at,updated_at)
               VALUES (:id,:url,:status,:progress,:file_path,:error,:cookie_path,:user_agent,:download_subdir,:created_at,:updated_at)""",
            job,
        )
    return job


def create_api_token(name: str, scopes: set[str], expires_in_days: int | None) -> tuple[dict[str, Any], str]:
    if not name.strip() or len(name.strip()) > 100 or not scopes or not scopes <= TOKEN_SCOPES:
        raise ValueError("Token 名稱或權限無效。")
    if expires_in_days is not None and not 1 <= expires_in_days <= 3650:
        raise ValueError("有效期限需為 1 至 3650 天。")
    token_id = uuid.uuid4().hex
    token = f"nd_{token_id}_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expires_in_days)).isoformat(timespec="seconds") if expires_in_days else None
    row = {
        "id": token_id,
        "name": name.strip(),
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "scopes": ",".join(sorted(scopes)),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "last_used_at": None,
        "revoked_at": None,
    }
    with connect() as db:
        db.execute("""INSERT INTO api_tokens (id,name,token_hash,scopes,created_at,expires_at,last_used_at,revoked_at)
                      VALUES (:id,:name,:token_hash,:scopes,:created_at,:expires_at,:last_used_at,:revoked_at)""", row)
    return _public_token(row), token


def _public_token(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "name", "scopes", "created_at", "expires_at", "last_used_at", "revoked_at")} | {"prefix": f"nd_{row['id'][:8]}..."}


def list_api_tokens() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM api_tokens ORDER BY created_at DESC").fetchall()
    return [_public_token(dict(row)) for row in rows]


def revoke_api_token(token_id: str) -> bool:
    with connect() as db:
        result = db.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (utc_now(), token_id))
        return result.rowcount == 1


def authenticate_api_token(token: str) -> dict[str, Any] | None:
    parts = token.split("_", 2)
    if len(token) > 160 or len(parts) != 3 or parts[0] != "nd" or not re.fullmatch(r"[0-9a-f]{32}", parts[1]):
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM api_tokens WHERE id=?", (parts[1],)).fetchone()
        if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] <= utc_now()):
            return None
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["token_hash"]):
            return None
        db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (utc_now(), row["id"]))
        return dict(row)


def create_agent_job(url: str, token_id: str, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
    request_hash = hashlib.sha256(url.encode()).hexdigest()
    now = utc_now()
    job = {
        "id": uuid.uuid4().hex, "url": url, "status": "QUEUED", "progress": 0.0,
        "file_path": None, "error": None, "cookie_path": None, "user_agent": None,
        "download_subdir": "", "api_token_id": token_id, "idempotency_key": idempotency_key,
        "request_hash": request_hash, "created_at": now, "updated_at": now,
    }
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
            db.execute("UPDATE jobs SET idempotency_key=NULL, request_hash=NULL WHERE api_token_id=? AND idempotency_key=? AND created_at<?", (token_id, idempotency_key, day_ago))
            existing = db.execute("SELECT * FROM jobs WHERE api_token_id=? AND idempotency_key=?", (token_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflict()
                return dict(existing), True
        minute_ago = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        recent = db.execute("SELECT COUNT(*) FROM jobs WHERE api_token_id=? AND created_at>=?", (token_id, minute_ago)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM jobs WHERE api_token_id=? AND status IN ('QUEUED','RUNNING')", (token_id,)).fetchone()[0]
        if recent >= 10 or pending >= 20:
            raise ApiLimitError()
        setting = db.execute("SELECT value FROM app_settings WHERE key='download_subdir'").fetchone()
        try:
            job["download_subdir"] = normalize_download_subdir(setting["value"] if setting else "")
        except ValueError:
            job["download_subdir"] = ""
        db.execute(
            """INSERT INTO jobs (id,url,status,progress,file_path,error,cookie_path,user_agent,download_subdir,
                                 created_at,updated_at,api_token_id,idempotency_key,request_hash)
               VALUES (:id,:url,:status,:progress,:file_path,:error,:cookie_path,:user_agent,:download_subdir,
                       :created_at,:updated_at,:api_token_id,:idempotency_key,:request_hash)""", job,
        )
    return job, False


def get_agent_job(job_id: str, token_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND api_token_id=?", (job_id, token_id)).fetchone()
    return dict(row) if row else None


def list_agent_jobs(token_id: str, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM jobs WHERE api_token_id=?", (token_id,)).fetchone()[0]
        rows = db.execute("SELECT * FROM jobs WHERE api_token_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (token_id, limit, offset)).fetchall()
    return [dict(row) for row in rows], total


def get_job(job_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def next_queued_job() -> dict[str, Any] | None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            now = utc_now()
            db.execute("UPDATE jobs SET status='RUNNING', updated_at=? WHERE id=?", (now, row["id"]))
            job = dict(row)
            job["status"] = "RUNNING"
            job["updated_at"] = now
        else:
            job = None
        db.commit()
    return job


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {"status", "progress", "file_path", "error", "cookie_path", "user_agent"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    params = list(values.values()) + [job_id]
    with connect() as db:
        db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", params)


def get_download_subdir() -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key='download_subdir'").fetchone()
    try:
        return normalize_download_subdir(row["value"] if row else "")
    except ValueError:
        return ""


def set_download_subdir(value: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('download_subdir', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )


def clear_job_history() -> int:
    with connect() as db:
        cursor = db.execute("DELETE FROM jobs WHERE status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'NEEDS_AUTH')")
        return cursor.rowcount


def clear_queued_jobs() -> list[dict[str, Any]]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT id, cookie_path FROM jobs WHERE status='QUEUED'").fetchall()
        db.execute("DELETE FROM jobs WHERE status='QUEUED'")
        db.commit()
    return [dict(row) for row in rows]


def delete_job(job_id: str) -> None:
    with connect() as db:
        db.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def cancel_queued_job(job_id: str) -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE jobs SET status='CANCELLED', error=NULL, updated_at=? WHERE id=? AND status='QUEUED'",
            (utc_now(), job_id),
        )
        return cursor.rowcount == 1


def finish_running_job(job_id: str, **fields: Any) -> bool:
    allowed = {"status", "progress", "file_path", "error"}
    values = {key: value for key, value in fields.items() if key in allowed}
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{key}=?" for key in values)
    params = list(values.values()) + [job_id]
    with connect() as db:
        cursor = db.execute(
            f"UPDATE jobs SET {assignments} WHERE id=? AND status='RUNNING'",
            params,
        )
        return cursor.rowcount == 1
