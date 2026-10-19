from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.demo_model import DemoModel
from email_router.llm import ModelError
from email_router.runner import Runner
from email_router.store import Store
from email_router.tracing import main, records
from email_router.tools import Tools


class TracingTests(unittest.TestCase):
    def test_input_snapshot_uses_run_start_after_message_changes(self):
        for mode in ("agent", "workflow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "changed-message.sqlite")
                try:
                    store.seed(ROOT / "data/seed.json")
                    original = store.get("messages", "mail-02")
                    report = Runner(store, DemoModel(), mode).run("mail-02")
                    with store.db:
                        store.put("messages", original | {"text": "CHANGED_AFTER_RUN"})
                    regenerated = store.report(report["run"]["id"])
                    snapshot = next(r for r in records(regenerated, report["run"]["id"]) if r["kind"] == "input_snapshot")
                    self.assertEqual(snapshot["payload"], original)
                    self.assertEqual(snapshot["snapshot_source"], "run_start")
                    self.assertNotIn("CHANGED_AFTER_RUN", json.dumps(list(records(regenerated, report["run"]["id"]))))
                    # The reader must also handle reports created by the old Store.report.
                    report["message"] = original | {"text": "CHANGED_AFTER_RUN"}
                    snapshot = next(r for r in records(report, report["run"]["id"]) if r["kind"] == "input_snapshot")
                    self.assertEqual(snapshot["payload"], original)
                finally:
                    store.close()

    def test_critical_initial_input_not_restored_after_reclassification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "reclassified.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                marker = "CRITICAL_ORIGINAL_INPUT"
                original = store.get("messages", "mail-02")
                with store.db:
                    store.put("messages", original | {"text": marker, "data_classification": "critical"})
                class CloudFixture(DemoModel):
                    is_live = True
                report = Runner(store, CloudFixture()).run("mail-02")
                with store.db:
                    store.put("messages", original)
                regenerated = store.report(report["run"]["id"])
                self.assertTrue(regenerated.get("redacted_for_staff"))
                self.assertNotIn(marker, json.dumps(regenerated))
                self.assertNotIn(marker, json.dumps(list(records(regenerated, report["run"]["id"]))))
            finally:
                store.close()

    def test_invalid_model_response_records_safe_paired_error(self):
        marker = "PROVIDER_BODY_MUST_NOT_BE_LOGGED"
        for error in (json.JSONDecodeError("Invalid JSON", marker, 0), ValueError(marker)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                class InvalidModel(DemoModel):
                    def complete(self, *args, **kwargs):
                        raise error
                store = Store(Path(directory) / "invalid-response.sqlite")
                try:
                    store.seed(ROOT / "data/seed.json")
                    report = Runner(store, InvalidModel()).run("mail-02")
                    requests = [e for e in report["events"] if e["kind"] == "model_request"]
                    errors = [e for e in report["events"] if e["kind"] == "model_error"]
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(len(errors), 1)
                    self.assertEqual(errors[0]["call_id"], requests[0]["call_id"])
                    self.assertEqual(errors[0]["payload"]["error_type"], type(error).__name__)
                    self.assertGreaterEqual(errors[0]["duration_seconds"], 0)
                    self.assertGreaterEqual(errors[0]["ended_at"], errors[0]["started_at"])
                    self.assertEqual(report["events"][-1]["payload"]["status"], "failed")
                    self.assertNotIn(marker, json.dumps(report))
                finally:
                    store.close()

    def test_current_restrictions_do_not_restore_original_snapshot_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "new-restriction.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                original = store.get("messages", "mail-02")
                report = Runner(store, DemoModel()).run("mail-02")
                with store.db:
                    store.put("messages", original | {"text": "CURRENT_CRITICAL_TEXT", "data_classification": "critical"})
                regenerated = store.report(report["run"]["id"])
                snapshot = next(r for r in records(regenerated, report["run"]["id"]) if r["kind"] == "input_snapshot")
                self.assertEqual(snapshot["snapshot_source"], "run_start_redacted")
                self.assertTrue(snapshot["snapshot_redacted"])
                self.assertNotIn("text", snapshot["payload"])
                self.assertNotIn("CURRENT_CRITICAL_TEXT", json.dumps(list(records(regenerated, report["run"]["id"]))))
            finally:
                store.close()

    def test_unexpected_model_exception_is_reraised_after_safe_error_record(self):
        error = RuntimeError("PRIVATE_PROVIDER_RESPONSE")
        class FailingModel(DemoModel):
            def complete(self, *args, **kwargs):
                raise error
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "unexpected.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                runner = Runner(store, FailingModel())
                with self.assertRaises(RuntimeError) as caught:
                    runner.run("mail-02")
                self.assertIs(caught.exception, error)
                report = store.report(runner.run_id)
                request = next(e for e in report["events"] if e["kind"] == "model_request")
                failed = next(e for e in report["events"] if e["kind"] == "model_error")
                self.assertEqual(request["call_id"], failed["call_id"])
                self.assertEqual(failed["payload"]["error_type"], "RuntimeError")
                self.assertNotIn("PRIVATE_PROVIDER_RESPONSE", json.dumps(report))
            finally:
                store.close()

    def test_repeat_ids_versions_call_pairs_and_roles_in_both_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            ids, versions = set(), set()
            for mode in ("agent", "workflow"):
                for repeat in range(3):
                    store = Store(Path(directory) / f"{mode}-{repeat}.sqlite")
                    try:
                        store.seed(ROOT / "data/seed.json")
                        report = Runner(store, DemoModel(), mode).run("mail-02")
                        ids.add(report["run"]["id"])
                        config = report["run"]["config"]
                        versions.add((config["code_version"], config["dataset_version"]))
                        self.assertEqual(config["case_id"], "mail-02")
                        events = report["events"]
                        self.assertEqual([e["event_index"] for e in events], list(range(1, len(events) + 1)))
                        self.assertTrue(all(e["run_id"] == report["run"]["id"] and e["case_id"] == "mail-02" for e in events))
                        requests = {e["call_id"]: e for e in events if e["kind"] in {"model_request", "tool_request"}}
                        for event in events:
                            self.assertEqual(event["code_version"], config["code_version"])
                            self.assertEqual(event["model_config_version"], config["model_config_version"])
                            if event["kind"] not in {"model_response", "tool"}:
                                continue
                            request = requests.pop(event["call_id"])
                            self.assertLess(request["event_index"], event["event_index"])
                            self.assertGreaterEqual(event["ended_at"], event["started_at"])
                            self.assertGreaterEqual(event["duration_seconds"], 0)
                            if event["kind"] == "model_response":
                                self.assertEqual(event["actual_model"], event["payload"]["model"])
                                self.assertIn(event["model_role"], config["model_roles"])
                        self.assertEqual(requests, {})
                        provider_calls = {call["id"] for e in events if e["kind"] == "model_response"
                                          for call in e["payload"]["message"].get("tool_calls", [])}
                        for event in events:
                            if event["kind"] == "tool" and mode == "agent":
                                self.assertIn(event["provider_call_id"], provider_calls)
                        lines = list(records(report, report["run"]["id"]))
                        self.assertEqual(lines[0]["kind"], "input_snapshot")
                        self.assertEqual(lines[-1]["payload"]["actions"], report["actions"])
                    finally:
                        store.close()
            self.assertEqual(len(ids), 6)
            self.assertEqual(len(versions), 1)

    def test_dataset_fingerprint_changes_with_source_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            versions = []
            for status in ("not_shipped", "shipped"):
                store = Store(Path(directory) / (status + ".sqlite"))
                try:
                    store.seed(ROOT / "data/seed.json")
                    with store.db:
                        store.put("orders", store.get("orders", "O-52") | {"shipping_status": status})
                    report = Runner(store, DemoModel(), "workflow").run("mail-02")
                    versions.append(report["run"]["config"]["dataset_version"])
                finally:
                    store.close()
            self.assertNotEqual(*versions)

    def test_model_failure_and_interruption_preserve_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            for error in (ModelError("simulated failure", http_status=429), KeyboardInterrupt()):
                class FailingModel(DemoModel):
                    def complete(self, *args, **kwargs):
                        raise error
                store = Store(Path(directory) / (type(error).__name__ + ".sqlite"))
                try:
                    store.seed(ROOT / "data/seed.json")
                    runner = Runner(store, FailingModel())
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            runner.run("mail-02")
                        report = store.report(runner.run_id)
                        self.assertFalse(any(e["kind"] == "run_end" for e in report["events"]))
                    else:
                        report = runner.run("mail-02")
                        request = next(e for e in report["events"] if e["kind"] == "model_request")
                        failed = next(e for e in report["events"] if e["kind"] == "model_error")
                        self.assertEqual(request["call_id"], failed["call_id"])
                        self.assertGreaterEqual(failed["duration_seconds"], 0)
                    self.assertTrue(any(e["kind"] == "model_request" for e in report["events"]))
                finally:
                    store.close()

    def test_retry_has_separate_call_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "retry.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                report = Runner(store, DemoModel(), "workflow", {"get_order": 1}).run("mail-02")
                reads = [e for e in report["events"] if e["kind"] == "tool" and e["payload"]["name"] == "get_order"]
                self.assertEqual(len(reads), 2)
                self.assertNotEqual(reads[0]["call_id"], reads[1]["call_id"])
            finally:
                store.close()

    def test_tool_exception_preserves_failed_call_identity(self):
        original_read = Tools.read
        def read(tools, name, arguments):
            if name == "get_order":
                raise KeyError("simulated invalid source")
            return original_read(tools, name, arguments)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "tool-error.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                with patch.object(Tools, "read", read):
                    report = Runner(store, DemoModel(), "workflow").run("mail-02")
                error = next(e for e in report["events"] if e["kind"] == "tool_error")
                request = next(e for e in report["events"] if e["kind"] == "tool_request" and e["call_id"] == error["call_id"])
                self.assertEqual(error["payload"]["error_type"], "KeyError")
                self.assertLess(request["event_index"], error["event_index"])
                self.assertGreaterEqual(error["duration_seconds"], 0)
                self.assertEqual(report["events"][-1]["payload"]["status"], "failed")
            finally:
                store.close()

    def test_legacy_view_is_read_only_and_marks_unknown_fields(self):
        source = ROOT / "artifacts/live-source-boundary-20261019T193947383517Z/missing_shipping.json"
        before = source.read_bytes()
        report = json.loads(before)
        rows = list(records(report, report["run"]["id"]))
        self.assertEqual(rows[0]["snapshot_source"], "available_report_message")
        events = [r for r in rows if r["event_index"] is not None]
        self.assertTrue(all(r["call_id"] is None and r["duration_seconds"] is None for r in events))
        self.assertIsNone(events[0]["code_version"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--report", str(source), "--run-id", report["run"]["id"], "--event-index", "1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["event_index"], 1)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--report", str(source), "--run-id", "wrong-run"]), 2)
        self.assertEqual(source.read_bytes(), before)

    def test_critical_content_stays_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "critical.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                marker = "CRITICAL_MARKER_MUST_STAY_LOCAL"
                with store.db:
                    store.put("messages", store.get("messages", "mail-02") | {
                        "data_classification": "critical", "text": marker})
                class MustNotCallModel(DemoModel):
                    is_live = True
                    def complete(self, *args, **kwargs):
                        raise AssertionError("Critical context must not reach model")
                report = Runner(store, MustNotCallModel()).run("mail-02")
                report["experiment"] = {"private": marker}
                exported = json.dumps(list(records(report, report["run"]["id"])), ensure_ascii=False)
                self.assertNotIn(marker, exported)
            finally:
                store.close()

    def test_jsonl_export_refuses_existing_file(self):
        source = ROOT / "artifacts/live-source-boundary-20261019T193947383517Z/missing_shipping.json"
        report = json.loads(source.read_text())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "trace.jsonl"
            args = ["--report", str(source), "--run-id", report["run"]["id"], "--output", str(target)]
            self.assertEqual(main(args), 0)
            before = target.read_bytes()
            self.assertTrue(all(json.loads(line)["run_id"] == report["run"]["id"] for line in before.splitlines()))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(target.read_bytes(), before)

    def test_malformed_report_does_not_create_export_or_echo_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "bad.json", root / "output.jsonl"
            source.write_text(json.dumps({"run": {"id": "test-run"}, "message": None,
                                          "private": "DO_NOT_ECHO_THIS_CONTENT"}))
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = main(["--report", str(source), "--run-id", "test-run", "--output", str(target)])
            self.assertEqual(result, 2)
            self.assertNotIn("DO_NOT_ECHO_THIS_CONTENT", errors.getvalue())
            self.assertFalse(target.exists())

    def test_legacy_events_schema_upgrade_preserves_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, at REAL, kind TEXT, payload TEXT)")
            db.execute("INSERT INTO events(run_id,at,kind,payload) VALUES('old',1,'old_event','{}')")
            db.commit()
            db.close()
            store = Store(path)
            try:
                row = store.db.execute("SELECT run_id,kind,payload,metadata FROM events").fetchone()
                self.assertEqual(tuple(row), ("old", "old_event", "{}", "{}"))
            finally:
                store.close()
