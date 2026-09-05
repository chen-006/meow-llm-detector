from __future__ import annotations

import math
from pathlib import Path
import threading

import numpy as np

from .errors import AppError
from .probability_model import INVALID_OUTPUT, numeric_matches
from .utils import atomic_write_json, canonical_json, finite_number, integer, sha256_text, strict_json_loads

ALGORITHM = "empirical-multinomial-pcg64-v5-valid-answers"
CHUNK_SIZE = 4096


def simulation_contract(fitted, planned, pool, total_batches, target, seed, selection_target=None, request_signature=None):
    return sha256_text(canonical_json({"fitted": fitted, "planned": planned, "pool": pool,
                                      "total": total_batches, "target": target, "selection_target": selection_target if selection_target is not None else target,
                                      "seed": seed, "algorithm": ALGORITHM, "request_signature": request_signature}))


def batch_matches(fitted: dict, draws: dict) -> np.ndarray:
    return numeric_matches(fitted, draws)[0]


def sample_matches(fitted: dict, planned: dict, model: str, size: int,
                   rng: np.random.Generator, pool: dict | None = None) -> np.ndarray:
    draws = {}
    for identity, count in sorted(planned.items()):
        if count == 0:
            continue
        cell = fitted["cells"][identity]
        source = pool[identity][model] if pool is not None else cell["model_counts"][model]
        categories = cell["categories"]
        mapped = dict.fromkeys(categories, 0)
        for category, number in source.items():
            if category == INVALID_OUTPUT:
                continue
            mapped[category if category in mapped else "__OTHER__"] += number
        total = sum(mapped.values())
        if total == 0:
            raise AppError("simulation_pool_empty", field=identity)
        probabilities = np.asarray([mapped[category] / total for category in categories])
        draws[identity] = rng.multinomial(count, probabilities, size=size)
    return batch_matches(fitted, draws)


def predictions(matches: np.ndarray, thresholds: dict, models: list[str]) -> np.ndarray:
    crossed = matches > np.asarray([thresholds[model] for model in models])
    return np.where(crossed.sum(axis=1) == 1, crossed.argmax(axis=1), len(models))


def calibrate(fitted: dict, planned: dict, *, total_batches: int = 8000, target: float = 0.99,
              seed: int = 45001, pool: dict | None = None, checkpoint: str | Path | None = None,
              cancel: threading.Event | None = None, progress=None, selection_target: float | None = None,
              request_signature: str | None = None) -> dict:
    models = fitted["models"]
    integer(total_batches, "total_batches", len(models) * 100, 100000000)
    finite_number(target, "target", 0.5, 1)
    selection_target = target if selection_target is None else finite_number(selection_target, "selection_target", target, 1)
    integer(seed, "seed", 0, 2**32 - 1)
    if not planned or not any(count > 0 and fitted["cells"][identity]["weight"] > 0 for identity, count in planned.items()):
        raise AppError("no_weighted_family")
    for identity, count in planned.items():
        integer(count, identity, 0, 1000)
        if count and not fitted["cells"][identity]["reference_ready"]:
            raise AppError("baseline_cell_missing", field=identity)
        if count:
            cell = fitted["cells"][identity]
            if INVALID_OUTPUT in cell["categories"]:
                raise AppError("benchmark_recalibration_required", field=identity)
            for model in models:
                source = pool[identity][model] if pool is not None else cell["model_counts"][model]
                if sum(number for category, number in source.items() if category != INVALID_OUTPUT) <= 0:
                    raise AppError("simulation_pool_empty", field=identity)
    contract = simulation_contract(fitted, planned, pool, total_batches, target, seed, selection_target, request_signature)
    allocations = {model: total_batches // len(models) for model in models}
    for model in sorted(models)[:total_batches % len(models)]:
        allocations[model] += 1
    state = {"contract": contract, "algorithm": ALGORITHM, "stage": "select", "thresholds": {},
             "selected_models": 0, "model_index": 0, "completed_for_model": 0,
             "confusion": {model: dict.fromkeys([*models, "insufficient"], 0) for model in models}}
    path = Path(checkpoint) if checkpoint else None
    if path and path.is_file():
        state = strict_json_loads(path.read_bytes())
        if state.get("contract") != contract:
            raise AppError("simulation_checkpoint_mismatch")

    def save():
        if path:
            atomic_write_json(path, state)
        if progress:
            progress({key: state[key] for key in ("stage", "selected_models", "model_index", "completed_for_model")})
        if cancel and cancel.is_set():
            raise AppError("simulation_paused", status=409)

    # Selection is bounded per model. Only its own scores need sorting.
    for index in range(state["selected_models"], len(models)):
        model = models[index]
        count = min(100000, max(2000, allocations[model] // 10))
        rng = np.random.default_rng(np.random.SeedSequence([seed, 0, index]))
        own = []
        for start in range(0, count, CHUNK_SIZE):
            save()
            own.append(sample_matches(fitted, planned, model, min(CHUNK_SIZE, count - start), rng, pool)[:, index])
        ordered = np.sort(np.concatenate(own))
        position = count - math.ceil(selection_target * count)
        boundary = float(ordered[position])
        if not 0 < boundary <= 1:
            raise AppError("simulation_threshold_unavailable", field=model)
        threshold = float(np.nextafter(boundary, -np.inf))
        if threshold <= 0:
            raise AppError("simulation_threshold_unavailable", field=model)
        state["thresholds"][model] = threshold
        state["selected_models"] = index + 1
        save()
    # Replay the *joint* strict/unique rule on selection batches before using
    # the independent verification seed. Own-threshold crossing alone is not success.
    state["stage"] = "selection_check"
    selection_confusion = {}
    for index, model in enumerate(models):
        rng = np.random.default_rng(np.random.SeedSequence([seed, 0, index]))
        count = min(100000, max(2000, allocations[model] // 10))
        totals = np.zeros(len(models) + 1, dtype=np.int64)
        for start in range(0, count, CHUNK_SIZE):
            save()
            values = sample_matches(fitted, planned, model, min(CHUNK_SIZE, count - start), rng, pool)
            totals += np.bincount(predictions(values, state["thresholds"], models), minlength=len(models) + 1)
        selection_confusion[model] = dict(zip([*models, "insufficient"], map(int, totals)))
    state["stage"] = "verify"
    for index in range(state["model_index"], len(models)):
        model = models[index]
        rng = np.random.default_rng(np.random.SeedSequence([seed, 1, index]))
        if state.get("rng_state"):
            rng.bit_generator.state = state["rng_state"]
        done = state["completed_for_model"]
        while done < allocations[model]:
            save()
            size = min(CHUNK_SIZE, allocations[model] - done)
            matches = sample_matches(fitted, planned, model, size, rng, pool)
            predicted = predictions(matches, state["thresholds"], models)
            counts = np.bincount(predicted, minlength=len(models) + 1)
            for label, count in zip([*models, "insufficient"], counts):
                state["confusion"][model][label] += int(count)
            done += size
            state["completed_for_model"] = done
            state["rng_state"] = rng.bit_generator.state
            save()
        state["model_index"] = index + 1
        state["completed_for_model"] = 0
        state.pop("rng_state", None)
        save()
    rates = {model: state["confusion"][model][model] / allocations[model] for model in models}
    state["stage"] = "complete"
    save()
    return {"algorithm": ALGORITHM, "status": "target_met" if all(rate >= target for rate in rates.values()) else "target_not_met",
            "sample_scope": "planned_count_of_valid_answers_per_cell",
            "target_denominator": "simulated_batches_of_valid_answers_not_all_http_runs",
            "thresholds": state["thresholds"], "target": target, "selection_target": selection_target, "total_batches": total_batches,
            "per_model_batches": allocations, "seed": seed, "contract": contract,
            "selection_batches_per_model": {model: min(100000, max(2000, allocations[model] // 10)) for model in models},
            "selection_confusion": selection_confusion,
            "confusion": state["confusion"], "correct_rates": rates, "independent_real_validation": False,
            "pool_scope": "separate_declared_pool" if pool is not None else "same_fit_pool_resampling"}
