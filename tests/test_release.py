import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gpt56_vnext.benchmark import cells_by_id, load_package
from gpt56_vnext.catalog import BenchmarkCatalog, validate_index
from gpt56_vnext.detector import calibration_matches
from gpt56_vnext.normalizers import normalize_answer
from gpt56_vnext.probability_model import score_counts
from gpt56_vnext.server import create_server
from gpt56_vnext.transport import build_payload


class ReleaseTests(unittest.TestCase):
    def test_public_catalog_and_bound_packages(self):
        index = validate_index(json.loads((ROOT / 'benchmarks/index.json').read_text()))
        for item in index['packages']:
            raw = (ROOT / item['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item['sha256'])
            package = load_package(raw)
            self.assertEqual(package['content_sha256'], item['content_sha256'])
            for tier in package['tiers']:
                self.assertTrue(calibration_matches(package, tier))

    def test_offline_bundles_requests_and_invalid_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = BenchmarkCatalog(Path(folder), ROOT / 'gpt56_vnext/baselines/v4.5.1')
            for item in catalog.local():
                self.assertEqual(item['publisher'], 'maintainer')
                package = catalog.get(item['id'], item['version'])
                cells = cells_by_id(package)
                for cell in cells.values():
                    payload = build_payload(package['mode'], 'synthetic-model', cell)
                    self.assertEqual(cell['system'], '.')
                    self.assertEqual(cell['history'], [])
                    self.assertNotIn('tools', payload)
                    self.assertTrue(payload['stream'])
                for tier in package['tiers'].values():
                    self.assertIn(sum(tier['counts'].values()), (20, 50, 100))
                    counts = {identity: {'__INVALID_OUTPUT__': n} for identity, n in tier['counts'].items()}
                    self.assertEqual(score_counts(package['fitted'], counts, tier['counts'], tier['thresholds'])['color'], 'yellow')

    def test_server_resources_without_private_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            server = create_server(port=0, runs_root=folder)
            try:
                self.assertEqual(len(server.state.catalog.local()), 2)
            finally:
                server.server_close()
                server.state.close()

    def test_normalizer_unknown_and_invalid_differ(self):
        self.assertEqual(normalize_answer(' '), '__INVALID_OUTPUT__')
        self.assertEqual(normalize_answer('unseen'), 'unseen')
        self.assertEqual(normalize_answer('3', {'id':'b80_exact_3'}), 'exact_3')


if __name__ == '__main__':
    unittest.main()
