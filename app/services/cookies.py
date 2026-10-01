from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.services.storage import AUTH_DIR


IMPORTED_COOKIE_FILE = AUTH_DIR / "imported-cookies.txt"
MAX_COOKIE_BYTES = 5 * 1024 * 1024
_cookie_lock = threading.RLock()


class CookieFileError(ValueError):
    pass


def _validate_netscape(data: bytes) -> bytes:
    if len(data) > MAX_COOKIE_BYTES:
        raise CookieFileError("cookies.txt 不可超過 5 MiB")
    if b"\x00" in data:
        raise CookieFileError("檔案含有無效字元")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieFileError("請使用 UTF-8 編碼的 Netscape cookies.txt") from exc
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), 0)
    header = lines[first].strip() if lines else ""
    if header not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
        raise CookieFileError("格式不符；第一行需為 Netscape HTTP Cookie File 標頭")

    cookie_rows = []
    for line in lines[first + 1:]:
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        http_only = line.startswith("#HttpOnly_")
        parsed = line[len("#HttpOnly_") :] if http_only else line
        columns = parsed.split("\t")
        if len(columns) != 7:
            raise CookieFileError("cookies.txt 含有欄位不完整的 cookie 記錄")
        domain, include_subdomains, path, secure, expiry, name, value = columns
        domain = domain.lower().rstrip(".")
        if not domain.lstrip(".") or any(char.isspace() or char in "/\\:?#" for char in domain) or not path.startswith("/") or not name:
            raise CookieFileError("cookies.txt 含有無效的 cookie 欄位")
        if include_subdomains.upper() not in ("TRUE", "FALSE") or secure.upper() not in ("TRUE", "FALSE"):
            raise CookieFileError("cookies.txt 的布林欄位格式錯誤")
        if not re.fullmatch(r"-?\d+", expiry):
            raise CookieFileError("cookies.txt 的到期時間格式錯誤")
        domain = ("." if include_subdomains.upper() == "TRUE" else "") + domain.lstrip(".")
        columns = [domain, include_subdomains.upper(), path, secure.upper(), expiry, name, value]
        cookie_rows.append(("#HttpOnly_" if http_only else "") + "\t".join(columns))
    if not cookie_rows:
        raise CookieFileError("cookies.txt 沒有 cookie 記錄")
    return ("# Netscape HTTP Cookie File\n" + "\n".join(cookie_rows) + "\n").encode()


def _atomic_write(data: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="cookies-", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            temporary.chmod(0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
        destination.chmod(0o600)
        if destination == IMPORTED_COOKIE_FILE:
            _cached_saved_rows.cache_clear()
    finally:
        temporary.unlink(missing_ok=True)


def _rows(data: bytes) -> list[tuple[str, list[str]]]:
    return [(line, line.removeprefix("#HttpOnly_").split("\t"))
            for line in data.decode().splitlines()[1:] if line]


@lru_cache(maxsize=1)
def _cached_saved_rows(modified_ns: int, size: int) -> list[tuple[str, list[str]]]:
    return _rows(_validate_netscape(IMPORTED_COOKIE_FILE.read_bytes()))


def _saved_rows() -> list[tuple[str, list[str]]]:
    if not IMPORTED_COOKIE_FILE.is_file():
        return []
    stat = IMPORTED_COOKIE_FILE.stat()
    return _cached_saved_rows(stat.st_mtime_ns, stat.st_size)


def save_imported_cookie_files(files: list[bytes]) -> None:
    """Merge atomically; newer domain/path/name entries replace older entries."""
    uploads = [_validate_netscape(data) for data in files]
    if not uploads:
        raise CookieFileError("請選擇至少一個 cookies.txt")
    with _cookie_lock:
        sources = ([_validate_netscape(IMPORTED_COOKIE_FILE.read_bytes())]
                   if IMPORTED_COOKIE_FILE.is_file() else []) + uploads
        merged = {}
        for source in sources:
            for line, columns in _rows(source):
                merged[(columns[0].lstrip("."), columns[2], columns[5])] = line
        data = ("# Netscape HTTP Cookie File\n" + "\n".join(merged.values()) + "\n").encode()
        if len(data) > MAX_COOKIE_BYTES:
            raise CookieFileError("合併後的 cookies 不可超過 5 MiB")
        _atomic_write(data, IMPORTED_COOKIE_FILE)


def save_imported_cookie_file(data: bytes) -> None:
    save_imported_cookie_files([data])


def delete_imported_cookie_file() -> None:
    with _cookie_lock:
        IMPORTED_COOKIE_FILE.unlink(missing_ok=True)
        _cached_saved_rows.cache_clear()


def configured_cookie_domains() -> list[str]:
    with _cookie_lock:
        now = time.time()
        return sorted({columns[0].lstrip(".") for _, columns in
                       _saved_rows()
                       if columns[4] == "0" or int(columns[4]) > now})


def has_cookies_for_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    host = {"youtu.be": "www.youtube.com", "fb.watch": "www.facebook.com"}.get(host, host)
    with _cookie_lock:
        for _, columns in _saved_rows():
            domain = columns[0].lstrip(".")
            if columns[4] != "0" and int(columns[4]) <= time.time():
                continue
            if host == domain or (columns[1] == "TRUE" and host.endswith("." + domain)):
                return True
    return False


def snapshot_cookies(destination: Path) -> bool:
    """Isolate yt-dlp cookie writebacks from imports and other downloads."""
    with _cookie_lock:
        if not IMPORTED_COOKIE_FILE.is_file():
            return False
        _atomic_write(IMPORTED_COOKIE_FILE.read_bytes(), destination)
        return True


def write_playwright_cookies(cookies: list[dict[str, Any]], destination: Path) -> int:
    """Write the dedicated browser's cookies as a Netscape cookie jar."""
    output = ["# Netscape HTTP Cookie File"]
    count = 0
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires", -1)
        try:
            expiry = str(max(0, int(expires)))
        except (TypeError, ValueError, OverflowError):
            expiry = "0"
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        path = str(cookie.get("path", "/")) or "/"
        if (
            not name
            or any("\t" in field or "\n" in field or "\r" in field for field in (domain, path, name, value))
        ):
            continue
        row = "\t".join((domain, include_subdomains, path, secure, expiry, name, value))
        if cookie.get("httpOnly"):
            row = "#HttpOnly_" + row
        output.append(row)
        count += 1
    if count == 0:
        raise CookieFileError("登入工作階段中沒有可供 yt-dlp 使用的 cookies")
    _atomic_write(_validate_netscape(("\n".join(output) + "\n").encode()), destination)
    return count
