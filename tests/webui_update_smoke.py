"""Headless WebUI/API update and rollback in an isolated application process."""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright


async def check(workspace):
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    env = {**os.environ, "NEGADOWNLOADER_CONFIG_DIR": str(workspace / "config"),
           "NEGADOWNLOADER_DOWNLOAD_DIR": str(workspace / "downloads"),
           "NEGADOWNLOADER_PASSWORD": "ui-test-password"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                response = await asyncio.to_thread(urllib.request.urlopen, base + "/healthz", timeout=1)
                response.close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("Test application did not start")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/login")
            await page.locator("#password").fill("ui-test-password")
            await page.get_by_role("button", name="登入", exact=True).click()
            await page.wait_for_url(base + "/")
            await page.wait_for_function("!document.getElementById('update-yt-dlp').disabled")
            await page.get_by_role("button", name="更新 yt-dlp", exact=True).click()
            await page.wait_for_function("document.getElementById('yt-dlp-source').textContent === '已保存版本' && !document.getElementById('rollback-yt-dlp').disabled", timeout=180000)
            assert (workspace / "config/runtime/yt-dlp/selection.json").is_file()
            print("WebUI button installed and activated official yt-dlp", flush=True)
            artifacts = Path(os.environ.get("TEST_ARTIFACT_DIR", str(workspace / "artifacts")))
            artifacts.mkdir(parents=True, exist_ok=True)
            await page.locator(".updater-panel").screenshot(path=str(artifacts / "engine-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.locator(".updater-panel").screenshot(path=str(artifacts / "engine-mobile.png"))
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await page.get_by_role("button", name="回復上一版本", exact=True).click()
            await page.wait_for_function("document.getElementById('yt-dlp-source').textContent === '內建版本'")
            assert not errors, errors
            await browser.close()
            print("WebUI login, polling, desktop/mobile layout and rollback passed", flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="negadownloader-webui-check-") as workspace:
        asyncio.run(check(Path(workspace)))
