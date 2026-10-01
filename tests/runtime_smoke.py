"""Exercise official PyPI installation as the app user with temporary storage."""
import asyncio
import tempfile
from pathlib import Path

from app.services.yt_dlp_runtime import YtDlpRuntime


async def check(root):
    manager = YtDlpRuntime(root)
    await manager.initialize()
    original = manager.active()
    try:
        await manager.start("stable")
        last = ""
        while manager.busy:
            status = manager.status()
            if status["phase"] != last:
                last = status["phase"]
                print(f"Update phase: {last}", flush=True)
            await asyncio.sleep(0.5)
        status = manager.status()
        assert status["phase"] == "SUCCEEDED", status
        updated = manager.active()
        assert updated.identity != "image"
        assert manager.matches(updated, ["https://www.youtube.com/watch?v=abcdefghijk", "https://www.facebook.com/watch/?v=123456"]) == [True, True]
        process = await asyncio.create_subprocess_exec(*updated.command(), "--version", stdout=asyncio.subprocess.PIPE)
        output, _ = await process.communicate()
        assert process.returncode == 0
        assert output.decode().strip() == updated.version
        # A fresh instance probes the persisted environment, as after a restart.
        reloaded = YtDlpRuntime(root)
        await reloaded.initialize()
        assert reloaded.active() == updated
        await reloaded.rollback()
        assert reloaded.active() == original
        assert Path(updated.python).is_file()
        print(f"Unprivileged official update, runtime selection, restart and rollback passed: {updated.version}", flush=True)
    finally:
        await manager.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="negadownloader-update-check-") as workspace:
        asyncio.run(check(Path(workspace) / "runtime"))
