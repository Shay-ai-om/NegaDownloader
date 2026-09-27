# NegaDownloader

NegaDownloader is a self-hosted web interface for yt-dlp. It provides a persistent download queue, an embedded Instagram sign-in browser, and an optional `cookies.txt` import.

## Docker Compose

1. Copy `.env.example` to `.env` and set a long, unique `NEGADOWNLOADER_PASSWORD`.
2. Set `NEGADOWNLOADER_CONFIG` to a private persistent directory and `NEGADOWNLOADER_DOWNLOADS` to the host directory where media should be stored.
3. Build and start:

   ```sh
   docker compose up -d --build
   ```

4. Open `http://<server-ip>:8080` and sign in with `NEGADOWNLOADER_PASSWORD`.

Example Unraid `.env` values:

```dotenv
NEGADOWNLOADER_PASSWORD=replace-with-a-long-random-password
NEGADOWNLOADER_CONFIG=/mnt/user/appdata/negadownloader/config
NEGADOWNLOADER_DOWNLOADS=/mnt/user/Media/NegaDownloader
NEGADOWNLOADER_PORT=8080
PUID=99
PGID=100
```

In Unraid's Docker template, map the appdata directory read/write to `/config` and your chosen media share read/write to `/downloads`. The interactive browser runs inside the container; publish only port `8080`. Do not publish the internal VNC/noVNC ports. Use a reverse proxy with TLS and WebSocket support for remote access.

### Choose or change the download directory

At deployment, `NEGADOWNLOADER_DOWNLOADS` (Compose) or the Unraid host-path mapping for `/downloads` selects the host directory mounted into the container. For example, map `/mnt/user/Media/NegaDownloader` to `/downloads`.

After deployment, the WebUI's **下載位置** setting selects a relative subfolder inside that mounted directory. Enter a value such as `Instagram/Reels`; leave it blank to use the mount root. The WebUI cannot switch to a host directory that Docker has not mounted. To use a different host share, change the host-path mapping and recreate the container. New jobs capture the current WebUI setting when they are added; queued and completed jobs keep their original destination.

## Sign in and download

- Add a public `http` or `https` video URL. Public videos can download without an account.
- If yt-dlp reports an authentication error for Instagram, choose **登入並重試**. Complete Instagram's sign-in, two-factor authentication, or checkpoint in the embedded browser, then choose **使用登入狀態重試**.
- Alternatively, import a Mozilla/Netscape-format `cookies.txt`. The service keeps only supported Instagram-related domains from the imported file.
- **清空佇列** removes jobs that have not started. Active downloads are preserved.
- **清除紀錄** removes completed, failed, needs-login, and cancelled job rows. Downloaded media files remain on disk.

The embedded browser runs inside NegaDownloader. The app does not receive the password entered on Instagram. Cookies and the browser profile are login credentials, so keep `/config` private and include it in protected backups. The app does not bypass CAPTCHA or access controls; a site may still expire or reject a session.

## Configuration

- `NEGADOWNLOADER_PASSWORD`: required WebUI password.
- `NEGADOWNLOADER_CONFIG`: host directory mounted at `/config`; stores the database and authentication state.
- `NEGADOWNLOADER_DOWNLOADS`: host directory mounted at `/downloads`.
- `NEGADOWNLOADER_PORT`: host port for WebUI port `8080` (default `8080`).
- `NEGADOWNLOADER_COOKIE_SECURE`: set to `true` when the app is accessed only over HTTPS.
- `NEGADOWNLOADER_CHROMIUM_SANDBOX`: keep enabled unless the host prevents Chromium from starting.
- `NEGADOWNLOADER_DOWNLOAD_TIMEOUT_SECONDS`: per-job timeout, default `21600` seconds.
- `PUID` / `PGID`: container identity for host file ownership; Unraid commonly uses `99` / `100`.

## Persistent files

- `/config/youlogger.sqlite3`: jobs, settings, and queue state. The existing filename is kept to preserve data when upgrading from YouLogger.
- `/config/session.secret`: WebUI session signing key.
- `/config/auth/imported-cookies.txt`: optional filtered cookie jar.
- `/config/auth/instagram-browser/`: dedicated Chromium profile.
- `/config/auth/temporary/`: short-lived per-job cookie exports.
- `/downloads/<subfolder>/<job-id>/`: downloaded media.

The `/config` and `/downloads` bind mounts survive image rebuilds. When updating, run `docker compose up -d --build` from the project directory. 
