from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .errors import AppError
from .normalizers import validate_normalizer
from .probability_model import SCORING_VERSION, fit_observations
from .utils import canonical_json, finite_number, integer, normalize_api_base_url, recognized_provider, sha256_text, strict_json_loads, utc_now

SCHEMA_VERSION = 1
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 65536
MODES = {"gpt": "responses", "claude": "messages", "chat": "chat"}
TIERS = ("low", "medium", "high")
DEFAULT_TIER_COUNTS = dict(zip(TIERS, (4, 10, 20)))
ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}")
PROFILES = {"standard", "claude-code"}


def identifier(value: Any, field: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise AppError("invalid_id", field=field)
    return value


def text(value: Any, field: str, *, empty: bool = False, limit: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode("utf-8")) > limit:
        raise AppError("invalid_text", field=field)
    return value


def content_hash(value: dict) -> str:
    return sha256_text(canonical_json({key: item for key, item in value.items() if key != "content_sha256"}))


def request_contract(cell: dict) -> dict:
    return {key: deepcopy(cell[key]) for key in ("system", "prompt", "history", "effort", "profile", "parameters")}


def cells_by_id(package: dict) -> dict[str, dict]:
    return {cell["id"]: {**cell, "probe_id": probe["id"], "family_id": probe["family_id"],
                         "normalizer": probe["normalizer"]}
            for probe in package["probes"] for cell in probe["cells"]}


def collection_contract(project: dict) -> str:
    """Exact sampling semantics, independent of display metadata and tier counts."""
    project = normalize_project(project, draft=True)
    return sha256_text(canonical_json({"mode": project["mode"],
        "models": [{"id": model["id"], "request_model": model["request_model"]} for model in project["models"]],
        "cells": {identity: {**request_contract(cell), "parameters": normalize_parameters(cell["parameters"], project["mode"]), "normalizer": cell["normalizer"]}
                  for identity, cell in cells_by_id(project).items()}}))


def normalize_parameters(value: dict, mode: str, *, _legacy=False) -> dict:
    parameters = deepcopy(value)
    if not isinstance(parameters, dict) or set(parameters) - {"max_output_tokens", "temperature", "top_p", "stop", "chat_token_field"}:
        raise AppError("unsupported_parameter")
    integer(parameters.get("max_output_tokens", 256), "max_output_tokens", 1, 65536)
    if "temperature" in parameters:
        finite_number(parameters["temperature"], "temperature", 0, 2)
    if "top_p" in parameters:
        finite_number(parameters["top_p"], "top_p", 0, 1)
    if "stop" in parameters:
        if not isinstance(parameters["stop"], list) or not 1 <= len(parameters["stop"]) <= 4:
            raise AppError("unsupported_parameter", field="stop")
        for stop in parameters["stop"]:
            text(stop, "stop", limit=256)
    if parameters.get("chat_token_field", "max_tokens") not in {"max_tokens", "max_completion_tokens"}:
        raise AppError("unsupported_parameter")
    if not _legacy:
        if (mode == "gpt" and "stop" in parameters) or (mode != "chat" and "chat_token_field" in parameters):
            raise AppError("unsupported_parameter")
        parameters.setdefault("max_output_tokens", 256)
        if mode == "chat":
            parameters.setdefault("chat_token_field", "max_tokens")
    return parameters


def normalize_project(value: Any, *, draft: bool = False, _legacy=False) -> dict:
    if not isinstance(value, dict) or value.get("mode") not in MODES:
        raise AppError("invalid_mode", field="mode")
    if any(key in value for key in ("juice", "gpt_checks", "anti_rewrite", "continuous")):
        raise AppError("legacy_configuration_requires_migration")
    project = {"mode": value["mode"]}
    project["id"] = identifier(value.get("id"))
    project["version"] = text(value.get("version", "0.1.0"), "version", limit=64)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", project["version"]):
        raise AppError("invalid_version", field="version")
    project["schema_version"] = SCHEMA_VERSION
    if type(value.get("schema_version", SCHEMA_VERSION)) is not int or value.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise AppError("unsupported_schema")
    models = value.get("models")
    if not isinstance(models, list) or not 2 <= len(models) <= 32:
        raise AppError("invalid_models", field="models")
    project["models"] = []
    for model in models:
        if not isinstance(model, dict):
            raise AppError("invalid_models")
        project["models"].append({"id": identifier(model.get("id"), "model_id"),
                                  "name": text(model.get("name", model.get("id")), "name", limit=512),
                                  "request_model": text(model.get("request_model", model.get("id")), "request_model", limit=256)})
    model_ids = [model["id"] for model in project["models"]]
    if len(set(model_ids)) != len(model_ids):
        raise AppError("duplicate_model")
    probes = value.get("probes")
    if not isinstance(probes, list) or not (0 if draft else 1) <= len(probes) <= 1000:
        raise AppError("invalid_probes")
    project["probes"] = []
    probe_ids, cell_ids = set(), set()
    for probe in probes:
        if not isinstance(probe, dict):
            raise AppError("invalid_probe")
        probe_id = identifier(probe.get("id"), "probe_id")
        if probe_id in probe_ids:
            raise AppError("duplicate_probe")
        probe_ids.add(probe_id)
        normalizer = deepcopy(probe.get("normalizer", {"id":"exact_trimmed_casefold", "parameters":{}}))
        validate_normalizer(normalizer)
        if not _legacy:
            normalizer.setdefault("parameters", {}).setdefault("max_length", 128)
        normalized = {"id": probe_id, "family_id": identifier(probe.get("family_id", probe_id), "family_id"),
                      "source_group": identifier(probe.get("source_group", probe.get("family_id", probe_id)), "source_group"),
                      "title": text(probe.get("title", probe_id), "title", limit=512),
                      "group": text(probe.get("group", "general"), "group", limit=128),
                      "normalizer": normalizer, "cells": []}
        source_cells = probe.get("cells", [{"id":probe_id, "prompt":probe.get("prompt")}])
        if not isinstance(source_cells, list) or not 1 <= len(source_cells) <= 16:
            raise AppError("invalid_cells")
        for cell in source_cells:
            if not isinstance(cell, dict):
                raise AppError("invalid_cell")
            cell_id = identifier(cell.get("id"), "cell_id")
            if cell_id in cell_ids:
                raise AppError("duplicate_cell")
            cell_ids.add(cell_id)
            profile = cell.get("profile", "claude-code" if project["mode"] == "claude" else "standard")
            if profile not in PROFILES or (profile == "claude-code" and project["mode"] != "claude"):
                raise AppError("invalid_profile")
            effort = cell.get("effort", "low")
            if effort != "low":
                raise AppError("invalid_effort")
            history = cell.get("history", [])
            if history != [] or cell.get("system", ".") != "." or "tools" in cell:
                raise AppError("unsupported_probe_context")
            parameters = normalize_parameters(cell.get("parameters", {}), project["mode"], _legacy=_legacy)
            item = {"id": cell_id, "system": text(cell.get("system", "."), "system", empty=True),
                "prompt": text(cell.get("prompt"), "prompt", empty=draft), "history": history, "effort": effort,
                "profile": profile, "parameters": parameters}
            total_text = len((item["system"] + item["prompt"]).encode("utf-8"))
            if total_text > MAX_TEXT_BYTES:
                raise AppError("cell_text_too_large")
            normalized["cells"].append(item)
        project["probes"].append(normalized)
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise AppError("invalid_metadata")
    project["metadata"] = {"name": text(metadata.get("name", project["id"]), "name", limit=512),
                           "description": text(metadata.get("description", ""), "description", empty=True, limit=8192),
                           "author": text(metadata.get("author", "local"), "author", limit=512),
                           "license": text(metadata.get("license", "unspecified"), "license", limit=256),
                           "language": text(metadata.get("language", "zh-CN"), "language", limit=64),
                           "created_at": text(metadata.get("created_at", utc_now()), "created_at", limit=64)}
    project["engine"] = {"minimum_version":"4.5.0", "scoring_version":"meow-fingerprint-v1" if _legacy else SCORING_VERSION,
                         "smoothing_alpha":0.5, "completion_ratio":0.9}
    accepted_engine = dict(project["engine"], scoring_version="meow-fingerprint-v1")
    if "engine" in value and value["engine"] not in (project["engine"], accepted_engine):
        raise AppError("unsupported_engine")
    tiers = value.get("tiers", {})
    if not isinstance(tiers, dict) or set(tiers) - set(TIERS):
        raise AppError("invalid_tiers")
    project["tiers"] = {}
    for tier, default in DEFAULT_TIER_COUNTS.items():
        if not isinstance(tiers.get(tier, {}), dict):
            raise AppError("invalid_tier", field=tier)
        counts = tiers.get(tier, {}).get("counts", {key:default for key in cell_ids})
        if not isinstance(counts, dict) or set(counts) != cell_ids:
            raise AppError("invalid_tier", field=tier)
        for key, count in counts.items():
            integer(count, key, 0, 1000)
        if not (0 if draft else 1) <= sum(counts.values()) <= 100000:
            raise AppError("invalid_tier", field=tier)
        thresholds = tiers.get(tier, {}).get("thresholds", {})
        if not isinstance(thresholds, dict) or (thresholds and set(thresholds) != set(model_ids)):
            raise AppError("invalid_thresholds")
        for model, threshold in thresholds.items():
            finite_number(threshold, model, 0, 1)
        project["tiers"][tier] = {"counts":counts, "thresholds":thresholds}
    return project


def validate_observations(observations: Any, models: list[str], cells: dict) -> None:
    if not isinstance(observations, dict) or set(observations) - set(cells):
        raise AppError("invalid_observations")
    for cell_id, data in observations.items():
        if not isinstance(data, dict) or set(data) - set(models):
            raise AppError("invalid_observations", field=cell_id)
        for model, windows in data.items():
            if not isinstance(windows, dict) or len(windows) > 1000:
                raise AppError("invalid_windows")
            for window_id, window in windows.items():
                identifier(window_id, "window_id")
                if not isinstance(window, dict) or not isinstance(window.get("counts"), dict):
                    raise AppError("invalid_counts")
                for category, count in window["counts"].items():
                    text(category, "category", limit=4096)
                    integer(count, "count", 0, 100000000)
                total = sum(window["counts"].values())
                completed = integer(window.get("completed", total), "completed", 0, 1000000000)
                planned = integer(window.get("planned", total), "planned", 0, 1000000000)
                if total != completed or completed > planned:
                    raise AppError("count_mismatch")


def build_package(project: dict, observations: dict, *, collection: dict | None = None,
                  calibration: dict | None = None, validation: dict | None = None, _legacy=False) -> dict:
    package = normalize_project(project, _legacy=_legacy)
    allowed = {"schema_version", "id", "version", "mode", "metadata", "engine", "models", "probes", "tiers"}
    package = {key:value for key, value in package.items() if key in allowed}
    models = [model["id"] for model in package["models"]]
    cells = cells_by_id(package)
    validate_observations(observations, models, cells)
    package["observations"] = deepcopy(observations)
    package["fitted"] = fit_observations(observations, models, cells, _legacy=_legacy)
    package["collection"] = deepcopy(collection or {"sources":[], "scope":"not_collected"})
    if not isinstance(package["collection"], dict) or not isinstance(package["collection"].get("sources"), list):
        raise AppError("invalid_collection")
    for source in package["collection"].get("sources", []):
        if not isinstance(source, dict):
            raise AppError("invalid_source")
        original = source.get("url")
        normalized = normalize_api_base_url(original, allow_insecure=True)
        if normalized != original:
            raise AppError("source_url_requires_sanitization")
        if source.get("provider") == "openrouter" and recognized_provider(original) != "openrouter":
            raise AppError("source_provider_mismatch")
    package["calibration"] = deepcopy(calibration or {"status":"not_calibrated"})
    package["validation"] = deepcopy(validation or {"status":"not_independently_validated"})
    if not isinstance(package["calibration"], dict) or not isinstance(package["validation"], dict):
        raise AppError("invalid_validation")
    package["content_sha256"] = content_hash(package)
    return package


def load_package(value: dict | str | bytes) -> dict:
    if isinstance(value, (str, bytes)):
        if len(value if isinstance(value, bytes) else value.encode("utf-8")) > MAX_PACKAGE_BYTES:
            raise AppError("package_too_large")
        value = strict_json_loads(value)
    if not isinstance(value, dict) or value.get("content_sha256") != content_hash(value):
        raise AppError("package_hash_mismatch")
    rebuilt = build_package(value, value.get("observations", {}), collection=value.get("collection"),
                            calibration=value.get("calibration"), validation=value.get("validation"),
                            _legacy=value.get("engine", {}).get("scoring_version") == "meow-fingerprint-v1")
    if canonical_json(rebuilt) != canonical_json(value):
        raise AppError("package_derived_data_mismatch")
    return rebuilt
