# WebUI yt-dlp updates

The WebUI includes a **下載引擎更新** panel with the current version, stable/nightly channel selection, **更新 yt-dlp**, update status, and **回復上一版本**. Updates run as the existing application user; root access and a web-server restart are not needed.

## Using the panel

Choose Stable for the latest official release, or Nightly for the latest available prerelease fixes. Click **更新 yt-dlp** and leave the service running while it builds, installs, and verifies the candidate environment. The panel polls every three seconds and disables further mutations while an operation is active. After success, new downloads use the selected runtime. Running downloads keep the interpreter path they captured before starting.

If needed, **回復上一版本** switches new jobs to the preceding runtime, including the image's built-in version. Both environments remain on disk, so a download already running in either environment can finish. Rolling back again swaps the two selections. A version can be saved independently even when the latest upstream version number equals the image version.

## Persistent runtime and failure handling

Immutable environments are created at `/app/data/runtime/yt-dlp/runtime-<id>/`. Each venv inherits the image's existing optional dependencies; pip installs only the official `yt-dlp` wheel from PyPI into the new venv using isolated pip settings. This button does not update Python, ffmpeg, Chromium, or the rest of the application. Package names, install URLs, and shell commands cannot be supplied through the API.

An offline import/extractor smoke test verifies the candidate version and confirms that its module actually resides inside the candidate directory. `selection.json` is replaced atomically only after verification. `operation.json` records the update phase and user-facing message. Failed installs, timeouts, and interrupted updates leave the old selection intact; unfinished directories are removed after a failure and during startup. The operation has a 15-minute overall timeout and shuts down its child process group on cancellation. UI errors do not expose arbitrary pip output or environment credentials.

The `/app/data` mount must retain its location inside the container and remain writable by the app user. Metadata checks the Python tag, platform, architecture, interpreter location, and runtime root. A fresh server process revalidates a saved runtime. Incompatible or unusable environments fall back to the built-in image version and display a warning to update again. Successful environments are retained to protect active downloads; repeated updates therefore use additional disk space.

The supported-site matcher runs the selected runtime's actual extractor predicates in a subprocess. Results are batched and cached by runtime identity, so login eligibility follows updates and rollbacks without restarting the server. Queue serialization and runtime probes run off the event loop.

## API

- `GET /api/settings/yt-dlp`: current version/channel/source, busy state, phase/message, fallback warning, and rollback availability.
- `POST /api/settings/yt-dlp/update`: JSON `{"channel":"stable"}` or `{"channel":"nightly"}`; returns HTTP 202. Concurrent mutations return HTTP 409.
- `POST /api/settings/yt-dlp/rollback`: restore the previous compatible environment.

All endpoints require an authenticated session. Mutations also require the existing `X-CSRF-Token` header.

## Manual image update

To update the built-in version instead, rebuild and recreate from the project directory:

```sh
docker compose build --no-cache negadownloader
docker compose up -d negadownloader
```

The normal Docker build cache can reuse the dependency installation layer, so `up --build` alone does not guarantee a built-in yt-dlp update. A compatible saved WebUI runtime continues taking precedence over the image's built-in version.

References: [yt-dlp updates](https://github.com/yt-dlp/yt-dlp#update), [Python virtual environments](https://docs.python.org/3/library/venv.html), [pip install](https://pip.pypa.io/en/stable/cli/pip_install/).
