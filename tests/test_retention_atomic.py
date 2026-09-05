from pathlib import Path
import tempfile
import unittest

from test_benchmarks import fixture
from test_execution import EchoModel, SECRET
from gpt56_vnext.benchmark import build_package
from gpt56_vnext.detector import DetectorSession
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.transport import build_payload


class RecordedResponse(EchoModel):
    async def request(self, mode, base, key, model, cell, **kwargs):
        value = await super().request(mode, base, key, model, cell, **kwargs)
        return {**value, "request_json": build_payload(mode, model, cell),
                "response_utf8": "data: synthetic-response\n\n", "headers": {"content-type": "text/event-stream"}}


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_response_contents_are_retained(self):
        project, observations = fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            session = DetectorSession(store, "contents", build_package(project, observations),
                {"base_url": "https://fixture.invalid/v1", "claimed_model": "a", "runtime": {"retain_raw": True}},
                SECRET, transport=RecordedResponse())
            await session.run()
            saved = store.retained_exchanges("contents")
            self.assertEqual(saved["coverage"], {"attempts": 9, "retained": 9})
            for row in saved["records"]:
                exchange = row["exchange"]
                self.assertTrue(exchange["body_available"])
                self.assertEqual(exchange["request"]["messages"][0], {"role": "system", "content": "."})
                self.assertEqual(exchange["response_utf8"], "data: synthetic-response\n\n")
                self.assertNotIn(SECRET, str(exchange))

    async def test_retention_roundtrip_and_resume_no_duplicates(self):
        project, observations = fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            config = {"base_url": "https://fixture.invalid/v1", "claimed_model": "a", "runtime": {"retain_raw": True}}
            package = build_package(project, observations)
            session = DetectorSession(store, "retained", package, config, SECRET, transport=EchoModel())
            await session.run()
            saved = store.retained_exchanges("retained")
            self.assertEqual(saved["coverage"], {"attempts": 9, "retained": 9})
            self.assertEqual(len(saved["records"]), 9)
            self.assertNotIn(SECRET, str(saved))
            self.assertFalse(saved["records"][0]["exchange"]["body_available"])
            session = DetectorSession(store, "retained", package, config, SECRET, transport=EchoModel())
            await session.run()
            self.assertEqual(store.retained_exchanges("retained"), saved)

    async def test_retention_write_failure_rolls_back_result_and_stops_run(self):
        project, observations = fixture()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            store._write(lambda connection: connection.execute(
                "CREATE TRIGGER fixture_fail BEFORE INSERT ON retained_exchanges BEGIN SELECT RAISE(ABORT,'fixture'); END"))
            fake = EchoModel()
            session = DetectorSession(store, "failed", build_package(project, observations),
                {"base_url": "https://fixture.invalid/v1", "claimed_model": "a", "runtime": {"workers": 1, "retain_raw": True}},
                SECRET, transport=fake)
            report = await session.run()
            self.assertEqual(report["operational_status"], "error")
            self.assertEqual(fake.calls, 1)
            self.assertEqual(report["progress"]["successful"], 0)
            self.assertEqual(store.retained_exchanges("failed")["coverage"]["retained"], 0)


if __name__ == "__main__":
    unittest.main()
