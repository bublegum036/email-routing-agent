"""Step 5: transient order-source failure followed by a clean normal run."""

import argparse
from datetime import datetime, timezone
import json

from .__main__ import ROOT
from .demo_model import DemoModel
from .evaluate import PacedModel, assess_result, metrics
from .llm import ChatModel, ModelError, load_env
from .runner import Runner
from .store import Store, encode


CASES = {
    "transient_order": {
        "faults": {"get_order": 1},
        "expected_reads": ["unavailable", "found"],
        "expected": "Retry the failed read, then route O-52 to order_operations; one new ticket and one staff notice",
    },
    "normal_order": {
        "faults": {},
        "expected_reads": ["found"],
        "expected": "Process the same original letter in a separate clean database without injected failures",
    },
}
EXPECTATION = {"message": "mail-02", "path": "paid_not_shipped",
               "routes": [{"customer": "C-21", "category": "order", "target": "order_operations"}]}
FORBIDDEN = "Routing before a successful order read; invented facts; duplicate tickets or notices; automatic delivery of customer content"


def assess_recovery(case, store, report, initial_tickets, initial_sources):
    problems = assess_result(EXPECTATION, store, report, initial_tickets)
    tools = [e["payload"] for e in report["events"] if e["kind"] == "tool"]
    reads = [e for e in tools if e["name"] == "get_order"]
    statuses = [e["result"]["status"] for e in reads]
    expected = CASES[case]["expected_reads"]
    if statuses[:len(expected)] != expected or any(s != "found" for s in statuses[len(expected):]):
        problems.append("wrong_recovery_read_sequence")
    if any(e["arguments"] != {"customer_id": "C-21", "order_id": "O-52"} for e in reads):
        problems.append("wrong_order_read_arguments")
    confirmed = False
    for event in tools:
        if event["name"] == "get_order" and event["result"]["status"] == "found":
            confirmed = True
        if event["name"] == "route_request" and event["result"]["status"] in {"applied", "already_applied"} and not confirmed:
            problems.append("routed_before_source_recovered")
    if any(store.all(kind) != rows for kind, rows in initial_sources.items()):
        problems.append("source_facts_changed")
    return sorted(set(problems)), statuses


def run_case(directory, case, model, mode="agent"):
    database = directory / (case + ".sqlite")
    trace = directory / (case + ".json")
    # Experiments must never resume a previous run or replace its evidence.
    for path in (database, trace):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Experiment artifact already exists: {path}; use a fresh directory")
    store = Store(database)
    try:
        store.seed(ROOT / "data/seed.json")
        initial_tickets = store.all("tickets")
        initial_sources = {k: store.all(k) for k in ("orders", "contracts", "customers", "contacts")}
        report = Runner(store, model, mode, dict(CASES[case]["faults"])).run("mail-02")
        with trace.open("x") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2))
        problems, statuses = assess_recovery(case, store, report, initial_tickets, initial_sources)
        return {"case": case, "passed": not problems, "problems": problems, "order_reads": statuses,
                "trace": case + ".json", "metrics": metrics(report),
                "model_errors": [e["payload"] for e in report["events"] if e["kind"] == "model_error"]}
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Scripted fixture only; no live LLM evaluation")
    parser.add_argument("--mode", choices=("agent", "workflow"), default="agent")
    args = parser.parse_args(argv)
    load_env()
    try:
        model = None if args.offline else PacedModel(ChatModel(), 5)
    except ModelError as exc:
        print(str(exc))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "artifacts" / (("offline" if args.offline else "live") + "-recovery-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "expectations.json").write_text(json.dumps(
        {"cases": CASES, "route": EXPECTATION, "forbidden": FORBIDDEN}, ensure_ascii=False, indent=2))
    summary = {"live_model": not args.offline, "mode": args.mode, "requested_checks": 2,
               "request_interval_seconds": 0 if args.offline else 5, "checks": [], "skipped": []}
    for case in CASES:
        if any(c["model_errors"] for c in summary["checks"]):
            summary["skipped"].append(case)
        else:
            result = run_case(directory, case, DemoModel() if args.offline else model, args.mode)
            summary["checks"].append(result)
            print(encode(result), flush=True)
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    passed = sum(c["passed"] for c in summary["checks"])
    print(f"{'OFFLINE (подстановка)' if args.offline else 'Реальные проверки'}: {passed}/2. Результаты: {directory}")
    return int(passed != 2)


if __name__ == "__main__":
    raise SystemExit(main())
