from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from app.services.cookies import snapshot_cookies
from app.services.storage import DOWNLOAD_DIR, TEMP_DIR, get_job
from app.services.yt_dlp_runtime import yt_dlp_runtime


ProgressCallback = Callable[[float], Awaitable[None]]
_active: dict[str, asyncio.subprocess.Process] = {}
_cancelled: set[str] = set()
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOGIN_MARKERS = (
    "login required",
    "login_required",
    "login to view",
    "log in to view",
    "please log in",
    "please login",
    "sign in to view",
    "authentication required",
    "not logged in",
    "private account",
    "checkpoint_required",
    "challenge_required",
    "you have to be logged in",
    "you must be logged in",
    "requires login",
)
DOWNLOAD_TIMEOUT_SECONDS = max(60, int(os.environ.get("NEGADOWNLOADER_DOWNLOAD_TIMEOUT_SECONDS", os.environ.get("YOULOGGER_DOWNLOAD_TIMEOUT_SECONDS", "21600"))))


def _discard_job_cookie(cookie_path: str | None) -> None:
    if not cookie_path:
        return
    try:
        path = Path(cookie_path).resolve()
        if path.parent == TEMP_DIR.resolve():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def is_auth_error(error_text: str, url: str = "") -> bool:
    lowered = error_text.lower()
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return True
    if re.search(r"http error 401\b|\b401 unauthorized\b", lowered):
        return True
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return (host == "instagram.com" or host.endswith(".instagram.com")) and bool(
            re.search(r"http error 403\b|\b403 forbidden\b", lowered)
        )
    except ValueError:
        return False


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "[網址已隱藏]"


def _safe_error(lines: list[str], original_url: str) -> str:
    message = "\n".join(lines[-12:]).strip()
    message = _ANSI_RE.sub("", message)
    message = message.replace(original_url, _safe_url(original_url))
    message = re.sub(r"https?://[^\s'\"<>]+", lambda match: _safe_url(match.group(0)), message)
    message = re.sub(
        r"(?i)(cookie|password|authorization|sessionid|access[_-]?token)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    return message[:1600] or "yt-dlp 執行失敗，請檢查網址或網站是否暫時無法使用。"


async def cancel_download(job_id: str) -> bool:
    _cancelled.add(job_id)
    process = _active.get(job_id)
    if process is None:
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    return True


async def run_download(job: dict, progress_callback: ProgressCallback) -> dict:
    job_id = job["id"]
    current_job = get_job(job_id)
    if job_id in _cancelled or (current_job and current_job["status"] == "CANCELLED"):
        _cancelled.discard(job_id)
        _discard_job_cookie(job.get("cookie_path"))
        return {"status": "CANCELLED", "error": None}
    configured_subdir = str(job.get("download_subdir") or "")
    download_root = (DOWNLOAD_DIR / configured_subdir).resolve()
    if not download_root.is_relative_to(DOWNLOAD_DIR):
        return {"status": "FAILED", "error": "下載路徑無效。"}
    folder = (download_root / job_id).resolve()
    if folder.parent != download_root:
        return {"status": "FAILED", "error": "下載路徑無效。"}
    folder.mkdir(parents=True, exist_ok=True)
    output_template = str(folder / "%(title).180B-%(id)s.%(ext)s")
    runtime = await asyncio.to_thread(yt_dlp_runtime.active)
    args = runtime.command() + [
        "--ignore-config",
        "--newline",
        "--progress",
        "--continue",
        "--paths",
        f"home:{folder}",
        "-o",
        output_template,
        "--print",
        "after_move:filepath",
    ]
    cookie_path = job.get("cookie_path")
    snapshot_path = TEMP_DIR / f"{job_id}.txt"
    if cookie_path:
        candidate = Path(cookie_path).resolve()
        if candidate.is_file():
            args.extend(("--cookies", str(candidate)))
        elif snapshot_cookies(snapshot_path):
            cookie_path = str(snapshot_path)
            args.extend(("--cookies", cookie_path))
    elif snapshot_cookies(snapshot_path):
        cookie_path = str(snapshot_path)
        args.extend(("--cookies", cookie_path))
    if job.get("user_agent"):
        args.extend(("--user-agent", job["user_agent"]))
    args.append(job["url"])

    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    latest_progress = 0.0
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _active[job_id] = process
        if job_id in _cancelled:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

        async def read_stdout() -> None:
            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    stdout_lines.append(line)

        async def read_stderr() -> None:
            nonlocal latest_progress
            assert process.stderr is not None
            async for raw in process.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                match = _PROGRESS_RE.search(line)
                if match:
                    try:
                        progress = min(99.9, max(0.0, float(match.group(1))))
                    except ValueError:
                        continue
                    if progress - latest_progress >= 0.25:
                        latest_progress = progress
                        await progress_callback(progress)
                elif line:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 40:
                        del stderr_lines[: len(stderr_lines) - 40]

        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr()),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        exit_code = await process.wait()
        if job_id in _cancelled:
            return {"status": "CANCELLED", "error": None}
        if exit_code != 0:
            raw_error = "\n".join(stderr_lines)
            status = "NEEDS_AUTH" if is_auth_error(raw_error, job["url"]) else "FAILED"
            return {"status": status, "error": _safe_error(stderr_lines, job["url"])}

        file_path = None
        for line in reversed(stdout_lines):
            possible = Path(line)
            try:
                resolved = possible.resolve()
                if resolved.is_relative_to(folder) and resolved.is_file():
                    file_path = str(resolved)
                    break
            except (OSError, ValueError):
                continue
        if not file_path:
            return {"status": "FAILED", "error": "下載命令已結束，但找不到輸出檔案。"}
        await progress_callback(100.0)
        return {"status": "SUCCEEDED", "error": None, "file_path": file_path}
    except asyncio.CancelledError:
        process = _active.get(job_id)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        raise
    except asyncio.TimeoutError:
        process = _active.get(job_id)
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        return {"status": "FAILED", "error": "下載逾時；確認網路後可重新建立工作。"}
    except Exception as exc:
        # Keep process details out of persistent logs; only expose a short generic failure.
        return {"status": "FAILED", "error": f"無法啟動或完成下載程序：{type(exc).__name__}"}
    finally:
        _active.pop(job_id, None)
        _cancelled.discard(job_id)
        _discard_job_cookie(cookie_path)
