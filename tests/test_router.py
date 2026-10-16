import copy
import io
import json
import os
import socket
import ssl
import tempfile
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

from email_router.demo_model import DemoModel
from email_router.llm import ChatModel, ModelError, http_error, network_error
from email_router.quota import safe_quota
from email_router.policy import cloud_context_allowed, critical_record
from email_router.runner import Runner, ground_hints
from email_router.store import Store, encode
from email_router.tools import Tools


SEED = Path(__file__).resolve().parent.parent / "data/seed.json"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite")
        self.addCleanup(self.store.close)
        self.store.seed(SEED)

    def setup_issue(self, message="mail-02", category="order", subject="Срок получения O-52"):
        self.tools = Tools(self.store, "test-run", message)
        self.issue = self.store.register(self.tools.message, [{"category": category,
                         "quote": self.tools.message["text"], "subject": subject}])[0]
        return self.issue

    def read(self, tool, **args):
        answer = self.tools.call(tool, args)
        self.assertIn("evidence_id", answer)
        return answer

    def order_proposal(self, target="order_operations"):
        self.setup_issue()
        answers = [self.read("resolve_customer"),
                   self.read("get_order", customer_id="C-21", order_id="O-52"),
                   self.read("find_tickets", customer_id="C-21", order_id="O-52", subject=self.issue["payload"]["subject"])]
        return {"issue_id": self.issue["id"], "customer_id": "C-21", "category": "order",
                "target_ref": target, "summary": "Срок получения O-52",
                "evidence_ids": [a["evidence_id"] for a in answers]}


class ToolTests(StoreCase):
    def test_routing_sends_only_staff_reference_and_requires_content_review(self):
        args = self.order_proposal()
        args["summary"] = "PRIVATE invented payment details MUST NOT be sent"
        answer = self.tools.call("route_request", args)
        self.assertEqual(answer["status"], "applied")
        self.assertEqual(answer["content_delivery"], "requires_staff")
        outgoing = json.loads(self.store.db.execute("SELECT payload FROM outgoing").fetchone()[0])
        self.assertEqual(outgoing["kind"], "staff_routing_notice")
        self.assertNotIn("source_text", outgoing)
        self.assertNotIn("summary", outgoing)
        self.assertNotIn("PRIVATE", encode(outgoing))
        self.assertNotIn(self.tools.message["text"], encode(outgoing))
        self.assertEqual(outgoing["attachments"], [])
        review = json.loads(self.store.db.execute("SELECT payload FROM reviews WHERE id=?", (answer["review_id"],)).fetchone()[0])
        self.assertEqual(review["kind"], "critical_data_review")
        self.assertEqual(review["state"], "awaiting_staff")
        self.assertEqual(review["queue_ref"], "order_operations")

    def test_model_cannot_supply_staff_approval_or_attachment(self):
        args = self.order_proposal()
        args["staff_approved"] = True
        args["attachments"] = ["private-contract.pdf"]
        self.assertEqual(self.tools.call("route_request", args)["status"], "invalid_arguments")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)

    def test_ambiguous_customer_requires_available_history_before_clarification(self):
        self.setup_issue("mail-01", "technical", "Терминалы")
        lookup = self.read("resolve_customer")
        self.assertEqual(lookup["status"], "ambiguous")
        answer = self.tools.call("request_clarification", {"issue_id": self.issue["id"],
                                "missing_fields": ["company_identifier"], "evidence_ids": []})
        self.assertEqual(answer["status"], "blocked")
        self.assertEqual(answer["error"]["code"], "read_thread_before_customer_clarification")
        self.assertIn("read_thread", answer["error"]["message"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)
        self.read("read_thread")
        resolved = self.read("resolve_customer", reference_id="D-12")
        self.assertEqual(resolved["data"]["customer_id"], "C-12")

    def test_unknown_sender_can_still_request_identification(self):
        self.setup_issue("mail-06", "technical", "Поломка")
        lookup = self.read("resolve_customer")
        answer = self.tools.call("request_clarification", {"issue_id": self.issue["id"],
                    "missing_fields": ["company_identifier"], "evidence_ids": [lookup["evidence_id"]]})
        self.assertEqual(answer["status"], "applied")

    def test_missing_customer_proof_returns_recovery_index_but_does_not_execute(self):
        args = self.order_proposal()
        all_evidence = list(args["evidence_ids"])
        args["evidence_ids"] = [all_evidence[1]]
        answer = self.tools.call("route_request", args)
        self.assertEqual(answer["error"]["code"], "verified_customer_required")
        self.assertTrue(any(e["evidence_id"] == all_evidence[0] for e in answer["available_evidence"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)
        args["evidence_ids"] = all_evidence
        self.assertEqual(self.tools.call("route_request", args)["status"], "applied")

    def test_order_ticket_search_requires_order_id_argument(self):
        self.setup_issue()
        answer = self.read("find_tickets", customer_id="C-21", subject="Срок получения O-52")
        self.assertEqual(answer["status"], "invalid_arguments")
        self.assertEqual(answer["error"]["code"], "order_id_required_for_order_ticket_search")

    def test_existing_absent_and_unavailable_are_distinct(self):
        self.setup_issue()
        self.assertEqual(self.read("get_order", customer_id="C-21", order_id="O-52")["status"], "found")
        message = self.tools.message | {"text": "Что с O-999?"}
        with self.store.db:
            self.store.put("messages", message)
        self.tools.message = message
        self.assertEqual(self.read("get_order", customer_id="C-21", order_id="O-999")["status"], "not_found")
        self.tools.faults["get_order"] = 1
        self.assertEqual(self.read("get_order", customer_id="C-21", order_id="O-999")["status"], "unavailable")

    def test_anna_ambiguity_resolved_by_observed_contract(self):
        self.setup_issue("mail-01", "technical", "Терминалы")
        self.assertEqual(self.read("resolve_customer")["status"], "ambiguous")
        self.assertEqual(self.read("resolve_customer", reference_id="D-12")["status"], "invalid_arguments")
        self.read("read_thread")
        self.assertEqual(self.read("resolve_customer", reference_id="D-12")["data"]["customer_id"], "C-12")
        self.assertEqual(self.read("get_contract", customer_id="C-12", contract_id="D-12")["data"]["service_status"], "expired")

    def test_candidate_name_does_not_allow_guessing_customer(self):
        self.setup_issue("mail-01", "technical", "Терминалы")
        self.read("resolve_customer")
        self.assertEqual(self.read("resolve_customer", company_hint="Вектор Север")["status"], "invalid_arguments")

    def test_company_name_does_not_authorize_unknown_sender(self):
        self.setup_issue("mail-06", "technical", "Поломка")
        self.assertEqual(self.read("resolve_customer", company_hint="Вектор")["status"], "unverified")
        answer = self.read("get_contract", customer_id="C-11")
        self.assertEqual(answer["status"], "forbidden")
        self.assertIsNone(answer["data"])

    def test_wrong_recipient_has_no_effect(self):
        args = self.order_proposal("logistics")
        answer = self.tools.call("route_request", args)
        self.assertEqual(answer["status"], "blocked")
        self.assertEqual(answer["error"]["code"], "wrong_recipient")
        self.assertEqual(len(self.store.all("tickets")), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)

    def test_changed_shipping_requires_fresh_evidence(self):
        args = self.order_proposal()
        with self.store.db:
            self.store.put("orders", self.store.get("orders", "O-52") | {"shipping_status": "shipped"})
        self.assertEqual(self.tools.call("route_request", args)["status"], "conflict")
        fresh = self.read("get_order", customer_id="C-21", order_id="O-52")
        args["evidence_ids"][1] = fresh["evidence_id"]
        args["target_ref"] = "logistics"
        self.assertEqual(self.tools.call("route_request", args)["recipient_ref"], "logistics")

    def test_new_ticket_after_search_cannot_create_duplicate(self):
        args = self.order_proposal()
        with self.store.db:
            self.store.put("tickets", {"id": "T-15", "customer_id": "C-21", "status": "open",
                "assignee_ref": "order_operations", "category": "order", "subject": "Срок O-52",
                "order_id": "O-52", "thread_id": "thread-02", "message_ids": []})
        self.assertEqual(self.tools.call("route_request", args)["status"], "conflict")
        self.assertEqual(len(self.store.all("tickets")), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)

    def test_same_action_after_lost_reply_has_one_effect(self):
        args = self.order_proposal()
        first = self.tools.call("route_request", args)
        second = self.tools.call("route_request", args)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "already_applied")
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(len(self.store.all("tickets")), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 1)
        self.assertEqual(self.tools.call("handoff_to_human", {"issue_id": self.issue["id"],
            "reason": "unsupported_case", "summary": "Повтор", "evidence_ids": []})["status"], "conflict")

    def test_action_survives_restart(self):
        args = self.order_proposal()
        first = self.tools.call("route_request", args)
        other = Store(Path(self.directory.name) / "state.sqlite")
        try:
            second = Tools(other, "test-run", "mail-02").call("route_request", args)
            self.assertEqual(second["status"], "already_applied")
            self.assertEqual(first["action_id"], second["action_id"])
        finally:
            other.close()

    def test_failed_mail_insert_rolls_back_ticket_and_action(self):
        args = self.order_proposal()
        self.store.db.execute("CREATE TRIGGER fail_mail BEFORE INSERT ON outgoing BEGIN SELECT RAISE(ABORT,'test fault'); END")
        self.assertEqual(self.tools.call("route_request", args)["status"], "unavailable")
        self.assertEqual(len(self.store.all("tickets")), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)
        self.assertEqual(self.store.issue(self.issue["id"])["state"], "pending")

    def test_unknown_and_foreign_evidence_rejected(self):
        args = self.order_proposal()
        args["evidence_ids"] = ["fabricated"]
        self.assertEqual(self.tools.call("route_request", args)["error"]["code"], "invalid_evidence")
        other = Tools(self.store, "other-run", "mail-02").call("resolve_customer", {})
        args["evidence_ids"] = [other["evidence_id"]]
        self.assertEqual(self.tools.call("route_request", args)["error"]["code"], "invalid_evidence")

    def test_extra_recipient_argument_is_rejected(self):
        self.setup_issue("mail-08", "forbidden", "Запрещённый запрос")
        answer = self.tools.call("request_clarification", {"issue_id": self.issue["id"],
            "missing_fields": ["company_identifier"], "evidence_ids": [],
            "recipient": "backup@outside.example.test"})
        self.assertEqual(answer["status"], "invalid_arguments")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outgoing").fetchone()[0], 0)

    def test_search_required_even_for_known_order(self):
        args = self.order_proposal()
        args["evidence_ids"].pop()
        self.assertEqual(self.tools.call("route_request", args)["error"]["code"], "ticket_search_required")

    def test_existing_ticket_uses_current_assignee(self):
        self.setup_issue("mail-04", "technical", "Продолжение T-9")
        with self.store.db:
            self.store.put("tickets", self.store.get("tickets", "T-9") | {"assignee_ref": "marina"})
        a = self.read("resolve_customer")
        b = self.read("find_tickets", customer_id="C-31", subject="Продолжение T-9", ticket_id="T-9")
        args = {"issue_id": self.issue["id"], "customer_id": "C-31", "category": "technical",
                "target_ref": "sergey", "ticket_id": "T-9", "summary": "Продолжение",
                "evidence_ids": [a["evidence_id"], b["evidence_id"]]}
        self.assertEqual(self.tools.call("route_request", args)["status"], "blocked")
        args["target_ref"] = "marina"
        self.assertEqual(self.tools.call("route_request", args)["ticket_operation"], "appended")
        self.assertEqual(len(self.store.all("tickets")), 1)


class ControllerTests(StoreCase):
    def test_real_or_unclassified_data_is_handed_to_staff_before_model_call(self):
        class MustNotCallModel(DemoModel):
            is_live = True
            def complete(self, *args, **kwargs):
                raise AssertionError("Restricted data must not reach a cloud model")
        message = self.store.get("messages", "mail-02")
        message.pop("data_classification")
        with self.store.db:
            self.store.put("messages", message)
        self.assertFalse(cloud_context_allowed(self.store))
        report = Runner(self.store, MustNotCallModel()).run("mail-02")
        self.assertEqual(report["actions"][0]["reason"], "sensitive_data")
        self.assertEqual(report["issues"][0]["state"], "human_review")
        self.assertEqual(report["outgoing"], [])
        self.assertFalse(any(e["kind"] == "model_request" for e in report["events"]))
        self.assertTrue(report["redacted_for_staff"])
        self.assertNotIn(message["text"], encode(report))
        self.assertNotIn(message["sender"], encode(report))

    def test_critical_source_blocks_cloud_even_with_synthetic_message(self):
        class MustNotCallModel(DemoModel):
            is_live = True
            def complete(self, *args, **kwargs):
                raise AssertionError("Critical source must not reach a cloud model")
        with self.store.db:
            self.store.put("orders", self.store.get("orders", "O-52") | {"data_classification": "critical"})
        report = Runner(self.store, MustNotCallModel(), "workflow").run("mail-02")
        self.assertEqual(report["actions"][0]["reason"], "sensitive_data")
        self.assertFalse(any(e["kind"] == "model_request" for e in report["events"]))

    def test_synthetic_multi_issue_fixture_can_use_model_interface_safely(self):
        class LocalModelDouble(DemoModel):
            is_live = True  # Exercises cloud boundary without network; test double only.
        self.assertTrue(cloud_context_allowed(self.store))
        report = Runner(self.store, LocalModelDouble(), "agent").run("mail-07")
        self.assertEqual([i["state"] for i in report["issues"]], ["routed", "routed"])
        self.assertTrue(cloud_context_allowed(self.store))
        self.assertTrue(all(o["kind"] == "staff_routing_notice" for o in report["outgoing"]))
        self.assertTrue(all(a["content_delivery"] == "requires_staff" for a in report["actions"]))

    def test_real_failed_extraction_is_recovered_from_explicit_order_number(self):
        # Exact malformed arguments from the saved real workflow-mail-02 attempt.
        raw = {"issues": [{"category": "order",
            "quote": "По заказу O-52 деньги ушли неделю назад. Когда получим оборудование?",
            "subject": "Payment confirmation and delivery timeline for order O-52",
            "company_hint": "customer.example.test", "reference_id": "mail-02"}]}
        class ReplayExtractionModel(DemoModel):
            model = "captured-extraction-replay-not-live"
            label = "offline:captured-extraction-replay"
            def complete(self, *args, **kwargs):
                return {"message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "replay-call", "type": "function", "function": {
                        "name": "register_issues", "arguments": encode(raw)}}]},
                    "model": self.model, "provider": "offline-replay", "usage": {}}
        report = Runner(self.store, ReplayExtractionModel(), "workflow").run("mail-02")
        self.assertEqual(report["actions"][0]["recipient_ref"], "order_operations")
        self.assertEqual(report["issues"][0]["payload"]["reference_id"], "O-52")
        self.assertEqual(report["issues"][0]["payload"]["company_hint"], "")
        self.assertEqual(report["events"][-1]["payload"]["status"], "completed")
        self.assertTrue(any(e["kind"] == "extraction_correction" for e in report["events"]))
        saved = next(e for e in report["events"] if e["kind"] == "model_response")
        self.assertEqual(json.loads(saved["payload"]["message"]["tool_calls"][0]["function"]["arguments"]), raw)

    def test_grounding_does_not_choose_between_multiple_order_numbers(self):
        text = "Что с O-51 и O-52?"
        raw = [{"category": "order", "quote": text, "subject": text,
                "company_hint": "customer.example.test", "reference_id": "mail-02"}]
        grounded, _ = ground_hints(raw, {"text": text})
        self.assertEqual(grounded[0]["reference_id"], "")
        self.assertEqual(grounded[0]["company_hint"], "")

    def test_grounding_does_not_invent_number_or_use_sender_domain(self):
        text = "Нужно узнать срок поставки"
        raw = [{"category": "order", "quote": text, "subject": text,
                "company_hint": "customer.example.test", "reference_id": "O-999"}]
        grounded, _ = ground_hints(raw, {"text": text, "sender": "oleg@customer.example.test"})
        self.assertEqual(grounded[0]["reference_id"], "")
        self.assertEqual(grounded[0]["company_hint"], "")

    def test_grounding_preserves_contract_from_read_history(self):
        text = "Договор действует?"
        raw = [{"category": "technical", "quote": text, "subject": text,
                "company_hint": "", "reference_id": "D-12"}]
        history = {"status": "found", "data": {"messages": [{"text": "Наш договор D-12"}]}}
        grounded, corrections = ground_hints(raw, {"text": text}, history)
        self.assertEqual(grounded[0]["reference_id"], "D-12")
        self.assertEqual(corrections, [])

    def test_eight_scripted_fixtures_in_both_modes(self):
        expected = {1: ["marina"], 2: ["order_operations"], 3: ["logistics"],
                    4: ["sergey"], 5: ["sales_north"], 7: ["sales_orion", "order_operations"]}
        for mode in ("agent", "workflow"):
            for n in range(1, 9):
                with self.subTest(mode=mode, email=n):
                    s = Store(Path(self.directory.name) / f"{mode}-{n}.sqlite")
                    try:
                        s.seed(SEED)
                        report = Runner(s, DemoModel(), mode).run(f"mail-{n:02}")
                        self.assertFalse(report["run"]["config"]["live_model"])
                        self.assertEqual(report["events"][-1]["payload"]["status"], "completed")
                        if n in expected:
                            self.assertEqual([a["recipient_ref"] for a in report["actions"]], expected[n])
                            self.assertEqual(len(report["outgoing"]), len(expected[n]))
                        elif n == 6:
                            self.assertEqual(report["issues"][0]["state"], "awaiting_customer")
                        else:
                            self.assertEqual(report["issues"][0]["state"], "human_review")
                            self.assertEqual(report["outgoing"], [])
                        if n == 4:
                            self.assertEqual(len(s.all("tickets")), 1)
                    finally:
                        s.close()

    def test_repeated_incoming_event_uses_same_receipt(self):
        first = Runner(self.store, DemoModel()).run("mail-02")
        second = Runner(self.store, DemoModel()).run("mail-02")
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(first["actions"], second["actions"])
        self.assertEqual(len(second["outgoing"]), 1)

    def test_model_failure_is_recorded_even_after_handoff(self):
        class BrokenModel(DemoModel):
            def complete(self, *a, **kw):
                raise ModelError("network unavailable")
        report = Runner(self.store, BrokenModel()).run("mail-02")
        self.assertEqual(report["events"][-1]["payload"]["status"], "failed")
        self.assertEqual(report["issues"][0]["state"], "human_review")
        self.assertEqual(report["actions"][0]["queue_ref"], "human_review")

    def test_source_read_retry_is_saved(self):
        report = Runner(self.store, DemoModel(), "workflow", {"get_order": 1}).run("mail-02")
        reads = [e for e in report["events"] if e["kind"] == "tool" and e["payload"]["name"] == "get_order"]
        self.assertEqual([e["payload"]["result"]["status"] for e in reads], ["unavailable", "found"])
        self.assertEqual(report["actions"][0]["recipient_ref"], "order_operations")

    def test_persistent_unavailable_source_does_not_create_order_ticket(self):
        report = Runner(self.store, DemoModel(), "workflow", {"get_order": 10}).run("mail-02")
        self.assertEqual(report["issues"][0]["state"], "human_review")
        self.assertEqual(report["outgoing"], [])
        self.assertEqual(report["actions"][0]["reason"], "source_unavailable")

    def test_invalid_customer_arguments_are_not_called_source_outage(self):
        class InvalidCustomerTools(Tools):
            def read(self, name, args):
                if name == "resolve_customer":
                    return {"status": "invalid_arguments", "data": None, "candidates": [],
                            "error": {"code": "invalid_test_arguments"}}
                return super().read(name, args)
        with patch("email_router.runner.Tools", InvalidCustomerTools):
            report = Runner(self.store, DemoModel(), "workflow").run("mail-02")
        self.assertEqual(report["actions"][0]["reason"], "unsupported_case")

    def test_workflow_changes_route_when_shipment_changes(self):
        with self.store.db:
            self.store.put("orders", self.store.get("orders", "O-52") | {"shipping_status": "shipped"})
        report = Runner(self.store, DemoModel(), "workflow").run("mail-02")
        self.assertEqual(report["actions"][0]["recipient_ref"], "logistics")

    def test_unconfirmed_payment_is_sent_for_manual_order_reconciliation(self):
        with self.store.db:
            self.store.put("orders", self.store.get("orders", "O-52") | {"payment_status": "pending"})
        report = Runner(self.store, DemoModel(), "workflow").run("mail-02")
        self.assertEqual(report["issues"][0]["state"], "human_review")
        self.assertEqual(report["actions"][0]["queue_ref"], "order_operations")
        self.assertEqual(report["outgoing"], [])


class ClientTests(unittest.TestCase):
    def test_critical_categories_and_payment_invoice_exception(self):
        base = {"data_classification": "operational", "contains_personal_data": False}
        for document_type in ("customer_contract", "acceptance_certificate", "document_workflow"):
            with self.subTest(document_type=document_type):
                self.assertTrue(critical_record(base | {"document_type": document_type}))
        self.assertTrue(critical_record(base | {"contains_personal_data": True}))
        self.assertFalse(critical_record(base | {"document_type": "payment_invoice"}))
        self.assertTrue(critical_record(base | {"document_type": "payment_invoice", "contains_personal_data": True}))
        self.assertTrue(critical_record({"document_type": "payment_invoice"}))
        self.assertTrue(critical_record({}))
        self.assertTrue(critical_record(base, "contracts"))
        self.assertFalse(critical_record({"data_classification": "synthetic"}))
    def test_smoke_can_run_one_workflow_case_without_other_requests(self):
        from email_router import smoke
        class Model:
            model = "gemini-3.5-flash-lite"
            provider = "gemini"
        report = {"events": [{"kind": "model_response", "payload": {"model": Model.model}},
                             {"kind": "run_end", "payload": {"status": "completed"}}],
                  "issues": [{"state": "routed"}], "reviews": [],
                  "actions": [{"recipient_ref": "order_operations", "ticket_id": "T-10", "outgoing_message_id": "out-1"}],
                  "outgoing": [{"recipient_ref": "order_operations", "ticket_id": "T-10", "id": "out-1"}]}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(smoke, "ROOT", Path(folder)), patch.object(smoke, "ChatModel", return_value=Model()), patch.object(smoke, "load_env"):
                with patch.object(smoke, "execute", return_value=report) as execute, patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(smoke.main(["--mode", "workflow", "--message", "mail-02"]), 0)
                    self.assertEqual(execute.call_count, 1)
                    self.assertEqual(execute.call_args.args[3:5], ("workflow", "mail-02"))
                    self.assertIn("1/1", output.getvalue())

    def test_gemini_requires_its_own_key_without_openrouter_fallback(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "OPENROUTER_API_KEY": "other-provider-test"}, clear=True):
            with self.assertRaisesRegex(ModelError, "Set GEMINI_API_KEY"):
                ChatModel()

    def test_gemini_rejects_models_outside_free_tier_candidates(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-4-argon", "GEMINI_API_KEY": "google-test"}, clear=True):
            with self.assertRaisesRegex(ModelError, "Free Tier candidates"):
                ChatModel()

    def test_gemini_protocol_preserves_tool_signature_on_next_turn(self):
        signature = {"google": {"thought_signature": "opaque-google-signature"}}
        call = {"id": "google-call-1", "type": "function", "function": {
                "name": "read_thread", "arguments": "{}"}, "extra_content": signature}
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps({"model": "gemini-3.5-flash-lite", "choices": [{
                    "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                    "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3}}).encode()
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "google-test", "OPENROUTER_API_KEY": "do-not-use"}, clear=True):
            with patch("urllib.request.urlopen", return_value=Response()) as request:
                model = ChatModel()
                self.assertEqual(model.model, "gemini-3.5-flash-lite")
                first = model.complete([{"role": "user", "content": "test"}], [])
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
                self.assertEqual(sent.get_header("Authorization"), "Bearer google-test")
                payload = json.loads(sent.data)
                self.assertNotIn("provider", payload)
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertEqual(first["message"]["tool_calls"][0]["extra_content"], signature)
                model.complete([first["message"], {"role": "tool", "tool_call_id": "google-call-1", "content": "{}"}], [])
                next_payload = json.loads(request.call_args.args[0].data)
                self.assertEqual(next_payload["messages"][0]["tool_calls"][0]["extra_content"], signature)

    def test_quota_with_gemini_does_not_query_openrouter(self):
        from email_router.quota import main
        with patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}, clear=True):
            with patch("urllib.request.urlopen") as request, patch("email_router.quota.load_env"), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(), 0)
                request.assert_not_called()
                self.assertIn("Google AI Studio", output.getvalue())

    def test_gemini_doctor_http_auth_failure_still_confirms_tls(self):
        from email_router.doctor import main
        error = urllib.error.HTTPError("https://generativelanguage.googleapis.com", 401, "unauthenticated", {}, None)
        with patch("sys.argv", ["doctor", "--provider", "gemini"]), patch("socket.getaddrinfo", return_value=[]):
            with patch("urllib.request.urlopen", side_effect=error) as request, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["host"], "generativelanguage.googleapis.com")
                self.assertEqual(report["https"], "OK")
                self.assertFalse(report["authentication_checked"])
                self.assertIsNone(request.call_args.args[0].get_header("Authorization"))

    def test_http_429_preserves_numeric_retry_but_not_provider_error_body(self):
        error = urllib.error.HTTPError("https://openrouter.ai", 429, "SECRET", {"Retry-After": "60"}, None)
        classified = http_error(error)
        self.assertEqual(classified.http_status, 429)
        self.assertIn("retry_after_seconds=60", str(classified))
        self.assertNotIn("SECRET", str(classified))

    def test_quota_output_excludes_secret_and_identifying_fields(self):
        answer = safe_quota({"label": "SECRET", "api_key": "SECRET", "usage": 999,
            "free_model_daily_requests": {"used": 20, "limit": 50, "remaining": 30, "extra": "SECRET"}})
        self.assertEqual(answer["free_model_daily_requests"], {"used": 20, "limit": 50, "remaining": 30})
        self.assertNotIn("SECRET", encode(answer))

    def test_missing_quota_is_not_reported_as_unlimited(self):
        answer = safe_quota({})
        self.assertIsNone(answer["free_model_daily_requests"])
        self.assertIn("does not prove", answer["note"])

    def test_network_error_distinguishes_dns_tls_and_timeout(self):
        cases = [(socket.gaierror(-2, "unavailable"), "DNS:"),
                 (ssl.SSLCertVerificationError(1, "certificate verify failed"), "TLS_CERTIFICATE:"),
                 (TimeoutError(), "TIMEOUT:"),
                 (ConnectionRefusedError(), "CONNECTION_REFUSED:")]
        for reason, prefix in cases:
            with self.subTest(prefix=prefix):
                self.assertTrue(network_error(urllib.error.URLError(reason)).startswith(prefix))

    def test_network_error_does_not_expose_proxy_or_key_text(self):
        error = urllib.error.URLError("https://user:SECRET@proxy.invalid")
        self.assertNotIn("SECRET", network_error(error))

    def test_paid_model_is_not_allowed(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "openrouter", "LLM_MODEL": "paid/model", "OPENROUTER_API_KEY": "test"}):
            with self.assertRaises(ModelError):
                ChatModel()

    def test_free_request_format_and_recorded_actual_model(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def read(self):
                return json.dumps({"id": "response-1", "model": "actual/model:free", "usage": {"prompt_tokens": 11},
                    "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [],
                    "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque-state"}]}, "finish_reason": "stop"}]}).encode()
        with patch.dict(os.environ, {"LLM_PROVIDER": "openrouter", "LLM_MODEL": "openrouter/free", "OPENROUTER_API_KEY": "test"}):
            with patch("urllib.request.urlopen", return_value=Response()) as request:
                answer = ChatModel().complete([{"role": "user", "content": "test"}], [])
                payload = json.loads(request.call_args.args[0].data)
                self.assertEqual(payload["provider"]["max_price"], {"prompt": 0, "completion": 0, "request": 0})
                self.assertEqual(answer["model"], "actual/model:free")
                self.assertEqual(answer["usage"]["prompt_tokens"], 11)
                self.assertEqual(answer["message"]["reasoning_details"][0]["data"], "opaque-state")


if __name__ == "__main__":
    unittest.main()
