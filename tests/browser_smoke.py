"""Run in the app image with Xvfb; uses synthetic cookies, no real accounts."""
import asyncio
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>Login smoke test</title><p>Local test page</p>")

    def log_message(self, *args):
        pass


async def check():
    from app.services.browser import BrowserSession
    from app.services.cookies import configured_cookie_domains, save_imported_cookie_file, write_playwright_cookies
    session = BrowserSession()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        await session.open("first", url)
        await session._context.add_cookies([
            {"domain": ".facebook.com", "path": "/", "name": "session", "value": "synthetic-fb", "expires": 2000000000, "httpOnly": True, "secure": True},
            {"domain": ".instagram.com", "path": "/", "name": "session", "value": "synthetic-ig", "expires": 2000000000, "secure": True},
        ])
        cookies, user_agent = await session.export_cookies("first")
        assert user_agent
        exported = Path(os.environ["NEGADOWNLOADER_CONFIG_DIR"]) / "export.txt"
        write_playwright_cookies(cookies, exported)
        save_imported_cookie_file(exported.read_bytes())
        assert configured_cookie_domains() == ["facebook.com", "instagram.com"]
        await session.close()
        await session.open("second", url)
        cookies, _ = await session.export_cookies("second")
        assert {cookie["domain"] for cookie in cookies} == {".facebook.com", ".instagram.com"}
        try:
            await session.export_cookies("first")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Export from a different job was accepted")
        print("Chromium multi-site export, persistence and job-switch checks passed")
    finally:
        await session.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="negadownloader-browser-check-") as config:
        os.environ["NEGADOWNLOADER_CONFIG_DIR"] = config
        asyncio.run(check())
