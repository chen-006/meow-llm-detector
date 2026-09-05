from __future__ import annotations

from collections import Counter, defaultdict

from .benchmark import TIERS, cells_by_id, collection_contract, load_package
from .errors import AppError
from .executor import FrozenRun, runtime_options
from .probability_model import SCORING_VERSION, score_counts
from .simulation import simulation_contract
from .utils import utc_now


def build_single_jobs(package: dict, tier: str, request_model: str) -> list[dict]:
    cells = cells_by_id(package)
    return [{"job_id": f"{identity}:{index}", "probe_id": cells[identity]["probe_id"],
             "cell_id": identity, "model": request_model}
            for identity, count in sorted(package["tiers"][tier]["counts"].items()) for index in range(count)]


def calibration_matches(package: dict, tier: str) -> bool:
    if package["engine"]["scoring_version"] != SCORING_VERSION:
        return False
    result = package["calibration"].get("tiers", {}).get(tier, {})
    if result.get("status") != "target_met" or result.get("thresholds") != package["tiers"][tier]["thresholds"]:
        return False
    if not all(key in result for key in ("total_batches", "target", "seed", "contract")):
        return False
    expected = simulation_contract(package["fitted"], package["tiers"][tier]["counts"],
        package["calibration"].get("simulation_pool"), result["total_batches"], result["target"], result["seed"], result.get("selection_target"), collection_contract(package))
    return result["contract"] == expected


class DetectorSession:
    def __init__(self, store, session_id: str, package: dict, config: dict, key: str, *, transport=None):
        self.store, self.session_id = store, session_id
        self.package = load_package(package)
        if self.package["engine"]["scoring_version"] != SCORING_VERSION:
            raise AppError("benchmark_recalibration_required")
        tier = config.get("tier", "low")
        models = [model["id"] for model in self.package["models"]]
        claimed = config.get("claimed_model")
        alias = config.get("request_model", claimed)
        if tier not in TIERS or claimed not in models or not isinstance(alias, str) or not alias.strip() or len(alias) > 256:
            raise AppError("invalid_detection_configuration")
        self.config = {"kind": "detection", "mode": self.package["mode"], "package": self.package,
                       "tier": tier, "claimed_model": claimed, "request_model": alias,
                       "base_url": config.get("base_url"), "allow_insecure": config.get("allow_insecure") is True,
                       "runtime": runtime_options(config.get("runtime", {}))}
        self.config["benchmark_publisher"] = config.get("benchmark_publisher", "local")
        jobs = build_single_jobs(self.package, tier, alias)
        self.runner = FrozenRun(store, session_id, self.config, jobs, key, transport=transport)

    async def run(self) -> dict:
        await self.runner.run()
        report = self.report()
        self.store.save_report(self.session_id, report)
        return report

    def stop(self) -> None:
        self.runner.stop()

    def report(self) -> dict:
        rows = self.store.latest_results(self.session_id)
        counts = defaultdict(Counter)
        for row in rows:
            if row["status"] == "ok":
                counts[row["cell_id"]][row["category"]] += 1
        tier = self.package["tiers"][self.config["tier"]]
        fingerprint = score_counts(self.package["fitted"], dict(counts), tier["counts"], tier["thresholds"],
                                   calibrated=calibration_matches(self.package, self.config["tier"]),
                                   claimed_model=self.config["claimed_model"])
        progress = self.store.progress(self.session_id)
        if self.runner.failure:
            fingerprint.update({"verdict": "insufficient", "color": "yellow", "model": None,
                                "reasons": sorted(set([*fingerprint["reasons"], self.runner.failure]))})
        return {"schema_version": 1, "product": "meow LLM Detector", "version": "4.5.0",
                "session_id": self.session_id, "updated_at": utc_now(), "operational_status": progress["status"],
                "fingerprint": fingerprint, "progress": progress, "mode": self.package["mode"],
                "tier": self.config["tier"], "claimed_model": self.config["claimed_model"],
                "request_model": self.config["request_model"], "endpoint": self.runner.base_url,
                "benchmark": {"id": self.package["id"], "version": self.package["version"],
                              "publisher": self.config["benchmark_publisher"],
                              "content_sha256": self.package["content_sha256"], "collection": self.package["collection"],
                              "validation": self.package["validation"]},
                "results": rows, "events": self.store.events(self.session_id)}
