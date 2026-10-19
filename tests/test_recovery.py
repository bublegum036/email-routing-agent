import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.__main__ import ROOT
from email_router.demo_model import DemoModel
from email_router.recovery import assess_recovery, run_case
from email_router.store import Store


class RecoveryTests(unittest.TestCase):
    def test_repeated_case_refuses_before_store_and_preserves_all_files(self):
        for mode in ("agent", "workflow"):
            for case in ("transient_order", "normal_order"):
                with self.subTest(mode=mode, case=case), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self.assertTrue(run_case(root, case, DemoModel(), mode)["passed"])
                    before = {p.name: p.read_bytes() for p in root.iterdir()}
                    with patch("email_router.recovery.Store") as store:
                        with self.assertRaises(FileExistsError):
                            run_case(root, case, DemoModel(), mode)
                        store.assert_not_called()
                    self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_existing_database_or_trace_alone_prevents_execution(self):
        for suffix in (".sqlite", ".json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ("transient_order" + suffix)
                path.write_bytes(b"previous experiment must remain unchanged")
                with patch("email_router.recovery.Store") as store:
                    with self.assertRaises(FileExistsError):
                        run_case(root, "transient_order", DemoModel())
                    store.assert_not_called()
                self.assertEqual({path.name: path.read_bytes()},
                                 {p.name: p.read_bytes() for p in root.iterdir()})

    def test_transient_failure_and_normal_letter_in_both_modes(self):
        for mode in ("agent", "workflow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                transient = run_case(root, "transient_order", DemoModel(), mode)
                normal = run_case(root, "normal_order", DemoModel(), mode)
                self.assertTrue(transient["passed"], transient)
                self.assertTrue(normal["passed"], normal)
                self.assertEqual(transient["order_reads"], ["unavailable", "found"])
                self.assertEqual(normal["order_reads"], ["found"])
                for name in ("transient_order", "normal_order"):
                    report = json.loads((root / (name + ".json")).read_text())
                    self.assertEqual(report["actions"][0]["ticket_operation"], "created")
                    self.assertEqual(len(report["outgoing"]), 1)
                    self.assertEqual(report["outgoing"][0]["kind"], "staff_routing_notice")
                    self.assertEqual(len(report["reviews"]), 1)

    def test_assessor_rejects_effect_before_confirmed_read_and_changed_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_case(root, "transient_order", DemoModel())
            report = json.loads((root / "transient_order.json").read_text())
            store = Store(root / "transient_order.sqlite")
            original = Store(root / "original.sqlite")
            try:
                original.seed(ROOT / "data/seed.json")
                tickets = original.all("tickets")
                sources = {k: original.all(k) for k in ("orders", "contracts", "customers", "contacts")}
                altered = copy.deepcopy(report)
                route = next(e for e in altered["events"] if e["kind"] == "tool" and e["payload"]["name"] == "route_request")
                altered["events"].remove(route)
                altered["events"].insert(0, route)
                with store.db:
                    store.put("orders", store.get("orders", "O-52") | {"shipping_status": "shipped"})
                problems, _ = assess_recovery("transient_order", store, altered, tickets, sources)
                self.assertIn("routed_before_source_recovered", problems)
                self.assertIn("source_facts_changed", problems)
                # Missing the injected failure must not count as exercising recovery.
                altered = copy.deepcopy(report)
                altered["events"] = [e for e in altered["events"] if not (
                    e["kind"] == "tool" and e["payload"]["name"] == "get_order"
                    and e["payload"]["result"]["status"] == "unavailable")]
                problems, _ = assess_recovery("transient_order", store, altered, tickets, sources)
                self.assertIn("wrong_recovery_read_sequence", problems)
            finally:
                store.close()
                original.close()
