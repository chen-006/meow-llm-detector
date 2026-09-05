import sys
import tempfile
import unittest
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORK))

from gpt56_vnext.errors import AppError, RequestError
from gpt56_vnext.normalizers import normalize_answer, validate_normalizer
from gpt56_vnext.security import SecretGuard
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.utils import normalize_api_base_url, strict_json_loads


class FoundationsTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(normalize_api_base_url("https://example.org/v1/responses"), "https://example.org/v1")
        self.assertEqual(normalize_api_base_url("http://[::1]:8888/v1"), "http://[::1]:8888/v1")
        for url in ("http://example.org/v1", "https://u:p@example.org/v1", "https://example.org/v1?key=x", "https://example.org/v1#x"):
            with self.assertRaises(AppError):
                normalize_api_base_url(url)

    def test_strict_json(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            with self.assertRaises(AppError):
                strict_json_loads(text)

    def test_normalizer_boundaries(self):
        with self.assertRaises(AppError):
            validate_normalizer({"id":"regex_capture", "parameters":{"pattern":"(a+)+$"}})
        self.assertEqual(normalize_answer("+003", {"id":"b80_exact_3"}), "exact_3")
        self.assertEqual(normalize_answer("r" * 70000, {"id":"behavior_label"}), "__INVALID_OUTPUT__")

    def test_exact_credential_echo(self):
        guard = SecretGuard(["synthetic-credential-only-uuid"])
        with self.assertRaises(RequestError):
            guard.check({"answer":"synthetic-credential-only-uuid"})
        self.assertEqual(guard.redact({"authorization":"synthetic", "value":"synthetic-credential-only-uuid"}), {"value":"[REDACTED]"})

    def test_duplicate_terminal_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            with SQLiteStateStore(Path(folder) / "test.sqlite3") as store:
                store.create_session(session_id="s", kind="test", status="running", config={}, config_hash="x", official=False)
                store.freeze_jobs("s", 0, [{"job_id":"j"}])
                first = store.record_terminal_result("s", "j", "cancelled", {"status":"cancelled"})
                second = store.record_terminal_result("s", "j", "cancelled", {"status":"cancelled"})
                self.assertEqual(first, second)
                self.assertEqual(store._read(lambda db: db.execute("SELECT COUNT(*) FROM results").fetchone()[0]), 1)
                self.assertEqual(store.integrity_check(), "ok")


if __name__ == "__main__":
    unittest.main()
