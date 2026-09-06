from __future__ import annotations

import asyncio

from .errors import AppError, RequestError
from .benchmark import cells_by_id
from .normalizers import normalize_answer
from .retention import exchange_record
from .security import SecretGuard
from .store import SQLiteStateStore
from .transport import AsyncTransport
from .utils import canonical_json, integer, normalize_api_base_url, sha256_text, utc_now


def runtime_options(value: dict) -> dict:
    if not isinstance(value, dict):
        raise AppError("invalid_runtime_options")
    if type(value.get("retain_raw", False)) is not bool:
        raise AppError("invalid_retention_option")
    return {"workers": integer(value.get("workers", 8), "workers", 1, 32),
            "retries": integer(value.get("retries", 2), "retries", 0, 10),
            "timeout": integer(value.get("timeout", 120), "timeout", 10, 600),
            "retain_raw": value.get("retain_raw", False)}


class FrozenRun:
    """One finite run. Scheduling and collection-window decisions live outside it."""
    def __init__(self, store: SQLiteStateStore, session_id: str, config: dict,
                 jobs: list[dict], key: str, *, transport=None, cells=None):
        self.store, self.session_id, self.config = store, session_id, config
        self.cells = cells if cells is not None else cells_by_id(config["package"] if config["kind"] == "detection" else config["project"])
        self.guard = SecretGuard([key])
        self.guard.check(config, code="credential_in_configuration")
        for job in jobs:
            self.guard.check(job, code="credential_in_configuration")
        self.key = key
        self.options = runtime_options(config.get("runtime", {}))
        self.base_url = normalize_api_base_url(config["base_url"], allow_insecure=config.get("allow_insecure") is True)
        self.transport = transport or AsyncTransport([key], timeout=self.options["timeout"], concurrency=self.options["workers"])
        self.active = set()
        self.stopped = False
        self.failure = None
        self.store.create_session(session_id=session_id, kind=config["kind"], status="prepared", config=config,
            config_hash=sha256_text(canonical_json(config)), official=False,
            claimed_model=config.get("claimed_model"), request_model=config.get("request_model"), safe_endpoint=self.base_url)
        self.store.freeze_jobs(session_id, 0, jobs)

    def stop(self) -> None:
        self.stopped = True
        self.store.request_stop(self.session_id)
        for task in tuple(self.active):
            task.cancel()

    async def _execute(self, job: dict) -> None:
        cell = self.cells[job["cell_id"]]
        limit = self.options["retries"] + 1
        while not self.stopped:
            attempt_number = self.store.next_attempt_number(self.session_id, job["job_id"])
            if attempt_number > limit:
                return
            attempt_id, started = None, None

            def dispatched():
                nonlocal attempt_id, started
                attempt_id = self.store.start_attempt(self.session_id, job["job_id"], attempt_number, max_attempts=limit)
                started = utc_now()

            try:
                response = await self.transport.request(self.config["mode"], self.base_url, self.key, job["model"], cell,
                    allow_insecure=self.config.get("allow_insecure") is True, on_dispatch=dispatched)
                self.guard.check(response)
                result = {"job_id": job["job_id"], "probe_id": job["probe_id"], "cell_id": job["cell_id"],
                    "model": job["model"], "candidate_model": job.get("candidate_model"),
                    "window": job.get("window", 1), "status": "ok", "started_at": started, "completed_at": utc_now(),
                    "answer": response["answer"], "category": normalize_answer(response["answer"], cell["normalizer"]),
                    **{key: response.get(key) for key in ("usage", "http_status", "elapsed_ms", "response_id", "provider")}}
                self.guard.check(result)
                if result["category"] == "__INVALID_OUTPUT__" and attempt_number < limit:
                    error = RequestError("invalid_answer", status=response.get("http_status"),
                                         evidence={"category": "__INVALID_OUTPUT__"})
                    error.exchange = response
                    raise error
                if result["category"] == "__INVALID_OUTPUT__":
                    result["error"] = RequestError("invalid_answer", retryable=False).public()
                self.store.finish_attempt(attempt_id=attempt_id, status="ok", stage="normalization" if result.get("error") else "complete",
                    category="invalid_answer" if result.get("error") else "complete",
                    retryable=False, http_status=response["http_status"], safe_message="invalid_answer" if result.get("error") else "complete",
                    final_result=result, final_job_status="ok",
                    exchange=exchange_record({**response, "error": result.get("error")}, self.guard) if self.options["retain_raw"] else None)
                return
            except asyncio.CancelledError as exc:
                if attempt_id is not None:
                    self.store.finish_attempt(attempt_id=attempt_id, status="cancelled", stage="transport", category="user_paused",
                        retryable=True, http_status=None, safe_message="user_paused",
                        exchange=exchange_record({**getattr(exc, "exchange", {}), "error": {"code": "user_paused"}}, self.guard)
                            if self.options["retain_raw"] else None)
                raise
            except RequestError as exc:
                if attempt_id is None:
                    raise
                self.guard.check(exc.evidence)
                safety_stop = exc.code in ("credential_echo", "credential_in_configuration")
                exc.retryable = not safety_stop
                retry = not safety_stop and attempt_number < limit and not self.stopped
                result = {"job_id": job["job_id"], "probe_id": job["probe_id"], "cell_id": job["cell_id"],
                          "model": job["model"], "candidate_model": job.get("candidate_model"), "window": job.get("window", 1),
                          "status": "error", "error": exc.public(), "evidence": self.guard.redact(exc.evidence),
                          "started_at": started, "completed_at": utc_now()}
                self.store.append_event(self.session_id, "attempt_decision", payload={"job_id": job["job_id"],
                    "attempt": attempt_number, "retryable": exc.retryable, "will_retry": retry, "code": exc.code})
                self.store.finish_attempt(attempt_id=attempt_id, status="error", stage="transport", category=exc.code,
                    retryable=exc.retryable, http_status=exc.status, safe_message=exc.code,
                    final_result=result if not retry else None, final_job_status="error" if not retry else None,
                    exchange=exchange_record({**getattr(exc, "exchange", {}), "error": exc.public()}, self.guard)
                        if self.options["retain_raw"] else None)
                if safety_stop:
                    self.failure = exc.code
                    self.stop()
                if not retry:
                    return
                await asyncio.sleep(min(2 ** attempt_number, 8))

    async def run(self) -> dict:
        limit = self.options["retries"] + 1
        self.store.reconcile_incomplete_attempts(self.session_id, limit)
        self.store.update_session_status(self.session_id, "running", clear_stop=True)
        queue = asyncio.Queue()
        pending = self.store.pending_jobs(self.session_id, max_attempts=limit)
        if self.config['kind'] == 'detection':
            pending.sort(key=lambda job: (int(job['job_id'].rsplit(':', 1)[1]), job['cell_id']))
        for job in pending:
            queue.put_nowait(job)

        async def worker():
            while not self.stopped and not queue.empty():
                await self._execute(queue.get_nowait())

        self.active = {asyncio.create_task(worker()) for _ in range(self.options["workers"])}
        try:
            await asyncio.gather(*self.active)
        except asyncio.CancelledError:
            self.stopped = True
        except Exception as exc:
            self.failure = exc.code if isinstance(exc, (AppError, RequestError)) else "runtime_failure"
            self.stopped = True
            self.store.append_event(self.session_id, "run_error", payload={"code": self.failure})
        finally:
            for task in self.active:
                task.cancel()
            await asyncio.gather(*self.active, return_exceptions=True)
            self.active.clear()
            await self.transport.close()
            self.key = ""
            self.guard = SecretGuard()
            self.store.update_session_status(self.session_id, "error" if self.failure else "paused" if self.stopped else "complete")
        return self.store.progress(self.session_id)
