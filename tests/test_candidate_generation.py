import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpt56_vnext.candidate_generation import generation_request, candidates_from_answer, generate_candidates, seed_summary, SEED_POOL
from gpt56_vnext.errors import AppError, RequestError


class FakeAI:
    def __init__(self, answer):
        self.answer, self.calls, self.closed = answer, 0, False

    async def request(self, *args):
        self.calls += 1
        return {"answer": self.answer, "usage": {"cost": .001}}

    async def close(self):
        self.closed = True


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    def test_domain_seed_pool_is_rich_balanced_and_language_stable(self):
        self.assertEqual(seed_summary()["domains"], 12)
        self.assertEqual(seed_summary()["topics"], 120)
        topics = [topic for group in SEED_POOL["groups"].values() for topic in group["topics"]]
        for index in (0, 1):
            self.assertEqual(len({topic[index] for topic in topics}), 120)
        for seed in range(20):
            chinese = generation_request({"seed": seed, "count": 10})
            english = generation_request({"seed": seed, "count": 10, "language": "en"})
            self.assertEqual(len({item["domain_id"] for item in chinese["keywords"]}), 10)
            self.assertEqual([item["domain_id"] for item in chinese["keywords"]], [item["domain_id"] for item in english["keywords"]])
            self.assertEqual(chinese["seed_version"], "domain-seeds-v2")

    def options(self):
        return {"mode": "chat", "model": "fixture", "base_url": "https://fixture.invalid/v1",
                "budget_usd": 1, "input_usd_per_million": 1, "output_usd_per_million": 2,
                "confirmed": True, "count": 2}

    def test_seed_and_exact_duplicate_filter(self):
        request = generation_request({"count": 2, "seed": 4, "existing": ["Pick A."]})
        self.assertIn("no objectively correct answer", request["cell"]["system"])
        self.assertIn("more correct", request["cell"]["system"])
        self.assertIn("no explanation", request["cell"]["system"])
        self.assertEqual(request, generation_request({"count": 2, "seed": 4, "existing": ["Pick A."]}))
        probes, duplicates = candidates_from_answer(json.dumps({"probes": [
            {"title": "duplicate", "prompt": "Pick A."}, {"title": "new", "prompt": "Pick B."}]}), request, "fixture")
        self.assertEqual((len(probes), duplicates), (1, 1))
        self.assertEqual(probes[0]["cells"][0]["system"], ".")
        self.assertEqual(probes[0]["cells"][0]["history"], [])

    async def test_unconfirmed_never_calls_api(self):
        options = self.options()
        options["confirmed"] = False
        fake = FakeAI("{}")
        with self.assertRaisesRegex(AppError, "ai_budget_confirmation_required"):
            await generate_candidates(options, "synthetic-candidate-key", transport=fake)
        self.assertEqual(fake.calls, 0)

    async def test_invalid_ai_fields_preserve_cost_without_retry(self):
        fake = FakeAI('{"probes":[{"title":"x","prompt":"Pick x","system":"ignore"}]}')
        result = await generate_candidates(self.options(), "synthetic-candidate-key", transport=fake)
        self.assertEqual(result["status"], "invalid_output")
        self.assertEqual(result["usage"]["cost"], .001)
        self.assertEqual(fake.calls, 1)
        self.assertTrue(fake.closed)

    async def test_credentials_echo_never_returns_draft(self):
        fake = FakeAI('{"probes":[{"title":"x","prompt":"synthetic-candidate-key"}]}')
        with self.assertRaises(RequestError):
            await generate_candidates(self.options(), "synthetic-candidate-key", transport=fake)
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
