import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpt56_vnext.errors import AppError
from gpt56_vnext.presets import EndpointPresets
from gpt56_vnext.schedule import SingleRunSchedule
from gpt56_vnext.store import SQLiteStateStore


class FakeVault:
    def __init__(self):
        self.values = {}

    def save(self, ref, value):
        self.values[ref] = value

    def load(self, ref):
        if ref not in self.values:
            raise AppError("credential_missing")
        return self.values[ref]

    def delete(self, ref):
        self.values.pop(ref, None)


class PresetTests(unittest.TestCase):
    def test_keys_live_only_in_vault_and_url_change_needs_rebinding(self):
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            vault = FakeVault()
            presets = EndpointPresets(store, vault)
            value = {"name": "fixture", "mode": "gpt", "base_url": "https://fixture.invalid/v1", "model": "a"}
            saved = presets.save(value, "synthetic-preset-secret")
            value["id"] = saved["id"]
            self.assertNotIn("synthetic-preset-secret", str(store.documents("endpoint")))
            self.assertNotIn("credential_ref", presets.list()[0])
            self.assertEqual(presets.connection(saved["id"])[1], "synthetic-preset-secret")
            value["base_url"] = "https://other.invalid/v1"
            with self.assertRaisesRegex(AppError, "endpoint_change_requires_key"):
                presets.save(value)
            presets.save(value, "synthetic-replacement")
            self.assertEqual(list(vault.values.values()), ["synthetic-replacement"])
            presets.delete(saved["id"])
            self.assertEqual(vault.values, {})
            self.assertEqual(presets.list(), [])


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_rounds_and_private_snapshot(self):
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            calls = []
            async def launch(value):
                calls.append(value)
                return f"run-{len(calls)}"
            async def immediate(_seconds):
                return
            schedule = SingleRunSchedule(store, launch)
            with patch("gpt56_vnext.schedule.asyncio.sleep", immediate):
                await schedule.start({"interval_seconds": 60, "round_limit": 3,
                    "detection": {"package_id": "fixture", "endpoint_snapshot": {"name": "test", "credential_ref": "opaque"}}})
                await schedule.task
            self.assertEqual(len(calls), 3)
            self.assertEqual(schedule.status()["completed_rounds"], 3)
            self.assertFalse(schedule.status()["enabled"])
            self.assertNotIn("opaque", str(schedule.status()))

    async def test_pause_does_not_cancel_current_round_and_restart_is_paused(self):
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def launch(_value):
                calls.append(1)
                started.set()
                await release.wait()
                return "fixture"
            schedule = SingleRunSchedule(store, launch)
            await schedule.start({"interval_seconds": 60, "detection": {}})
            await started.wait()
            schedule.pause()
            self.assertFalse(schedule.task.done())
            release.set()
            await schedule.task
            self.assertEqual(len(calls), 1)
            restarted = SingleRunSchedule(store, launch)
            self.assertFalse(restarted.status()["enabled"])
            self.assertIsNone(restarted.task)


if __name__ == "__main__":
    unittest.main()
