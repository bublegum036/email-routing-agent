from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import main
from email_router.demo_model import DemoModel
from email_router.policy import cloud_context_allowed
from email_router.runner import Runner
from email_router.store import Store
from email_router.tracing import records


ROOT = Path(__file__).resolve().parent.parent


class StoreSecurityTests(unittest.TestCase):
    def test_main_cli_preserves_critical_record_in_synthetic_seed_and_never_calls_model(self):
        for mode in ('workflow', 'agent'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                seed = json.loads((ROOT / 'data/seed.json').read_text())
                marker = 'CRITICAL_MESSAGE_MUST_REMAIN_LOCAL'
                seed['messages'][0].update(data_classification='critical', text=marker)
                source = root / 'seed.json'
                source.write_text(json.dumps(seed))
                model = DemoModel()
                model.is_live = True
                output = root / 'report.json'
                with patch('email_router.__main__.load_env'), \
                        patch('email_router.__main__.ChatModel', return_value=model), \
                        patch.object(model, 'complete', side_effect=AssertionError('Cloud boundary bypassed')) as call, \
                        redirect_stdout(io.StringIO()):
                    code = main(['run', '--mode', mode, '--message', 'mail-01', '--seed', str(source),
                                 '--db', str(root / 'state.sqlite'), '--output', str(output)])
                self.assertEqual(code, 0)  # Controlled staff handoff is a completed run.
                call.assert_not_called()
                report = json.loads(output.read_text())
                self.assertTrue(report['redacted_for_staff'])
                self.assertNotIn(marker, output.read_text())
                self.assertEqual(report['issues'][0]['state'], 'human_review')
                store = Store(root / 'state.sqlite')
                try:
                    self.assertEqual(store.get('messages', 'mail-01')['data_classification'], 'critical')
                    self.assertFalse(cloud_context_allowed(store))
                finally:
                    store.close()

    def test_seed_never_loosens_dataset_or_individual_restrictions(self):
        for dataset, individual, expected in (
                ('synthetic', 'critical', 'critical'), ('synthetic', 'restricted', 'restricted'),
                ('synthetic', None, None), ('critical', 'synthetic', 'critical'),
                ('restricted', 'synthetic', 'restricted')):
            with self.subTest(dataset=dataset, individual=individual), tempfile.TemporaryDirectory() as directory:
                seed = {'processing_date': '2026-10-20', 'data_classification': dataset,
                        'messages': [{'id': 'test', 'data_classification': individual}]}
                path = Path(directory) / 'seed.json'
                path.write_text(json.dumps(seed))
                store = Store(Path(directory) / 'state.sqlite')
                try:
                    store.seed(path)
                    self.assertEqual(store.get('messages', 'test')['data_classification'], expected)
                    self.assertFalse(cloud_context_allowed(store))
                finally:
                    store.close()

    def test_critical_order_history_stays_redacted_after_replacement_and_reopen(self):
        for mode in ('agent', 'workflow'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.sqlite'
                store = Store(path)
                store.seed(ROOT / 'data/seed.json')
                original = store.get('orders', 'O-52')
                marker = 'CRITICAL_HISTORICAL_ORDER'
                with store.db:
                    store.put('orders', original | {'data_classification': 'critical', 'private_note': marker})
                report = Runner(store, DemoModel(), mode).run('mail-02')
                run_id = report['run']['id']
                self.assertTrue(report['redacted_for_staff'])
                self.assertIn(marker, ' '.join(r[0] for r in store.db.execute('SELECT payload FROM events')))
                with store.db:
                    store.put('orders', original)
                store.close()
                store = Store(path)
                try:
                    self.assertTrue(cloud_context_allowed(store))
                    report = store.report(run_id)
                    self.assertTrue(report['redacted_for_staff'])
                    self.assertNotIn(marker, json.dumps(report))
                    self.assertNotIn(marker, json.dumps(list(records(report, run_id))))
                    self.assertTrue(store.db.execute(
                        "SELECT 1 FROM run_source_classifications WHERE run_id=? AND kind='orders' "
                        "AND source_id='O-52' AND restricted=1", (run_id,)).fetchone())
                finally:
                    store.close()

    def test_restriction_observed_midrun_is_sticky_and_legacy_provenance_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state.sqlite')
            try:
                store.seed(ROOT / 'data/seed.json')
                report = Runner(store, DemoModel(), 'workflow').run('mail-02')
                run_id = report['run']['id']
                self.assertFalse(report.get('redacted_for_staff', False))
                original = store.get('orders', 'O-52')
                with store.db:
                    store.put('orders', original | {'data_classification': 'critical'})
                store.event(run_id, 'source_snapshot', {'private': 'MIDRUN_CRITICAL'})
                with store.db:
                    store.put('orders', original)
                self.assertNotIn('MIDRUN_CRITICAL', json.dumps(list(records(store.report(run_id), run_id))))
                with store.db:
                    store.db.execute('DELETE FROM run_export_policy WHERE run_id=?', (run_id,))
                    store.db.execute('DELETE FROM run_source_classifications WHERE run_id=?', (run_id,))
                self.assertTrue(store.report(run_id)['redacted_for_staff'])
                store.event(run_id, 'legacy_resume', {})
                self.assertTrue(store.report(run_id)['redacted_for_staff'])
            finally:
                store.close()

    def test_private_permissions_under_permissive_umask_and_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_umask = os.umask(0o022)
            try:
                path = root / 'new' / 'nested' / 'state.sqlite'
                store = Store(path)
                store.close()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)
                path.parent.chmod(0o755)
                path.chmod(0o644)
                store = Store(path)
                store.close()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            finally:
                os.umask(old_umask)

    def test_symlink_database_directory_ancestor_and_sidecar_are_rejected(self):
        for target_type in ('database', 'parent', 'ancestor', 'sidecar'):
            with self.subTest(target=target_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / 'target'
                target.mkdir()
                protected = target / 'protected'
                protected.write_text('UNCHANGED_PRIVATE_FILE')
                before = protected.read_bytes()
                if target_type == 'database':
                    path = root / 'state.sqlite'
                    path.symlink_to(protected)
                elif target_type in ('parent', 'ancestor'):
                    link = root / 'link'
                    link.symlink_to(target, target_is_directory=True)
                    path = link / ('nested/state.sqlite' if target_type == 'ancestor' else 'state.sqlite')
                else:
                    path = root / 'state.sqlite'
                    Path(str(path) + '-wal').symlink_to(protected)
                with self.assertRaises(OSError):
                    Store(path)
                self.assertEqual(protected.read_bytes(), before)
                self.assertFalse((target / 'nested').exists())

    def test_hardlinked_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / 'protected'
            protected.write_text('UNCHANGED')
            path = root / 'state.sqlite'
            os.link(protected, path)
            with self.assertRaises(PermissionError):
                Store(path)
            self.assertEqual(protected.read_text(), 'UNCHANGED')

    def test_shared_nonsticky_ancestor_rejected_before_connection_race(self):
        for permissions in (0o777, 0o775):
            with self.subTest(permissions=oct(permissions)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shared = root / 'shared'
                shared.mkdir()
                shared.chmod(permissions)
                private = shared / 'private'
                private.mkdir(mode=0o700)
                target = root / 'attacker'
                target.mkdir()

                def swap_then_connect(*args, **kwargs):
                    private.rename(shared / 'displaced')
                    private.symlink_to(target, target_is_directory=True)
                    self.fail('SQLite must not be reached through an unsafe ancestor')

                with patch('email_router.store.sqlite3.connect', side_effect=swap_then_connect) as connect:
                    with self.assertRaises(PermissionError):
                        Store(private / 'state.sqlite')
                connect.assert_not_called()
                self.assertFalse((target / 'state.sqlite').exists())

    def test_swap_at_connect_is_detected_before_schema_or_data_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, replacement = root / 'private', root / 'replacement'
            private.mkdir(mode=0o700)
            replacement.mkdir(mode=0o700)
            target = replacement / 'state.sqlite'
            original_connect = sqlite3.connect
            db = original_connect(target)
            db.execute('CREATE TABLE sentinel(value TEXT)')
            db.execute("INSERT INTO sentinel VALUES('UNCHANGED')")
            db.commit()
            db.close()
            target.chmod(0o600)
            before = target.read_bytes()

            def swap_then_connect(*args, **kwargs):
                # Fault injection with this process's privileges exercises the exact gap.
                private.rename(root / 'displaced')
                private.symlink_to(replacement, target_is_directory=True)
                return original_connect(*args, **kwargs)

            with patch('email_router.store.sqlite3.connect', side_effect=swap_then_connect):
                with self.assertRaises(PermissionError):
                    Store(private / 'state.sqlite')
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual((root / 'displaced/state.sqlite').stat().st_size, 0)

    def test_swap_to_missing_database_does_not_create_unchecked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, replacement = root / 'private', root / 'replacement'
            private.mkdir(mode=0o700)
            replacement.mkdir(mode=0o700)
            original_connect = sqlite3.connect

            def swap_then_connect(*args, **kwargs):
                private.rename(root / 'displaced')
                private.symlink_to(replacement, target_is_directory=True)
                return original_connect(*args, **kwargs)

            with patch('email_router.store.sqlite3.connect', side_effect=swap_then_connect):
                with self.assertRaises(sqlite3.OperationalError):
                    Store(private / 'state.sqlite')
            self.assertFalse((replacement / 'state.sqlite').exists())

    def test_owned_entry_in_sticky_shared_ancestor_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / 'shared'
            shared.mkdir()
            shared.chmod(0o1777)
            path = shared / 'private' / 'state.sqlite'
            store = Store(path)
            store.close()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
