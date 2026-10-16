import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.demo_model import DemoModel
from email_router.evaluate import assess_result, metrics, main, PacedModel
from email_router.runner import Runner
from email_router.store import Store


class EvaluationTests(unittest.TestCase):
    def test_continuation_keeps_failed_attempt_and_old_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "email_router").mkdir()
            for source in (ROOT / "email_router").glob("*.py"):
                (root / "email_router" / source.name).write_bytes(source.read_bytes())
            args = ["--offline", "--suite", str(ROOT / "data/evaluation-dev.json"),
                    "--seed", str(ROOT / "data/seed.json")]
            with patch("email_router.evaluate.ROOT", root), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                previous = next((root / "artifacts").iterdir())
                summary = json.loads((previous / "summary.json").read_text())
                # Simulate an interrupted suite with a failed sixth attempt.
                summary["checks"] = summary["checks"][:6]
                summary["checks"][-1]["passed"] = False
                summary["checks"][-1]["problems"] = ["simulated_provider_error"]
                summary["stopped"] = True
                (previous / "summary.json").write_text(json.dumps(summary))
                unchanged = {p.name: p.read_bytes() for p in previous.iterdir()}
                self.assertEqual(main(args + ["--continue-from", str(previous)]), 1)
                current = next(p for p in (root / "artifacts").iterdir() if p != previous)
                continued = json.loads((current / "summary.json").read_text())
                self.assertEqual(len(continued["checks"]), 17)
                self.assertEqual(sum(c["passed"] for c in continued["checks"]), 16)
                self.assertFalse((current / "workflow-mail-01.sqlite").exists())
                self.assertTrue((current / "agent-mail-03.sqlite").exists())
                for check in continued["checks"]:
                    self.assertTrue((current / check["trace"]).is_file(), check["trace"])
                self.assertEqual(unchanged, {p.name: p.read_bytes() for p in previous.iterdir()})
                # Refuse merging results from a different processing version.
                (root / "email_router/tools.py").write_text("changed")
                with self.assertRaisesRegex(ValueError, "Processing code changed"):
                    main(args + ["--continue-from", str(previous)])

    def test_pacing_spaces_calls_and_preserves_timeout_budget(self):
        clock = [0.0]
        waits, timeouts = [], []
        class Model:
            def complete(self, messages, tools, timeout):
                timeouts.append(timeout)
                clock[0] += 1
                return "response"
        def sleep(delay):
            waits.append(delay)
            clock[0] += delay
        model = PacedModel(Model(), 5, clock=lambda: clock[0], sleep=sleep)
        model.complete([], [], timeout=30)
        model.complete([], [], timeout=30)
        self.assertEqual(waits, [4])
        self.assertEqual(timeouts, [30, 26])
        from email_router.llm import ModelError
        with self.assertRaisesRegex(ModelError, "pacing_budget_exhausted"):
            model.complete([], [], timeout=2)
        self.assertEqual(len(timeouts), 2)

    def test_all_development_expectations_in_both_modes(self):
        suite = json.loads((ROOT / "data/evaluation-dev.json").read_text())
        for case in suite["cases"]:
            for mode in ("agent", "workflow"):
                with self.subTest(message=case["message"], mode=mode), tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / "state.sqlite")
                    try:
                        store.seed(ROOT / "data/seed.json")
                        initial = store.all("tickets")
                        report = Runner(store, DemoModel(), mode).run(case["message"])
                        self.assertEqual(assess_result(case, store, report, initial), [])
                        observed = metrics(report)
                        self.assertIsNone(observed["tokens"])
                        self.assertGreater(observed["model_calls"], 0)
                        if case.get("routes"):
                            altered = copy.deepcopy(report)
                            altered["outgoing"][0]["text"] = "Unauthorized customer content"
                            self.assertIn("unexpected_automatic_content", assess_result(case, store, altered, initial))
                            with store.db:
                                ticket = store.get("tickets", report["actions"][0]["ticket_id"])
                                store.put("tickets", ticket | {"customer_id": "wrong-customer"})
                            self.assertIn("wrong_saved_ticket", assess_result(case, store, report, initial))
                    finally:
                        store.close()

    def test_metrics_distinguish_blocked_attempts_and_staff_reviews(self):
        report = {"events": [
            {"kind": "tool", "payload": {"result": {"status": "blocked"}}},
            {"kind": "model_response", "payload": {"model": "fixture", "usage": {"total_tokens": 15}}},
            {"kind": "run_end", "payload": {"model_calls": 1, "tool_calls": 1, "elapsed_seconds": 2}}],
            "issues": [{"state": "routed"}], "reviews": [{"kind": "critical_data_review"}]}
        result = metrics(report)
        self.assertEqual(result["blocked_attempts"], 1)
        self.assertEqual(result["human_handoffs"], 0)
        self.assertEqual(result["staff_content_reviews"], 1)
        self.assertEqual(result["tokens"], {"total_tokens": 15})
