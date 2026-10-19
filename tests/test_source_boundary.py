import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.demo_model import DemoModel
from email_router.error_cases import OfflineCaseModel
from email_router.policy import SOURCE_KINDS
from email_router.source_boundary import assess_boundary, run_case
from email_router.store import Store, encode, uid
from email_router.tools import Tools


class ReviewSourceFixture(OfflineCaseModel):
    """Test-only model that hands observed order responses to staff."""

    def complete(self, messages, tools, timeout=30):
        answers = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if any((a.get("data") or {}).get("id") == "O-52" for a in answers):
            context = json.loads(messages[-1]["content"])
            arguments = {"issue_id": context["issues"][0]["id"], "reason": "conflicting_facts",
                         "summary": "Проверка ответа источника сотрудником",
                         "evidence_ids": [a["evidence_id"] for a in answers if a.get("evidence_id")]}
            return {"model": self.model, "usage": {}, "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": uid("call"), "type": "function", "function": {
                        "name": "handoff_to_human", "arguments": encode(arguments)}}]}}
        return super().complete(messages, tools, timeout)


class SourceBoundaryTests(unittest.TestCase):
    def test_timeout_or_complete_response_cannot_confirm_missing_field_case(self):
        full_order = next(o for o in json.loads((ROOT / "data/seed.json").read_text())["orders"] if o["id"] == "O-52")
        responses = {
            "timeout": {"status": "unavailable", "data": None, "candidates": [], "error": {"code": "source_timeout"}},
            "complete": {"status": "found", "data": full_order, "candidates": [], "error": None},
            "other_missing_field": {"status": "unavailable", "data": None, "candidates": [],
                                    "error": {"code": "source_data_incomplete", "missing_fields": ["payment_status"]}},
            "generic_validation_error": {"status": "unavailable", "data": None, "candidates": [],
                                         "error": {"code": "source_data_incomplete"}},
        }
        original_read = Tools.read
        for name, answer in responses.items():
            with self.subTest(response=name), tempfile.TemporaryDirectory() as directory:
                def read(tools, tool, arguments):
                    return copy.deepcopy(answer) if tool == "get_order" else original_read(tools, tool, arguments)
                with patch.object(Tools, "read", read):
                    result = run_case(Path(directory), "missing_shipping", ReviewSourceFixture())
                self.assertEqual(result["actual"], ["source_unavailable" if answer["status"] == "unavailable" else "conflicting_facts"])
                self.assertFalse(result["passed"])
                self.assertIn("incomplete_order_not_observed", result["problems"])
                self.assertFalse(result["incomplete_order_observed"])

    def test_missing_record_or_specific_validation_error_confirms_scenario(self):
        original_read = Tools.read
        for structured_error in (False, True):
            with self.subTest(structured_error=structured_error), tempfile.TemporaryDirectory() as directory:
                def read(tools, tool, arguments):
                    if tool == "get_order":
                        if structured_error:
                            return {"status": "unavailable", "data": None, "candidates": [],
                                    "error": {"code": "source_data_incomplete", "missing_fields": ["shipping_status"]}}
                        # Legacy adapter response, before order-field validation.
                        return {"status": "found", "data": tools.store.get("orders", "O-52"),
                                "candidates": [], "error": None}
                    return original_read(tools, tool, arguments)
                with patch.object(Tools, "read", read):
                    result = run_case(Path(directory), "missing_shipping", ReviewSourceFixture())
                self.assertTrue(result["passed"], result)
                self.assertTrue(result["incomplete_order_observed"])
                self.assertEqual(result["controller_errors"], [])

    def test_incomplete_source_is_controlled_in_both_modes_without_retry(self):
        for mode in ("agent", "workflow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result = run_case(root, "missing_shipping", OfflineCaseModel(), mode)
                self.assertTrue(result["passed"], result)
                self.assertEqual(result["controller_errors"], [])
                self.assertEqual(result["actual"], ["source_unavailable"])
                report = json.loads((root / "missing_shipping.json").read_text())
                reads = [e["payload"]["result"] for e in report["events"] if e["kind"] == "tool"
                         and e["payload"]["name"] == "get_order"]
                self.assertEqual(len(reads), 1)
                self.assertEqual(reads[0]["status"], "unavailable")
                self.assertIsNone(reads[0]["data"])
                self.assertEqual(reads[0]["error"]["code"], "source_data_incomplete")
                self.assertEqual(reads[0]["error"]["missing_fields"], ["shipping_status"])
                self.assertEqual(report["outgoing"], [])
                self.assertEqual(len(report["reviews"]), 1)

    def test_required_order_status_fields_cannot_be_missing_null_or_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                original = store.get("orders", "O-52")
                tools = Tools(store, "validation-test", "mail-02")
                for field in ("shipping_status", "payment_status"):
                    for value in ("missing", None, ""):
                        with self.subTest(field=field, value=value):
                            incomplete = dict(original)
                            if value == "missing":
                                incomplete.pop(field)
                            else:
                                incomplete[field] = value
                            with store.db:
                                store.put("orders", incomplete)
                            response = tools.read("get_order", {"customer_id": "C-21", "order_id": "O-52"})
                            self.assertEqual(response["status"], "unavailable")
                            self.assertIsNone(response["data"])
                            self.assertEqual(response["error"]["code"], "source_data_incomplete")
                            self.assertEqual(response["error"]["missing_fields"], [field])
                            self.assertEqual(store.get("orders", "O-52"), incomplete)
            finally:
                store.close()

    def test_case_inputs_differ_only_by_removed_shipping_field(self):
        for mode in ("agent", "workflow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertTrue(run_case(root, "complete_order", DemoModel(), mode)["passed"])
                # No assertion requiring the processing defect to remain unfixed.
                run_case(root, "missing_shipping", DemoModel(), mode)
                original = json.loads((root / "complete_order.seed.json").read_text())
                incomplete = json.loads((root / "missing_shipping.seed.json").read_text())
                expected = copy.deepcopy(original)
                next(o for o in expected["orders"] if o["id"] == "O-52").pop("shipping_status")
                self.assertEqual(incomplete, expected)

    def test_existing_artifacts_and_critical_inputs_refused_before_store(self):
        for suffix in (".sqlite", ".json", ".seed.json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ("complete_order" + suffix)
                path.write_bytes(b"previous experiment")
                with patch("email_router.source_boundary.Store") as store:
                    with self.assertRaises(FileExistsError):
                        run_case(root, "complete_order", DemoModel())
                    store.assert_not_called()
                self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, {path.name: b"previous experiment"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = json.loads((ROOT / "data/seed.json").read_text())
            seed["orders"][1]["data_classification"] = "critical"
            seed["orders"][1]["private_marker"] = "must-not-export"
            with patch("email_router.source_boundary.Store") as store:
                with self.assertRaisesRegex(ValueError, "synthetic"):
                    run_case(root, "missing_shipping", DemoModel(), seed=seed)
                store.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_handoff_is_not_misreported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite")
            try:
                store.seed(ROOT / "data/seed.json")
                initial = store.all("tickets")
                sources = {k: store.all(k) for k in SOURCE_KINDS if k != "tickets"}
                report = {"issues": [{"state": "human_review"}], "actions": [{"reason": "source_unavailable"}],
                          "outgoing": [], "reviews": [{"id": "test-review"}], "events": [
                              {"kind": "tool", "payload": {"name": "get_order", "arguments": {
                                  "customer_id": "C-21", "order_id": "O-52"}, "result": {
                                      "status": "found", "data": {"id": "O-52", "customer_id": "C-21"}}}},
                              {"kind": "run_end", "payload": {"status": "completed"}}]}
                self.assertEqual(assess_boundary("missing_shipping", store, report, initial, sources), [])
                report["events"].insert(0, {"kind": "controller_error", "payload": {"error": "shipping_status"}})
                report["events"][-1]["payload"]["status"] = "failed"
                problems = assess_boundary("missing_shipping", store, report, initial, sources)
                self.assertIn("controller_error", problems)
                self.assertIn("run_failed_instead_of_controlled_handoff", problems)
            finally:
                store.close()
