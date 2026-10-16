import json
import tempfile
import unittest
from pathlib import Path

from email_router.error_cases import OfflineCaseModel, run_case, assess_case
from email_router.store import Store
from email_router.__main__ import ROOT


class ErrorCaseChecks(unittest.TestCase):
    def test_three_cases_in_both_modes(self):
        for mode in ("agent", "workflow"):
            for case in ("critical", "ambiguous", "unavailable"):
                with self.subTest(mode=mode, case=case), tempfile.TemporaryDirectory() as directory:
                    result = run_case(Path(directory), case, OfflineCaseModel(), mode)
                    self.assertTrue(result["passed"], result)
                    if case == "critical":
                        self.assertEqual(result["model_calls"], 0)

    def test_assessor_rejects_failed_run_and_forbidden_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_case(root, "unavailable", OfflineCaseModel())
            report = json.loads((root / "unavailable.json").read_text())
            store = Store(root / "unavailable.sqlite")
            try:
                initial = store.all("tickets")
                report["events"][-1]["payload"]["status"] = "failed"
                with store.db:
                    store.put("tickets", {"id": "unexpected"})
                problems = assess_case("unavailable", store, report, 4, initial)
                self.assertIn("run_failed", problems)
                self.assertIn("tickets_changed", problems)
            finally:
                store.close()

    def test_critical_assessor_rejects_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_case(root, "critical", OfflineCaseModel())
            store = Store(root / "critical.sqlite")
            try:
                report = json.loads((root / "critical.json").read_text())
                self.assertIn("forbidden_model_call", assess_case(
                    "critical", store, report, 1, store.all("tickets")))
            finally:
                store.close()
