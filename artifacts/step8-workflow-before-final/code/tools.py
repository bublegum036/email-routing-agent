"""The shared read tools, business guard and atomic local action executor."""

import json
import re
import sqlite3
from datetime import date
from email.utils import parseaddr

from .schemas import ACTIONS, READS, SPECS, validate
from .store import encode, uid
from .policy import routing_notice


def result(status, data=None, candidates=None, code=None):
    return {"status": status, "data": data, "candidates": candidates or [],
            "error": {"code": code or status} if status not in {"found", "not_found", "ambiguous"} else None}


class Tools:
    def __init__(self, store, run_id, message_id, faults=None):
        self.store, self.run_id = store, run_id
        self.message = store.get("messages", message_id)
        if not self.message:
            raise ValueError("Unknown message")
        self.faults = faults or {}

    def authorized(self, customer_id):
        contact = self.store.get("contacts", self.message["sender"].lower())
        return contact and customer_id in contact["customer_ids"]

    def known_reference(self, identifier):
        texts = [self.message["text"]]
        thread = self.store.get("threads", self.message["thread_id"])
        # A reference in history becomes available only after the model/program read it.
        if thread and self.store.db.execute(
                "SELECT 1 FROM observations WHERE run_id=? AND tool='read_thread'", (self.run_id,)).fetchone():
            texts.extend(m["text"] for m in thread["messages"])
        return identifier in set(re.findall(r"\b[DOT]-\d+\b", " ".join(texts)))

    def observation(self, name, args):
        answer = self.read(name, args)
        previous = self.store.db.execute(
            "SELECT id,result FROM observations WHERE run_id=? AND tool=? AND arguments=? ORDER BY rowid DESC LIMIT 1",
            (self.run_id, name, encode(args))).fetchone()
        if previous and json.loads(previous["result"]) == answer and answer["status"] != "unavailable":
            return answer | {"evidence_id": previous["id"]}
        evidence_id = uid("obs")
        with self.store.db:
            self.store.db.execute("INSERT INTO observations VALUES(?,?,?,?,?)",
                (evidence_id, self.run_id, name, encode(args), encode(answer)))
        return answer | {"evidence_id": evidence_id}

    def read(self, name, args):
        if self.faults.get(name, 0):
            self.faults[name] -= 1
            return result("unavailable", code="source_timeout")
        s = self.store
        if name == "read_thread":
            thread = s.get("threads", self.message["thread_id"])
            return result("found", thread | {"complete": True}) if thread else result("not_found")
        if name == "resolve_customer":
            contact = s.get("contacts", self.message["sender"].lower())
            if not contact:
                return result("unverified", code="sender_not_registered")
            customers = [s.get("customers", c) for c in contact["customer_ids"]]
            customers = [c for c in customers if c]
            hint, ref = args.get("company_hint"), args.get("reference_id")
            if hint:
                known_text = self.message["text"]
                thread = s.get("threads", self.message["thread_id"])
                if thread and s.db.execute("SELECT 1 FROM observations WHERE run_id=? AND tool='read_thread'",
                                           (self.run_id,)).fetchone():
                    known_text += " " + " ".join(m["text"] for m in thread["messages"])
                if hint.casefold() not in known_text.casefold():
                    return result("invalid_arguments", code="company_hint_not_observed")
                customers = [c for c in customers if hint.casefold() in c["name"].casefold()]
            if ref:
                if not self.known_reference(ref):
                    return result("invalid_arguments", code="reference_not_observed")
                kind = {"D": "contracts", "O": "orders", "T": "tickets"}.get(ref[:1])
                entity = s.get(kind, ref) if kind else None
                if not entity:
                    return result("not_found")
                if not self.authorized(entity["customer_id"]):
                    return result("forbidden")
                customers = [c for c in customers if c["id"] == entity["customer_id"]]
            if not customers:
                return result("not_found")
            if len(customers) > 1:
                return result("ambiguous", candidates=[{"customer_id": c["id"], "name": c["name"]} for c in customers])
            return result("found", customers[0] | {"customer_id": customers[0]["id"],
                          "identity_status": "verified", "basis": "registered_contact"})
        customer_id = args["customer_id"]
        if not self.authorized(customer_id):
            return result("forbidden")
        if name in {"get_contract", "get_order"}:
            kind = "contracts" if name == "get_contract" else "orders"
            key = "contract_id" if name == "get_contract" else "order_id"
            identifier = args.get(key)
            if identifier and not self.known_reference(identifier):
                return result("invalid_arguments", code="reference_not_observed")
            if identifier:
                entity = s.get(kind, identifier)
                if not entity:
                    return result("not_found")
                if entity["customer_id"] != customer_id:
                    return result("forbidden")
                matches = [entity]
            else:
                matches = [c for c in s.all(kind) if c["customer_id"] == customer_id]
            if not matches:
                return result("not_found")
            if len(matches) > 1:
                return result("ambiguous", candidates=[{key: c["id"]} for c in matches])
            entity = matches[0] | {key: matches[0]["id"]}
            if name == "get_contract":
                when = s.get("meta", "settings")["processing_date"]
                start, end = entity.get("valid_from"), entity.get("valid_until")
                try:
                    today = date.fromisoformat(when)
                    active = date.fromisoformat(start) <= today <= date.fromisoformat(end)
                    state = "active" if active else "expired" if today > date.fromisoformat(end) else "unknown"
                except (TypeError, ValueError):
                    state = "unknown"
                entity["service_status"] = state
            return result("found", entity)
        if name == "find_tickets":
            subject_orders = set(re.findall(r"\bO-[0-9]+\b", args["subject"]))
            if self.subject_category(args["subject"]) == "order" and len(subject_orders) == 1 and not args.get("order_id"):
                return result("invalid_arguments", code="order_id_required_for_order_ticket_search")
            tickets = [t for t in s.all("tickets") if t["customer_id"] == customer_id]
            exact = args.get("ticket_id")
            if exact:
                if not self.known_reference(exact):
                    return result("invalid_arguments", code="reference_not_observed")
                ticket = s.get("tickets", exact)
                if not ticket:
                    return result("not_found")
                if ticket["customer_id"] != customer_id:
                    return result("forbidden")
                return result("found", ticket | {"ticket_id": exact, "match_basis": "explicit_ticket_reference"})
            order = args.get("order_id")
            if order and not self.known_reference(order):
                return result("invalid_arguments", code="reference_not_observed")
            # Exact thread links are reliable; a shared order alone is only a candidate.
            linked = [t for t in tickets if t["status"] == "open" and t.get("thread_id") == self.message["thread_id"]]
            category = self.subject_category(args["subject"])
            linked = [t for t in linked if t["category"] == category and (not order or t.get("order_id") == order)]
            if len(linked) == 1:
                return result("found", linked[0] | {"ticket_id": linked[0]["id"], "match_basis": "thread_and_category"})
            candidates = [t for t in tickets if t["status"] == "open" and
                          ((order and t.get("order_id") == order) or (not order and t["category"] == category))]
            if candidates:
                return result("ambiguous", candidates=[{"ticket_id": t["id"], "subject": t["subject"]} for t in candidates])
            return result("not_found")
        raise ValueError("Unknown read tool")

    @staticmethod
    def subject_category(text):
        text = text.casefold()
        if any(t in text for t in ("покуп", "комплект", "purchase", "ещё пять")):
            return "purchase"
        if any(t in text for t in ("соглас", "вариант", "approval")):
            return "approval"
        if re.search(r"\bo-\d+", text) or any(t in text for t in ("заказ", "достав", "order")):
            return "order"
        return "technical"

    def evidence(self, ids):
        observations = []
        for identifier in ids:
            row = self.store.db.execute("SELECT * FROM observations WHERE id=? AND run_id=?",
                                         (identifier, self.run_id)).fetchone()
            if not row:
                raise ValueError("Unknown or out-of-context evidence")
            observations.append({"id": identifier, "tool": row["tool"],
                                 "args": json.loads(row["arguments"]), "result": json.loads(row["result"])})
        return observations

    def available_evidence(self):
        """Compact index of successful observations for the current run only."""
        entries = {}
        for row in self.store.db.execute("SELECT * FROM observations WHERE run_id=? ORDER BY rowid", (self.run_id,)):
            answer = json.loads(row["result"])
            if answer["status"] not in {"found", "not_found", "ambiguous", "unverified"}:
                continue
            args = json.loads(row["arguments"])
            entries[(row["tool"], row["arguments"])] = {
                "evidence_id": row["id"], "tool": row["tool"], "arguments": args,
                "status": answer["status"], "customer_id": (answer.get("data") or {}).get("customer_id")}
        return list(entries.values())

    def route_basis(self, issue, args, obs):
        """Shared business guard. Returns allowed target/ticket or a structured rejection."""
        customer = args["customer_id"]
        def records(tool, statuses=("found",)):
            return [o for o in obs if o["tool"] == tool and o["result"]["status"] in statuses
                    and (o["result"].get("data") or o["args"]).get("customer_id") == customer]
        if not self.authorized(customer) or not records("resolve_customer"):
            return result("blocked", code="verified_customer_required")
        known_category = issue["payload"]["category"]
        if known_category not in {"unknown", args["category"]}:
            return result("blocked", code="issue_category_mismatch")
        if known_category == "unknown" and args["category"] != "approval":
            return result("blocked", code="unresolved_issue")
        searches = records("find_tickets", ("found", "not_found"))
        # Require the search to describe the registered issue, not a different one.
        searches = [o for o in searches if o["args"]["subject"] == issue["payload"]["subject"]]
        if not searches:
            return result("blocked", code="ticket_search_required")
        search = searches[-1]
        for o in obs:
            if o["tool"] in {"resolve_customer", "get_order", "get_contract", "find_tickets"}:
                if self.read(o["tool"], o["args"]) != o["result"]:
                    return result("conflict", code="source_changed")
        ticket = search["result"]["data"]
        if ticket:
            if ticket["status"] != "open":
                return result("blocked", code="closed_ticket")
            if args.get("ticket_id") != ticket["id"]:
                return result("blocked", code="existing_ticket_must_be_appended")
            if ticket["category"] != args["category"]:
                return result("blocked", code="ticket_subject_mismatch")
            target = ticket["assignee_ref"]
        else:
            if args.get("ticket_id"):
                return result("not_found", code="ticket_not_found")
            category = args["category"]
            if category == "technical":
                contracts = records("get_contract")
                if not contracts:
                    return result("blocked", code="contract_required")
                c = contracts[-1]["result"]["data"]
                if c["service_status"] == "expired":
                    target = c.get("contract_owner_ref")
                elif c["service_status"] == "active" and c["service_level"] == "extended":
                    target = c.get("support_group_ref")
                elif c["service_status"] == "active" and c["service_level"] == "standard":
                    target = "general_support"
                else:
                    return result("blocked", code="service_unknown")
            elif category == "order":
                orders = records("get_order")
                refs = re.findall(r"\bO-\d+\b", issue["payload"]["quote"])
                orders = [o for o in orders if not refs or o["args"]["order_id"] in refs]
                orders = list({o["args"]["order_id"]: o for o in orders}.values())
                if len(orders) != 1 or search["args"].get("order_id") != orders[0]["args"]["order_id"]:
                    return result("blocked", code="specific_order_required")
                order = orders[0]["result"]["data"]
                if order["shipping_status"] in {"shipped", "delivered"}:
                    target = "logistics"
                elif order["shipping_status"] == "not_shipped" and order["payment_status"] == "paid":
                    target = "order_operations"
                else:
                    return result("blocked", code="payment_or_shipping_requires_review")
            else:
                if category == "approval":
                    histories = [o for o in obs if o["tool"] == "read_thread" and o["result"]["status"] == "found"]
                    if not histories or histories[-1]["result"]["data"].get("customer_id") != customer:
                        return result("blocked", code="approval_context_required")
                target = self.store.get("customers", customer).get("sales_owner_ref")
        if not target or not self.store.get("recipients", target):
            return result("blocked", code="missing_owner")
        if args["target_ref"] != target:
            return result("blocked", code="wrong_recipient")
        return {"target": target, "ticket": ticket, "search": search}

    def action(self, name, args):
        s = self.store
        issue = s.issue(args["issue_id"])
        if not issue or issue["message_id"] != self.message["id"]:
            return result("blocked", code="unknown_issue")
        # BEGIN IMMEDIATE serializes the recheck, ticket mutation, mail and receipt.
        s.db.execute("BEGIN IMMEDIATE")
        try:
            previous = s.db.execute("SELECT * FROM actions WHERE issue_id=?", (issue["id"],)).fetchone()
            if previous:
                if previous["tool"] != name or previous["arguments"] != encode(args):
                    s.db.rollback()
                    return result("conflict", code="issue_already_completed")
                receipt = json.loads(previous["receipt"]) | {"status": "already_applied"}
                s.db.rollback()
                return receipt
            try:
                obs = self.evidence(args["evidence_ids"])
            except ValueError:
                s.db.rollback()
                return result("blocked", code="invalid_evidence")
            if issue["payload"]["category"] == "forbidden" and name != "handoff_to_human":
                s.db.rollback()
                return result("blocked", code="forbidden_request")
            receipt = {"status": "applied", "action_id": uid("act"), "issue_id": issue["id"]}
            if name == "route_request":
                basis = self.route_basis(issue, args, obs)
                if "status" in basis:
                    s.db.rollback()
                    return basis
                # Read results include explanation fields (match_basis, ticket_id).
                # Persist the source ticket, not the enriched tool response.
                ticket = s.get("tickets", basis["ticket"]["id"]) if basis["ticket"] else None
                operation = "appended" if ticket else "created"
                if not ticket:
                    numbers = [int(t["id"][2:]) for t in s.all("tickets") if re.fullmatch(r"T-\d+", t["id"])]
                    ticket = {"id": f"T-{max(numbers, default=0) + 1}", "customer_id": args["customer_id"], "status": "open",
                              "assignee_ref": basis["target"], "category": args["category"],
                              "subject": issue["payload"]["subject"],
                              "order_id": basis["search"]["args"].get("order_id"),
                              "thread_id": self.message["thread_id"], "message_ids": [],
                              "data_classification": self.message.get("data_classification", "restricted")}
                ticket["message_ids"] = list(dict.fromkeys(ticket["message_ids"] + [self.message["id"]]))
                s.put("tickets", ticket)
                target = s.get("recipients", basis["target"])
                outgoing = routing_notice(self.message["id"], issue["id"], ticket["id"], target["id"], uid("out"))
                outgoing["recipient"] = target["address"]
                review = {"id": uid("review"), "message_id": self.message["id"], "issue_id": issue["id"],
                          "kind": "critical_data_review", "queue_ref": target["id"],
                          "reason": "sensitive_data", "ticket_id": ticket["id"],
                          "state": "awaiting_staff", "content_delivery": "requires_staff"}
                s.db.execute("INSERT INTO reviews VALUES(?,?,?)", (review["id"], self.message["id"], encode(review)))
                receipt |= {"ticket_id": ticket["id"], "ticket_operation": operation,
                            "recipient_ref": target["id"], "recipient_address": target["address"],
                            "outgoing_message_id": outgoing["id"], "review_id": review["id"],
                            "content_delivery": "requires_staff"}
                state = "routed"
            elif name == "request_clarification":
                fields = list(dict.fromkeys(args["missing_fields"]))
                if "company_identifier" in fields:
                    lookups = s.db.execute("SELECT result FROM observations WHERE run_id=? AND tool='resolve_customer' ORDER BY rowid DESC LIMIT 1",
                                           (self.run_id,)).fetchone()
                    if not lookups:
                        s.db.rollback()
                        return result("blocked", code="resolve_customer_before_clarification")
                    ambiguous = json.loads(lookups[0])["status"] == "ambiguous"
                    has_history = s.get("threads", self.message["thread_id"])
                    read_history = s.db.execute("SELECT 1 FROM observations WHERE run_id=? AND tool='read_thread'",
                                                (self.run_id,)).fetchone()
                    if ambiguous and has_history and not read_history:
                        s.db.rollback()
                        return result("blocked", code="read_thread_before_customer_clarification")
                address = parseaddr(self.message["sender"])[1]
                if not fields or not re.fullmatch(r"[^\s@]+@[^\s@]+\.example\.test", address):
                    s.db.rollback()
                    return result("blocked", code="invalid_clarification")
                questions = {
                    "company_identifier": "Укажите реквизиты вашей компании для проверки принадлежности обращения.",
                    "order_id": "Укажите номер заказа.", "ticket_id": "Укажите номер обращения.",
                    "proposal_reference": "Укажите номер предложения и какой вариант вы согласовали.",
                    "equipment_details": "Уточните модель оборудования.",
                    "problem_details": "Опишите неисправность и когда она появилась."}
                outgoing = {"id": uid("out"), "message_id": self.message["id"], "issue_id": issue["id"],
                            "recipient": address, "kind": "clarification", "attachments": [],
                            "text": "\n".join(questions[f] for f in fields)}
                receipt |= {"outgoing_message_id": outgoing["id"], "recipient_address": address,
                            "missing_fields": fields}
                state = "awaiting_customer"
            else:
                queue = "human_review"
                # Manual payment reconciliation belongs to order operations, without
                # representing the payment as confirmed or creating a commercial order.
                if args["reason"] == "conflicting_facts" and issue["payload"]["category"] == "order":
                    for o in obs:
                        data = o["result"].get("data") or {}
                        if (o["tool"] == "get_order" and o["result"]["status"] == "found"
                                and data.get("shipping_status") == "not_shipped"
                                and data.get("payment_status") != "paid"):
                            queue = "order_operations"
                review = {"id": uid("review"), "message_id": self.message["id"], "issue_id": issue["id"],
                          "queue_ref": queue, "reason": args["reason"], "summary": args["summary"]}
                s.db.execute("INSERT INTO reviews VALUES(?,?,?)", (review["id"], self.message["id"], encode(review)))
                receipt |= {"review_id": review["id"], "queue_ref": queue, "reason": args["reason"]}
                state, outgoing = "human_review", None
            if outgoing:
                s.db.execute("INSERT INTO outgoing VALUES(?,?,?,?)", (outgoing["id"], self.message["id"],
                             outgoing["recipient"], encode(outgoing)))
            s.db.execute("INSERT INTO actions VALUES(?,?,?,?)", (issue["id"], name, encode(args), encode(receipt)))
            s.db.execute("UPDATE issues SET state=? WHERE id=?", (state, issue["id"]))
            s.db.commit()
            return receipt
        except Exception:
            s.db.rollback()
            raise

    def call(self, name, arguments):
        try:
            if name not in SPECS:
                raise ValueError("Unknown tool")
            validate(arguments, SPECS[name][1])
            answer = self.observation(name, arguments) if name in READS else self.action(name, arguments)
        except ValueError as exc:
            answer = result("invalid_arguments", code=str(exc))
        except sqlite3.Error:
            answer = result("unavailable", code="storage_error")
        if answer["status"] == "blocked":
            instructions = {
                "verified_customer_required": "Include the evidence_id of the successful resolve_customer for this customer, not only the order evidence.",
                "ticket_search_required": "Call find_tickets for this issue with its exact registered subject (and order_id for an order); include its evidence_id.",
                "specific_order_required": "Include matching get_order evidence AND find_tickets evidence with the same explicit order_id.",
                "read_thread_before_customer_clarification": "Before asking the customer, call read_thread: internal history is available and may resolve the customer.",
                "resolve_customer_before_clarification": "Call resolve_customer before asking the customer to identify their company.",
                "wrong_recipient": "Use the business role or current assignee from observed facts, not an order ID or an email address.",
            }
            code = answer["error"]["code"]
            if code in instructions:
                answer["error"]["message"] = instructions[code]
                answer["available_evidence"] = self.available_evidence()
        self.store.event(self.run_id, "tool", {"name": name, "arguments": arguments, "result": answer})
        return answer
