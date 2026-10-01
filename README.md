# NegaDownloader

NegaDownloader is a self-hosted web interface for yt-dlp. It provides a persistent download queue, an embedded website sign-in browser, and merged multi-site `cookies.txt` imports.

## Docker Compose

1. Copy `.env.example` to `.env` and set a long, unique `NEGADOWNLOADER_PASSWORD`.
2. Set `NEGADOWNLOADER_CONFIG` to a private persistent directory and `NEGADOWNLOADER_DOWNLOADS` to the host directory where media should be stored.
3. Build and start:

   ```sh
   docker compose up -d --build
   ```

4. Open `http://<server-ip>:20880` and sign in with `NEGADOWNLOADER_PASSWORD`.

Example Unraid `.env` values:

```dotenv
NEGADOWNLOADER_PASSWORD=replace-with-a-long-random-password
NEGADOWNLOADER_CONFIG=/mnt/user/appdata/negadownloader/config
NEGADOWNLOADER_DOWNLOADS=/mnt/user/Media/NegaDownloader
NEGADOWNLOADER_PORT=20880
PUID=99
PGID=100
```

In Unraid's Docker template, map the appdata directory read/write to `/config` and your chosen media share read/write to `/downloads`. The interactive browser runs inside the container; publish only host port `20880` to container port `8080` (`20880:8080`). Do not publish the internal VNC/noVNC ports. Use a reverse proxy with TLS and WebSocket support for remote access.

### Choose or change the download directory

At deployment, `NEGADOWNLOADER_DOWNLOADS` (Compose) or the Unraid host-path mapping for `/downloads` selects the host directory mounted into the container. For example, map `/mnt/user/Media/NegaDownloader` to `/downloads`.

After deployment, the WebUI's **下載位置** setting selects a relative subfolder inside that mounted directory. Enter a value such as `Instagram/Reels`; leave it blank to use the mount root. The WebUI cannot switch to a host directory that Docker has not mounted. To use a different host share, change the host-path mapping and recreate the container. New jobs capture the current WebUI setting when they are added; queued and completed jobs keep their original destination.

## Sign in and download

- Add a public `http` or `https` video URL. Public videos can download without an account.
- When a download fails (including non-authentication failures), a supported site's job automatically opens the sign-in browser if no unexpired cookies match that site's hostname. Each job prompts once per page load; additional prompts wait until the current dialog closes. Installed yt-dlp extractor patterns determine supported sites; Generic extractor attempts are also eligible unless yt-dlp reports an unsupported URL. Failure does not necessarily mean that login will fix it.
- Choose **登入並重試** to reopen the browser manually, including when saved cookies have expired or no longer work. Complete the website's sign-in, two-factor authentication, or checkpoint, then choose **使用登入狀態重試**. Browser cookies are merged into the saved jar for subsequent downloads. The dedicated browser supports multiple website sessions, but only one interactive job at a time.
- Alternatively, select one or more Mozilla/Netscape-format `cookies.txt` files and choose **合併匯入**. All website domains are accepted. Importing Facebook cookies followed by Instagram cookies preserves both; matching domain/path/name entries are replaced by the newer import. Each batch and the merged jar are limited to 5 MiB, with at most 20 files per batch. The WebUI lists saved, unexpired cookie domains without exposing values.
- Cookie detection follows host-only/subdomain flags and expiration, with aliases for `youtu.be` and `fb.watch`. Additional redirect domains may require manually opening login. The presence of cookies does not verify that the account is authenticated. **移除全部** removes the saved jar; the dedicated browser profile retains its sessions.
- **清空佇列** removes jobs that have not started. Active downloads are preserved.
- **清除紀錄** removes completed, failed, needs-login, and cancelled job rows. Downloaded media files remain on disk.

The embedded browser runs inside NegaDownloader. The app does not receive the password entered on Instagram. Cookies and the browser profile are login credentials, so keep `/config` private and include it in protected backups. The app does not bypass CAPTCHA or access controls; a site may still expire or reject a session.

## Configuration

- `NEGADOWNLOADER_PASSWORD`: required WebUI password.
- `NEGADOWNLOADER_CONFIG`: host directory mounted at `/config`; stores the database and authentication state.
- `NEGADOWNLOADER_DOWNLOADS`: host directory mounted at `/downloads`.
- `NEGADOWNLOADER_PORT`: host port for WebUI port `8080` (default `20880`).
- `NEGADOWNLOADER_COOKIE_SECURE`: set to `true` when the app is accessed only over HTTPS.
- `NEGADOWNLOADER_CHROMIUM_SANDBOX`: keep enabled unless the host prevents Chromium from starting.
- `NEGADOWNLOADER_DOWNLOAD_TIMEOUT_SECONDS`: per-job timeout, default `21600` seconds.
- `PUID` / `PGID`: container identity for host file ownership; Unraid commonly uses `99` / `100`.

## Persistent files

- `/config/youlogger.sqlite3`: jobs, settings, and queue state.
- `/config/session.secret`: WebUI session signing key.
- `/config/auth/imported-cookies.txt`: merged multi-site cookie jar, including imports and browser exports.
- `/config/auth/instagram-browser/`: dedicated multi-site Chromium profile; the legacy directory name preserves existing sessions.
- `/config/auth/temporary/`: isolated per-job cookie snapshots, deleted after download completion, failure, or cancellation.
- `/config/runtime/yt-dlp/`: saved update environments, active/previous selections, and update status.
- `/downloads/<subfolder>/<job-id>/`: downloaded media.

The `/config` and `/downloads` bind mounts survive image rebuilds. When updating, run `docker compose up -d --build` from the project directory.

## WebUI yt-dlp updates

Use **下載引擎更新** to view the current version, choose Stable or Nightly, and click **更新 yt-dlp**. The app installs and verifies the official PyPI package in a persistent environment under `/config`; new downloads switch only after success. Running downloads continue using their original version. **回復上一版本** restores the previous environment, including the built-in image version.

Saved versions survive container recreation with the same `/config` mount. Incompatible Python environments fall back to the image version and show a warning. See [update implementation and usage](docs/yt-dlp-webui-update.md) for details.

## Verification

With the project dependencies and the test-only `httpx` package installed, run from the project root:

```sh
python -m unittest discover -s tests -v
node tests/test_auto_login.cjs
node tests/test_engine_ui.cjs
```

The real Chromium smoke test needs the image's installed browser, Xvfb, `DISPLAY`, and Chromium sandbox permissions:

```sh
python -m tests.browser_smoke
```

The real update smoke tests install an official stable release and use temporary configuration:

```sh
python -m tests.runtime_smoke
python -m tests.webui_update_smoke
```

Tests do not use existing account sessions or live configuration. Verified in WSL with isolated containers: backend tests, frontend auto-login/update checks, unprivileged official PyPI installation, runtime reload/rollback, and real Chromium WebUI update/rollback at desktop and mobile widths.
