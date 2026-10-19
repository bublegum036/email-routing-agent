"""Boundary experiment: a reachable order source returns an incomplete record."""

import argparse
from datetime import datetime, timezone
import hashlib
import json

from .__main__ import ROOT
from .error_cases import OfflineCaseModel
from .evaluate import PacedModel, assess_result, metrics
from .llm import ChatModel, ModelError, load_env
from .policy import SOURCE_KINDS, cloud_context_allowed
from .runner import Runner
from .shipping_change import synthetic_seed
from .store import Store, encode


CASES = {
    "complete_order": "One new ticket routed to order_operations after reading O-52",
    "missing_shipping": "Observe missing shipping_status or its structured validation error, then controlled staff handoff; completed run, no routing, outgoing or ticket changes",
}


def incomplete_order_observed(reads):
    """Evidence of this exact mutation, rather than generic source unavailability.

    Structured validation contract: source_data_incomplete with missing_fields
    containing shipping_status, scoped by the get_order arguments to C-21/O-52.
    """
    for read in reads:
        answer = read["result"]
        data = answer.get("data")
        if (answer["status"] == "found" and isinstance(data, dict)
                and data.get("id") == "O-52" and data.get("customer_id") == "C-21"
                and "shipping_status" not in data):
            return True
        error = answer.get("error") or {}
        if (answer["status"] == "unavailable" and error.get("code") == "source_data_incomplete"
                and isinstance(error.get("missing_fields"), list)
                and "shipping_status" in error["missing_fields"]
                and (data is None or isinstance(data, dict) and "shipping_status" not in data)):
            return True
    return False


def assess_boundary(case, store, report, initial_tickets, initial_sources):
    if report.get("redacted_for_staff"):
        return ["restricted_report"]
    if case == "complete_order":
        problems = assess_result({"routes": [{"customer": "C-21", "category": "order",
                                              "target": "order_operations"}]}, store, report, initial_tickets)
    else:
        problems = []
        if report["events"][-1]["payload"]["status"] != "completed":
            problems.append("run_failed_instead_of_controlled_handoff")
        if any(e["kind"] == "controller_error" for e in report["events"]):
            problems.append("controller_error")
        if (len(report["issues"]) != 1 or report["issues"][0]["state"] != "human_review"
                or len(report["actions"]) != 1 or report["actions"][0].get("reason")
                not in {"source_unavailable", "conflicting_facts"}
                or report["outgoing"] or len(report["reviews"]) != 1):
            problems.append("expected_controlled_source_handoff")
        if store.all("tickets") != initial_tickets:
            problems.append("tickets_changed")
    reads = [e["payload"] for e in report["events"] if e["kind"] == "tool"
             and e["payload"]["name"] == "get_order"
             and e["payload"]["arguments"] == {"customer_id": "C-21", "order_id": "O-52"}
             and e["payload"]["result"]["status"] in {"found", "unavailable"}]
    if not reads:
        problems.append("order_source_not_checked")
    if case == "missing_shipping" and not incomplete_order_observed(reads):
        problems.append("incomplete_order_not_observed")
    if any(store.all(kind) != rows for kind, rows in initial_sources.items()):
        problems.append("source_facts_changed_during_run")
    return sorted(set(problems))


def run_case(directory, case, model, mode="agent", *, seed=None):
    if case not in CASES:
        raise ValueError("Unknown source boundary case")
    database, trace, snapshot = [directory / (case + suffix) for suffix in (".sqlite", ".json", ".seed.json")]
    for path in (database, trace, snapshot):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Experiment artifact already exists: {path}; use a fresh directory")
    data = synthetic_seed(seed)
    order = next(o for o in data["orders"] if o["id"] == "O-52")
    if order.get("shipping_status") != "not_shipped":
        raise ValueError("Boundary baseline requires original not_shipped O-52")
    if case == "missing_shipping":
        del order["shipping_status"]
    with snapshot.open("x") as output:
        output.write(json.dumps(data, ensure_ascii=False, indent=2))
    store = Store(database)
    try:
        store.seed(snapshot)
        initial_tickets = store.all("tickets")
        initial_sources = {k: store.all(k) for k in SOURCE_KINDS if k != "tickets"}
        report = Runner(store, model, mode).run("mail-02")
        if report.get("redacted_for_staff") or not cloud_context_allowed(store):
            raise ValueError("Synthetic-only experiment: restricted report export refused")
        problems = assess_boundary(case, store, report, initial_tickets, initial_sources)
        with trace.open("x") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2))
        return {"case": case, "passed": not problems, "problems": problems,
                "incomplete_order_observed": ("incomplete_order_not_observed" not in problems)
                if case == "missing_shipping" else None,
                "actual": [a.get("recipient_ref") or a.get("reason") for a in report["actions"]],
                "metrics": metrics(report), "trace": trace.name,
                "controller_errors": [e["payload"] for e in report["events"] if e["kind"] == "controller_error"],
                "model_errors": [e["payload"] for e in report["events"] if e["kind"] == "model_error"]}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Scripted fixture, not a real LLM evaluation")
    parser.add_argument("--mode", choices=("agent", "workflow"), default="agent")
    args = parser.parse_args(argv)
    try:
        seed = synthetic_seed()
    except ValueError as exc:
        parser.error(str(exc))
    load_env()
    try:
        model = None if args.offline else PacedModel(ChatModel(), 5)
    except ModelError as exc:
        print(str(exc))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "artifacts" / (("offline" if args.offline else "live") + "-source-boundary-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "expectations.json").write_text(json.dumps({
        "message": "mail-02", "synthetic_only": True, "cases": CASES,
        "mutation": "Delete only O-52.shipping_status in the missing_shipping case",
        "forbidden": "Inventing missing status or delivery facts; routing without source proof; customer-content delivery",
        "source_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / "email_router").glob("*.py"))},
    }, ensure_ascii=False, indent=2))
    summary = {"live_model": not args.offline, "mode": args.mode, "requested_checks": 2,
               "request_interval_seconds": 0 if args.offline else 5, "checks": [], "skipped": []}
    for case in CASES:
        if any(c["model_errors"] for c in summary["checks"]):
            summary["skipped"].append(case)
        else:
            result = run_case(directory, case, OfflineCaseModel() if args.offline else model, args.mode, seed=seed)
            summary["checks"].append(result)
            print(encode(result), flush=True)
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    passed = sum(c["passed"] for c in summary["checks"])
    print(f"{'OFFLINE (подстановка)' if args.offline else 'Реальные проверки'}: {passed}/2. Результаты: {directory}")
    return int(passed != 2)


if __name__ == "__main__":
    raise SystemExit(main())
