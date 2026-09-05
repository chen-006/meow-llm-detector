from __future__ import annotations

import asyncio
from copy import deepcopy
import time

from .errors import AppError
from .utils import integer


class SingleRunSchedule:
    def __init__(self, store, launch):
        self.store, self.launch = store, launch
        self.task = None
        saved = store.document("schedule", "active")
        if saved:
            saved.update({"enabled": False, "next_due": None})
            store.put_document("schedule", "active", saved)

    async def start(self, value):
        if self.task and not self.task.done():
            raise AppError("schedule_already_active", status=409)
        config = deepcopy(value)
        config["interval_seconds"] = integer(config.get("interval_seconds", 3600), "interval_seconds", 60, 2592000)
        limit = config.get("round_limit")
        if limit is not None:
            integer(limit, "round_limit", 1, 10000)
        config.update({"enabled": True, "completed_rounds": 0, "next_due": time.time()})
        self.store.put_document("schedule", "active", config)
        self.task = asyncio.create_task(self._run(config))

    def status(self):
        saved = self.store.document("schedule", "active")
        if not saved:
            return None
        endpoint = saved.get("detection", {}).get("endpoint_snapshot", {})
        return {key: value for key, value in saved.items() if key != "detection"} | {
            "endpoint_name": endpoint.get("name"), "endpoint_url": endpoint.get("base_url"),
            "package_id": saved.get("detection", {}).get("package_id")}

    async def _run(self, config):
        try:
            while config["enabled"]:
                await asyncio.sleep(max(0, config["next_due"] - time.time()))
                if not self.store.document("schedule", "active")["enabled"]:
                    break
                config["next_due"] = None
                self.store.put_document("schedule", "active", config)
                session_id = await self.launch(config["detection"])
                config["last_session_id"] = session_id
                config["completed_rounds"] += 1
                if not self.store.document("schedule", "active")["enabled"]:
                    break
                if config.get("round_limit") and config["completed_rounds"] >= config["round_limit"]:
                    break
                config["next_due"] = time.time() + config["interval_seconds"]
                self.store.put_document("schedule", "active", config)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            config["error"] = exc.code if isinstance(exc, AppError) else "schedule_failed"
        finally:
            config.update({"enabled": False, "next_due": None})
            self.store.put_document("schedule", "active", config)

    def pause(self):
        config = self.store.document("schedule", "active")
        if config:
            config["enabled"] = False
            self.store.put_document("schedule", "active", config)
            if config.get("next_due") and self.task:
                self.task.cancel()
