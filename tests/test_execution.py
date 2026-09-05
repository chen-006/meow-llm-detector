import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_benchmarks import fixture
from gpt56_vnext.benchmark import build_package, content_hash
from gpt56_vnext.detector import DetectorSession, build_single_jobs
from gpt56_vnext.errors import RequestError
from gpt56_vnext.generator import ProbeGeneratorSession, calibrate_package, merge_windows
from gpt56_vnext.store import SQLiteStateStore

SECRET = "synthetic-execution-secret-not-real"


class EchoModel:
    def __init__(self):
        self.calls = 0
        self.failures = []

    async def request(self, mode, base, key, model, cell, **kwargs):
        if kwargs.get("on_dispatch"):
            kwargs["on_dispatch"]()
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"answer": model, "http_status": 200, "usage": {}, "elapsed_ms": 1}

    async def close(self):
        pass


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_calibration_detection_roundtrip(self):
        project, _ = fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "test.sqlite3") as store:
            reports = []
            for window in (1, 2):
                runner = ProbeGeneratorSession(store, f"c{window}", project,
                    {"base_url": "https://fixture.invalid/v1", "window": window, "samples": 3}, SECRET, transport=EchoModel())
                reports.append(await runner.run())
            merged, observations, collection = merge_windows(reports)
            package = calibrate_package(merged, observations, collection, {"batches": dict.fromkeys(("low", "medium", "high"), 600)})
            for claimed, color in (("a", "green"), ("b", "red")):
                fake = EchoModel()
                session = DetectorSession(store, "d" + claimed, package,
                    {"base_url": "https://fixture.invalid/v1", "claimed_model": claimed, "request_model": "a"}, SECRET, transport=fake)
                self.assertEqual(session.report()["operational_status"], "prepared")
                report = await session.run()
                self.assertEqual(report["fingerprint"]["color"], color)
                self.assertEqual(fake.calls, 9)
                self.assertEqual(report["operational_status"], "complete")
                self.assertNotIn(SECRET, str(report))

    async def test_changed_tier_cannot_reuse_calibration(self):
        project, observations = fixture()
        package = calibrate_package(project, observations, {"sources": []}, {"batches": dict.fromkeys(("low", "medium", "high"), 600)})
        package["tiers"]["low"]["counts"]["ab"] = 5
        package["content_sha256"] = content_hash(package)
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "test.sqlite3") as store:
            session = DetectorSession(store, "d", package, {"base_url": "https://fixture.invalid/v1", "claimed_model": "a"}, SECRET, transport=EchoModel())
            self.assertEqual((await session.run())["fingerprint"]["color"], "yellow")

    async def test_relaxed_selection_target_is_bound_to_calibration(self):
        project, observations = fixture()
        package = calibrate_package(project, observations, {"sources": []},
            {"batches": dict.fromkeys(("low", "medium", "high"), 600), "selection_target": 0.999})
        from gpt56_vnext.detector import calibration_matches
        self.assertTrue(calibration_matches(package, "low"))
        package["calibration"]["tiers"]["low"]["selection_target"] = 0.99
        self.assertFalse(calibration_matches(package, "low"))

    async def test_network_error_not_wrong_model(self):
        project, observations = fixture()
        package = build_package(project, observations)
        fake = EchoModel()
        fake.failures = [RequestError("upstream_http_error", status=401)] * 9
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "test.sqlite3") as store:
            session = DetectorSession(store, "d", package,
                {"base_url": "https://fixture.invalid/v1", "claimed_model": "a", "runtime": {"retries": 0}}, SECRET, transport=fake)
            report = await session.run()
            self.assertEqual(report["fingerprint"]["color"], "yellow")
            self.assertEqual(report["progress"]["errors"], 9)
            self.assertEqual(fake.calls, 9)

    async def test_retry_and_resume_preserve_attempts(self):
        project, observations = fixture()
        package = build_package(project, observations)
        fake = EchoModel()
        fake.failures = [RequestError("upstream_http_error", status=524)]
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "test.sqlite3") as store:
            config = {"base_url": "https://fixture.invalid/v1", "claimed_model": "a", "runtime": {"workers": 1}}
            session = DetectorSession(store, "d", package, config, SECRET, transport=fake)
            async def immediate(_seconds):
                return
            with patch("gpt56_vnext.executor.asyncio.sleep", immediate):
                report = await session.run()
            self.assertEqual(report["progress"]["retries"], 1)
            session = DetectorSession(store, "d", package, config, SECRET, transport=fake)
            await session.run()
            self.assertEqual(fake.calls, 10)


if __name__ == "__main__":
    unittest.main()
