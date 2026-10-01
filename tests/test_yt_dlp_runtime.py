import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.yt_dlp_runtime import UpdateBusy, UpdateError, YtDlpRuntime, _write_json


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.manager = YtDlpRuntime(Path(self.workspace.name) / "runtime")
        await self.manager.initialize()
        self.commands = []

    async def asyncTearDown(self):
        await self.manager.close()
        self.workspace.cleanup()

    async def install(self, args, timeout):
        self.commands.append(args)
        if "venv" in args:
            directory = Path(args[-1])
            interpreter = self.manager._python(directory)
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
        if "-c" in args:
            directory = Path(args[0]).parent.parent
            return json.dumps({"version": "2026.10.01", "module": str(directory / "lib/yt_dlp/__init__.py")})
        return ""

    async def update(self, channel="stable"):
        with patch.object(self.manager, "_run", self.install):
            await self.manager.start(channel)
            await self.manager._task

    async def test_staged_update_switches_atomically_and_rolls_back(self):
        original = self.manager.active()
        await self.update()
        updated = self.manager.active()
        self.assertNotEqual(updated.python, original.python)
        self.assertEqual(updated.version, "2026.10.01")
        self.assertEqual(self.manager.status()["phase"], "SUCCEEDED")
        self.assertTrue(self.manager.status()["rollback_available"])
        pip_command = next(args for args in self.commands if "pip" in args)
        self.assertEqual(pip_command[-1], "yt-dlp")
        self.assertIn("https://pypi.org/simple", pip_command)
        self.assertIn("--isolated", pip_command)
        self.assertNotIn("--pre", pip_command)
        await self.manager.rollback()
        self.assertEqual(self.manager.active(), original)
        # Runtime paths already captured by a download remain available.
        self.assertTrue(Path(updated.python).is_file())
        await self.manager.rollback()
        self.assertEqual(self.manager.active(), updated)

    async def test_nightly_uses_pre_release_flag(self):
        await self.update("nightly")
        self.assertIn("--pre", next(args for args in self.commands if "pip" in args))
        self.assertEqual(self.manager.active().channel, "nightly")

    async def test_site_matching_follows_runtime_switch_and_rollback(self):
        from app.services import sites
        url = "https://newly-supported.example/item/1"
        with patch.object(sites, "yt_dlp_runtime", self.manager):
            self.assertFalse(sites.has_site_extractor(url))
            await self.update()
            with patch.object(self.manager, "matches", return_value=[True]) as matches:
                sites.prefetch([url])
                self.assertTrue(sites.has_site_extractor(url))
                self.assertTrue(sites.has_site_extractor(url))
                matches.assert_called_once()
            await self.manager.rollback()
            self.assertFalse(sites.has_site_extractor(url))
            await self.update()
            with patch.object(self.manager, "matches", return_value=[False]):
                self.assertFalse(sites.has_site_extractor(url))

    async def test_failed_verification_keeps_prior_selection_and_cleans_stage(self):
        await self.update()
        original = self.manager.active()
        async def bad_probe(args, timeout):
            result = await self.install(args, timeout)
            if "-c" in args:
                return json.dumps({"version": "2026.10.02", "module": "/outside/yt_dlp.py"})
            return result
        with patch.object(self.manager, "_run", bad_probe):
            await self.manager.start("stable")
            await self.manager._task
        self.assertEqual(self.manager.active(), original)
        self.assertEqual(self.manager.status()["phase"], "FAILED")
        self.assertEqual(len(list(self.manager.root.glob("runtime-*"))), 1)

    async def test_busy_and_shutdown_cancel_preserve_original(self):
        waiting = asyncio.Event()
        async def blocked(args, timeout):
            await self.install(args, timeout)
            waiting.set()
            await asyncio.Event().wait()
        with patch.object(self.manager, "_run", blocked):
            await self.manager.start("stable")
            await waiting.wait()
            with self.assertRaises(UpdateBusy):
                await self.manager.start("nightly")
            with self.assertRaises(UpdateBusy):
                await self.manager.rollback()
            await self.manager.close()
        self.assertEqual(self.manager.active().identity, "image")
        self.assertFalse(list(self.manager.root.glob("runtime-*")))
        self.assertEqual(self.manager.status()["phase"], "CANCELLED")

    async def test_runtime_reloads_after_restart_and_incompatible_python_falls_back(self):
        await self.update()
        updated = self.manager.active()
        reloaded = YtDlpRuntime(self.manager.root)
        probe = json.dumps({"version": updated.version, "module": str(Path(updated.python).parent.parent / "lib/yt_dlp/__init__.py")})
        with patch("app.services.yt_dlp_runtime.subprocess.run") as run:
            run.return_value.stdout = probe
            await reloaded.initialize()
        self.assertEqual(reloaded.active(), updated)
        metadata_path = Path(updated.python).parent.parent / "runtime.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["compatibility"]["python"] = "incompatible"
        _write_json(metadata_path, metadata)
        self.assertEqual(reloaded.active().identity, "image")
        self.assertTrue(reloaded.status()["warning"])

    async def test_interrupted_startup_and_invalid_selection_are_safe(self):
        _write_json(self.manager.root / "operation.json", {"phase": "INSTALLING"})
        _write_json(self.manager.root / "selection.json", {"current": "../../outside", "previous": "invalid"})
        incomplete = self.manager.root / ("runtime-" + "1" * 32)
        incomplete.mkdir()
        await self.manager.initialize()
        self.assertFalse(incomplete.exists())
        self.assertEqual(self.manager.active().identity, "image")
        self.assertEqual(self.manager.status()["phase"], "FAILED")
        with self.assertRaises(UpdateError):
            await self.manager.rollback()
        with self.assertRaises(UpdateError):
            await self.manager.start("arbitrary-package")

    async def test_subprocess_timeout_terminates_process(self):
        with self.assertRaises(TimeoutError):
            await self.manager._run([self.manager.image.python, "-c", "import time; time.sleep(30)"], 0.1)

    async def test_disk_error_still_reports_failure_without_changing_selection(self):
        ready = asyncio.Event()
        resume = asyncio.Event()
        async def held_install(args, timeout):
            ready.set()
            await resume.wait()
            return await self.install(args, timeout)
        with patch.object(self.manager, "_run", held_install):
            await self.manager.start("stable")
            await ready.wait()
            with patch("app.services.yt_dlp_runtime._write_json", side_effect=OSError("disk full")):
                resume.set()
                await self.manager._task
        self.assertEqual(self.manager.active().identity, "image")
        self.assertEqual(self.manager.status()["phase"], "FAILED")


if __name__ == "__main__":
    unittest.main()
