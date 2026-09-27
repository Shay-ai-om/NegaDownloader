from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from websockets.asyncio.client import connect as websocket_connect

from app.services.browser import browser_session
from app.services.cookies import (
    IMPORTED_COOKIE_FILE,
    CookieFileError,
    delete_imported_cookie_file,
    save_imported_cookie_file,
    write_playwright_cookies,
)
from app.services.downloader import cancel_download, run_download
from app.services.storage import CONFIG_DIR, DOWNLOAD_DIR, TEMP_DIR, cancel_queued_job, clear_job_history, clear_queued_jobs, create_job, finish_running_job, get_download_subdir, get_job, init_db, list_jobs, next_queued_job, normalize_download_subdir, prepare_storage, set_download_subdir, update_job


APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))
MAX_URL_LENGTH = 4096
PASSWORD = os.environ.get("NEGADOWNLOADER_PASSWORD", os.environ.get("YOULOGGER_PASSWORD", ""))
COOKIE_SECURE = os.environ.get("NEGADOWNLOADER_COOKIE_SECURE", os.environ.get("YOULOGGER_COOKIE_SECURE", "false")).lower() == "true"


def _load_session_secret() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = CONFIG_DIR / "session.secret"
    try:
        with secret_file.open("x", encoding="utf-8") as file:
            file.write(secrets.token_urlsafe(48))
        secret_file.chmod(0o600)
    except FileExistsError:
        pass
    return secret_file.read_text(encoding="utf-8").strip()


SESSION_SECRET = _load_session_secret()
ticket_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="youlogger-browser-ws-v1")


async def _download_worker() -> None:
    while True:
        job = next_queued_job()
        if job is None:
            await asyncio.sleep(1)
            continue

        async def update_progress(value: float) -> None:
            update_job(job["id"], progress=value)

        result = await run_download(job, update_progress)
        finish_running_job(job["id"], **result)
        if result.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_AUTH"):
            update_job(job["id"], cookie_path=None, user_agent=None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not PASSWORD:
        raise RuntimeError("請設定 NEGADOWNLOADER_PASSWORD 環境變數後再啟動 NegaDownloader。")
    prepare_storage()
    init_db()
    worker = asyncio.create_task(_download_worker(), name="download-worker")
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await browser_session.close()


app = FastAPI(title="NegaDownloader", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="youlogger_session",
    same_site="strict",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 24 * 14,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.url.path.endswith("/vnc.html") or request.url.path.endswith("/auth-session"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
NOVNC_DIR = Path("/usr/share/novnc")
if NOVNC_DIR.is_dir():
    app.mount("/novnc", StaticFiles(directory=str(NOVNC_DIR), html=True), name="novnc")


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=MAX_URL_LENGTH)


class DownloadPathRequest(BaseModel):
    path: str = Field(default="", max_length=1024)


class ClearJobsRequest(BaseModel):
    scope: Literal["queue", "history"]


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


async def require_session(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="請先登入 NegaDownloader。")


def _csrf_matches(request: Request, token: str | None) -> bool:
    expected = request.session.get("csrf_token", "")
    return bool(expected and token and hmac.compare_digest(expected, token))


def _check_csrf(request: Request, token: str | None) -> None:
    if not _csrf_matches(request, token):
        raise HTTPException(status_code=403, detail="工作階段已過期，請重新整理頁面。")


def _new_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _safe_display_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "網址"


def _is_instagram_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return host == "instagram.com" or host.endswith(".instagram.com")
    except Exception:
        return False


async def _validate_public_http_url(value: str) -> str:
    url = value.strip()
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError
        if parts.username or parts.password:
            raise ValueError
        port = parts.port
        host = parts.hostname.rstrip(".").lower()
        if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            addresses = {ipaddress.ip_address(host)}
        except ValueError:
            records = await asyncio.to_thread(socket.getaddrinfo, host, port or (443 if parts.scheme == "https" else 80), 0, socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(record[4][0]) for record in records}
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
    except (ValueError, OSError, socket.gaierror):
        raise HTTPException(status_code=400, detail="請輸入指向公開網站的完整 HTTP/HTTPS 網址。")
    return url


def _serialize_job(job: dict) -> dict:
    file_path = job.get("file_path")
    filename = Path(file_path).name if file_path else None
    return {
        "id": job["id"],
        "url": _safe_display_url(job["url"]),
        "status": job["status"],
        "progress": job["progress"],
        "filename": filename,
        "error": job.get("error"),
        "created_at": job["created_at"],
        "can_login": _is_instagram_url(job["url"]),
        "cookies_configured": IMPORTED_COOKIE_FILE.is_file(),
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"csrf": _new_csrf(request), "error": error},
    )


_login_failures: dict[str, list[float]] = {}


@app.post("/login")
async def login(request: Request, password: str = Form(...), csrf: str = Form(...)):
    _check_csrf(request, csrf)
    client_ip = request.client.host if request.client else "unknown"
    now = asyncio.get_running_loop().time()
    recent = [attempt for attempt in _login_failures.get(client_ip, []) if now - attempt < 600]
    _login_failures[client_ip] = recent
    if len(recent) >= 8:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"csrf": _new_csrf(request), "error": "登入嘗試過多，請稍後再試。"},
            status_code=429,
        )
    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        recent.append(now)
        _login_failures[client_ip] = recent
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"csrf": _new_csrf(request), "error": "密碼不正確。"},
            status_code=401,
        )
    request.session.clear()
    request.session["authenticated"] = True
    request.session["sid"] = secrets.token_urlsafe(24)
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    _check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "csrf": _new_csrf(request),
            "jobs": [_serialize_job(job) for job in list_jobs()],
            "has_imported_cookies": IMPORTED_COOKIE_FILE.is_file(),
            "download_subdir": get_download_subdir(),
            "download_root": str(DOWNLOAD_DIR),
        },
    )


@app.get("/api/jobs")
async def api_jobs(_: None = Depends(require_session)):
    return {
        "jobs": [_serialize_job(job) for job in list_jobs()],
        "cookies_configured": IMPORTED_COOKIE_FILE.is_file(),
    }


@app.get("/api/settings/download-path")
async def get_download_path_setting(_: None = Depends(require_session)):
    subdir = get_download_subdir()
    return {"path": subdir, "full_path": str(DOWNLOAD_DIR / subdir) if subdir else str(DOWNLOAD_DIR)}


@app.put("/api/settings/download-path")
async def update_download_path_setting(
    request: Request,
    body: DownloadPathRequest,
    _: None = Depends(require_session),
):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    try:
        subdir = normalize_download_subdir(body.path)
        target = (DOWNLOAD_DIR / subdir).resolve() if subdir else DOWNLOAD_DIR
        target.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "無法建立下載目錄，請檢查 Docker 掛載目錄權限。"
        raise HTTPException(status_code=400, detail=message) from exc
    set_download_subdir(subdir)
    return {"path": subdir, "full_path": str(target)}


@app.post("/api/jobs/clear")
async def clear_jobs(request: Request, body: ClearJobsRequest, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    if body.scope == "history":
        return {"cleared": clear_job_history(), "scope": "history"}
    removed_jobs = clear_queued_jobs()
    temp_root = TEMP_DIR.resolve()
    for job in removed_jobs:
        if not job.get("cookie_path"):
            continue
        try:
            cookie_file = Path(job["cookie_path"]).resolve()
            if cookie_file.parent == temp_root:
                cookie_file.unlink(missing_ok=True)
        except OSError:
            continue
    return {"cleared": len(removed_jobs), "scope": "queue"}


@app.post("/api/jobs")
async def create_download_job(request: Request, body: CreateJobRequest, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    url = await _validate_public_http_url(body.url)
    job = create_job(url)
    return JSONResponse(_serialize_job(job), status_code=201)


@app.post("/api/cookies")
async def upload_cookies(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(require_session),
):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    data = await file.read(5 * 1024 * 1024 + 1)
    try:
        save_imported_cookie_file(data)
    except CookieFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()
    return {"configured": True}


@app.delete("/api/cookies")
async def remove_cookies(request: Request, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    delete_imported_cookie_file()
    return {"configured": False}


@app.post("/api/jobs/{job_id}/auth-session")
async def start_auth_session(job_id: str, request: Request, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到下載工作。")
    if not _is_instagram_url(job["url"]):
        raise HTTPException(status_code=400, detail="互動式瀏覽器目前只支援 Instagram；其他網站請匯入 cookies.txt。")
    if job["status"] not in ("NEEDS_AUTH", "FAILED"):
        raise HTTPException(status_code=409, detail="請等目前的下載工作結束後再開啟登入。")
    try:
        await browser_session.open(job_id, job["url"])
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"無法啟動登入瀏覽器：{type(exc).__name__}") from exc
    request.session.setdefault("sid", secrets.token_urlsafe(24))
    ticket = ticket_serializer.dumps({"sid": request.session["sid"], "nonce": secrets.token_urlsafe(24)})
    return {"ticket": ticket}


@app.post("/api/jobs/{job_id}/retry-auth")
async def retry_with_browser_auth(job_id: str, request: Request, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到下載工作。")
    if not _is_instagram_url(job["url"]):
        raise HTTPException(status_code=400, detail="互動式登入目前只支援 Instagram。")
    if job["status"] not in ("NEEDS_AUTH", "FAILED"):
        raise HTTPException(status_code=409, detail="此工作目前無法使用登入狀態重試。")
    cookie_path = TEMP_DIR / f"{job_id}.txt"
    try:
        cookies, user_agent = await browser_session.export_cookies()
        write_playwright_cookies(cookies, cookie_path)
    except (RuntimeError, CookieFileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    update_job(job_id, status="QUEUED", progress=0, error=None, cookie_path=str(cookie_path), user_agent=user_agent)
    return {"status": "QUEUED"}


@app.post("/api/jobs/{job_id}/retry-cookies")
async def retry_with_imported_cookies(job_id: str, request: Request, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到下載工作。")
    if job["status"] not in ("FAILED", "NEEDS_AUTH"):
        raise HTTPException(status_code=409, detail="此工作目前無法使用 cookies 重試。")
    if not IMPORTED_COOKIE_FILE.is_file():
        raise HTTPException(status_code=400, detail="請先匯入 cookies.txt。")
    update_job(job_id, status="QUEUED", progress=0, error=None, cookie_path=None, user_agent=None)
    return {"status": "QUEUED"}


@app.post("/api/jobs/{job_id}/cancel")
async def stop_job(job_id: str, request: Request, _: None = Depends(require_session)):
    _check_csrf(request, request.headers.get("x-csrf-token"))
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到下載工作。")
    if job["status"] == "QUEUED":
        if not cancel_queued_job(job_id):
            refreshed = get_job(job_id)
            if refreshed and refreshed["status"] == "RUNNING":
                await cancel_download(job_id)
                update_job(job_id, status="CANCELLED", error=None)
            elif refreshed and refreshed["status"] != "CANCELLED":
                raise HTTPException(status_code=409, detail="此工作已經結束，無法取消。")
    elif job["status"] == "RUNNING":
        await cancel_download(job_id)
        update_job(job_id, status="CANCELLED", error=None)
    else:
        raise HTTPException(status_code=409, detail="此工作目前無法取消。")
    return {"status": "CANCELLED"}


@app.get("/api/jobs/{job_id}/file")
async def download_file(job_id: str, _: None = Depends(require_session)):
    job = get_job(job_id)
    if not job or job["status"] != "SUCCEEDED" or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="檔案尚未就緒。")
    candidate = Path(job["file_path"]).resolve()
    if not candidate.is_relative_to(DOWNLOAD_DIR) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="找不到下載檔案。")
    return FileResponse(candidate, filename=candidate.name)


@app.get("/api/auth/status")
async def auth_status(_: None = Depends(require_session)):
    return {"cookies_configured": IMPORTED_COOKIE_FILE.is_file()}


@app.websocket("/api/browser/ws")
async def browser_websocket(websocket: WebSocket):
    if not websocket.session.get("authenticated"):
        await websocket.close(code=4401)
        return
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    if origin and urlsplit(origin).netloc.lower() != host.lower():
        await websocket.close(code=4403)
        return
    token = websocket.query_params.get("ticket", "")
    try:
        payload = ticket_serializer.loads(token, max_age=600)
    except (BadSignature, SignatureExpired):
        await websocket.close(code=4403)
        return
    if not hmac.compare_digest(str(payload.get("sid", "")), str(websocket.session.get("sid", ""))):
        await websocket.close(code=4403)
        return
    protocols = websocket.scope.get("subprotocols", [])
    subprotocol = next((protocol for protocol in ("binary", "base64") if protocol in protocols), None)
    await websocket.accept(subprotocol=subprotocol)
    try:
        offered = [subprotocol] if subprotocol else []
        async with websocket_connect(
            "ws://127.0.0.1:6081/websockify",
            max_size=None,
            ping_interval=20,
            subprotocols=offered,
        ) as backend:
            async def client_to_browser() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is not None:
                        await backend.send(data)
                    elif message.get("text") is not None:
                        await backend.send(message["text"])

            async def browser_to_client() -> None:
                async for message in backend:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            directions = [asyncio.create_task(client_to_browser()), asyncio.create_task(browser_to_client())]
            done, pending = await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
