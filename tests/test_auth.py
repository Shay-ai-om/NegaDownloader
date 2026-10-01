import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_workspace = tempfile.TemporaryDirectory()
os.environ["NEGADOWNLOADER_CONFIG_DIR"] = str(Path(_workspace.name) / "config")
os.environ["NEGADOWNLOADER_DOWNLOAD_DIR"] = str(Path(_workspace.name) / "downloads")
os.environ["NEGADOWNLOADER_PASSWORD"] = "test-password"

from fastapi.testclient import TestClient
from app.main import app
from app.services import cookies, downloader, storage
from app.services.browser import BrowserSession
from app.services.sites import can_login
from yt_dlp.cookies import YoutubeDLCookieJar
from app.services.yt_dlp_runtime import Runtime, UpdateBusy


def jar(domain, value="test", *, expiry="0", subdomains="TRUE", path="/", name="session"):
    return f"# Netscape HTTP Cookie File\n{domain}\t{subdomains}\t{path}\tTRUE\t{expiry}\t{name}\t{value}\n".encode()


class CookieTests(unittest.TestCase):
    def setUp(self):
        storage.prepare_storage()
        cookies.delete_imported_cookie_file()

    def test_sequential_and_batch_imports_preserve_sites(self):
        cookies.save_imported_cookie_file(jar(".facebook.com"))
        cookies.save_imported_cookie_files([jar(".instagram.com"), jar(".youtube.com")])
        self.assertEqual(cookies.configured_cookie_domains(), ["facebook.com", "instagram.com", "youtube.com"])
        self.assertTrue(cookies.has_cookies_for_url("https://www.instagram.com/reel/123"))

    def test_replace_same_cookie_and_keep_paths(self):
        cookies.save_imported_cookie_files([jar(".facebook.com", "old"), jar(".facebook.com", "other", path="/other")])
        cookies.save_imported_cookie_file(jar(".facebook.com", "new"))
        data = cookies.IMPORTED_COOKIE_FILE.read_text()
        self.assertNotIn("old", data)
        self.assertIn("new", data)
        self.assertIn("other", data)

    def test_validation_failure_leaves_existing_jar_unchanged(self):
        cookies.save_imported_cookie_file(jar(".facebook.com"))
        before = cookies.IMPORTED_COOKIE_FILE.read_bytes()
        with self.assertRaises(cookies.CookieFileError):
            cookies.save_imported_cookie_files([jar(".instagram.com"), b"invalid"])
        self.assertEqual(cookies.IMPORTED_COOKIE_FILE.read_bytes(), before)

    def test_domain_boundaries_host_only_expiration_and_aliases(self):
        cookies.save_imported_cookie_files([jar(".facebook.com"), jar(".instagram.com", expiry="1"), jar("example.org", subdomains="FALSE")])
        self.assertFalse(cookies.has_cookies_for_url("https://www.instagram.com/"))
        self.assertFalse(cookies.has_cookies_for_url("https://evilfacebook.com/"))
        self.assertFalse(cookies.has_cookies_for_url("https://facebook.com.evil.org/"))
        self.assertFalse(cookies.has_cookies_for_url("https://sub.example.org/"))
        self.assertTrue(cookies.has_cookies_for_url("https://example.org/"))
        self.assertTrue(cookies.has_cookies_for_url("https://fb.watch/123"))
        cookies.save_imported_cookie_file(jar(".youtube.com"))
        self.assertTrue(cookies.has_cookies_for_url("https://youtu.be/abcdefghijk"))

    def test_snapshot_is_isolated(self):
        cookies.save_imported_cookie_file(jar(".facebook.com"))
        snapshot = storage.TEMP_DIR / "snapshot.txt"
        self.assertTrue(cookies.snapshot_cookies(snapshot))
        snapshot.write_text("modified by downloader")
        self.assertTrue(cookies.has_cookies_for_url("https://www.facebook.com/"))
        snapshot.unlink()

    def test_browser_export_accepts_other_domains_and_http_only(self):
        destination = storage.TEMP_DIR / "browser.txt"
        self.assertEqual(cookies.write_playwright_cookies([
            {"domain": ".facebook.com", "name": "session", "value": "fb", "httpOnly": True},
            {"domain": ".instagram.com", "name": "session", "value": "ig"},
        ], destination), 2)
        cookies.save_imported_cookie_file(destination.read_bytes())
        self.assertIn("#HttpOnly_", cookies.IMPORTED_COOKIE_FILE.read_text())
        self.assertEqual(cookies.configured_cookie_domains(), ["facebook.com", "instagram.com"])
        destination.unlink()

    def test_normalized_multi_site_jar_loads_in_yt_dlp(self):
        cookies.save_imported_cookie_files([jar("FACEBOOK.COM"), jar(".instagram.com")])
        loaded = YoutubeDLCookieJar(str(cookies.IMPORTED_COOKIE_FILE))
        loaded.load(ignore_discard=True, ignore_expires=True)
        self.assertEqual({cookie.domain for cookie in loaded}, {".facebook.com", ".instagram.com"})

    def test_empty_and_malformed_files_are_rejected(self):
        for data in [b"", b"# Netscape HTTP Cookie File\n", jar(".facebook.com", expiry="bad"), jar("bad/domain")]:
            with self.subTest(data=data), self.assertRaises(cookies.CookieFileError):
                cookies.save_imported_cookie_file(data)


class ApiTests(unittest.TestCase):
    def setUp(self):
        storage.prepare_storage()
        storage.init_db()
        storage.clear_job_history()
        storage.clear_queued_jobs()
        cookies.delete_imported_cookie_file()
        self.client = TestClient(app)
        response = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
        self.client.post("/login", data={"password": "test-password", "csrf": token})
        home = self.client.get("/")
        token = re.search(r'name="csrf-token" content="([^"]+)"', home.text)[1]
        self.headers = {"X-CSRF-Token": token}

    def tearDown(self):
        self.client.close()

    def failed_job(self, url):
        job = storage.create_job(url)
        storage.update_job(job["id"], status="FAILED", error="network failed")
        return job

    def test_multi_upload_and_site_specific_status(self):
        response = self.client.post("/api/cookies", headers=self.headers, files=[
            ("file", ("fb.txt", jar(".facebook.com"))),
            ("file", ("yt.txt", jar(".youtube.com"))),
        ])
        self.assertEqual(response.status_code, 200, response.text)
        ig = self.failed_job("https://www.instagram.com/reel/ABC/")
        fb = self.failed_job("https://www.facebook.com/watch/?v=123456")
        jobs = {job["id"]: job for job in self.client.get("/api/jobs").json()["jobs"]}
        self.assertTrue(jobs[ig["id"]]["can_login"])
        self.assertFalse(jobs[ig["id"]]["cookies_configured"])
        self.assertTrue(jobs[fb["id"]]["cookies_configured"])

    def test_non_instagram_login_and_persisted_browser_cookies(self):
        job = self.failed_job("https://www.facebook.com/watch/?v=123456")
        with patch("app.main._validate_public_http_url", AsyncMock(return_value=job["url"])), patch("app.main.browser_session.open", AsyncMock()) as opened:
            response = self.client.post(f'/api/jobs/{job["id"]}/auth-session', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        opened.assert_awaited_once_with(job["id"], job["url"])
        cookies.save_imported_cookie_file(jar(".instagram.com"))
        exported = ([{"domain": ".facebook.com", "name": "session", "value": "fb"}], "test-agent")
        with patch("app.main.browser_session.export_cookies", AsyncMock(return_value=exported)):
            response = self.client.post(f'/api/jobs/{job["id"]}/retry-auth', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(cookies.configured_cookie_domains(), ["facebook.com", "instagram.com"])
        snapshot = Path(storage.get_job(job["id"])["cookie_path"])
        self.assertIn("instagram.com", snapshot.read_text())
        snapshot.unlink()

    def test_mutation_requires_csrf_and_authentication(self):
        self.assertEqual(self.client.post("/api/cookies", files={"file": ("c.txt", jar(".facebook.com"))}).status_code, 403)
        anonymous = TestClient(app)
        try:
            self.assertEqual(anonymous.get("/api/jobs").status_code, 401)
        finally:
            anonymous.close()

    def test_single_upload_compatibility_and_invalid_batch(self):
        response = self.client.post("/api/cookies", headers=self.headers, files={"file": ("fb.txt", jar(".facebook.com"))})
        self.assertEqual(response.status_code, 200, response.text)
        before = cookies.IMPORTED_COOKIE_FILE.read_bytes()
        response = self.client.post("/api/cookies", headers=self.headers, files=[
            ("file", ("ig.txt", jar(".instagram.com"))), ("file", ("bad.txt", b"invalid")),
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(cookies.IMPORTED_COOKIE_FILE.read_bytes(), before)

    def test_reject_unsupported_and_running_jobs(self):
        job = self.failed_job("https://unsupported.example/video")
        response = self.client.post(f'/api/jobs/{job["id"]}/auth-session', headers=self.headers)
        self.assertEqual(response.status_code, 400)
        job = storage.create_job("https://www.instagram.com/reel/ABC/")
        response = self.client.post(f'/api/jobs/{job["id"]}/auth-session', headers=self.headers)
        self.assertEqual(response.status_code, 409)

    def test_update_endpoints_require_authentication_and_csrf(self):
        anonymous = TestClient(app)
        try:
            self.assertEqual(anonymous.get("/api/settings/yt-dlp").status_code, 401)
            self.assertEqual(anonymous.post("/api/settings/yt-dlp/update", json={"channel": "stable"}).status_code, 401)
        finally:
            anonymous.close()
        self.assertEqual(self.client.post("/api/settings/yt-dlp/update", json={"channel": "stable"}).status_code, 403)
        self.assertEqual(self.client.post("/api/settings/yt-dlp/rollback").status_code, 403)

    def test_update_channel_validation_and_busy_response(self):
        response = self.client.post("/api/settings/yt-dlp/update", headers=self.headers, json={"channel": "arbitrary; command"})
        self.assertEqual(response.status_code, 422)
        with patch("app.main.yt_dlp_runtime.start", AsyncMock(side_effect=UpdateBusy("busy"))):
            response = self.client.post("/api/settings/yt-dlp/update", headers=self.headers, json={"channel": "stable"})
        self.assertEqual(response.status_code, 409)

    def test_update_and_rollback_are_wired_to_runtime(self):
        with patch("app.main.yt_dlp_runtime.start", AsyncMock(return_value={"busy": True})) as start:
            response = self.client.post("/api/settings/yt-dlp/update", headers=self.headers, json={"channel": "nightly"})
        self.assertEqual(response.status_code, 202)
        start.assert_awaited_once_with("nightly")
        with patch("app.main.yt_dlp_runtime.rollback", AsyncMock(return_value={"version": "previous"})) as rollback:
            response = self.client.post("/api/settings/yt-dlp/rollback", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        rollback.assert_awaited_once()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_rejects_export_after_job_switch(self):
        session = BrowserSession()
        session._context = AsyncMock()
        session._job_id = "second"
        with self.assertRaisesRegex(RuntimeError, "其他工作"):
            await session.export_cookies("first")

    async def test_download_uses_private_snapshot_and_cleans_failure(self):
        cookies.delete_imported_cookie_file()
        cookies.save_imported_cookie_file(jar(".facebook.com"))
        job = storage.create_job("https://www.facebook.com/watch/?v=123456")
        process = AsyncMock()
        process.stdout = asyncio.StreamReader()
        process.stdout.feed_eof()
        process.stderr = asyncio.StreamReader()
        process.stderr.feed_data(b"network failed\n")
        process.stderr.feed_eof()
        process.wait.return_value = 1
        selected = Runtime("test", "/selected/runtime/python", "2026.10.01", "stable")
        with patch("app.services.downloader.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as launch, patch("app.services.downloader.yt_dlp_runtime.active", return_value=selected):
            result = await downloader.run_download(job, AsyncMock())
        args = launch.call_args.args
        self.assertEqual(args[0], selected.python)
        cookie_path = Path(args[args.index("--cookies") + 1])
        self.assertNotEqual(cookie_path, cookies.IMPORTED_COOKIE_FILE)
        self.assertFalse(cookie_path.exists())
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(cookies.has_cookies_for_url(job["url"]))

    async def test_site_support_comes_from_installed_extractors(self):
        self.assertTrue(can_login("https://www.youtube.com/watch?v=abcdefghijk"))
        self.assertTrue(can_login("https://www.facebook.com/watch/?v=123456"))
        self.assertFalse(can_login("https://unsupported.example/video"))
        self.assertTrue(can_login("https://embedded.example/video", "[generic] unable to extract"))
        self.assertFalse(can_login("https://embedded.example/video", "[generic] Unsupported URL"))


if __name__ == "__main__":
    unittest.main()
