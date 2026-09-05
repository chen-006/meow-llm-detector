from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from .benchmark import DEFAULT_TIER_COUNTS, MAX_PACKAGE_BYTES, build_package, normalize_project
from .catalog import BenchmarkCatalog
from .selection import similar_probes
from .candidate_generation import generate_candidates, seed_summary
from .detector import DetectorSession
from .directory_lock import exclusive_directory
from .transport import AsyncTransport
from .errors import AppError, RequestError
from .executor import runtime_options
from .generator import COLLECTION_WINDOW_GAP_SECONDS, ProbeGeneratorSession, analyze_reports, calibrate_package, collection_contract, merge_windows, selected_project
from .presets import EndpointPresets, estimate_plan
from .schedule import SingleRunSchedule
from .store import SQLiteStateStore
from .updates import ProgramUpdates
from .utils import canonical_json, integer, normalize_api_base_url, recognized_provider, strict_json_loads

WEB_ROOT = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/assets/workbench.js": ("workbench.js", "text/javascript; charset=utf-8"),
          "/assets/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
          "/assets/style.css": ("style.css", "text/css; charset=utf-8")}


class AppState:
    def __init__(self, root, locale="zh-CN", *, bundled=True):
        if locale not in {"zh-CN", "en"}:
            raise AppError("unsupported_language")
        self.locale = locale
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as resources:
            resources.enter_context(exclusive_directory(self.root))
            self.store = SQLiteStateStore(self.root / "state.sqlite3")
            resources.callback(self.store.close)
            self.store.interrupt_active_sessions()
            self.catalog = BenchmarkCatalog(self.root / "benchmarks", Path(__file__).with_name("baselines") / "v4.5.0" if bundled else None)
            self.presets = EndpointPresets(self.store)
            self.updates = ProgramUpdates(self.root / "updates", self.store)
            self.active = {}
            self._closed = False
            self.rate_gates = {}
            self.calibration = None
            for task in self.store.documents("simulation_task"):
                if task["status"] == "running":
                    task["status"] = "paused"
                    self.store.put_document("simulation_task", task["id"], task)
            for identity in self.store.document_ids("simulation_result"):
                saved = self.store.document("simulation_task", identity)
                if not saved or "models" not in saved or any("sample_scope" not in value for value in saved.get("tiers", {}).values()):
                    package = self.store.document("simulation_result", identity)
                    self.store.put_document("simulation_task", identity, self.simulation_summary(identity, package))
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
            self.thread.start()
            resources.callback(self._stop_loop)
            self.schedule = SingleRunSchedule(self.store, self.scheduled_run)
            self._resources = resources.pop_all()

    def call(self, coroutine, timeout=30):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise

    async def generate(self, body):
        if getattr(self, "generation_task", None) and not self.generation_task.done():
            raise AppError("generation_already_running", status=409)
        options = body.get("options")
        key = body.get("key")
        if not isinstance(options, dict) or not isinstance(key, str) or not key:
            raise AppError("generation_connection_required")
        self.generation_task = asyncio.current_task()
        result = await generate_candidates(options, key, gates=self.rate_gates)
        self.store.put_document("generation", result["id"], result)
        return result

    def connection(self, body):
        key = body.get("key", "")
        if body.get("endpoint_id"):
            preset, saved = self.presets.connection(body["endpoint_id"])
            return {"base_url": preset["base_url"], "allow_insecure": preset["allow_insecure"],
                    "request_model": body.get("request_model") or preset["model"]}, key or saved
        return {key: body.get(key) for key in ("base_url", "allow_insecure", "request_model")}, key

    async def start_run(self, kind, body, *, connection_override=None):
        connection, key = connection_override or self.connection(body)
        if not isinstance(key, str) or not key:
            raise AppError("credential_required")
        identity = body.get("resume_id") or uuid.uuid4().hex
        if identity in self.active:
            raise AppError("session_already_running", status=409)
        if body.get("resume_id"):
            saved = self.store.session(identity)
            if not saved or saved["kind"] != kind:
                raise AppError("resume_not_found")
            config = saved["config"]
            if connection.get("base_url") != config["base_url"]:
                raise AppError("frozen_configuration_mismatch")
            source = config["package"] if kind == "detection" else config["project"]
            if kind == "detection":
                self.catalog.check_withdrawal(source)
        else:
            config = {**connection, "runtime": body.get("runtime", {})}
            if kind == "detection":
                source = self.catalog.get(body.get("package_id"), body.get("package_version"))
                config["benchmark_publisher"] = next(item["publisher"] for item in self.catalog.local() if item["id"] == source["id"] and item["version"] == source["version"])
                config.update({"tier": body.get("tier", "low"), "claimed_model": body.get("claimed_model")})
                if not config.get("request_model"):
                    config["request_model"] = body.get("claimed_model")
            else:
                source = normalize_project(body.get("project"))
                config.update({"window": body.get("window", 1), "samples": body.get("samples", 3), "probe_ids": body.get("probe_ids")})
                if config["window"] != 1:
                    prior = self.store.report(body.get("prior_session_id", ""))
                    if not prior or prior.get("kind") != "collection" or collection_contract(prior["project"]) != collection_contract(source) or prior["progress"]["status"] != "complete":
                        raise AppError("prior_collection_required")
                    previous = prior["collection"]["windows"].get(str(config["window"] - 1))
                    if not previous:
                        raise AppError("prior_collection_required")
                    ended = datetime.fromisoformat(previous["ended_at"])
                    if (datetime.now(timezone.utc) - ended).total_seconds() < COLLECTION_WINDOW_GAP_SECONDS:
                        raise AppError("collection_window_gap_not_elapsed")
        if kind == "collection" and any(runner.config["kind"] == "collection" and
                runner.config["project"]["id"] == source["id"] for runner, _task in self.active.values()):
            raise AppError("collection_already_running", status=409)
        options = runtime_options(config.get("runtime", {}))
        sender = AsyncTransport([key], timeout=options["timeout"], concurrency=options["workers"], gates=self.rate_gates)
        try:
            runner = (DetectorSession if kind == "detection" else ProbeGeneratorSession)(
                self.store, identity, source, config, key, transport=sender)
        except Exception:
            await sender.close()
            raise
        task = asyncio.create_task(runner.run())
        self.active[identity] = (runner, task)
        task.add_done_callback(lambda _future: self.active.pop(identity, None))
        return identity

    def collection_history(self, project):
        project = normalize_project(project, draft=True)
        contract = collection_contract(project)
        result = []
        for row in self.store.collection_sessions(project["id"]):
            config = strict_json_loads(row["config_json"])
            if collection_contract(config["project"]) != contract:
                continue
            result.append({"session_id": row["session_id"], "window": config["window"], "samples": config["samples"],
                "base_url": config["base_url"], "created_at": row["created_at"],
                "next_due": datetime.fromisoformat(row["updated_at"]).timestamp() + COLLECTION_WINDOW_GAP_SECONDS,
                **self.store.progress(row["session_id"])})
        return result

    async def scheduled_run(self, body):
        endpoint = body["endpoint_snapshot"]
        key = self.presets.vault.load(endpoint["credential_ref"])
        package = self.catalog.get(body["package_id"], body["package_version"])
        if package["content_sha256"] != body["package_sha256"]:
            raise AppError("scheduled_package_changed")
        connection = {"base_url": endpoint["base_url"], "allow_insecure": endpoint["allow_insecure"],
                      "request_model": body["request_model"]}
        identity = await self.start_run("detection", body, connection_override=(connection, key))
        await self.active[identity][1]
        return identity

    async def start_schedule(self, body):
        if self.schedule.task and not self.schedule.task.done():
            raise AppError("schedule_already_active", status=409)
        detection = body.get("detection", {})
        if not isinstance(detection, dict) or detection.get("key"):
            raise AppError("schedule_requires_saved_endpoint")
        endpoint = self.store.document("endpoint", detection.get("endpoint_id", ""))
        if not endpoint or not endpoint.get("credential_ref"):
            raise AppError("schedule_requires_saved_endpoint")
        package = self.catalog.get(detection.get("package_id"), detection.get("package_version"))
        if package["mode"] != endpoint["mode"]:
            raise AppError("endpoint_mode_mismatch")
        if detection.get("claimed_model") not in {model["id"] for model in package["models"]} or detection.get("tier", "low") not in package["tiers"]:
            raise AppError("invalid_detection_configuration")
        previous = self.store.document("schedule", "active")
        runtime = runtime_options(detection.get("runtime", {}))
        reference = uuid.uuid4().hex
        self.presets.vault.save(reference, self.presets.vault.load(endpoint["credential_ref"]))
        endpoint = {**endpoint, "credential_ref": reference, "schedule_owned": True}
        frozen = {"package_id": package["id"], "package_version": package["version"],
                  "package_sha256": package["content_sha256"], "endpoint_snapshot": endpoint,
                  "claimed_model": detection.get("claimed_model"),
                  "request_model": detection.get("request_model") or endpoint["model"],
                  "tier": detection.get("tier", "low"), "runtime": runtime}
        try:
            await self.schedule.start({"detection": frozen,
                "interval_seconds": body.get("interval_seconds", 3600), "round_limit": body.get("round_limit")})
        except Exception:
            self.presets.vault.delete(reference)
            raise
        old_endpoint = (previous or {}).get("detection", {}).get("endpoint_snapshot", {})
        if old_endpoint.get("schedule_owned"):
            self.presets.vault.delete(old_endpoint["credential_ref"])

    async def stop_run(self, identity):
        if identity in self.active:
            self.active[identity][0].stop()
        return {"stopping": identity}

    async def delete_schedule(self):
        if self.schedule.task and not self.schedule.task.done():
            raise AppError("schedule_already_active", status=409)
        saved = self.store.document("schedule", "active")
        endpoint = (saved or {}).get("detection", {}).get("endpoint_snapshot", {})
        if endpoint.get("schedule_owned"):
            self.presets.vault.delete(endpoint["credential_ref"])
        self.store.delete_document("schedule", "active")
        return {"deleted": True}

    def status(self):
        return {"sessions": self.store.session_summaries(), "active": list(self.active),
                "schedule": self.schedule.status(), "calibration": self.calibration}

    def snapshot(self):
        return {**self.status(), "version": "4.5.0", "brand": "meow LLM Detector", "packages": self.catalog.local(),
                "endpoints": self.presets.list(), "projects": self.store.documents("project"), "catalog": self.catalog.index()}

    @staticmethod
    def simulation_summary(identity, package):
        return {"id": identity, "status": "complete", "project_id": package["id"], "name": package["metadata"]["name"],
            "models": {model["id"]: model["name"] for model in package["models"]},
            "package_id": package["id"], "package_version": package["version"],
            "tiers": {tier: {"sample_scope": result.get("sample_scope", "not_declared"),
                "target_denominator": result.get("target_denominator", "not_declared"),
                **{key: value for key, value in result.items() if key in
                {"status", "thresholds", "correct_rates", "target", "selection_target"}}}
                for tier, result in package["calibration"]["tiers"].items()}}

    async def simulate(self, body):
        if self.calibration and self.calibration.get("status") == "running":
            raise AppError("simulation_already_running", status=409)
        identity = body.get("resume_id")
        if identity:
            task = self.store.document("simulation_task", identity)
            if not task:
                raise AppError("simulation_result_not_found", status=404)
            if task["status"] == "complete":
                return {"id": identity}
            inputs = self.store.document("simulation_input", identity)
            if not inputs:
                raise AppError("simulation_input_missing")
            project, observations, collection, options = (inputs[key] for key in ("project", "observations", "collection", "options"))
        else:
            reports = [self.store.report(identity) for identity in body.get("session_ids", [])]
            if any(not report or report.get("kind") != "collection" for report in reports):
                raise AppError("collection_required")
            project, observations, collection = merge_windows(reports)
            if body.get("project"):
                current = normalize_project(body["project"])
                if collection_contract(current) != collection_contract(project):
                    raise AppError("collection_contract_mismatch")
                project = current
            project = selected_project(project, body.get("selected", []), body.get("tiers"))
            options = body.get("options", {})
            identity = uuid.uuid4().hex
            self.store.put_document("simulation_input", identity, {
                "project": project, "observations": observations, "collection": collection, "options": options})
        cancel = threading.Event()
        self.calibration = {"id": identity, "project_id": project["id"], "name": project["metadata"]["name"],
                            "status": "running", "progress": {}}
        self.store.put_document("simulation_task", identity, self.calibration)
        self.calibration_cancel = cancel

        async def calculate():
            try:
                package = await asyncio.to_thread(calibrate_package, project, observations, collection, options,
                    checkpoint_root=self.root / "simulations" / identity, cancel=cancel,
                    progress=lambda progress: self.calibration.update({"progress": progress}))
                self.store.put_document("simulation_result", identity, package)
                self.calibration = self.simulation_summary(identity, package)
            except AppError as exc:
                self.calibration.update({"status": "paused" if exc.code == "simulation_paused" else "error", "error": exc.public()})
            except Exception:
                self.calibration.update({"status": "error", "error": {"code": "simulation_failed"}})
            finally:
                self.store.put_document("simulation_task", identity, self.calibration)
        self.calibration_task = asyncio.create_task(calculate())
        return {"id": identity}

    async def shutdown(self):
        self.schedule.pause()
        for runner, _task in tuple(self.active.values()):
            runner.stop()
        if self.active:
            await asyncio.gather(*(task for _runner, task in tuple(self.active.values())), return_exceptions=True)
        if self.schedule.task:
            await asyncio.gather(self.schedule.task, return_exceptions=True)
        if hasattr(self, "calibration_cancel"):
            self.calibration_cancel.set()
            await asyncio.gather(self.calibration_task, return_exceptions=True)
        generation = getattr(self, "generation_task", None)
        if generation and not generation.done():
            generation.cancel()
            await asyncio.gather(generation, return_exceptions=True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.call(self.shutdown())
        finally:
            self._resources.close()

    def _stop_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "meow"
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def _send(self, value, status=200, content_type="application/json; charset=utf-8", bootstrap=False):
        body = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if bootstrap:
            self.send_header("Set-Cookie", f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def _check(self, authenticated=True):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise AppError("invalid_host", status=403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise AppError("invalid_origin", status=403)
        if authenticated:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            candidate = self.headers.get("X-Meow-Token") or (cookie[self.server.cookie_name].value if self.server.cookie_name in cookie else "")
            if not secrets.compare_digest(candidate, self.server.token):
                raise AppError("session_token_required", status=403)

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            self._check(authenticated=path not in ASSETS and path != "/api/bootstrap")
            if path in ASSETS:
                name, content_type = ASSETS[path]
                self._send((WEB_ROOT / name).read_bytes(), content_type=content_type, bootstrap=path == "/")
            elif path == "/api/bootstrap":
                self._send({"token": self.server.token, "version": "4.5.0", "locale": self.server.state.locale, "seed_pool": seed_summary(), "tier_defaults": DEFAULT_TIER_COUNTS}, bootstrap=True)
            elif path == "/api/snapshot":
                self._send(self.server.state.snapshot())
            elif path == "/api/status":
                self._send(self.server.state.status())
            elif path == "/api/simulations":
                self._send(self.server.state.store.documents("simulation_task"))
            elif path.startswith("/api/simulation/"):
                identity = path.removeprefix("/api/simulation/")
                state = self.server.state
                task = state.calibration if (state.calibration or {}).get("id") == identity else state.store.document("simulation_task", identity)
                if not task:
                    raise AppError("simulation_result_not_found", status=404)
                self._send(task)
            elif path.startswith("/api/retention/"):
                identity = path.removeprefix("/api/retention/")
                if not self.server.state.store.session(identity):
                    raise AppError("session_not_found", status=404)
                after = integer(int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0]), "after", 0, 2 ** 63 - 1)
                self._send(self.server.state.store.retained_exchanges(identity, after))
            elif path.startswith("/api/report/"):
                identity = path.removeprefix("/api/report/")
                report = self.server.state.store.report(identity)
                if identity in self.server.state.active:
                    report = self.server.state.active[identity][0].report()
                if not report:
                    raise AppError("report_not_found", status=404)
                self._send(report)
            elif path.startswith("/api/progress/"):
                try:
                    self._send(self.server.state.store.progress(path.removeprefix("/api/progress/")))
                except KeyError:
                    raise AppError("session_not_found", status=404)
            else:
                raise AppError("not_found", status=404)
        except AppError as exc:
            self._send({"error": exc.public()}, exc.status)
        except RequestError as exc:
            self._send({"error": exc.public()}, 502)
        except (OSError, ValueError):
            self._send({"error": {"code": "not_found"}}, 404)

    def do_POST(self):
        try:
            raw_length = self.headers.get("Content-Length", "")
            if not raw_length.isascii() or not raw_length.isdecimal() or not 0 < int(raw_length) <= MAX_PACKAGE_BYTES:
                raise AppError("invalid_content_length", status=413)
            self.connection.settimeout(10)
            raw = self.rfile.read(int(raw_length))
            if len(raw) != int(raw_length):
                raise AppError("incomplete_request")
            self._check()
            body = strict_json_loads(raw)
            if not isinstance(body, dict):
                raise AppError("invalid_request")
            path = urlsplit(self.path).path
            state = self.server.state
            if path == "/api/run/start":
                result = {"session_id": state.call(state.start_run("detection", body))}
            elif path == "/api/collection/start":
                result = {"session_id": state.call(state.start_run("collection", body))}
            elif path == "/api/collection/profile":
                provider = recognized_provider(normalize_api_base_url(body.get("base_url", "")))
                result = {"provider": provider, "max_in_flight": 4 if provider == "openrouter" else None}
            elif path == "/api/candidates/generate":
                result = state.call(state.generate(body), timeout=130)
            elif path == "/api/run/stop":
                result = state.call(state.stop_run(body.get("session_id")))
            elif path == "/api/run/estimate":
                package = state.catalog.get(body.get("package_id"), body.get("package_version"))
                result = estimate_plan(package, body.get("tier", "low"), runtime_options(body.get("runtime", {}))["retries"])
            elif path == "/api/project/save":
                result = normalize_project(body.get("project"), draft=True)
                selected = body.get("selected", body.get("project", {}).get("selected"))
                if selected is not None:
                    if not isinstance(selected, list) or len(set(selected)) != len(selected) or set(selected) - {probe["id"] for probe in result["probes"]}:
                        raise AppError("invalid_selection")
                    result["selected"] = selected
                state.store.put_document("project", result["id"], result)
            elif path == "/api/project/collections":
                result = state.collection_history(body.get("project"))
            elif path == "/api/project/similar":
                result = similar_probes(normalize_project(body.get("project"), draft=True))
            elif path == "/api/project/delete":
                state.store.delete_document("project", body.get("id"))
                result = {"deleted": True}
            elif path == "/api/endpoint/save":
                result = state.presets.save(body.get("preset"), body.get("key"))
            elif path == "/api/endpoint/delete":
                state.presets.delete(body.get("id"))
                result = {"deleted": True}
            elif path == "/api/catalog/refresh":
                result = state.call(state.catalog.refresh(), timeout=70)
            elif path == "/api/program/check-update":
                result = state.call(state.updates.check(body.get("locale", state.locale)), timeout=40)
            elif path == "/api/program/download-update":
                if body.get("confirmed") is not True:
                    raise AppError("update_download_confirmation_required")
                result = state.call(state.updates.download(body.get("version"), body.get("locale", state.locale)), timeout=310)
            elif path == "/api/catalog/install":
                result = state.call(state.catalog.install(body.get("id"), body.get("version")), timeout=40)
            elif path == "/api/package/import":
                result = state.catalog.install_local(body.get("package"))
            elif path == "/api/package/export":
                result = state.catalog.get(body.get("id"), body.get("version"))
            elif path == "/api/selection":
                reports = [state.store.report(identity) for identity in body.get("session_ids", [])]
                result = analyze_reports(reports, body.get("project"), body.get("options"))
            elif path == "/api/simulation/start":
                result = state.call(state.simulate(body))
            elif path in {"/api/simulation/export", "/api/simulation/install"}:
                package = state.store.document("simulation_result", body.get("id"))
                if not package:
                    raise AppError("simulation_result_not_found", status=404)
                if body.get("version"):
                    package["version"] = body["version"]
                    package = build_package(package, package["observations"], collection=package["collection"],
                        calibration=package["calibration"], validation=package["validation"])
                result = state.catalog.install_local(package) if path.endswith("install") else package
            elif path == "/api/simulation/stop":
                if hasattr(state, "calibration_cancel") and body.get("id") == (state.calibration or {}).get("id"):
                    state.calibration_cancel.set()
                result = {"stopping": True}
            elif path == "/api/schedule/start":
                state.call(state.start_schedule(body))
                result = {"started": True}
            elif path == "/api/schedule/pause":
                state.loop.call_soon_threadsafe(state.schedule.pause)
                result = {"paused": True}
            elif path == "/api/schedule/delete":
                result = state.call(state.delete_schedule())
            else:
                raise AppError("not_found", status=404)
            self._send(result)
        except AppError as exc:
            self._send({"error": exc.public()}, exc.status)
        except RequestError as exc:
            self._send({"error": exc.public()}, 502)
        except (TimeoutError, FutureTimeout):
            self._send({"error": {"code": "operation_timeout"}}, 504)
        except Exception:
            self._send({"error": {"code": "operation_failed"}}, 500)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state):
        self.state, self.token = state, secrets.token_urlsafe(32)
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)
        self.cookie_name = f"meow_session_{self.server_port}"

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_close(self):
        super().server_close()
        self.state.close()


def create_server(*, port=0, runs_root=None, locale="zh-CN"):
    root = Path(runs_root) if runs_root else Path(__file__).resolve().parent.parent / "meow_runs"
    state = AppState(root, locale)
    try:
        return AppServer(("127.0.0.1", port), state)
    except Exception:
        state.close()
        raise
