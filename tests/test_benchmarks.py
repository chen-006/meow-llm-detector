from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from gpt56_vnext.benchmark import build_package, cells_by_id, content_hash, load_package, normalize_project
from gpt56_vnext.errors import AppError
from gpt56_vnext.probability_model import fit_cell, fit_observations, score_counts
from gpt56_vnext.selection import recommend
from gpt56_vnext.simulation import batch_matches, calibrate


def fixture():
    project = normalize_project({"id": "fixture", "mode": "chat", "models": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "probes": [{"id": "ab", "prompt": "Choose 1"}, {"id": "ac", "prompt": "Choose 2"}, {"id": "bc", "prompt": "Choose 3"}],
        "tiers": {tier: {"counts": dict.fromkeys(("ab", "ac", "bc"), count)}
                  for tier, count in zip(("low", "medium", "high"), (3, 5, 9))}})
    observations = {cell: {model: {"1": {"counts": {model: 100}, "planned": 100},
                                      "2": {"counts": {model: 100}, "planned": 100}}
                           for model in ("a", "b", "c")} for cell in ("ab", "ac", "bc")}
    return project, observations


class BenchmarkTests(unittest.TestCase):
    def test_basic_contract_and_new_tier_defaults(self):
        for mode in ("gpt", "claude", "chat"):
            project = normalize_project({"id": "basic", "mode": mode,
                "models": [{"id": "a"}, {"id": "b"}], "probes": [{"id": "p", "prompt": "Choose a color."}]})
            self.assertEqual([project["tiers"][tier]["counts"]["p"] for tier in ("low", "medium", "high")], [4, 10, 20])
            self.assertEqual(project["probes"][0]["cells"][0]["system"], ".")
            for field, value in (("history", [{"role": "user", "content": "old"}]),
                                 ("system", "client wrapper"), ("effort", "high"),
                                 ("profile", "codex-like"), ("tools", [])):
                changed = deepcopy(project)
                changed["probes"][0]["cells"][0][field] = value
                with self.subTest(mode=mode, field=field), self.assertRaises(AppError):
                    normalize_project(changed)

    def test_package_roundtrip_and_derived_tampering(self):
        project, observations = fixture()
        package = build_package(project, observations)
        self.assertEqual(load_package(json.dumps(package)), package)
        changed = deepcopy(package)
        changed["fitted"]["cells"]["ab"]["weight"] = 0.2
        changed["content_sha256"] = content_hash(changed)
        with self.assertRaises(AppError):
            load_package(changed)

    def test_bad_shapes_counts_and_models(self):
        project, observations = fixture()
        for field, value in (("models", [{"id": "a"}, {"id": "a"}]), ("probes", [None]),
                             ("schema_version", True), ("tiers", {"low": []})):
            with self.assertRaises(AppError):
                normalize_project({**project, field: value})
        for value in (-1, True, 1.5):
            altered = deepcopy(observations)
            altered["ab"]["a"]["1"]["counts"]["a"] = value
            with self.assertRaises(AppError):
                build_package(project, altered)

    def test_missing_windows_not_stable(self):
        model = fit_cell({"a": {"1": {"counts": {"x": 3}}, "2": {"counts": {}}},
                          "b": {"1": {"counts": {"y": 3}}, "2": {"counts": {}}}}, ["a", "b"])
        self.assertFalse(model["drift_estimable"])
        self.assertIsNone(model["model_drift"]["a"])

    def test_exactly_three_conclusions_and_each_cell_completion(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        plan = {"ab": 3, "ac": 3, "bc": 3}
        observed = {cell: {"a": 3} for cell in plan}
        thresholds = dict.fromkeys(("a", "b", "c"), 0.8)
        self.assertEqual(score_counts(fitted, observed, plan, thresholds, claimed_model="a")["color"], "green")
        self.assertEqual(score_counts(fitted, observed, plan, thresholds, claimed_model="b")["color"], "red")
        observed["bc"] = {"a": 2}
        self.assertEqual(score_counts(fitted, observed, plan, thresholds, claimed_model="b")["color"], "yellow")
        self.assertEqual(score_counts(fitted, observed, plan, {}, claimed_model="a")["color"], "yellow")

    def test_recommender_complements_weakest_pair(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        fitted["cells"]["ab"]["pairwise_jsd"] = {"a|b": 0.4, "a|c": 0.05, "b|c": 0.05}
        fitted["cells"]["ac"]["pairwise_jsd"] = {"a|b": 0.3, "a|c": 0.0, "b|c": 0.0}
        fitted["cells"]["bc"]["pairwise_jsd"] = {"a|b": 0.0, "a|c": 0.08, "b|c": 0.08}
        result = recommend(project, fitted, maximum=2, locked=["ab"])
        self.assertEqual(result["selected"], ["ab", "bc"])
        self.assertEqual(result["preview_requests"], 6)
        self.assertEqual(recommend(project, fitted, maximum=2, locked=["ab"]), result)

    def test_same_source_and_zero_coverage_not_forced(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        for probe in project["probes"]:
            probe["source_group"] = "same"
        self.assertEqual(len(recommend(project, fitted)["selected"]), 1)
        for cell in fitted["cells"].values():
            cell["weight"] = 0
        self.assertEqual(recommend(project, fitted)["selected"], [])


class SimulationTests(unittest.TestCase):
    def test_selection_margin_is_separate_from_acceptance_target(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        plan = dict.fromkeys(fitted["cells"], 4)
        original = calibrate(fitted, plan, total_batches=600, target=0.99)
        relaxed = calibrate(fitted, plan, total_batches=600, target=0.99, selection_target=0.999)
        self.assertEqual(relaxed["target"], 0.99)
        self.assertEqual(relaxed["selection_target"], 0.999)
        self.assertNotEqual(original["contract"], relaxed["contract"])
        self.assertTrue(all(relaxed["thresholds"][m] <= original["thresholds"][m] for m in fitted["models"]))
        with self.assertRaises(AppError):
            calibrate(fitted, plan, total_batches=600, target=0.99, selection_target=0.9)

    def test_vectorized_scores_match_runtime_with_shared_family(self):
        project, observations = fixture()
        project["probes"][1]["family_id"] = "ab"
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        fitted["cells"]["ac"]["weight"] = 0.4
        rng = np.random.default_rng(12)
        draws = {identity: rng.multinomial(5, [1 / len(cell["categories"])] * len(cell["categories"]), size=64)
                 for identity, cell in fitted["cells"].items()}
        matches = batch_matches(fitted, draws)
        for index in range(64):
            counts = {identity: dict(zip(fitted["cells"][identity]["categories"], map(int, values[index])))
                      for identity, values in draws.items()}
            result = score_counts(fitted, counts, dict.fromkeys(draws, 5), claimed_model="a")
            np.testing.assert_array_equal(matches[index], list(result["matches"].values()))

    def test_selected_threshold_crosses_in_real_scorer_after_json_roundtrip(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        planned = {"ab": 3, "ac": 3, "bc": 3}
        calibration = calibrate(fitted, planned, total_batches=600)
        frozen = json.loads(json.dumps(fitted, sort_keys=True))
        for model in fitted["models"]:
            result = score_counts(frozen, {cell: {model: 3} for cell in planned}, planned,
                                  calibration["thresholds"], claimed_model=model)
            self.assertEqual(result["color"], "green")

    def test_calibration_is_reproducible_and_no_fake_target(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        result = calibrate(fitted, {"ab": 3, "ac": 3, "bc": 3}, total_batches=600)
        self.assertEqual(result["status"], "target_met")
        self.assertEqual(calibrate(fitted, {"ab": 3, "ac": 3, "bc": 3}, total_batches=600), result)
        self.assertFalse(result["independent_real_validation"])
        for cell in fitted["cells"].values():
            for model in fitted["models"]:
                cell["model_distributions"][model] = cell["model_distributions"]["a"].copy()
        bad = calibrate(fitted, {"ab": 3}, total_batches=600)
        self.assertEqual(bad["status"], "target_not_met")

    def test_checkpoint_resume_is_exact(self):
        project, observations = fixture()
        fitted = fit_observations(observations, ["a", "b", "c"], cells_by_id(project))
        planned = {"ab": 3, "ac": 3, "bc": 3}
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "checkpoint.json"
            stop = threading.Event()
            def progress(state):
                if state["stage"] == "verify" and state["completed_for_model"] >= 4096:
                    stop.set()
            with self.assertRaisesRegex(AppError, "simulation_paused"):
                calibrate(fitted, planned, total_batches=15000, checkpoint=checkpoint, cancel=stop, progress=progress)
            resumed = calibrate(fitted, planned, total_batches=15000, checkpoint=checkpoint)
            full = calibrate(fitted, planned, total_batches=15000)
            self.assertEqual(resumed, full)
            with self.assertRaisesRegex(AppError, "simulation_checkpoint_mismatch"):
                calibrate(fitted, {"ab": 4}, total_batches=15000, checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
