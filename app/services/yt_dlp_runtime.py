from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from yt_dlp.version import __version__ as IMAGE_VERSION

from app.services.storage import CONFIG_DIR


_RUNTIME_ID = re.compile(r"runtime-[0-9a-f]{32}\Z")
_PROBE = """
import json
import yt_dlp
from yt_dlp.version import __version__
from yt_dlp.extractor import gen_extractor_classes
classes = list(gen_extractor_classes())
assert any(c.IE_NAME != 'generic' and c.suitable('https://www.youtube.com/watch?v=abcdefghijk') for c in classes)
print(json.dumps({'version': __version__, 'module': yt_dlp.__file__}))
"""
_MATCH = """
import json, sys
from yt_dlp.extractor import gen_extractor_classes
classes = [c for c in gen_extractor_classes() if c.IE_NAME != 'generic']
print(json.dumps([any(c.suitable(url) for c in classes) for url in json.load(sys.stdin)]))
"""


class UpdateError(RuntimeError):
    pass


class UpdateBusy(UpdateError):
    pass


@dataclass(frozen=True)
class Runtime:
    identity: str
    python: str
    version: str
    channel: str

    def command(self) -> list[str]:
        return [self.python, "-I", "-m", "yt_dlp"]


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            temporary.chmod(0o600)
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class YtDlpRuntime:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.image = Runtime("image", sys.executable, IMAGE_VERSION, "image")
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._file_lock = threading.RLock()
        self._validated: dict[str, Runtime] = {}
        self._state = {"phase": "IDLE", "message": "可更新至最新版本。"}

    @property
    def busy(self) -> bool:
        return self._lock.locked() or (self._task is not None and not self._task.done())

    def _compatibility(self) -> dict:
        return {"python": sys.implementation.cache_tag, "platform": sys.platform,
                "machine": platform.machine(), "base": str(Path(sys.executable).resolve()),
                "root": str(self.root)}

    def _python(self, directory: Path) -> Path:
        return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _selection(self) -> dict:
        return _read_json(self.root / "selection.json")

    def _resolve(self, identity: str | None) -> Runtime | None:
        if identity == "image":
            return self.image
        if not isinstance(identity, str) or not _RUNTIME_ID.fullmatch(identity):
            return None
        directory = self.root / identity
        if directory.is_symlink() or directory.resolve().parent != self.root:
            return None
        metadata = _read_json(directory / "runtime.json")
        if metadata.get("compatibility") != self._compatibility():
            return None
        interpreter = self._python(directory)
        if not interpreter.is_file():
            return None
        if identity in self._validated:
            return self._validated[identity]
        try:
            result = subprocess.run([str(interpreter), "-I", "-c", _PROBE],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=20, check=True, text=True)
            probe = json.loads(result.stdout)
            self._validate_probe(directory, probe)
            if probe["version"] != metadata.get("version") or metadata.get("channel") not in ("stable", "nightly"):
                return None
        except (OSError, ValueError, KeyError, UpdateError, subprocess.SubprocessError):
            return None
        runtime = Runtime(identity, str(interpreter), probe["version"], metadata["channel"])
        self._validated[identity] = runtime
        return runtime

    def active(self) -> Runtime:
        with self._file_lock:
            return self._resolve(self._selection().get("current", "image")) or self.image

    def status(self) -> dict:
        with self._file_lock:
            selection = self._selection()
            active = self.active()
            previous = self._resolve(selection.get("previous"))
            warning = ""
            if selection.get("current", "image") != active.identity:
                warning = "保存的版本無法使用，已改用映像檔版本；請重新更新。"
            return {"version": active.version, "channel": active.channel,
                    "source": "image" if active.identity == "image" else "persistent",
                    "busy": self.busy, "rollback_available": previous is not None,
                    "rollback_version": previous.version if previous else None,
                    "warning": warning, **self._state}

    def _state_changed(self, phase: str, message: str) -> None:
        with self._file_lock:
            state = {"phase": phase, "message": message}
            self._state = state
            _write_json(self.root / "operation.json", state)

    def _failure(self, phase: str, message: str) -> None:
        # Reporting an out-of-space/read-only error must still work in memory.
        try:
            self._state_changed(phase, message)
        except OSError:
            pass

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        saved = _read_json(self.root / "operation.json")
        if saved.get("phase") in ("QUEUED", "CREATING", "INSTALLING", "VERIFYING"):
            self._state_changed("FAILED", "前次更新被中斷，原版本仍可使用；可重新更新。")
        elif saved.get("phase") in ("SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK"):
            self._state = {"phase": saved["phase"], "message": str(saved.get("message", ""))}
        # These directories never became selectable; clean only our own IDs.
        for directory in self.root.glob("runtime-*"):
            if not (directory / "runtime.json").is_file():
                self._discard(directory)
        await asyncio.to_thread(self.active)

    def _discard(self, directory: Path) -> None:
        if (_RUNTIME_ID.fullmatch(directory.name) and not directory.is_symlink()
                and directory.resolve().parent == self.root):
            shutil.rmtree(directory, ignore_errors=True)

    async def _run(self, args: list[str], timeout: float) -> str:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
            if process.returncode != 0:
                raise UpdateError("更新程序未能完成，請確認網路與設定目錄的可用空間後重試。")
            return output.decode("utf-8", errors="replace")
        finally:
            if process.returncode is None:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()

    def _validate_probe(self, directory: Path, probe: dict) -> None:
        if not isinstance(probe, dict):
            raise UpdateError("新版的版本資訊驗證失敗。")
        version = probe.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,79}", version):
            raise UpdateError("新版的版本資訊驗證失敗。")
        if not isinstance(probe.get("module"), str):
            raise UpdateError("新版的安裝位置驗證失敗。")
        module = Path(probe["module"]).resolve()
        if not module.is_relative_to(directory.resolve()):
            raise UpdateError("新版未正確安裝至獨立環境。")

    async def start(self, channel: str) -> dict:
        if channel not in ("stable", "nightly"):
            raise UpdateError("請選擇 stable 或 nightly。")
        if self.busy:
            raise UpdateBusy("正在更新或回復版本，請稍候。")
        self._state_changed("QUEUED", "正在準備更新…")
        self._task = asyncio.create_task(self._update(channel), name="yt-dlp-update")
        return await asyncio.to_thread(self.status)

    async def _update(self, channel: str) -> None:
        directory = self.root / f"runtime-{uuid.uuid4().hex}"
        activated = False
        async with self._lock:
            try:
                async with asyncio.timeout(900):
                    self._state_changed("CREATING", "正在建立獨立更新環境…")
                    await self._run([sys.executable, "-I", "-m", "venv", "--system-site-packages", str(directory)], 120)
                    interpreter = str(self._python(directory))
                    self._state_changed("INSTALLING", f"正在下載及安裝最新 {channel} 版本…")
                    args = [interpreter, "-I", "-m", "pip", "--isolated", "install",
                            "--disable-pip-version-check", "--no-cache-dir", "--no-deps",
                            "--ignore-installed", "--only-binary", ":all:", "--progress-bar", "off",
                            "--index-url", "https://pypi.org/simple", "--timeout", "30", "--retries", "2"]
                    if channel == "nightly":
                        args.append("--pre")
                    args.append("yt-dlp")
                    await self._run(args, 700)
                    self._state_changed("VERIFYING", "正在驗證新版與網站解析功能…")
                    probe = json.loads(await self._run([interpreter, "-I", "-c", _PROBE], 30))
                    self._validate_probe(directory, probe)
                    metadata = {"version": probe["version"], "channel": channel,
                                "compatibility": self._compatibility()}
                    _write_json(directory / "runtime.json", metadata)
                    with self._file_lock:
                        old = self.active()
                        _write_json(self.root / "selection.json", {"current": directory.name, "previous": old.identity})
                        activated = True
                        self._validated[directory.name] = Runtime(directory.name, interpreter, probe["version"], channel)
                    self._state_changed("SUCCEEDED", f"已更新至 {probe['version']}，新下載工作會使用此版本。")
            except asyncio.CancelledError:
                self._failure("CANCELLED", "更新已中斷；尚未切換的更新不會影響原版本。")
                raise
            except TimeoutError:
                self._failure("FAILED", "更新逾時，原版本仍可使用；請稍後重試。")
            except (OSError, ValueError, UpdateError) as exc:
                message = str(exc) if isinstance(exc, UpdateError) else "更新或驗證失敗，請檢查網路與設定目錄權限後重試。"
                if activated:
                    message = "新版已啟用，但無法保存更新狀態；請重新整理確認版本。"
                self._failure("FAILED", message)
            finally:
                if not activated:
                    self._discard(directory)

    async def rollback(self) -> dict:
        if self.busy:
            raise UpdateBusy("正在更新或回復版本，請稍候。")
        async with self._lock:
            await asyncio.to_thread(self._rollback)
        return self.status()

    def _rollback(self) -> None:
        with self._file_lock:
            selection = self._selection()
            previous = self._resolve(selection.get("previous"))
            if previous is None:
                raise UpdateError("沒有可回復的相容版本。")
            current = self.active()
            _write_json(self.root / "selection.json", {"current": previous.identity, "previous": current.identity})
            self._state_changed("ROLLED_BACK", f"已回復至 {previous.version}，新下載工作會使用此版本。")

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def matches(self, runtime: Runtime, urls: list[str]) -> list[bool]:
        result = subprocess.run([runtime.python, "-I", "-c", _MATCH], input=json.dumps(urls),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, check=True, text=True)
        values = json.loads(result.stdout)
        if not isinstance(values, list) or len(values) != len(urls) or any(type(value) is not bool for value in values):
            raise UpdateError("無法取得網站支援資訊。")
        return values


yt_dlp_runtime = YtDlpRuntime(CONFIG_DIR / "runtime" / "yt-dlp")
