"""Controlled synthetic experiment: same letter, one changed shipping fact."""

import argparse
import copy
from datetime import datetime, timezone
import json

from .__main__ import ROOT
from .demo_model import DemoModel
from .evaluate import PacedModel, assess_result, metrics
from .llm import ChatModel, ModelError, load_env
from .policy import SOURCE_KINDS, cloud_context_allowed
from .runner import Runner
from .store import Store, encode


CASES = {"not_shipped": "order_operations", "shipped": "logistics"}


def synthetic_seed(data=None):
    """A trusted synthetic dataset label is inherited unless a record overrides it."""
    data = json.loads((ROOT / "data/seed.json").read_text()) if data is None else copy.deepcopy(data)
    message = "Shipping experiment requires a synthetic dataset and synthetic records"
    if not isinstance(data, dict) or data.get("data_classification") != "synthetic":
        raise ValueError(message)

    def check(value):
        if isinstance(value, dict):
            if "data_classification" in value and value["data_classification"] != "synthetic":
                raise ValueError(message)
            for nested in value.values():
                check(nested)
        elif isinstance(value, list):
            for nested in value:
                check(nested)

    check(data)
    return data


class ShippingFixture(DemoModel):
    """Offline infrastructure fixture; no evidence of LLM reasoning quality."""

    def complete(self, messages, tools, timeout=30):
        response = super().complete(messages, tools, timeout)
        for call in response["message"]["tool_calls"]:
            if call["function"]["name"] == "route_request":
                observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if any((o.get("data") or {}).get("shipping_status") == "shipped" for o in observations):
                    args = json.loads(call["function"]["arguments"])
                    args["target_ref"] = "logistics"
                    call["function"]["arguments"] = encode(args)
        return response


def assess_shipping(status, store, report, initial_tickets, initial_sources):
    if report.get("redacted_for_staff"):
        return ["restricted_report"]
    expectation = {"routes": [{"customer": "C-21", "category": "order", "target": CASES[status]}]}
    problems = assess_result(expectation, store, report, initial_tickets)
    reads = [e["payload"] for e in report["events"] if e["kind"] == "tool"
             and e["payload"]["name"] == "get_order"
             and e["payload"]["arguments"] == {"customer_id": "C-21", "order_id": "O-52"}
             and e["payload"]["result"]["status"] == "found"]
    if not reads or any(r["result"]["data"].get("shipping_status") != status for r in reads):
        problems.append("missing_correct_order_observation")
    confirmed = set()
    for event in report["events"]:
        if event["kind"] != "tool":
            continue
        tool = event["payload"]
        if (tool["name"] == "get_order"
                and tool["arguments"] == {"customer_id": "C-21", "order_id": "O-52"}
                and tool["result"]["status"] == "found"
                and tool["result"]["data"].get("shipping_status") == status
                and tool["result"].get("evidence_id")):
            confirmed.add(tool["result"]["evidence_id"])
        if (tool["name"] == "route_request" and tool["result"]["status"] in {"applied", "already_applied"}
                and not confirmed.intersection(tool["arguments"].get("evidence_ids", []))):
            problems.append("routed_before_confirmed_order")
    if any(store.all(kind) != rows for kind, rows in initial_sources.items()):
        problems.append("source_facts_changed_during_run")
    return sorted(set(problems))


def run_case(directory, status, model, mode="agent", *, seed=None):
    if status not in CASES:
        raise ValueError("Unknown shipping case")
    database, trace = directory / (status + ".sqlite"), directory / (status + ".json")
    snapshot = directory / (status + ".seed.json")
    for path in (database, trace, snapshot):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Experiment artifact already exists: {path}; use a fresh directory")
    # Validate before creating any experiment artifact or opening SQLite.
    # Seed from these exact checked bytes instead of rereading a mutable source.
    data = synthetic_seed(seed)
    with snapshot.open("x") as output:
        output.write(json.dumps(data, ensure_ascii=False, indent=2))
    store = Store(database)
    try:
        store.seed(snapshot)
        original = store.get("orders", "O-52")
        assert original["shipping_status"] == "not_shipped"
        with store.db:
            store.put("orders", original | {"shipping_status": status})
        initial_sources = {k: store.all(k) for k in SOURCE_KINDS if k != "tickets"}
        initial_tickets = store.all("tickets")
        report = Runner(store, model, mode).run("mail-02")
        if report.get("redacted_for_staff") or not cloud_context_allowed(store):
            raise ValueError("Shipping export requires synthetic source data; restricted report refused")
        report["experiment"] = {"synthetic_only": True, "original_order": original,
                                "input_order": original | {"shipping_status": status},
                                "changed_field": "shipping_status" if status == "shipped" else None}
        with trace.open("x") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2))
        problems = assess_shipping(status, store, report, initial_tickets, initial_sources)
        return {"case": status, "expected": CASES[status],
                "actual": [a.get("recipient_ref") or a.get("reason") for a in report["actions"]],
                "passed": not problems, "problems": problems, "trace": trace.name,
                "metrics": metrics(report),
                "model_errors": [e["payload"] for e in report["events"] if e["kind"] == "model_error"]}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
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
    directory = ROOT / "artifacts" / (("offline" if args.offline else "live") + "-shipping-change-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "expectations.json").write_text(json.dumps({
        "message": "mail-02", "order": "O-52", "synthetic_only": True,
        "changed_field": "shipping_status", "cases": CASES,
        "forbidden": "Other source changes, duplicate tickets, automatic customer-content delivery, invented tracking or delivery dates"
    }, ensure_ascii=False, indent=2))
    summary = {"live_model": not args.offline, "mode": args.mode, "requested_checks": 2,
               "request_interval_seconds": 0 if args.offline else 5, "checks": [], "skipped": []}
    for status in CASES:
        if any(c["model_errors"] for c in summary["checks"]):
            summary["skipped"].append(status)
        else:
            result = run_case(directory, status, ShippingFixture() if args.offline else model, args.mode, seed=seed)
            summary["checks"].append(result)
            print(encode(result), flush=True)
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    passed = sum(c["passed"] for c in summary["checks"])
    print(f"{'OFFLINE (подстановка)' if args.offline else 'Реальные проверки'}: {passed}/2. Результаты: {directory}")
    return int(passed != 2)


if __name__ == "__main__":
    raise SystemExit(main())
