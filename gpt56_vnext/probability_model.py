from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
import math
from typing import Any, Iterable

import numpy as np

SMOOTHING = 0.5
COMPLETION_RATIO = 0.90
SCORING_VERSION = "meow-fingerprint-v2"
INVALID_OUTPUT = "__INVALID_OUTPUT__"


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def distribution(counts: dict[str, int], categories: list[str]) -> dict[str, float]:
    denominator = sum(counts.values()) + SMOOTHING * len(categories)
    return {category: (counts.get(category, 0) + SMOOTHING) / denominator
            for category in categories}


def js_divergence(left: dict[str, float], right: dict[str, float]) -> float:
    categories = sorted(set(left) | set(right))
    result = 0.0
    for category in categories:
        a, b = left.get(category, 0.0), right.get(category, 0.0)
        middle = (a + b) / 2
        if a > 0:
            result += 0.5 * a * math.log2(a / middle)
        if b > 0:
            result += 0.5 * b * math.log2(b / middle)
    return result


def fit_cell(windows: dict[str, dict], models: list[str], *, _legacy=False) -> dict[str, Any]:
    categories = sorted({"__OTHER__", "__INVALID_OUTPUT__"} | {
        category for model in models for window in windows.get(model, {}).values()
        for category in window.get("counts", {})
    })
    if not _legacy:
        categories.remove(INVALID_OUTPUT)
    combined, distributions, quality, drift, drift_max = {}, {}, {}, {}, {}
    window_pairs = {}
    for model in models:
        actual = windows.get(model, {})
        total = Counter()
        nonempty = []
        for window_id, window in actual.items():
            counts = window.get("counts", {})
            total.update(counts)
            if not _legacy:
                counts = {key: count for key, count in counts.items() if key != INVALID_OUTPUT}
            if sum(counts.values()) > 0:
                nonempty.append((window_id, distribution(counts, categories)))
        effective = dict(total) if _legacy else {key: count for key, count in total.items() if key != INVALID_OUTPUT}
        combined[model] = effective
        distributions[model] = distribution(effective, categories)
        pair_values = {f"{a[0]}|{b[0]}": js_divergence(a[1], b[1])
                       for a, b in combinations(nonempty, 2)}
        window_pairs[model] = pair_values
        drift[model] = _mean(pair_values.values()) if pair_values else None
        drift_max[model] = max(pair_values.values()) if pair_values else None
        completed = sum(total.values())
        quality[model] = {
            "completed": completed,
            "planned": sum(window.get("planned", sum(window.get("counts", {}).values()))
                           for window in actual.values()),
            "nonempty_windows": len(nonempty),
            "invalid": total.get("__INVALID_OUTPUT__", 0),
            "valid_rate": (completed - total.get("__INVALID_OUTPUT__", 0)) / completed if completed else None,
            "singleton_mass": sum(count for count in total.values() if count == 1) / completed if completed else None,
        }
    pairs = {f"{a}|{b}": js_divergence(distributions[a], distributions[b])
             for a, b in combinations(models, 2)}
    ready = all(sum(combined[model].values()) > 0 for model in models)
    between = _mean(pairs.values()) if ready else None
    measured_drift = _mean(value for value in drift.values() if value is not None)
    weight = (between - measured_drift) / between if ready and between > measured_drift else 0.0
    return {
        "categories": categories, "model_counts": combined,
        "model_distributions": distributions, "pairwise_jsd": pairs if ready else {},
        "between_model_jsd": between, "within_model_jsd": measured_drift,
        "model_drift": drift, "model_drift_max": drift_max,
        "window_pair_jsd": window_pairs, "weight": weight, "quality": quality,
        "reference_ready": ready,
        "drift_estimable": all(value["nonempty_windows"] >= 2 for value in quality.values()),
    }


def fit_observations(observations: dict, models: list[str], cells: dict, *, _legacy=False) -> dict[str, Any]:
    return {
        "models": models,
        "cells": {cell_id: {**fit_cell(observations.get(cell_id, {}), models, _legacy=_legacy),
                            "family_id": cell["family_id"]}
                  for cell_id, cell in cells.items()},
    }


def aggregate_families(models: list[str], families: dict) -> tuple[dict, dict]:
    """The same family formula accepts scalar or NumPy-array likelihoods."""
    scores = {model: 0.0 for model in models}
    details = {}
    for family, entries in sorted(families.items()):
        weight_sum = sum(weight for weight, _ in entries)
        family_weight = max(weight for weight, _ in entries)
        contributions = {model: family_weight * sum(weight * values[model] for weight, values in entries) / weight_sum
                         for model in models}
        details[family] = contributions
        for model in models:
            scores[model] += contributions[model]
    return scores, details


def numeric_matches(fitted: dict, draws: dict) -> tuple:
    """The exact numeric path for both one real batch and simulated batches.

    Reduce categories in a fixed order, not a BLAS matrix product whose rounding
    can depend on the batch shape. Strict threshold comparisons need this parity.
    """
    models = fitted["models"]
    size = len(next(iter(draws.values()))) if draws else 1
    families, likelihoods = defaultdict(list), {}
    for identity in sorted(draws):
        cell, counts = fitted["cells"][identity], draws[identity].copy()
        if INVALID_OUTPUT in cell["categories"]:
            counts[:, cell["categories"].index(INVALID_OUTPUT)] = 0
        totals = counts.sum(axis=1)
        if not np.any(totals):
            continue
        values = {}
        for model in models:
            logs = np.asarray([math.log(cell["model_distributions"][model][category]) for category in cell["categories"]])
            values[model] = (counts * logs).sum(axis=1) / np.maximum(totals, 1)
        likelihoods[identity] = values
        if cell["weight"] > 0:
            families[cell["family_id"]].append((cell["weight"], values))
    if families:
        scores, details = aggregate_families(models, families)
        matrix = np.column_stack([scores[model] for model in models])
    else:
        matrix, details = np.zeros((size, len(models))), {}
    weights = np.exp(matrix - matrix.max(axis=1, keepdims=True))
    return weights / weights.sum(axis=1, keepdims=True), matrix, details, likelihoods


def score_counts(fitted: dict, counts: dict[str, dict[str, int]], planned: dict[str, int],
                 thresholds: dict[str, float] | None = None, *, calibrated: bool = True,
                 claimed_model: str | None = None) -> dict[str, Any]:
    models = fitted["models"]
    draws = {}
    cell_details, reasons = {}, []
    for cell_id, requested in planned.items():
        if requested == 0:
            continue
        cell = fitted["cells"].get(cell_id)
        if cell is None or not cell["reference_ready"]:
            reasons.append("baseline_cell_missing")
            continue
        allowed = set(cell["categories"])
        normalized = Counter()
        for category, count in counts.get(cell_id, {}).items():
            normalized[category if category in allowed or category == INVALID_OUTPUT else "__OTHER__"] += count
        total = sum(normalized.values())
        valid = total - normalized.get(INVALID_OUTPUT, 0)
        minimum = math.ceil(requested * COMPLETION_RATIO)
        if valid < minimum:
            reasons.append("samples_incomplete")
        if total > requested:
            reasons.append("samples_exceed_plan")
        draws[cell_id] = np.asarray([[normalized.get(category, 0) for category in cell["categories"]]])
        cell_details[cell_id] = {
            "planned": requested, "minimum": minimum, "completed": total, "valid": valid,
            "counts": dict(normalized), "weight": cell["weight"],
            "average_log_likelihood": {},
        }
    matched, matrix, details, likelihoods = numeric_matches(fitted, draws)
    scores = {model: float(matrix[0, index]) for index, model in enumerate(models)}
    matches = {model: float(matched[0, index]) for index, model in enumerate(models)}
    family_details = {family: {model: float(value[0]) for model, value in values.items()} for family, values in details.items()}
    for identity, detail in cell_details.items():
        detail["average_log_likelihood"] = {model: float(likelihoods[identity][model][0]) if identity in likelihoods else 0.0 for model in models}
    if not details:
        reasons.append("no_weighted_family")
    if not calibrated or not thresholds or set(thresholds) != set(models):
        reasons.append("uncalibrated")
    if claimed_model is not None and claimed_model not in models:
        reasons.append("unknown_claimed_model")
    winners = [model for model in models if not reasons and matches[model] > thresholds[model]]
    if not reasons and len(winners) != 1:
        reasons.append("multiple_thresholds" if winners else "no_threshold")
    winner = winners[0] if len(winners) == 1 else None
    verdict = "insufficient" if winner is None else ("match" if winner == claimed_model else "mismatch")
    return {
        "verdict": verdict, "color": {"match":"green", "insufficient":"yellow", "mismatch":"red"}[verdict],
        "model": winner, "claimed_model": claimed_model, "reasons": sorted(set(reasons)),
        "matches": matches, "scores": scores, "thresholds": thresholds or {},
        "cells": cell_details, "families": family_details,
    }
