import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.compare_modes import compare_modes, main


class CompareModesTests(unittest.TestCase):
    def summary(self, mode, outcomes):
        return {'live_model': True, 'modes': [mode], 'pending': 0,
                'suite': {'version': 'test', 'cases': [{'id': 'mail-01'}]}, 'seed_hash': 'fixed',
                'source_hashes': {'runner.py': 'same', 'regression.py': 'same'},
                'model_config': {'model': 'fixture'},
                'checks': [{'case_id': 'mail-01', 'mode': mode, 'repetition': i,
                            'run_id': mode + str(i), 'state': 'finished', 'passed': passed,
                            'problems': [] if passed else ['execution_error'], 'diagnostics': {},
                            'trace': f'{mode}-{i}/trace.jsonl',
                            'metrics': {'model_calls': 1, 'elapsed_seconds': 2,
                                        'actual_models': ['fixture'],
                                        'tokens': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}}
                           for i, passed in enumerate(outcomes, 1)]}

    def test_failure_stays_in_denominator_and_stability_and_traces(self):
        result = compare_modes(self.summary('workflow', [True] * 3), self.summary('agent', [True, False, True]))
        self.assertEqual(result['agent']['passed'], 2)
        self.assertEqual(result['agent']['launched'], 3)
        self.assertEqual(result['agent']['pass_at_3'], 1)
        self.assertEqual(result['agent']['pass_power_3'], 0)
        self.assertEqual(result['cases'][0]['agent']['problems'], {'execution_error': 1})
        self.assertEqual(len(result['cases'][0]['agent']['traces']), 3)

    def test_incomplete_mismatched_or_duplicate_slots_are_rejected(self):
        w, a = self.summary('workflow', [True] * 3), self.summary('agent', [True] * 3)
        for key, value in [('pending', 1), ('source_hashes', {}), ('seed_hash', 'changed'),
                           ('model_config', {}), ('live_model', False), ('suite', {'cases': [{'id': 'another'}]})]:
            changed = copy.deepcopy(a)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): compare_modes(w, changed)
        duplicate = copy.deepcopy(a)
        duplicate['checks'][1]['repetition'] = 1
        with self.assertRaises(ValueError): compare_modes(w, duplicate)
        changed = copy.deepcopy(a)
        changed['checks'][0]['metrics']['actual_models'] = ['another-model']
        with self.assertRaises(ValueError): compare_modes(w, changed)

    def test_reports_are_created_without_replacing_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ('workflow', 'agent'):
                (root / mode).mkdir()
                (root / mode / 'summary.json').write_text(json.dumps(self.summary(mode, [True] * 3)))
            args = ['--workflow', str(root / 'workflow'), '--agent', str(root / 'agent'), '--output', str(root / 'report.md')]
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(main(args), 0)
            files = {p.name: p.read_bytes() for p in root.glob('report.*')}
            with self.assertRaises(FileExistsError): main(args)
            self.assertEqual(files, {p.name: p.read_bytes() for p in root.glob('report.*')})

    def test_same_model_sets_with_different_distributions_are_rejected(self):
        w, a = self.summary('workflow', [True] * 3), self.summary('agent', [True] * 3)
        for summary, models in ((w, ['A', 'A', 'B']), (a, ['A', 'B', 'B'])):
            for check, model in zip(summary['checks'], models):
                check['metrics']['actual_models'] = [model]
        with self.assertRaisesRegex(ValueError, 'one identical actual provider model'):
            compare_modes(w, a)

    def test_empty_different_or_mixed_actual_models_are_rejected(self):
        for workflow_models, agent_models in (([], []), (['A'], ['B']), (['A', 'B'], ['A', 'B'])):
            with self.subTest(workflow=workflow_models, agent=agent_models):
                w, a = self.summary('workflow', [True] * 3), self.summary('agent', [True] * 3)
                for summary, models in ((w, workflow_models), (a, agent_models)):
                    for check in summary['checks']:
                        check['metrics']['actual_models'] = models
                with self.assertRaises(ValueError):
                    compare_modes(w, a)

    def test_attempts_without_model_calls_allow_same_known_model(self):
        w, a = self.summary('workflow', [True] * 3), self.summary('agent', [True] * 3)
        for summary in (w, a):
            summary['checks'][0]['metrics'].update(actual_models=[], model_calls=0, tokens=None)
        result = compare_modes(w, a)
        self.assertEqual(result['actual_models'], ['fixture'])
        self.assertEqual(result['workflow']['launched'], 3)
        self.assertEqual(result['agent']['launched'], 3)
