from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import math
import random
from statistics import median

from .benchmark import build_package, cells_by_id, collection_contract, normalize_project, TIERS
from .errors import AppError
from .executor import FrozenRun, runtime_options
from .probability_model import fit_observations
from .selection import compare_selection, recommend
from .simulation import calibrate
from .transport import build_payload
from .utils import integer, recognized_provider, utc_now

COLLECTION_WINDOW_GAP_SECONDS = 60


def collection_jobs(project: dict, samples: int, window: int, seed: int = 45001, probe_ids=None) -> list[dict]:
    integer(samples, "samples_per_model_per_cell", 1, 1000)
    cells = cells_by_id(project)
    if probe_ids is not None:
        if not isinstance(probe_ids, list) or not probe_ids or len(set(probe_ids)) != len(probe_ids) or set(probe_ids) - {p["id"] for p in project["probes"]}:
            raise AppError("invalid_selection")
        cells = {identity: cell for identity, cell in cells.items() if cell["probe_id"] in probe_ids}
    if len(cells) * len(project["models"]) * samples > 100000:
        raise AppError("collection_too_large")
    jobs = []
    rng = random.Random(seed + window)
    for repetition in range(samples):
        block = [{"job_id": f"{window}:{identity}:{model['id']}:{repetition}", "probe_id": cell["probe_id"],
                  "cell_id": identity, "model": model["request_model"],
                  "candidate_model": model["id"], "window": window}
                 for identity, cell in cells.items() for model in project["models"]]
        rng.shuffle(block)
        jobs.extend(block)
    return jobs


def collection_config(project: dict, config: dict) -> dict:
    project = normalize_project(project)
    return {"kind": "collection", "mode": project["mode"], "project": project,
                       "base_url": config.get("base_url"), "allow_insecure": config.get("allow_insecure") is True,
                       "samples": integer(config.get("samples", 3), "samples", 1, 1000),
                       "window": integer(config.get("window", 1), "window", 1, 1000),
                       "probe_ids": config.get("probe_ids"),
                       "runtime": runtime_options(config.get("runtime", {}))}


class ProbeGeneratorSession:
    def __init__(self, store, session_id: str, project: dict, config: dict, key: str, *, transport=None):
        self.store, self.session_id = store, session_id
        saved = store.session(session_id)
        try:
            current = collection_config(project, config)
            self.project = current["project"]
            cells = cells_by_id(self.project)
            jobs = collection_jobs(self.project, current["samples"], current["window"], probe_ids=current["probe_ids"])
            if saved:
                original = saved["config"]
                if (saved["kind"] != "collection" or original.get("kind") != "collection" or
                        original.get("mode") != current["mode"] or collection_config(original["project"], original) != current):
                    raise AppError("frozen_configuration_mismatch")
                frozen = store.frozen_jobs(session_id, 0)
                if [{k: v for k, v in job.items() if k != "cell"} for job in frozen] != jobs:
                    raise AppError("frozen_jobs_mismatch")
                for job in frozen:
                    if "cell" not in job:
                        continue
                    old, cell = job["cell"], cells[job["cell_id"]]
                    normalizer = {"id": old["normalizer"]["id"], "parameters": {"max_length": 128, **old["normalizer"].get("parameters", {})}}
                    if (normalizer != cell["normalizer"] or
                            build_payload(current["mode"], job["model"], old) != build_payload(current["mode"], job["model"], cell)):
                        raise AppError("frozen_request_mismatch")
                # Read adapter only: the literal config and manifests remain immutable.
                self.config, jobs = original, frozen
            else:
                self.config = current
        except (AppError, KeyError, TypeError, ValueError) as exc:
            if saved:
                raise AppError("collection_resume_incompatible", field=getattr(exc, "code", "invalid_saved_configuration")) from exc
            raise
        self.runner = FrozenRun(store, session_id, self.config, jobs, key, transport=transport, cells=cells)

    async def run(self) -> dict:
        await self.runner.run()
        report = self.report()
        self.store.save_report(self.session_id, report)
        return report

    def stop(self):
        self.runner.stop()

    def report(self):
        results = self.store.latest_results(self.session_id)
        observations = observations_from_rows(results)
        fitted = fit_observations(observations, [model["id"] for model in self.project["models"]], cells_by_id(self.project))
        source = {"url": self.runner.base_url}
        if recognized_provider(self.runner.base_url) == "openrouter":
            source.update(provider="openrouter", rate_limit_profile="openrouter_headers_v1",
                          collection_concurrency_cap=4, provenance_scope="collection_endpoint_not_model_attestation")
        return {"schema_version": 1, "session_id": self.session_id, "project": self.project,
                "kind": "collection", "progress": self.store.progress(self.session_id),
                "observations": observations, "fitted": fitted, "results": results,
                "collection": {"sources": [source], "windows": {
                    str(self.config["window"]): {"started_at": self.store.session(self.session_id)["created_at"],
                                                "ended_at": utc_now(), "session_id": self.session_id}}}}


def observations_from_rows(rows: list[dict]) -> dict:
    observations = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        window = observations[row["cell_id"]][row["candidate_model"]].setdefault(str(row.get("window", 1)),
            {"counts": {}, "planned": 0, "completed": 0, "errors": 0, "pending": 0})
        window["planned"] += 1
        if row["status"] == "ok":
            window["completed"] += 1
            category = row["category"]
            window["counts"][category] = window["counts"].get(category, 0) + 1
        elif row["status"] == "pending":
            window["pending"] += 1
        else:
            window["errors"] += 1
    return dict(observations)


def merge_windows(reports: list[dict]) -> tuple[dict, dict, dict]:
    if not reports:
        raise AppError("collection_required")
    project = reports[0]["project"]
    observations, windows, sources = {}, {}, []
    for report in reports:
        if collection_contract(report["project"]) != collection_contract(project):
            raise AppError("collection_contract_mismatch")
        for identity, models in report["observations"].items():
            for model, values in models.items():
                destination = observations.setdefault(identity, {}).setdefault(model, {})
                if set(destination) & set(values):
                    raise AppError("duplicate_collection_window")
                destination.update(deepcopy(values))
        windows.update(report["collection"]["windows"])
        for source in report["collection"]["sources"]:
            if source not in sources:
                sources.append(source)
    return project, observations, {"sources": sources, "windows": windows}


def analyze_selection(project: dict, observations: dict, options: dict | None = None, measured_costs=None) -> dict:
    fitted = fit_observations(observations, [model["id"] for model in project["models"]], cells_by_id(project))
    options = options or {}
    recommendation = recommend(project, fitted,
            maximum=options.get("maximum", 7), request_budget=options.get("request_budget", 100),
            locked=options.get("locked", []), measured_costs=measured_costs)
    result = {"fitted": fitted, "recommendation": recommendation}
    if options.get("preview") is True:
        result["preview"] = compare_selection(project, fitted, recommendation,
            locked=options.get("locked", []), measured_costs=measured_costs)
    return result


def analyze_reports(reports: list[dict], current=None, options=None) -> dict:
    if any(not report or report.get("kind") != "collection" for report in reports):
        raise AppError("collection_required")
    project, observations, collection = merge_windows(reports)
    if current is not None:
        current = normalize_project(current)
        if collection_contract(current) != collection_contract(project):
            raise AppError("collection_contract_mismatch")
        project = current
    costs = defaultdict(list)
    for report in reports:
        for row in report.get("results", []):
            cost = (row.get("usage") or {}).get("cost")
            if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                costs[row["probe_id"]].append(cost)
    return {"project": project, "observations": observations, "collection": collection,
            **analyze_selection(project, observations, options, {identity: median(values) for identity, values in costs.items()})}


def selected_project(project: dict, selected: list[str], tiers: dict | None = None) -> dict:
    if not selected or len(set(selected)) != len(selected) or set(selected) - {probe["id"] for probe in project["probes"]}:
        raise AppError("invalid_selection")
    result = deepcopy(project)
    result["probes"] = [probe for probe in result["probes"] if probe["id"] in selected]
    cells = cells_by_id(result)
    result["tiers"] = tiers or {tier: {"counts": {identity: count for identity, count in value["counts"].items() if identity in cells},
                                       "thresholds": {}} for tier, value in project["tiers"].items()}
    return normalize_project(result)


def calibrate_package(project: dict, observations: dict, collection: dict, options: dict,
                      *, checkpoint_root=None, cancel=None, progress=None) -> dict:
    cells = cells_by_id(project)
    observations = {identity: value for identity, value in observations.items() if identity in cells}
    package = build_package(project, observations, collection=collection)
    results = {}
    for index, tier in enumerate(TIERS):
        result = calibrate(package["fitted"], package["tiers"][tier]["counts"],
            request_signature=collection_contract(package),
            total_batches=options.get("batches", {}).get(tier, 8000), target=options.get("target", 0.99),
            seed=options.get("seed", 45001) + index,
            selection_target=options.get("selection_target"),
            checkpoint=checkpoint_root / (tier + ".json") if checkpoint_root else None,
            cancel=cancel, progress=(lambda value, tier=tier: progress({"tier": tier, **value})) if progress else None)
        results[tier] = result
        package["tiers"][tier]["thresholds"] = result["thresholds"]
    return build_package(package, observations, collection=collection,
                         calibration={"status": "complete", "tiers": results})
