import copy
import io
import contextlib
import errno
import http.client
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from monitoring.exporter import DIAGNOSTICS, collect, config, handler, project, summary_path
from monitoring import manage

ROOT = Path(__file__).resolve().parent.parent


class MonitoringTests(unittest.TestCase):
    def summary(self):
        return {'live_model': True, 'modes': ['workflow'], 'suite': {'cases': [{'id': 'mail-01'}]},
                'checks': [{'case_id': 'mail-01', 'mode': 'workflow', 'repetition': 1,
                            'passed': True, 'state': 'finished',
                            'metrics': {'model_calls': 1, 'elapsed_seconds': 2,
                                        'tokens': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}},
                            'diagnostics': dict.fromkeys(DIAGNOSTICS, 0) | {'routes': 1}}]}

    def save(self, root, name, summary):
        folder = root / name
        folder.mkdir()
        (folder / 'summary.json').write_text(json.dumps(summary))
        return folder

    def test_real_saved_results_are_projected_without_historical_failure_alerts(self):
        source = ROOT / 'artifacts/live-regression-20261019T203453740389Z/summary.json'
        result = project(json.loads(source.read_text()))['workflow']
        self.assertEqual((result['passed'], result['failed'], result['model_calls']), (51, 0, 48))
        self.assertEqual(result['total_tokens'], 62123)
        self.assertEqual(result['pass_power_3_ratio'], 1)
        self.assertEqual(result['unknown_usage_attempts'], 0)
        baseline = json.loads((ROOT / 'artifacts/step8-workflow-before-final/summary.json').read_text())
        self.assertEqual(project(baseline)['workflow']['failed'], 3)
        rules = json.loads((ROOT / 'monitoring/prometheus/alerts.json').read_text())['groups'][0]['rules']
        for rule in rules:
            if rule['alert'] not in {'EmailRouterExporterUnavailable'}:
                self.assertIn('role="current"', rule['expr'])

    def test_aggregate_free_text_and_unknown_fields_never_reach_metrics(self):
        summary = self.summary()
        marker = 'private@example.invalid SECRET marker\nmetric_injection 9'
        summary['aggregate'] = {'passed': 9999}
        summary['suite']['cases'][0]['text'] = marker
        summary['checks'][0]['problems'] = [marker]
        summary['checks'][0]['metrics']['provider_body'] = marker
        summary['checks'][0]['diagnostics'][marker] = 22
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, 'current', summary)
            output = collect(root, [{'id': 'current', 'role': 'current', 'directory': 'current'}])
        self.assertNotIn(marker, output)
        self.assertNotIn('private@', output)
        self.assertNotIn('9999', output)
        self.assertIn('snapshot_passed{execution="live",mode="workflow",role="current",source="current"} 1', output)
        self.assertNotIn('pass_power_3_ratio', output)  # Two attempts are still unlaunched.

    def test_latest_live_selector_does_not_fall_back_from_broken_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, 'live-regression-20261019T200000000000Z', self.summary())
            self.save(root, 'offline-regression-20261020T200000000000Z', self.summary())
            latest = self.save(root, 'live-regression-20261020T200000000000Z', self.summary())
            (latest / 'summary.json').write_text('{invalid private marker')
            source = {'id': 'current', 'role': 'current', 'latest_live': True}
            self.assertEqual(summary_path(root, source).parent, latest.resolve())
            output = collect(root, [source])
            self.assertIn('source_loaded{role="current",source="current"} 0', output)
            self.assertNotIn('snapshot_passed', output)
            self.assertNotIn('private marker', output)

    def test_latest_run_without_summary_does_not_publish_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, 'live-regression-20261019T200000000000Z', self.summary())
            latest = root / 'live-regression-20261020T200000000000Z'
            latest.mkdir()
            source = {'id': 'current', 'role': 'current', 'latest_live': True}
            self.assertEqual(summary_path(root, source), latest.resolve() / 'summary.json')
            output = collect(root, [source])
            self.assertIn('source_loaded{role="current",source="current"} 0', output)
            self.assertNotIn('email_router_snapshot_', output)
            # Publishing the new summary recovers automatically on the next scrape.
            new_summary = self.summary()
            new_summary['checks'][0]['passed'] = False
            (latest / 'summary.json').write_text(json.dumps(new_summary))
            output = collect(root, [source])
            self.assertIn('source_loaded{role="current",source="current"} 1', output)
            self.assertIn('snapshot_failed{execution="live",mode="workflow",role="current",source="current"} 1', output)

    def test_finished_attempt_requires_every_diagnostic_field(self):
        for missing in (*DIAGNOSTICS, 'entire_diagnostics'):
            summary = self.summary()
            if missing == 'entire_diagnostics':
                del summary['checks'][0]['diagnostics']
            else:
                del summary['checks'][0]['diagnostics'][missing]
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    project(summary)
                root = Path(directory)
                self.save(root, 'sample', summary)
                output = collect(root, [{'id': 'current', 'role': 'current', 'directory': 'sample'}])
                self.assertIn('source_loaded{role="current",source="current"} 0', output)
                self.assertNotIn('email_router_snapshot_', output)

    def test_started_attempt_keeps_unknown_diagnostics_visible(self):
        summary = self.summary()
        summary['checks'][0].update(state='started', passed=False, diagnostics={})
        values = project(summary)['workflow']
        self.assertEqual(values['incomplete'], 1)
        self.assertEqual(values['unknown_diagnostics_attempts'], 1)
        summary['checks'][0]['state'] = 'finished'
        with self.assertRaises(ValueError):
            project(summary)

    def test_latest_symlink_run_is_unavailable_instead_of_using_older_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, 'live-regression-20261019T200000000000Z', self.summary())
            (root / 'live-regression-20261020T200000000000Z').symlink_to(root / 'missing', target_is_directory=True)
            output = collect(root, [{'id': 'current', 'role': 'current', 'latest_live': True}])
            self.assertIn('source_loaded{role="current",source="current"} 0', output)
            self.assertNotIn('email_router_snapshot_', output)

    def test_duplicate_and_invalid_attempts_fail_closed_without_partial_metrics(self):
        for change in ('duplicate', 'nan', 'negative', 'bool', 'unknown_case', 'bad_mode', 'bad_usage'):
            summary = self.summary()
            check = summary['checks'][0]
            if change == 'duplicate': summary['checks'].append(copy.deepcopy(check))
            elif change == 'nan': check['metrics']['elapsed_seconds'] = float('nan')
            elif change == 'negative': check['metrics']['model_calls'] = -1
            elif change == 'bool': check['metrics']['model_calls'] = True
            elif change == 'unknown_case': check['case_id'] = 'unregistered'
            elif change == 'bad_mode': check['mode'] = 'customer@example.invalid'
            else: check['metrics']['tokens'] = ['invalid']
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.save(root, 'sample', summary)
                output = collect(root, [{'id': 'sample', 'role': 'current', 'directory': 'sample'}])
                self.assertIn('source_loaded{role="current",source="sample"} 0', output)
                self.assertNotIn('snapshot_', output)

    def test_missing_or_partial_usage_is_distinct_from_zero_cloud_calls(self):
        for usage, calls, unknown in [(None, 1, 1), ({'prompt_tokens': 3}, 1, 1), (None, 0, 0)]:
            summary = self.summary()
            summary['checks'][0]['metrics'].update(tokens=usage, model_calls=calls)
            with self.subTest(usage=usage, calls=calls):
                self.assertEqual(project(summary)['workflow']['unknown_usage_attempts'], unknown)
        summary = self.summary()
        summary['checks'][0].update(state='started', passed=False, metrics={'model_calls': None, 'tokens': None})
        values = project(summary)['workflow']
        self.assertEqual(values['unknown_call_count_attempts'], 1)
        self.assertEqual(values['incomplete'], 1)

    def test_three_attempt_stability_keeps_failures(self):
        summary = self.summary()
        first = summary['checks'][0]
        summary['checks'] = [dict(first, repetition=i, passed=i != 2) for i in (1, 2, 3)]
        values = project(summary)['workflow']
        self.assertEqual((values['passed'], values['failed']), (2, 1))
        self.assertEqual((values['pass_at_3_ratio'], values['pass_power_3_ratio']), (1, 0))

    def test_source_config_and_symlinks_cannot_export_external_paths(self):
        for source in [
            {'id': 'private"\nmetric 1', 'role': 'current', 'directory': 'sample'},
            {'id': 'sample', 'role': 'current', 'directory': '../secrets'},
            {'id': 'sample', 'role': 'baseline', 'latest_live': True},
        ]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'sources.json'
                path.write_text(json.dumps({'sources': [source]}))
                with self.assertRaises(ValueError): config(path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outside').mkdir()
            (root / 'sample').symlink_to(root / 'outside', target_is_directory=True)
            with self.assertRaises(ValueError):
                summary_path(root, {'directory': 'sample'})

    def test_offline_input_cannot_masquerade_as_current_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = self.summary()
            summary['live_model'] = False
            self.save(root, 'sample', summary)
            self.assertIn('source_loaded{role="current",source="sample"} 0',
                          collect(root, [{'id': 'sample', 'role': 'current', 'directory': 'sample'}]))
            self.assertIn('execution="offline"',
                          collect(root, [{'id': 'sample', 'role': 'offline', 'directory': 'sample'}]))

    def test_local_setup_does_not_overwrite_secret_and_down_keeps_volumes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage, 'ROOT', Path(directory)):
            (Path(directory) / 'monitoring').mkdir()
            manage.prepare()
            secret = Path(directory) / 'monitoring/local/grafana_admin_password'
            original = secret.read_bytes()
            manage.prepare()
            self.assertEqual(secret.read_bytes(), original)
            self.assertEqual(secret.parent.stat().st_mode & 0o777, 0o700)
        with patch.object(manage.subprocess, 'call', return_value=0) as call:
            self.assertEqual(manage.main(['down']), 0)
            command = call.call_args.args[0]
            self.assertNotIn('-v', command)
            self.assertIn('--env-file', command)

    def test_http_responses_are_fixed_and_do_not_echo_request_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, 'sample', self.summary())
            factory = handler(root, [{'id': 'sample', 'role': 'current', 'directory': 'sample'}])
            for path, status in [('/metrics', 200), ('/healthz', 200), ('/private-customer-content', 404)]:
                request = factory.__new__(factory)
                request.path, request.wfile = path, io.BytesIO()
                responses, headers = [], {}
                request.send_response = responses.append
                request.send_header = lambda key, value: headers.update({key: value})
                request.end_headers = lambda: None
                request.do_GET()
                body = request.wfile.getvalue()
                self.assertEqual(responses, [status])
                self.assertEqual(int(headers['Content-Length']), len(body))
                self.assertNotIn(b'private-customer-content', body)
                if path == '/metrics':
                    self.assertIn(b'email_router_source_loaded', body)

    def test_readiness_reports_each_failure_without_provider_or_secret_text(self):
        def get(url):
            if url.endswith('/healthz'):
                raise OSError('private-secret-marker')
            if url.endswith('/metrics'):
                return b'email_router_source_loaded{role="current",source="current"} 0\n'
            if url.endswith('/api/health'):
                return b'{"database":"ok"}'
            if '/query?' in url:
                return b'{"data":{"result":[]}}'
            return b'Prometheus ready'
        output = io.StringIO()
        with patch.object(manage, 'get', get), contextlib.redirect_stdout(output):
            self.assertEqual(manage.main(['check']), 1)
        results = json.loads(output.getvalue())
        self.assertEqual(len(results), 5)
        self.assertFalse(results['exporter_http']['ok'])
        self.assertEqual(results['current_source']['reason'], 'source_not_loaded')
        self.assertTrue(results['grafana_database']['ok'])
        self.assertTrue(results['prometheus_http']['ok'])
        self.assertEqual(results['prometheus_scrape']['reason'], 'exporter_target_not_up')
        self.assertNotIn('private-secret-marker', output.getvalue())

    def test_readiness_waits_for_scrape_and_accepts_numeric_up_value(self):
        def get(url):
            if url.endswith('/healthz'): return b'ok\n'
            if url.endswith('/metrics'): return b'email_router_source_loaded{role="current",source="current"} 1\n'
            if url.endswith('/api/health'): return b'{"database":"ok"}'
            if '/query?' in url: return b'{"data":{"result":[{"value":[1,"1.0"]}]}}'
            return b'ready'
        with patch.object(manage, 'get', get):
            results = manage.readiness()
            self.assertTrue(all(item['ok'] for item in results.values()))
        failed = {name: {'ok': False, 'reason': 'connection_or_response_error'} for name in results}
        healthy = {name: {'ok': True, 'reason': 'ok'} for name in failed}
        with tempfile.TemporaryDirectory() as directory, patch.object(manage, 'prepare'), \
                patch.object(manage.subprocess, 'run'), patch.object(manage, 'readiness', side_effect=[failed, healthy]), \
                patch.object(manage.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manage.main(['up']), 0)
            sleep.assert_called_once_with(5)

    def test_network_failure_reasons_are_specific_and_do_not_echo_exceptions(self):
        examples = [
            (urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'private')), 'connection_refused'),
            (ConnectionResetError(errno.ECONNRESET, 'private'), 'connection_reset'),
            (PermissionError(errno.EPERM, 'private'), 'permission_denied'),
            (TimeoutError('private'), 'connection_timeout'),
            (http.client.RemoteDisconnected('private'), 'connection_closed'),
            (json.JSONDecodeError('private', 'secret-document', 0), 'invalid_response'),
        ]
        for exception, expected in examples:
            with self.subTest(expected=expected), patch.object(manage, 'get', side_effect=exception):
                result = manage.readiness()
                self.assertEqual(len(result), 5)
                self.assertTrue(all(not r['ok'] and r['reason'] == expected for r in result.values()))
                self.assertNotIn('private', json.dumps(result))
                self.assertNotIn('secret-document', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
