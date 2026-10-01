from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Playwright, async_playwright

from app.services.storage import AUTH_DIR


# Keep the existing directory so upgrades preserve existing login sessions.
PROFILE_DIR = AUTH_DIR / "instagram-browser"


def _discard_stale_chromium_locks(profile_dir: Path) -> None:
    """Remove abandoned Chromium singleton links after a container restart."""
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return

    try:
        owner = os.readlink(lock)
        pid = int(owner.rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return

    process_dir = Path("/proc") / str(pid)
    if process_dir.exists():
        try:
            cmdline = (process_dir / "cmdline").read_bytes()
        except OSError:
            # If the owner cannot be checked, leave the profile untouched.
            return
    else:
        cmdline = b""

    profile_arg = str(profile_dir.resolve()).encode()
    if profile_arg in cmdline and (b"chrome" in cmdline.lower() or b"chromium" in cmdline.lower()):
        return

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        candidate = profile_dir / name
        try:
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
        except OSError:
            return


class BrowserSession:
    """One private, persistent Chromium profile used only for interactive login."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page = None
        self._job_id: str | None = None

    async def open(self, job_id: str, url: str) -> str:
        async with self._lock:
            if self._context is None:
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                _discard_stale_chromium_locks(PROFILE_DIR)
                self._playwright = await async_playwright().start()
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=False,
                    chromium_sandbox=os.environ.get("NEGADOWNLOADER_CHROMIUM_SANDBOX", os.environ.get("YOULOGGER_CHROMIUM_SANDBOX", "true")).lower() == "true",
                    viewport={"width": 1280, "height": 800},
                    args=["--no-first-run", "--disable-dev-shm-usage"],
                )
                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            elif self._page is None or self._page.is_closed():
                self._page = await self._context.new_page()
            self._job_id = job_id
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return await self._page.evaluate("navigator.userAgent")

    async def export_cookies(self, job_id: str) -> tuple[list[dict[str, Any]], str | None]:
        async with self._lock:
            if self._context is None:
                raise RuntimeError("尚未開啟登入瀏覽器")
            if self._job_id != job_id:
                raise RuntimeError("登入瀏覽器已切換至其他工作，請重新開啟此工作的登入視窗")
            cookies = await self._context.cookies()
            user_agent = None
            if self._page is not None and not self._page.is_closed():
                user_agent = await self._page.evaluate("navigator.userAgent")
            return cookies, user_agent

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                await self._context.close()
            self._context = None
            self._page = None
            self._job_id = None
            if self._playwright is not None:
                await self._playwright.stop()
            self._playwright = None


browser_session = BrowserSession()
