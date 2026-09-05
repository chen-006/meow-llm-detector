"""Minimal third-audit regressions; synthetic credentials and loopback only."""
import asyncio
from copy import deepcopy
import gc
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings

import httpx

from test_benchmarks import fixture
from test_execution import EchoModel, SECRET
from test_audit_regressions import response, one_job_package
from gpt56_vnext.benchmark import cells_by_id, normalize_project
from gpt56_vnext.errors import AppError
from gpt56_vnext.generator import collection_config, collection_jobs, ProbeGeneratorSession
from gpt56_vnext.server import AppState, create_server
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.transport import AsyncTransport
from gpt56_vnext.utils import canonical_json, sha256_text


def old_collection(store, *, corrupt=False):
    project, _ = fixture()
    project["engine"]["scoring_version"] = "meow-fingerprint-v1"
    for probe in project["probes"]:
        probe["normalizer"]["parameters"].pop("max_length", None)
        for cell in probe["cells"]:
            cell["parameters"] = {}
    config = {"kind": "collection", "mode": "chat", "project": project, "base_url": "https://fixture.invalid/v1",
        "allow_insecure": False, "samples": 1, "window": 1, "runtime": {"workers": 2, "retries": 1, "timeout": 120, "retain_raw": False}}
    jobs = collection_jobs(project, 1, 1)
    cells = cells_by_id(project)
    for job in jobs:
        job["cell"] = deepcopy(cells[job["cell_id"]])
    if corrupt:
        jobs[0]["cell"]["prompt"] = "Different actual request"
    store.create_session(session_id="old", kind="collection", status="paused", config=config,
        config_hash=sha256_text(canonical_json(config)), official=False)
    store.freeze_jobs("old", 0, jobs)
    return project, config, jobs


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_collection_preserves_manifest_success_and_budget(self):
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "db.sqlite3") as store:
            project, config, jobs = old_collection(store)
            first = jobs[0]
            attempt = store.start_attempt("old", first["job_id"], 1)
            result = {k: v for k, v in first.items() if k != "cell"} | {"status": "ok", "answer": "a", "category": "a", "usage": {}}
            store.finish_attempt(attempt_id=attempt, status="ok", stage="complete", category="complete", retryable=False,
                http_status=200, safe_message="complete", final_result=result, final_job_status="ok")
            for number in (1, 2):
                attempt = store.start_attempt("old", jobs[1]["job_id"], number)
                store.finish_attempt(attempt_id=attempt, status="cancelled", stage="transport", category="user_paused",
                    retryable=True, http_status=None, safe_message="user_paused")
            before = store.frozen_jobs("old", 0)
            sender = EchoModel()
            runner = ProbeGeneratorSession(store, "old", project, config, SECRET, transport=sender)
            report = await runner.run()
            self.assertEqual(sender.calls, len(jobs) - 2)
            self.assertEqual(report["progress"]["http_attempts"], 10)
            self.assertEqual(report["progress"]["successful"], 8)
            self.assertEqual(store.session("old")["config"], config)
            self.assertEqual(store.frozen_jobs("old", 0), before)
            self.assertEqual(store.latest_results("old")[0]["category"], result["category"])
            self.assertEqual(runner.project, normalize_project(project))

    async def test_changed_legacy_request_is_explicitly_rejected(self):
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "db.sqlite3") as store:
            project, config, jobs = old_collection(store, corrupt=True)
            with self.assertRaisesRegex(AppError, "collection_resume_incompatible") as error:
                ProbeGeneratorSession(store, "old", project, config, SECRET, transport=EchoModel())
            self.assertEqual(error.exception.field, "frozen_request_mismatch")
            self.assertEqual(store.progress("old")["http_attempts"], 0)
            self.assertEqual(store.frozen_jobs("old", 0), jobs)

    async def test_explicit_default_port_uses_the_same_rate_gate(self):
        active = peak = 0
        async def handle(_request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.005)
            active -= 1
            return httpx.Response(200, text=response())
        bases = ["https://openrouter.ai/api/v1", "https://openrouter.ai:443/api/v1"]
        gates = {}
        senders = [AsyncTransport([SECRET], concurrency=8, gates=gates) for _ in bases]
        for sender, base in zip(senders, bases):
            sender._clients[base] = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            cell = one_job_package()["probes"][0]["cells"][0]
            await asyncio.gather(*(sender.request("gpt", base, SECRET, "a", cell) for sender, base in zip(senders, bases) for _ in range(8)))
            self.assertEqual(len(gates), 1)
            self.assertEqual(peak, 4)
        finally:
            for sender in senders: await sender.close()


class ServerTests(unittest.TestCase):
    def test_real_bind_error_and_repeated_close_keep_original_error(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                with self.assertRaises(OSError):
                    create_server(port=occupied.getsockname()[1], runs_root=folder)
                gc.collect()
            self.assertFalse(any(issubclass(w.category, RuntimeWarning) for w in recorded))
            app = AppState(folder, bundled=False)
            app.close()
            app.close()

    def test_progress_route_does_not_build_collection_report(self):
        with tempfile.TemporaryDirectory() as folder:
            server = create_server(runs_root=folder)
            project, _ = fixture()
            runner = ProbeGeneratorSession(server.state.store, "progress", project,
                {"base_url": "https://fixture.invalid/v1", "samples": 1}, SECRET, transport=EchoModel())
            server.state.active["progress"] = (runner, None)
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            try:
                with patch.object(runner, "report", side_effect=AssertionError("full report must not run")), \
                     patch.object(server.state.store, "latest_results", side_effect=AssertionError("no full result scan")):
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    client.request("GET", "/api/progress/progress", headers={"X-Meow-Token": server.token})
                    result = client.getresponse()
                    self.assertEqual(result.status, 200)
                    body = json.loads(result.read())
                    self.assertEqual(body["planned"], 9)
                    self.assertNotIn("fitted", body)
                    client.close()
            finally:
                thread.join(timeout=3)
                server.state.active.clear()
                server.server_close()

    def test_simulation_summary_retains_scope_and_does_not_invent_legacy_scope(self):
        package = one_job_package()
        package["calibration"]["tiers"] = {"low": {"sample_scope": "planned_count_of_valid_answers_per_cell",
            "target_denominator": "simulated_batches_of_valid_answers_not_all_http_runs"}, "high": {}}
        value = AppState.simulation_summary("s", package)
        self.assertEqual(value["tiers"]["low"]["sample_scope"], package["calibration"]["tiers"]["low"]["sample_scope"])
        self.assertEqual(value["tiers"]["low"]["target_denominator"], package["calibration"]["tiers"]["low"]["target_denominator"])
        self.assertEqual(value["tiers"]["high"]["target_denominator"], "not_declared")


if __name__ == "__main__": unittest.main()
