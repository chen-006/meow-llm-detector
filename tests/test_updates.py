import hashlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import httpx

from gpt56_vnext.updates import ProgramUpdates, release_info, REPOSITORY
from gpt56_vnext.store import SQLiteStateStore
from gpt56_vnext.errors import AppError


class UpdateTests(unittest.TestCase):
    def release(self):
        name = "meow-llm-detector-v4.6.0-en.zip"
        return {"tag_name": "v4.6.0", "assets": [{"name": name, "size": 4,
            "digest": "sha256:" + hashlib.sha256(b"test").hexdigest(),
            "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/v4.6.0/{name}"}]}

    def test_program_update_is_separate_and_requires_verified_language_asset(self):
        value = self.release()
        self.assertTrue(release_info(value, "en")["available"])
        self.assertIsNotNone(release_info(value, "en")["download"])
        self.assertIsNone(release_info(value, "zh-CN")["download"])
        value["assets"][0]["browser_download_url"] = "https://unrelated.invalid/archive.zip"
        self.assertIsNone(release_info(value, "en")["download"])

    def test_older_release_is_not_offered_as_upgrade(self):
        value = {"tag_name": "v4.1.1", "assets": []}
        self.assertFalse(release_info(value, "en")["available"])


class UpdateDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_verifies_and_never_installs(self):
        value = UpdateTests().release()
        with tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
            updates = ProgramUpdates(Path(folder) / "updates", store)
            store.put_document("program_update", "en", release_info(value, "en"))
            calls = []
            def handle(request):
                calls.append(request)
                self.assertNotIn("authorization", request.headers)
                if request.url.host == "github.com":
                    return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/test.zip"})
                return httpx.Response(200, content=b"test")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            with patch("gpt56_vnext.updates.httpx.AsyncClient", return_value=client):
                result = await updates.download("4.6.0", "en")
            self.assertTrue(result["verified"])
            self.assertFalse(result["installed"])
            self.assertEqual(Path(result["path"]).read_bytes(), b"test")
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(Path(folder).rglob("*.part")), [])

    async def test_corruption_and_redirect_rejected_without_partial_package(self):
        for error, response in (("release_hash_mismatch", httpx.Response(200, content=b"evil")),
                                ("release_size_mismatch", httpx.Response(200, content=b"too large")),
                                ("release_redirect_rejected", httpx.Response(302, headers={"location": "https://evil.invalid/x"}))):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder, SQLiteStateStore(Path(folder) / "state.sqlite3") as store:
                updates = ProgramUpdates(Path(folder) / "updates", store)
                store.put_document("program_update", "en", release_info(UpdateTests().release(), "en"))
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response))
                with patch("gpt56_vnext.updates.httpx.AsyncClient", return_value=client), self.assertRaisesRegex(AppError, error):
                    await updates.download("4.6.0", "en")
                self.assertEqual(list(Path(folder).rglob("*.part")), [])
                self.assertEqual(list(Path(folder).rglob("*.zip")), [])


if __name__ == "__main__":
    unittest.main()
