"""Offline reproduction from published packages. Never sends model requests."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gpt56_vnext.benchmark import collection_contract, load_package
from gpt56_vnext.detector import calibration_matches
from gpt56_vnext.simulation import calibrate
from gpt56_vnext.utils import atomic_write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['gpt', 'claude'])
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'reproduction')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'gpt56_vnext/baselines/v4.5.1/manifest.json').read_text())
    for item in manifest['packages']:
        package = load_package((ROOT / 'gpt56_vnext/baselines/v4.5.1' / item['file']).read_bytes())
        if args.mode and package['mode'] != args.mode:
            continue
        for tier, expected in package['calibration']['tiers'].items():
            if not calibration_matches(package, tier):
                raise RuntimeError('Calibration binding mismatch: ' + tier)
            if args.check_only:
                print(package['mode'], tier, 'binding OK')
                continue
            folder = args.output / package['mode'] / ('quick' if args.quick else 'full')
            result = calibrate(package['fitted'], package['tiers'][tier]['counts'],
                request_signature=collection_contract(package),
                total_batches=8000 if args.quick else expected['total_batches'],
                target=expected['target'], selection_target=expected['selection_target'], seed=expected['seed'],
                pool=package['calibration'].get('simulation_pool'), checkpoint=folder / (tier + '.checkpoint.json'))
            atomic_write_json(folder / (tier + '.json'), result)
            matches = all(result[key] == expected[key] for key in ('thresholds', 'confusion', 'contract'))
            print(package['mode'], tier, result['status'], 'quick check' if args.quick else f'exact reproduction={matches}', flush=True)
            if not args.quick and not matches:
                raise RuntimeError('Numerical reproduction differs; retain output and inspect runtime versions.')


if __name__ == '__main__':
    main()
