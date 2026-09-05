"""Small, offline reproductions of the sidebar audit; no paid calls or real keys."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from test_benchmarks import fixture
from test_presets_schedule import FakeVault
from gpt56_vnext.benchmark import build_package, collection_contract, load_package
from gpt56_vnext.catalog import BenchmarkCatalog
from gpt56_vnext.detector import DetectorSession, calibration_matches
from gpt56_vnext.errors import AppError, RequestError
from gpt56_vnext.generator import analyze_reports, calibrate_package, collection_jobs
from gpt56_vnext.normalizers import normalize_answer
from gpt56_vnext.probability_model import score_counts
from gpt56_vnext.proxies import ProxyDecision, http_client_options
from gpt56_vnext.selection import recommend
from gpt56_vnext.server import AppState
from gpt56_vnext.simulation import sample_matches, predictions
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.transport import AsyncTransport

SECRET = "synthetic-audit-secret"
BASE = "https://fixture.invalid/v1"


def response(answer="a"):
    return 'data: ' + json.dumps({"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": answer}]}]}}) + '\n\n'


def one_job_package():
    project, observations = fixture()
    project["mode"] = "gpt"
    for probe in project["probes"]:
        for cell in probe["cells"]:
            cell["parameters"].pop("chat_token_field", None)
    for tier in project["tiers"].values():
        tier["counts"] = {"ab": 1, "ac": 0, "bc": 0}
    return build_package(project, observations)


def transport(handler):
    value = AsyncTransport([SECRET], concurrency=8)
    value._clients[BASE] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return value


class AuditMathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project, observations = fixture()
        cls.package = calibrate_package(project, observations, {"sources": []},
            {"batches": dict.fromkeys(("low", "medium", "high"), 600)})

    def test_invalid_is_evidence_but_never_a_direction(self):
        package = self.package
        plan = package["tiers"]["low"]["counts"]
        counts = {cell: {"__INVALID_OUTPUT__": n} for cell, n in plan.items()}
        result = score_counts(package["fitted"], counts, plan, dict.fromkeys(("a", "b", "c"), .2), claimed_model="a")
        self.assertEqual(result["color"], "yellow")
        self.assertTrue(all(cell["valid"] == 0 for cell in result["cells"].values()))
        pool = {cell: {model: {"__INVALID_OUTPUT__": 100} for model in ("a", "b", "c")} for cell in plan}
        with self.assertRaisesRegex(AppError, "simulation_pool_empty"):
            sample_matches(package["fitted"], plan, "a", 12, np.random.default_rng(1), pool)
        counts = {cell: {"a": n} for cell, n in plan.items()}
        counts["ab"] = {"previously-unseen-answer": 3}
        self.assertNotIn("samples_incomplete", score_counts(package["fitted"], counts, plan)["reasons"])
        self.assertEqual(normalize_answer(" ", {"id": "fixed_enum", "parameters": {"values": {"A": "a"}}}), "__INVALID_OUTPUT__")

    def test_exact_request_contract_bound_but_names_are_not(self):
        self.assertTrue(calibration_matches(self.package, "low"))
        changes = [lambda p: p["probes"][0]["cells"][0].update(prompt="Different prompt"),
                   lambda p: p["probes"][0]["cells"][0]["parameters"].update(max_output_tokens=123),
                   lambda p: p["probes"][0].update(normalizer={"id": "exact_trimmed", "parameters": {}}),
                   lambda p: p.update(mode="gpt"),
                   lambda p: p["models"][0].update(request_model="different-alias")]
        for change in changes:
            altered = deepcopy(self.package)
            change(altered)
            if altered["mode"] == "gpt":
                for probe in altered["probes"]:
                    for cell in probe["cells"]:
                        cell["parameters"].pop("chat_token_field", None)
            rebuilt = load_package(build_package(altered, altered["observations"], calibration=altered["calibration"]))
            self.assertFalse(calibration_matches(rebuilt, "low"))
        renamed = deepcopy(self.package)
        renamed["metadata"]["name"] = "Renamed"
        renamed["version"] = "0.2.0"
        self.assertTrue(calibration_matches(build_package(renamed, renamed["observations"], calibration=renamed["calibration"]), "low"))

    def test_disabled_cells_have_no_recommendation_gain(self):
        project = deepcopy(self.package)
        project["tiers"]["low"]["counts"]["ab"] = 0
        result = recommend(project, project["fitted"])
        self.assertNotIn("ab", result["selected"])
        self.assertEqual(result["excluded"]["ab"], "disabled_in_tier")
        old = deepcopy(project)
        old["tiers"]["low"]["counts"]["ab"] = 3
        report = {"kind": "collection", "project": old, "observations": old["observations"], "collection": {"sources": [], "windows": {}}}
        current = analyze_reports([report], project)
        self.assertNotIn("ab", current["recommendation"]["selected"])

    def test_job_limit_is_checked_before_expansion(self):
        project, _ = fixture()
        project["models"] = [{"id": f"m{i}", "request_model": f"m{i}"} for i in range(32)]
        project["probes"] = [deepcopy(project["probes"][0]) for _ in range(1000)]
        for i, probe in enumerate(project["probes"]):
            probe["id"] = probe["cells"][0]["id"] = f"p{i}"
        with patch("gpt56_vnext.generator.random.Random", side_effect=AssertionError("expanded before limit")):
            with self.assertRaisesRegex(AppError, "collection_too_large"):
                collection_jobs(project, 1000, 1)

    def test_proxy_policy_and_catalog_cache(self):
        with patch("gpt56_vnext.proxies.resolve_proxy", return_value=ProxyDecision("http", "http://127.0.0.1:9999", "test")) as resolve:
            self.assertEqual(http_client_options("https://github.com")["proxy"], "http://127.0.0.1:9999")
            self.assertIsNone(http_client_options("http://127.0.0.1:8765")["proxy"])
            self.assertEqual(resolve.call_count, 1)
        with tempfile.TemporaryDirectory() as folder:
            catalog = BenchmarkCatalog(Path(folder))
            catalog.install_local(self.package)
            with patch("gpt56_vnext.catalog.load_package", wraps=load_package) as loader:
                catalog.local()
                first = catalog.get(self.package["id"], self.package["version"])
                first["metadata"]["name"] = "Mutation"
                self.assertNotEqual(catalog.get(self.package["id"], self.package["version"])["metadata"]["name"], "Mutation")
                self.assertEqual(loader.call_count, 1)


class AuditLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_cancel_does_not_consume_attempts(self):
        package = one_job_package()
        package["tiers"]["low"]["counts"]["ab"] = 20
        package = build_package(package, package["observations"])
        entered = asyncio.Event()
        async def blocked(_request):
            entered.set()
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            config = {"base_url": BASE, "claimed_model": "a", "runtime": {"workers": 8, "retries": 0, "retain_raw": True}}
            session = DetectorSession(store, "pause", package, config, SECRET, transport=transport(blocked))
            task = asyncio.create_task(session.run())
            await entered.wait()
            self.assertEqual(store.progress("pause")["http_attempts"], 1)
            session.stop()
            await task
            resumed = DetectorSession(store, "pause", package, config, SECRET,
                transport=transport(lambda request: httpx.Response(200, text=response())))
            report = await resumed.run()
            self.assertEqual(report["progress"]["successful"], 19)
            self.assertEqual(report["progress"]["http_attempts"], 20)
            self.assertEqual(store.retained_exchanges("pause")["coverage"], {"attempts": 20, "retained": 20})

    async def test_failed_http_and_parser_bodies_are_retained(self):
        cases = [(code, '{"error":{"message":"synthetic failure"}}') for code in (400, 401, 429, 500)]
        cases += [(200, 'data: {broken}\n\n')]
        for index, (status, body) in enumerate(cases):
            with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
                session = DetectorSession(store, str(index), one_job_package(),
                    {"base_url": BASE, "claimed_model": "a", "runtime": {"retries": 0, "retain_raw": True}}, SECRET,
                    transport=transport(lambda request: httpx.Response(status, text=body)))
                report = await session.run()
                saved = store.retained_exchanges(str(index))["records"][0]["exchange"]
                self.assertEqual(report["fingerprint"]["color"], "yellow")
                self.assertEqual(saved["response_utf8"], body)
                self.assertTrue(saved["request"])
                self.assertTrue(saved["body_complete"])
                self.assertNotIn(SECRET, json.dumps(saved))

    async def test_partial_response_and_credential_echo(self):
        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: partial'
                raise httpx.ReadError("synthetic")
        for body in (Partial(), None):
            client = transport(lambda request: httpx.Response(200, stream=body) if body else httpx.Response(200, text=response(SECRET)))
            try:
                with self.assertRaises(RequestError) as error:
                    await client.request("gpt", BASE, SECRET, "a", one_job_package()["probes"][0]["cells"][0])
                exchange = error.exception.exchange
                self.assertNotIn(SECRET, json.dumps(exchange))
                if body:
                    self.assertFalse(exchange["body_complete"])
                    self.assertEqual(exchange["response_utf8"], "data: partial")
                else:
                    self.assertTrue(exchange["redacted"])
            finally:
                await client.close()

    async def test_startup_recovery_and_light_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with SQLiteStateStore(root / "state.sqlite3") as store:
                DetectorSession(store, "interrupted", one_job_package(), {"base_url": BASE, "claimed_model": "a"}, SECRET)
                store.update_session_status("interrupted", "running")
            app = AppState(root, bundled=False)
            try:
                self.assertEqual(app.store.session("interrupted")["status"], "paused")
                self.assertEqual(app.store.progress("interrupted")["http_attempts"], 0)
                self.assertNotIn("packages", app.status())
                self.assertNotIn("projects", app.status())
            finally:
                app.close()

    async def test_stopping_collection_does_not_pause_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            app = AppState(folder, bundled=False)
            try:
                app.store.put_document("schedule", "active", {"enabled": True, "detection": {}})
                app.call(app.stop_run("collection"))
                self.assertTrue(app.schedule.status()["enabled"])
            finally:
                app.close()

    async def test_schedule_credentials_are_independent_of_preset(self):
        with tempfile.TemporaryDirectory() as folder:
            app = AppState(folder, bundled=False)
            app.presets.vault = FakeVault()
            async def no_calls(_body):
                return "synthetic-run"
            app.schedule.launch = no_calls
            try:
                package = one_job_package()
                app.catalog.install_local(package)
                preset = app.presets.save({"name": "fake", "mode": "gpt", "base_url": BASE, "model": "a"}, SECRET)
                app.call(app.start_schedule({"detection": {"endpoint_id": preset["id"], "package_id": package["id"],
                    "package_version": package["version"], "claimed_model": "a"}, "round_limit": 1}))
                app.call(asyncio.wait_for(app.schedule.task, 3))
                reference = app.store.document("schedule", "active")["detection"]["endpoint_snapshot"]["credential_ref"]
                app.presets.save(preset, "synthetic-new-secret")
                app.presets.delete(preset["id"])
                self.assertEqual(app.presets.vault.load(reference), SECRET)
                app.call(app.delete_schedule())
                self.assertEqual(app.presets.vault.values, {})
            finally:
                app.close()

    async def test_simulation_resume_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            project, observations = fixture()
            inputs = {"project": project, "observations": observations, "collection": {"sources": []},
                      "options": {"batches": dict.fromkeys(("low", "medium", "high"), 600)}}
            stop = threading.Event()
            stop.set()
            with self.assertRaisesRegex(AppError, "simulation_paused"):
                calibrate_package(project, observations, inputs["collection"], inputs["options"],
                    checkpoint_root=Path(folder) / "simulations" / "saved", cancel=stop)
            with SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
                store.put_document("simulation_input", "saved", inputs)
                store.put_document("simulation_task", "saved", {"id": "saved", "project_id": project["id"], "status": "running"})
            app = AppState(folder, bundled=False)
            try:
                self.assertEqual(app.store.document("simulation_task", "saved")["status"], "paused")
                result = app.call(app.simulate({"resume_id": "saved"}))
                app.call(asyncio.wait_for(app.calibration_task, 10))
                self.assertEqual(result["id"], "saved")
                self.assertEqual(app.store.document("simulation_task", "saved")["status"], "complete")
                self.assertTrue(calibration_matches(app.store.document("simulation_result", "saved"), "low"))
            finally:
                app.close()
            reopened = AppState(folder, bundled=False)
            try:
                self.assertEqual(reopened.store.documents("simulation_task")[0]["id"], "saved")
                self.assertIsNotNone(reopened.store.document("simulation_result", "saved"))
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
