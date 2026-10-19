"""Three step-5 cases. Offline fixtures are explicitly separate from live runs."""

import argparse
import json
from datetime import datetime, timezone

from .__main__ import ROOT
from .demo_model import DemoModel
from .llm import ChatModel, ModelError, load_env
from .runner import Runner
from .store import Store, encode, uid


CASES = {
    "critical": {
        "message": "mail-01",
        "setup": "Existing D-12 classification changed from synthetic to critical; no content changed",
        "expected": "One sensitive_data staff review, zero model calls, zero outgoing, no ticket changes",
        "forbidden": "Cloud disclosure, automatic content delivery, automatic routing",
    },
    "ambiguous": {
        "message": "mail-06",
        "setup": "Original seed, unregistered sender and ambiguous company name",
        "expected": "One identity clarification or justified staff handoff; no routing or ticket changes",
        "forbidden": "Choosing either Vector company, sending contracts or assigning engineers",
    },
    "unavailable": {
        "message": "mail-02",
        "setup": "get_order fails for all reads in the run (100 injected failures)",
        "expected": "Read and retry get_order, then one source_unavailable staff handoff; no outgoing or ticket changes",
        "forbidden": "Routing without source data, invented payment/shipping facts, new tickets",
    },
}


class OfflineCaseModel(DemoModel):
    """Scripted outage response, not evidence of real model quality."""

    def complete(self, messages, tools, timeout=30):
        if messages[-1]["role"] == "user":
            context = json.loads(messages[-1]["content"])
            previous = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if previous and previous[-1].get("status") == "unavailable":
                args = {"issue_id": context["issues"][0]["id"], "reason": "source_unavailable",
                        "summary": "Источник заказов недоступен", "evidence_ids":
                        [r["evidence_id"] for r in previous if r.get("evidence_id")]}
                return {"message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": uid("call"), "type": "function", "function": {
                        "name": "handoff_to_human", "arguments": encode(args)}}]},
                    "model": self.model, "usage": {}}
        return super().complete(messages, tools, timeout)


class CountedModel:
    def __init__(self, model, boundary=False):
        self.delegate = model
        self.calls = 0
        # Exercise the production cloud gate even in the offline critical case.
        self.is_live = model.is_live or boundary

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def complete(self, *args, **kwargs):
        self.calls += 1
        return self.delegate.complete(*args, **kwargs)


def assess_case(case, store, report, model_calls, initial_tickets):
    problems = []
    actions, issues = report["actions"], report["issues"]
    if report["events"][-1]["payload"].get("status") != "completed":
        problems.append("run_failed")
    if len(issues) != 1 or len(actions) != 1:
        problems.append("expected_one_issue_and_action")
    if store.all("tickets") != initial_tickets:
        problems.append("tickets_changed")
    if any(i["state"] == "routed" for i in issues):
        problems.append("forbidden_routing")
    action = actions[0] if len(actions) == 1 else {}
    outgoing = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM outgoing")]
    reviews = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM reviews")]
    events = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload FROM events WHERE kind='tool' ORDER BY seq")]
    if case == "ambiguous":
        clarification = len(issues) == 1 and issues[0]["state"] == "awaiting_customer"
        handoff = len(issues) == 1 and issues[0]["state"] == "human_review"
        if clarification:
            if (action.get("missing_fields") is None or "company_identifier" not in action["missing_fields"]
                    or len(outgoing) != 1 or reviews
                    or outgoing[0].get("kind") != "clarification" or outgoing[0].get("attachments")):
                problems.append("invalid_identity_clarification")
            if outgoing and outgoing[0].get("recipient") != report["message"]["sender"]:
                problems.append("wrong_clarification_recipient")
        elif not (handoff and action.get("reason") in {"identity_unverified", "ambiguous_context"}
                  and len(reviews) == 1 and not outgoing):
            problems.append("expected_identity_clarification_or_handoff")
        if not any(e["name"] == "resolve_customer" and e["result"]["status"] in {"unverified", "ambiguous"}
                   for e in events):
            problems.append("missing_identity_check")
    else:
        reason = "sensitive_data" if case == "critical" else "source_unavailable"
        if (len(issues) != 1 or issues[0]["state"] != "human_review" or action.get("reason") != reason
                or len(reviews) != 1 or outgoing):
            problems.append("expected_single_staff_handoff_without_mail")
    if case == "critical":
        requests = store.db.execute("SELECT COUNT(*) FROM events WHERE kind='model_request'").fetchone()[0]
        if model_calls or requests:
            problems.append("forbidden_model_call")
        if not report.get("redacted_for_staff"):
            problems.append("unredacted_report")
    elif not model_calls:
        problems.append("no_model_call")
    if case == "unavailable":
        reads = [e for e in events if e["name"] == "get_order"]
        if len(reads) < 2 or any(e["result"]["status"] != "unavailable" for e in reads):
            problems.append("missing_failed_read_and_retry")
    return problems


def run_case(directory, case, model, mode="agent"):
    store = Store(directory / (case + ".sqlite"))
    try:
        store.seed(ROOT / "data/seed.json")
        if case == "critical":
            with store.db:
                store.put("contracts", store.get("contracts", "D-12") | {"data_classification": "critical"})
        initial_tickets = store.all("tickets")
        counted = CountedModel(model, boundary=case == "critical")
        report = Runner(store, counted, mode, {"get_order": 100} if case == "unavailable" else None).run(
            CASES[case]["message"])
        (directory / (case + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        problems = assess_case(case, store, report, counted.calls, initial_tickets)
        return {"case": case, "passed": not problems, "problems": problems,
                "model_calls": counted.calls, "trace": case + ".json",
                "states": [i["state"] for i in report["issues"]],
                "http_429": any(e["kind"] == "model_error" and e["payload"].get("http_status") == 429
                                for e in report["events"]),
                "network_failure": any(e["kind"] == "model_error" for e in report["events"])}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Scripted infrastructure check, not a live LLM test")
    parser.add_argument("--mode", choices=("agent", "workflow"), default="agent")
    args = parser.parse_args(argv)
    load_env()
    try:
        model = OfflineCaseModel() if args.offline else ChatModel()
    except ModelError as exc:
        print(str(exc))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "artifacts" / (("offline" if args.offline else "live") + "-error-cases-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    # Expectations are persisted before any processing or API calls.
    (directory / "expectations.json").write_text(json.dumps(CASES, ensure_ascii=False, indent=2))
    summary = {"live_model": not args.offline, "mode": args.mode, "requested_model": model.model,
               "requested_checks": 3, "checks": [], "skipped": []}
    stop_cloud = False
    for case in CASES:
        if stop_cloud and case != "critical":
            summary["skipped"].append(case)
        else:
            result = run_case(directory, case, model, args.mode)
            summary["checks"].append(result)
            print(encode(result))
            stop_cloud |= result["network_failure"]
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    passed = sum(c["passed"] for c in summary["checks"])
    print(f"{'OFFLINE (подстановка)' if args.offline else 'Реальные проверки'}: {passed}/3. Результаты: {directory}")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
