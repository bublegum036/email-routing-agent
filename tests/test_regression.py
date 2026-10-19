import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.regression import (RegressionFixture, aggregate, attempt, compare,
                                     diagnostics, main, validate_suite)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((ROOT / 'data/evaluation-regression-v1.json').read_text())
        self.seed = json.loads((ROOT / 'data/seed-held-out.json').read_text())

    def summary(self, checks):
        return {'suite': {'version': 'test', 'cases': [{'id': 'a'}, {'id': 'b'}]},
                'modes': ['agent'], 'checks': checks, 'seed_hash': 'same', 'live_model': False,
                'model_config': {'model': 'fixture'}}

    def check(self, case, repetition, passed):
        return {'case_id': case, 'mode': 'agent', 'repetition': repetition, 'passed': passed,
                'metrics': {'model_calls': 1, 'tokens': None, 'elapsed_seconds': 1}, 'diagnostics': {}}

    def test_three_attempt_metrics_are_per_case_and_keep_failures(self):
        checks = [self.check(case, i + 1, passed) for case, outcomes in
                  [('a', [True, False, True]), ('b', [False, False, False])]
                  for i, passed in enumerate(outcomes)]
        measured = aggregate(self.summary(checks))['agent']
        self.assertEqual((measured['passed'], measured['launched']), (2, 6))
        self.assertEqual(measured['pass_at_3'], .5)
        self.assertEqual(measured['pass_power_3'], 0)
        self.assertIsNone(aggregate(self.summary(checks[:-1]))['agent']['pass_at_3'])

    def test_compare_refuses_changed_data_expectations_or_model(self):
        before = self.summary([self.check('a', 1, False), self.check('b', 1, True)])
        after = self.summary([self.check('a', 1, True), self.check('b', 1, False)])
        result = compare(before, after)
        self.assertEqual(result['fixed'], [['a', 'agent', 1]])
        self.assertEqual(result['regressed'], [['b', 'agent', 1]])
        for key, value in [('seed_hash', 'changed'), ('suite', {}), ('model_config', {}), ('live_model', True)]:
            changed = copy.deepcopy(after)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare(before, changed)

    def test_all_cases_in_both_modes_are_fresh_and_critical_is_redacted(self):
        ids = set()
        with tempfile.TemporaryDirectory() as directory:
            for case in self.suite['cases']:
                for mode in ('agent', 'workflow'):
                    path = Path(directory) / (case['id'] + mode)
                    check = attempt(path, case, self.seed, RegressionFixture(), mode)
                    self.assertTrue(check['passed'], check)
                    self.assertNotIn(check['run_id'], ids)
                    ids.add(check['run_id'])
                    self.assertEqual(check['diagnostics']['forbidden_content_effects'], 0)
                    trace = json.loads((path / 'trace.json').read_text())
                    if case['variant'] == 'critical':
                        self.assertEqual(check['metrics']['model_calls'], 0)
                        self.assertTrue(trace['redacted_for_staff'])
                        self.assertNotIn('text', trace['message'])
                    previous = {p.name: p.read_bytes() for p in path.iterdir()}
                    with self.assertRaises(FileExistsError):
                        attempt(path, case, self.seed, RegressionFixture(), mode)
                    self.assertEqual(previous, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_critical_external_seed_is_refused_before_files_or_store(self):
        seed = copy.deepcopy(self.seed)
        seed['orders'][0]['data_classification'] = 'critical'
        seed['orders'][0]['private_marker'] = 'do-not-export'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'attempt'
            with patch('email_router.regression.Store') as store:
                with self.assertRaises(ValueError):
                    attempt(target, self.suite['cases'][0], seed, RegressionFixture(), 'agent')
                store.assert_not_called()
            self.assertFalse(target.exists())

    def test_timeout_cannot_pass_incomplete_record_regression(self):
        case = next(c for c in self.suite['cases'] if c['variant'] == 'missing_shipping')
        from email_router.tools import Tools
        original = Tools.read
        def read(tools, name, arguments):
            if name == 'get_order':
                return {'status': 'unavailable', 'data': None, 'candidates': [], 'error': {'code': 'source_timeout'}}
            return original(tools, name, arguments)
        with tempfile.TemporaryDirectory() as directory, patch.object(Tools, 'read', read):
            check = attempt(Path(directory) / 'attempt', case, self.seed, RegressionFixture(), 'agent')
            self.assertFalse(check['passed'])
            self.assertIn('incomplete_order_not_observed', check['problems'])

    def test_stop_and_continue_preserves_failed_slot_instead_of_retrying_it(self):
        from email_router.llm import ModelError
        class Failing(RegressionFixture):
            def complete(self, *args, **kwargs):
                raise ModelError('test_transport_error')
        suite = dict(self.suite, cases=self.suite['cases'][:1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'suite.json'
            source.write_text(json.dumps(suite))
            first, second = root / 'first', root / 'second'
            with patch('email_router.regression.RegressionFixture', Failing):
                self.assertEqual(main(['--offline', '--suite', str(source), '--mode', 'agent', '--directory', str(first)]), 1)
            summary = json.loads((first / 'summary.json').read_text())
            self.assertEqual((len(summary['checks']), summary['pending']), (1, 2))
            trace = first / summary['checks'][0]['trace']
            original = trace.read_bytes()
            self.assertEqual(main(['--offline', '--suite', str(source), '--mode', 'agent',
                                   '--continue-from', str(first), '--directory', str(second)]), 1)
            updated = json.loads((second / 'summary.json').read_text())
            self.assertEqual([c['repetition'] for c in updated['checks']], [1, 2, 3])
            self.assertEqual([c['passed'] for c in updated['checks']], [False, True, True])
            self.assertEqual(original, trace.read_bytes())
            self.assertEqual(updated['aggregate']['agent']['pass_power_3'], 0)

    def test_duplicate_checks_distinguish_planned_retry_and_do_not_hide_content(self):
        case = next(c for c in self.suite['cases'] if c['variant'] == 'transient')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'case'
            check = attempt(path, case, self.seed, RegressionFixture(), 'agent')
            self.assertEqual(check['diagnostics']['planned_technical_retries'], 1)
            self.assertEqual(check['diagnostics']['repeated_argument_calls'], 0)
            report = json.loads((path / 'trace.json').read_text())
            report['outgoing'][0]['text'] += ' customer content'
            self.assertEqual(diagnostics(case, report)['forbidden_content_effects'], 1)

    def test_versioned_suite_keeps_original_expectations_and_rejects_bad_metadata(self):
        validate_suite(self.suite, self.seed)
        for filename in ('evaluation-dev.json', 'evaluation-held-out.json'):
            for original in json.loads((ROOT / 'data' / filename).read_text())['cases']:
                current = next(c for c in self.suite['cases'] if c['id'] == original['message'])
                for key, value in original.items():
                    self.assertEqual(current[key], value)
        broken = copy.deepcopy(self.suite)
        broken['cases'].append(broken['cases'][0])
        with self.assertRaises(ValueError):
            validate_suite(broken, self.seed)

    def test_interruption_is_journaled_and_not_replaced_on_continuation(self):
        suite = dict(self.suite, cases=self.suite['cases'][:1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'suite.json'
            source.write_text(json.dumps(suite))
            first, second = root / 'first', root / 'second'
            with patch('email_router.regression.Runner.run', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    main(['--offline', '--suite', str(source), '--mode', 'agent', '--directory', str(first)])
            original = json.loads((first / 'summary.json').read_text())
            self.assertEqual(original['checks'][0]['state'], 'started')
            self.assertEqual(original['aggregate']['agent']['launched'], 1)
            self.assertEqual(original['aggregate']['agent']['unknown_call_count_attempts'], 1)
            self.assertEqual(main(['--offline', '--suite', str(source), '--mode', 'agent', '--continue-from',
                                   str(first), '--directory', str(second)]), 1)
            updated = json.loads((second / 'summary.json').read_text())
            self.assertEqual(len(updated['checks']), 3)
            self.assertEqual(updated['checks'][0]['problems'], ['interrupted_attempt'])
            self.assertEqual(updated['aggregate']['agent']['pass_power_3'], 0)

    def test_archived_runtime_can_continue_from_relative_directory(self):
        suite = dict(self.suite, cases=self.suite['cases'][:1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'suite.json'
            source.write_text(json.dumps(suite))
            first, second = root / 'first', root / 'second'
            self.assertEqual(main(['--offline', '--runtime-ref', 'HEAD', '--suite', str(source),
                                   '--mode', 'workflow', '--directory', str(first)]), 0)
            original = (first / 'summary.json').read_bytes()
            self.assertTrue((first / 'code' / 'runner.py').is_file())
            self.assertEqual(main(['--offline', '--runtime-ref', 'HEAD', '--suite', str(source),
                                   '--mode', 'workflow', '--continue-from', os.path.relpath(first, ROOT),
                                   '--directory', str(second)]), 0)
            self.assertEqual(original, (first / 'summary.json').read_bytes())
            self.assertEqual(len(json.loads((second / 'summary.json').read_text())['checks']), 3)

    def test_incompatible_variant_expectations_refused_before_files_or_runner(self):
        for original in self.suite['cases']:
            if original['variant'] == 'original':
                continue
            for change in ('expectation', 'message'):
                case = copy.deepcopy(original)
                if change == 'message':
                    case['message'] = 'mail-03'
                elif case.get('routes'):
                    case['routes'][0]['target'] = 'order_operations' if case['variant'] == 'shipped' else 'logistics'
                else:
                    case['outcome'] = 'forbidden'
                with self.subTest(case=case['id'], change=change), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'attempt'
                    with patch('email_router.regression.Runner') as runner:
                        with self.assertRaises(ValueError):
                            validate_suite(dict(self.suite, cases=[case]), self.seed)
                        with self.assertRaises(ValueError):
                            attempt(path, case, self.seed, RegressionFixture(), 'agent')
                        runner.assert_not_called()
                    self.assertFalse(path.exists())

    def test_declared_expectations_remain_required_in_special_cases(self):
        # Even if validation or a special oracle is bypassed, the declared
        # expected result must still reject a conflicting actual effect.
        for original in self.suite['cases']:
            if original['variant'] == 'original':
                continue
            case = copy.deepcopy(original)
            if case.get('routes'):
                case['routes'][0]['target'] = 'sergey'
            else:
                case['outcome'] = 'forbidden'
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as directory:
                with patch('email_router.regression.validate_suite'):
                    check = attempt(Path(directory) / 'attempt', case, self.seed, RegressionFixture(), 'agent')
                self.assertFalse(check['passed'], check)
                if case.get('routes'):
                    self.assertIn('wrong_recipients_or_action_count', check['problems'])
                    self.assertEqual(check['diagnostics']['false_routes'], 1)
                else:
                    self.assertIn('expected_forbidden_request_handoff', check['problems'])

    def test_changed_saved_clarification_text_fails_full_attempt_in_both_modes(self):
        from email_router.runner import Runner
        original_run = Runner.run
        case = next(c for c in self.suite['cases'] if c.get('outcome') == 'identity')
        for mode in ('agent', 'workflow'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                def run(runner, message_id):
                    report = original_run(runner, message_id)
                    outgoing = dict(report['outgoing'][0], text='arbitrary-customer-content-marker')
                    with runner.store.db:
                        runner.store.db.execute('UPDATE outgoing SET payload=? WHERE id=?',
                                                (json.dumps(outgoing), outgoing['id']))
                    return runner.store.report(report['run']['id'])
                with patch.object(Runner, 'run', run):
                    check = attempt(Path(directory) / 'attempt', case, self.seed, RegressionFixture(), mode)
                self.assertFalse(check['passed'], check)
                self.assertEqual(check['diagnostics']['forbidden_content_effects'], 1)
                self.assertIn('forbidden_automatic_content', check['problems'])

    def test_clarification_contract_rejects_missing_unknown_or_mismatched_fields(self):
        case = next(c for c in self.suite['cases'] if c.get('outcome') == 'identity')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'attempt'
            check = attempt(path, case, self.seed, RegressionFixture(), 'agent')
            self.assertTrue(check['passed'], check)
            report = json.loads((path / 'trace.json').read_text())
            for fields in ([], None, ['unrecognized'], ['company_identifier', 'company_identifier'], ['order_id'], [{}]):
                changed = copy.deepcopy(report)
                changed['actions'][0]['missing_fields'] = fields
                with self.subTest(fields=fields):
                    self.assertEqual(diagnostics(case, changed)['forbidden_content_effects'], 1)


if __name__ == '__main__':
    unittest.main()
