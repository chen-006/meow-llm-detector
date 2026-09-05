"""Offline startup/resume withdrawal parity; do not mutate frozen evidence."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_benchmarks import fixture
from test_execution import EchoModel, SECRET
from gpt56_vnext.benchmark import build_package
from gpt56_vnext.detector import DetectorSession
from gpt56_vnext.errors import AppError
from gpt56_vnext.server import AppState
from gpt56_vnext.utils import atomic_write_json


class WithdrawalTests(unittest.TestCase):
    def test_new_and_resumed_detection_share_offline_withdrawal_check(self):
        project, observations = fixture()
        package = build_package(project, observations)
        with tempfile.TemporaryDirectory() as folder:
            app = AppState(folder, bundled=False)
            try:
                app.catalog.install_local(package)
                config = {"base_url": "https://fixture.invalid/v1", "claimed_model": "a"}
                DetectorSession(app.store, "paused", package, config, SECRET, transport=EchoModel())
                app.store.update_session_status("paused", "paused")
                app.store.save_report("paused", {"historical": "preserve"})
                before = app.store.session("paused")
                jobs = app.store.frozen_jobs("paused", 0)
                index = {"schema_version": 1, "commit": "a" * 40, "packages": [{
                    "id": package["id"], "version": package["version"], "mode": package["mode"],
                    "publisher": "maintainer", "path": "benchmarks/official/fixture.meow.json",
                    "sha256": "b" * 64, "content_sha256": package["content_sha256"], "withdrawn": "security"}]}
                atomic_write_json(app.catalog.index_path, index)
                for extra in ({"package_id": package["id"], "package_version": package["version"]}, {"resume_id": "paused"}):
                    with patch("gpt56_vnext.server.AsyncTransport", side_effect=AssertionError("must reject before transport")):
                        with self.assertRaisesRegex(AppError, "package_withdrawn"):
                            app.call(app.start_run("detection", {**config, "key": SECRET, **extra}))
                self.assertEqual(app.store.session("paused"), before)
                self.assertEqual(app.store.frozen_jobs("paused", 0), jobs)
                self.assertEqual(app.store.progress("paused")["http_attempts"], 0)
                self.assertEqual(app.store.report("paused"), {"historical": "preserve"})
                self.assertEqual(len(app.store.list_sessions()), 1)
            finally:
                app.close()

    def test_nonwithdrawn_resume_uses_frozen_package_without_local_install(self):
        project, observations = fixture()
        package = build_package(project, observations)
        with tempfile.TemporaryDirectory() as folder:
            app = AppState(folder, bundled=False)
            sender = EchoModel()
            try:
                config = {"base_url": "https://fixture.invalid/v1", "claimed_model": "a"}
                DetectorSession(app.store, "offline", package, config, SECRET, transport=EchoModel())
                app.store.update_session_status("offline", "paused")
                before = app.store.session("offline")["config"]
                async def resume():
                    identity = await app.start_run("detection", {**config, "key": SECRET, "resume_id": "offline"})
                    await app.active[identity][1]
                with patch.object(app.catalog, "get", side_effect=AssertionError("no reinstall or download")), \
                        patch("gpt56_vnext.server.AsyncTransport", return_value=sender):
                    app.call(resume())
                self.assertEqual(sender.calls, 9)
                self.assertEqual(app.store.session("offline")["config"], before)
            finally:
                app.close()


if __name__ == "__main__": unittest.main()
