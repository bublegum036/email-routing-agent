import copy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from email_router.demo_model import DemoModel
from email_router.__main__ import ROOT
from email_router.policy import SOURCE_KINDS
from email_router.shipping_change import ShippingFixture, assess_shipping, main, run_case
from email_router.store import Store


class ShippingChangeTests(unittest.TestCase):
    def test_restricted_seed_rejected_before_store_model_or_export(self):
        original = json.loads((ROOT / "data/seed.json").read_text())
        for classification in ("critical", None, "critical_order", "unclassified_order"):
            with self.subTest(classification=classification), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "data").mkdir()
                output = root / "output"
                output.mkdir()
                seed = copy.deepcopy(original)
                seed["orders"][1]["private_marker"] = "CRITICAL_CONTENT_MUST_NOT_BE_EXPORTED"
                if classification == "critical":
                    seed["data_classification"] = "critical"
                elif classification is None:
                    seed.pop("data_classification")
                else:
                    seed["orders"][1]["data_classification"] = (
                        "critical" if classification == "critical_order" else None)
                path = root / "data/seed.json"
                path.write_text(json.dumps(seed))
                original_bytes = path.read_bytes()
                with patch("email_router.shipping_change.ROOT", root), \
                        patch("email_router.shipping_change.Store") as store, \
                        patch("email_router.shipping_change.Runner") as runner, \
                        patch("email_router.shipping_change.ChatModel") as model:
                    with self.assertRaisesRegex(ValueError, "synthetic"):
                        run_case(output, "shipped", ShippingFixture())
                    store.assert_not_called()
                    runner.assert_not_called()
                    self.assertEqual(list(output.iterdir()), [])
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            main([])
                    self.assertEqual(error.exception.code, 2)
                    model.assert_not_called()
                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertFalse((root / "artifacts").exists())

    def test_assessor_rejects_route_before_order_and_unreferenced_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(run_case(root, "shipped", ShippingFixture())["passed"])
            report = json.loads((root / "shipped.json").read_text())
            store = Store(root / "shipped.sqlite")
            try:
                initial = [store.get("tickets", "T-9")]
                sources = {k: store.all(k) for k in SOURCE_KINDS if k != "tickets"}
                self.assertEqual(assess_shipping("shipped", store, report, initial, sources), [])
                altered = copy.deepcopy(report)
                route = next(e for e in altered["events"] if e["kind"] == "tool" and e["payload"]["name"] == "route_request")
                altered["events"].remove(route)
                altered["events"].insert(0, route)
                self.assertIn("routed_before_confirmed_order", assess_shipping(
                    "shipped", store, altered, initial, sources))
                altered = copy.deepcopy(report)
                route = next(e for e in altered["events"] if e["kind"] == "tool" and e["payload"]["name"] == "route_request")
                route["payload"]["arguments"]["evidence_ids"] = ["unobserved-order"]
                self.assertIn("routed_before_confirmed_order", assess_shipping(
                    "shipped", store, altered, initial, sources))
            finally:
                store.close()

    def test_redacted_report_is_not_augmented_or_exported(self):
        marker = "LATE_CRITICAL_CONTENT_MUST_NOT_BE_EXPORTED"

        class RestrictedRunner:
            def __init__(self, store, model, mode):
                self.store = store

            def run(self, message):
                with self.store.db:
                    self.store.put("orders", self.store.get("orders", "O-52") | {
                        "data_classification": "critical", "private_marker": marker})
                return {"redacted_for_staff": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("email_router.shipping_change.Runner", RestrictedRunner):
                with self.assertRaisesRegex(ValueError, "restricted report refused"):
                    run_case(root, "shipped", ShippingFixture())
            self.assertFalse((root / "shipped.json").exists())
            for path in root.glob("*.json"):
                self.assertNotIn(marker, path.read_text())

    def test_both_modes_change_only_shipping_fact_and_route(self):
        for mode in ("agent", "workflow"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sources = {}
                for status, recipient in (("not_shipped", "order_operations"), ("shipped", "logistics")):
                    result = run_case(root, status, ShippingFixture(), mode)
                    self.assertTrue(result["passed"], result)
                    self.assertEqual(result["actual"], [recipient])
                    report = json.loads((root / (status + ".json")).read_text())
                    self.assertEqual(len(report["actions"]), 1)
                    self.assertEqual(len(report["outgoing"]), 1)
                    self.assertEqual(report["actions"][0]["ticket_operation"], "created")
                    store = Store(root / (status + ".sqlite"))
                    try:
                        sources[status] = {(r[0], r[1]): json.loads(r[2]) for r in store.db.execute(
                            "SELECT kind,id,payload FROM entities WHERE kind != 'tickets'")}
                    finally:
                        store.close()
                before, after = sources["not_shipped"], sources["shipped"]
                differences = [key for key in before if before[key] != after[key]]
                self.assertEqual(differences, [("orders", "O-52")])
                self.assertEqual(after[("orders", "O-52")],
                                 before[("orders", "O-52")] | {"shipping_status": "shipped"})

    def test_existing_artifacts_rejected_before_store_and_unchanged(self):
        for status in ("not_shipped", "shipped"):
            for suffix in (".sqlite", ".json", ".seed.json"):
                with self.subTest(status=status, suffix=suffix), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = root / (status + suffix)
                    path.write_bytes(b"previous experiment")
                    with patch("email_router.shipping_change.Store") as store:
                        with self.assertRaises(FileExistsError):
                            run_case(root, status, ShippingFixture())
                        store.assert_not_called()
                    self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()},
                                     {path.name: b"previous experiment"})

    def test_model_using_original_route_after_shipping_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            # The original fixture always proposes order_operations for mail-02.
            # The guard blocks that proposal after O-52 is shipped.
            result = run_case(Path(directory), "shipped", DemoModel())
            self.assertFalse(result["passed"])
            self.assertNotEqual(result["actual"], ["logistics"])
