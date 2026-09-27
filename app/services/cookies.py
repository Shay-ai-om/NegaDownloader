from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.services.storage import AUTH_DIR


IMPORTED_COOKIE_FILE = AUTH_DIR / "imported-cookies.txt"
MAX_COOKIE_BYTES = 5 * 1024 * 1024
_ALLOWED_IG_COOKIE_SUFFIXES = (
    "instagram.com",
    "cdninstagram.com",
    "fbcdn.net",
    "facebook.com",
)


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
    header = next((line.strip() for line in lines if line.strip()), "")
    if header not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
        raise CookieFileError("格式不符；第一行需為 Netscape HTTP Cookie File 標頭")

    instagram_cookie_rows = []
    for line in lines[1:]:
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        http_only = line.startswith("#HttpOnly_")
        parsed = line[len("#HttpOnly_") :] if http_only else line
        columns = parsed.split("\t")
        if len(columns) != 7:
            raise CookieFileError("cookies.txt 含有欄位不完整的 cookie 記錄")
        domain, include_subdomains, path, secure, expiry, name, value = columns
        if not domain or not path.startswith("/") or not name:
            raise CookieFileError("cookies.txt 含有無效的 cookie 欄位")
        if include_subdomains.upper() not in ("TRUE", "FALSE") or secure.upper() not in ("TRUE", "FALSE"):
            raise CookieFileError("cookies.txt 的布林欄位格式錯誤")
        if not re.fullmatch(r"-?\d+", expiry):
            raise CookieFileError("cookies.txt 的到期時間格式錯誤")
        if is_domain_allowed_for_instagram(domain):
            instagram_cookie_rows.append(("#HttpOnly_" if http_only else "") + "\t".join(columns))
    if not instagram_cookie_rows:
        raise CookieFileError("cookies.txt 沒有 Instagram 網域的 cookie 記錄")
    return ("# Netscape HTTP Cookie File\n" + "\n".join(instagram_cookie_rows) + "\n").encode()


def save_imported_cookie_file(data: bytes) -> None:
    normalized = _validate_netscape(data)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="cookies-", suffix=".tmp", dir=AUTH_DIR)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(normalized)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(IMPORTED_COOKIE_FILE)
        IMPORTED_COOKIE_FILE.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def delete_imported_cookie_file() -> None:
    IMPORTED_COOKIE_FILE.unlink(missing_ok=True)


def is_domain_allowed_for_instagram(domain: str) -> bool:
    normalized = domain.lstrip(".").lower().rstrip(".")
    return any(normalized == suffix or normalized.endswith("." + suffix) for suffix in _ALLOWED_IG_COOKIE_SUFFIXES)


def write_playwright_cookies(cookies: list[dict[str, Any]], destination: Path) -> int:
    """Write only Instagram-related Playwright cookies as a Netscape cookie jar."""
    output = ["# Netscape HTTP Cookie File"]
    count = 0
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        if not is_domain_allowed_for_instagram(domain):
            continue
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
        raise CookieFileError("登入工作階段中沒有可供 yt-dlp 使用的 Instagram cookies")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as file:
        file.write("\n".join(output) + "\n")
    destination.chmod(0o600)
    return count
