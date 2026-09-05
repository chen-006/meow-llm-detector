from __future__ import annotations

from itertools import combinations
import math
import re

import numpy as np

from .benchmark import request_contract
from .errors import AppError
from .probability_model import numeric_matches
from .utils import canonical_json, integer, sha256_text

ALGORITHM = "pair-coverage-greedy-v1"


def recommend(project: dict, fitted: dict, *, maximum: int = 7, request_budget: int = 100,
              locked: list[str] = (), measured_costs: dict | None = None, _by_jsd=False) -> dict:
    integer(maximum, "maximum", 1, 1000)
    integer(request_budget, "request_budget", 1, 100000)
    models = fitted["models"]
    pairs = [f"{a}|{b}" for a, b in combinations(models, 2)]
    contributions, costs, excluded, source_groups = {}, {}, {}, {}
    seen_contracts = set()
    for probe in sorted(project["probes"], key=lambda item: item["id"]):
        identity = probe["id"]
        active_cells = [cell for cell in probe["cells"] if project["tiers"]["low"]["counts"][cell["id"]] > 0]
        if not active_cells:
            excluded[identity] = "disabled_in_tier"
            continue
        source_groups[identity] = probe.get("source_group", probe["family_id"])
        signature = canonical_json({"mode": project["mode"], "normalizer": probe["normalizer"],
                                   "requests": [request_contract(cell) for cell in active_cells]})
        if signature in seen_contracts:
            excluded[identity] = "duplicate_contract"
            continue
        seen_contracts.add(signature)
        usable = [fitted["cells"][cell["id"]] for cell in active_cells
                  if fitted["cells"][cell["id"]]["reference_ready"] and
                  fitted["cells"][cell["id"]]["drift_estimable"] and
                  fitted["cells"][cell["id"]]["weight"] > 0]
        if not usable:
            excluded[identity] = "no_stable_weighted_cell"
            continue
        gain = {pair: 0.0 for pair in pairs}
        for cell in usable:
            valid_rate = min(quality["valid_rate"] for quality in cell["quality"].values())
            for a, b in combinations(models, 2):
                pair = f"{a}|{b}"
                separation = max(0.0, cell["pairwise_jsd"][pair] -
                                 max(cell["model_drift_max"][a], cell["model_drift_max"][b]))
                gain[pair] = max(gain[pair], cell["weight"] * valid_rate * separation)
        contributions[identity] = gain
        costs[identity] = sum(project["tiers"]["low"]["counts"][cell["id"]] for cell in probe["cells"])
    if len(set(locked)) != len(locked) or len(locked) > maximum or any(item not in contributions for item in locked):
        raise AppError("invalid_locked_probes")
    selected, reasons = [], []
    coverage = dict.fromkeys(pairs, 0.0)
    used_groups = set()
    remaining = request_budget

    def add(identity, reason):
        nonlocal remaining
        selected.append(identity)
        used_groups.add(source_groups[identity])
        remaining -= costs[identity]
        before = min(coverage.values())
        for pair in pairs:
            coverage[pair] += contributions[identity][pair]
        reasons.append({"probe_id": identity, "reason": reason, "minimum_before": before,
                        "minimum_after": min(coverage.values()), "contributions": contributions[identity]})

    for identity in locked:
        if costs[identity] > remaining or source_groups[identity] in used_groups:
            raise AppError("locked_probes_exceed_constraints")
        add(identity, "locked")
    while len(selected) < maximum:
        eligible = [identity for identity in contributions if identity not in selected and
                    source_groups[identity] not in used_groups and costs[identity] <= remaining]
        if not eligible:
            break

        def rank(identity):
            if _by_jsd:
                probe = next(probe for probe in project["probes"] if probe["id"] == identity)
                vector = (max(fitted["cells"][cell["id"]]["between_model_jsd"] or 0 for cell in probe["cells"]
                              if project["tiers"]["low"]["counts"][cell["id"]] > 0),)
            else:
                vector = tuple(sorted(coverage[pair] + contributions[identity][pair] for pair in pairs))
            cost = (measured_costs or {}).get(identity)
            cost = cost if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else math.inf
            return vector, -costs[identity], -cost

        candidate = max(sorted(eligible), key=rank)
        if not any(contributions[candidate][pair] > 0 for pair in pairs):
            break
        add(candidate, "weakest_pair_coverage")
    return {"algorithm": ALGORITHM, "selected": selected, "reasons": reasons,
            "coverage": coverage, "uncovered_pairs": [pair for pair, value in coverage.items() if value <= 0],
            "excluded": excluded, "preview_requests": request_budget - remaining,
            "maximum": maximum, "scoring_weights_changed": False}


def similar_probes(project: dict) -> dict:
    """Token-set Jaccard hints only. Never delete or change a sampling contract."""
    tokens = {probe["id"]: set(re.findall(r"[\u4e00-\u9fff]|[^\W_]+", " ".join(
        cell["prompt"] for cell in probe["cells"]).casefold())) for probe in project["probes"]}
    pairs, total = [], 0
    for a, b in combinations(sorted(tokens), 2):
        left, right = tokens[a], tokens[b]
        if not left or not right or min(len(left), len(right)) < .9 * max(len(left), len(right)):
            continue
        similarity = len(left & right) / len(left | right)
        if similarity >= .9:
            total += 1
            if len(pairs) < 100:
                pairs.append({"left": a, "right": b, "similarity": similarity})
    return {"method": "token-set-jaccard-v1", "threshold": .9, "pairs": pairs, "total": total}


def compare_selection(project: dict, fitted: dict, recommendation: dict, *, locked=(), measured_costs=None) -> dict:
    """Small paired, same-pool ranking preview, not threshold calibration."""
    selected = recommendation["selected"]
    if not selected:
        return {"status": "no_weighted_family"}
    baseline = recommend(project, fitted, maximum=len(selected), request_budget=recommendation["preview_requests"],
                         locked=locked, measured_costs=measured_costs, _by_jsd=True)
    groups = {"recommended": selected, "mean_jsd": baseline["selected"]}
    plans = {name: {cell["id"]: project["tiers"]["low"]["counts"][cell["id"]]
                    for probe in project["probes"] if probe["id"] in probes for cell in probe["cells"]
                    if project["tiers"]["low"]["counts"][cell["id"]] > 0} for name, probes in groups.items()}
    models, batches, seed = fitted["models"], 2000, 45006
    union = plans["recommended"] | plans["mean_jsd"]
    if any(not fitted["cells"][identity]["reference_ready"] for identity in union):
        return {"status": "baseline_cell_missing"}
    # Keep this preview small even when an advanced draft requests thousands of samples.
    if sum(union.values()) * len(models) * batches > 20_000_000:
        return {"status": "preview_budget_exceeded"}
    results = {name: {"selected": probes, "requests": sum(plans[name].values()),
                     "confusion": {}, "correct_rates": {}} for name, probes in groups.items()}
    for index, model in enumerate(models):
        rngs = {identity: np.random.default_rng(np.random.SeedSequence([seed, index, int(sha256_text(identity)[:8], 16)]))
                for identity in union}
        totals = {name: np.zeros(len(models) + 1, dtype=np.int64) for name in groups}
        for start in range(0, batches, 250):
            draws = {}
            for identity, count in sorted(union.items()):
                cell = fitted["cells"][identity]
                probability = np.asarray([cell["model_counts"][model].get(category, 0) for category in cell["categories"]], dtype=float)
                draws[identity] = rngs[identity].multinomial(count, probability / probability.sum(), size=min(250, batches - start))
            for name, plan in plans.items():
                matches = numeric_matches(fitted, {identity: draws[identity] for identity in plan})[0]
                highest = matches == matches.max(axis=1, keepdims=True)
                winner = np.where(highest.sum(axis=1) == 1, matches.argmax(axis=1), len(models))
                totals[name] += np.bincount(winner, minlength=len(models) + 1)
        for name in groups:
            results[name]["confusion"][model] = dict(zip([*models, "tie"], map(int, totals[name])))
            results[name]["correct_rates"][model] = int(totals[name][index]) / batches
    return {"status": "complete", "seed": seed, "batches_per_model": batches,
            "scope": "same_pool_valid_answers_top_score_not_strong_match", "groups": results,
            "matched_cost_and_count": len(selected) == len(baseline["selected"]) and
                results["recommended"]["requests"] == results["mean_jsd"]["requests"]}
