"""Offline defense rehearsal; explicitly scripted, never a Gemini evaluation."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from email_router.__main__ import ROOT
from email_router.regression import RegressionFixture, attempt, validate_suite


CASES = ('mail-09', 'mail-02', 'order-shipped', 'critical-boundary', 'order-missing-shipping')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args(argv)
    suite = json.loads((ROOT / 'data/evaluation-regression-v1.json').read_text())
    seed = json.loads((ROOT / 'data/seed-held-out.json').read_text())
    validate_suite(suite, seed)
    cases = {case['id']: case for case in suite['cases']}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory = args.directory or ROOT / 'artifacts/private' / ('defense-rehearsal-' + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    summary = {'live_model': False, 'description': 'Scripted infrastructure rehearsal; not model quality', 'checks': []}
    (directory / 'expectations.json').write_text(json.dumps([cases[name] for name in CASES], ensure_ascii=False, indent=2))
    for name in CASES:
        for mode in ('workflow', 'agent'):
            folder = directory / f'{name}-{mode}'
            check = attempt(folder, cases[name], seed, RegressionFixture(), mode)
            summary['checks'].append(dict(check, trace=str(folder / 'trace.json')))
            (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            report = json.loads((folder / 'trace.json').read_text())
            actual = [a.get('recipient_ref') or a.get('reason') for a in report['actions']]
            print(json.dumps({'case': name, 'mode': mode, 'offline': True, 'passed': check['passed'],
                              'actual': actual, 'run_id': check['run_id'], 'problems': check['problems']}, ensure_ascii=False))
    print('Offline rehearsal: ' + str(directory))
    return int(any(not c['passed'] for c in summary['checks']))


if __name__ == '__main__':
    raise SystemExit(main())
