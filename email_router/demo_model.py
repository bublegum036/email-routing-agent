"""Explicit scripted test double for the eight development emails; NOT an LLM.

This exercises the controller/executor offline. It must never be used as evidence
of an agent's model quality, token cost, or superiority over a workflow.
"""

import json

from .store import encode, uid


class DemoModel:
    is_live = False
    model = "scripted-development-fixture"
    label = "offline:scripted-development-fixture"

    def __init__(self):
        self.step = 0

    def complete(self, messages, tools, timeout=30):
        context = json.loads(messages[-1]["content"])
        mail = context["message"]
        number = int(mail["id"].split("-")[1])
        definitions = {
            1: [("technical", mail["text"], "Техническая проблема терминалов и договор", "", "")],
            2: [("order", mail["text"], "Срок получения заказа O-52", "", "O-52")],
            3: [("order", mail["text"], "Где доставка O-51", "", "O-51")],
            4: [("technical", mail["text"], "Продолжение проблемы T-9", "", "T-9")],
            5: [("approval", mail["text"], "Согласие на второй вариант покупки", "", "")],
            6: [("technical", mail["text"], "Неисправность оборудования Вектора", "Вектор", "")],
            7: [("purchase", "Нужно ещё пять комплектов", "Покупка пяти комплектов", "", ""),
                ("order", "объясните, почему O-52 не отправлен", "Причина задержки заказа O-52", "", "O-52")],
            8: [("forbidden", mail["text"], "Запрос внешней передачи договора в обход правил", "", "")],
        }
        if number not in definitions:
            raise ValueError("Offline fixture supports only development emails 1..8")
        if tools[0]["function"]["name"] == "register_issues":
            issues = [dict(zip(("category", "quote", "subject", "company_hint", "reference_id"), values))
                      for values in definitions[number]]
            if (context.get("history") or {}).get("status") == "found":
                if number == 1:
                    issues[0]["reference_id"] = "D-12"
                if number == 5:
                    issues[0]["company_hint"] = "Вектор Север"
            name, arguments = "register_issues", {"issues": issues}
        else:
            pending = [i for i in context["issues"] if i["state"] == "pending"]
            issue = pending[0]
            p = issue["payload"]
            observed = []
            for m in messages:
                if m["role"] == "tool":
                    answer = json.loads(m["content"])
                    if answer.get("status") in {"applied", "already_applied"}:
                        observed = []
                    elif answer.get("evidence_id"):
                        observed.append(answer)
            evidence = [o["evidence_id"] for o in observed]
            subject = p["subject"]
            def route(customer, target, category, ticket=None):
                args = {"issue_id": issue["id"], "customer_id": customer, "category": category,
                        "target_ref": target, "summary": subject, "evidence_ids": evidence}
                if ticket:
                    args["ticket_id"] = ticket
                return "route_request", args
            search = lambda customer, **kw: ("find_tickets", {"customer_id": customer, "subject": subject, **kw})
            plans = {
                1: [("resolve_customer", {}), ("read_thread", {}),
                    ("resolve_customer", {"reference_id": "D-12"}),
                    ("get_contract", {"customer_id": "C-12", "contract_id": "D-12"}),
                    search("C-12"), route("C-12", "marina", "technical")],
                2: [("resolve_customer", {}), ("get_order", {"customer_id": "C-21", "order_id": "O-52"}),
                    search("C-21", order_id="O-52"), route("C-21", "order_operations", "order")],
                3: [("resolve_customer", {"reference_id": "O-51"}),
                    ("get_order", {"customer_id": "C-11", "order_id": "O-51"}),
                    search("C-11", order_id="O-51"), route("C-11", "logistics", "order")],
                4: [("resolve_customer", {}), search("C-31", ticket_id="T-9"),
                    route("C-31", "sergey", "technical", "T-9")],
                5: [("read_thread", {}), ("resolve_customer", {"company_hint": "Вектор Север"}),
                    search("C-11"), route("C-11", "sales_north", "approval")],
                6: [("resolve_customer", {"company_hint": "Вектор"}),
                    ("request_clarification", {"issue_id": issue["id"], "missing_fields": ["company_identifier", "equipment_details"], "evidence_ids": evidence})],
                8: [("handoff_to_human", {"issue_id": issue["id"], "reason": "forbidden_request",
                                         "summary": subject, "evidence_ids": []})],
            }
            if number == 7:
                if p["category"] == "purchase":
                    plan = [("resolve_customer", {}), search("C-21"), route("C-21", "sales_orion", "purchase")]
                else:
                    plan = [("resolve_customer", {}), ("get_order", {"customer_id": "C-21", "order_id": "O-52"}),
                            search("C-21", order_id="O-52"), route("C-21", "order_operations", "order")]
            else:
                plan = plans[number]
            # All previous successful read observations correspond to the scripted prefix.
            index = len(observed)
            if index >= len(plan):
                raise ValueError("Offline plan exhausted; this is a test failure")
            name, arguments = plan[index]
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": uid("call"), "type": "function", "function": {"name": name, "arguments": encode(arguments)}}]}
        return {"message": message, "model": self.model, "provider": "offline-fixture",
                "usage": {}, "response_id": None, "finish_reason": "tool_calls"}
