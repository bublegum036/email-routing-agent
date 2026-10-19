"""Compare agent and workflow against expectations fixed before processing."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .__main__ import ROOT
from .demo_model import DemoModel
from .error_cases import assess_case
from .llm import ChatModel, ModelError, load_env
from .policy import routing_notice
from .runner import Runner
from .smoke import assess
from .store import Store, encode


class PacedModel:
    """Space request starts across cases; waiting remains inside the run budget."""

    def __init__(self, model, interval, clock=time.monotonic, sleep=time.sleep):
        self.delegate, self.interval = model, interval
        self.clock, self.sleep, self.last_started = clock, sleep, None

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def complete(self, messages, tools, timeout=30):
        delay = 0 if self.last_started is None else max(0, self.last_started + self.interval - self.clock())
        if delay >= timeout:
            raise ModelError("pacing_budget_exhausted: insufficient time for another request")
        if delay:
            self.sleep(delay)
        self.last_started = self.clock()
        return self.delegate.complete(messages, tools, timeout=timeout - delay)


def continuation(previous, suite, seed, hashes, live_model, model):
    """Read previous attempts; never reopen their databases for execution."""
    summary = json.loads((previous / "summary.json").read_text())
    if (json.loads((previous / "expectations.json").read_text()) != suite
            or json.loads((previous / "seed.json").read_text()) != seed
            or summary["live_model"] != live_model):
        raise ValueError("Continuation requires the same seed, expectations and live/offline mode")
    for name in ("runner.py", "tools.py", "schemas.py", "store.py", "policy.py", "llm.py", "demo_model.py"):
        if summary["source_hashes"].get(name) != hashes[name]:
            raise ValueError("Processing code changed: start a separate comparison instead of continuing")
    checks = []
    for check in summary["checks"]:
        trace = (previous / check["trace"]).resolve()
        report = json.loads(trace.read_text())
        config = report["run"]["config"]
        expected = {"requested_model": model.model, "provider": getattr(model, "provider", "offline"),
                    **getattr(model, "generation_config", {"temperature": 0, "max_tokens": 2400})}
        if any(config.get(k) != v for k, v in expected.items()):
            raise ValueError("Model configuration changed: start a separate comparison")
        checks.append(dict(check, trace=str(trace), model_errors=[
            e["payload"] for e in report["events"] if e["kind"] == "model_error"]))
    return checks


def assess_result(case, store, report, initial_tickets):
    end = report["events"][-1]["payload"]
    if case.get("outcome") == "identity":
        return assess_case("ambiguous", store, report, end["model_calls"], initial_tickets)
    if case.get("outcome") == "forbidden":
        problems = []
        if end["status"] != "completed":
            problems.append("run_failed")
        if (len(report["issues"]) != 1 or report["issues"][0]["state"] != "human_review"
                or len(report["actions"]) != 1 or report["actions"][0].get("reason") != "forbidden_request"
                or report["outgoing"] or len(report["reviews"]) != 1):
            problems.append("expected_forbidden_request_handoff")
        if store.all("tickets") != initial_tickets:
            problems.append("tickets_changed")
        return problems
    expected = case["routes"]
    actions = report["actions"]
    problems = []
    if end["status"] != "completed":
        problems.append("run_failed")
    if not any(e["kind"] == "model_response" for e in report["events"]):
        problems.append("no_model_response")
    if Counter(a.get("recipient_ref") for a in actions) != Counter(r["target"] for r in expected):
        problems.append("wrong_recipients_or_action_count")
    if len(report["issues"]) != len(expected) or len(report["outgoing"]) != len(expected):
        problems.append("wrong_issue_or_mail_count")
    if len(report["reviews"]) != len(expected):
        problems.append("wrong_review_count")
    issues = {i["id"]: i for i in report["issues"]}
    remaining = list(expected)
    appended = set()
    for action in actions:
        issue = issues.get(action["issue_id"])
        match = next((r for r in remaining if r["target"] == action.get("recipient_ref")
                      and issue and r["category"] == issue["payload"]["category"]), None)
        if not match:
            problems.append("unexpected_action_or_category")
            continue
        remaining.remove(match)
        subset = dict(report, issues=[issue], actions=[action],
                      outgoing=[o for o in report["outgoing"] if o["issue_id"] == issue["id"]],
                      reviews=[r for r in report["reviews"] if r["issue_id"] == issue["id"]])
        problems.extend(assess(subset, match["target"]))
        ticket = store.get("tickets", action.get("ticket_id", ""))
        if not ticket or any(ticket.get(k) != value for k, value in {
                "customer_id": match["customer"], "category": match["category"],
                "assignee_ref": match["target"], "status": "open"}.items()):
            problems.append("wrong_saved_ticket")
        elif report["message"]["id"] not in ticket.get("message_ids", []):
            problems.append("missing_message_ticket_link")
        operation = "appended" if match.get("ticket") else "created"
        if action.get("ticket_operation") != operation:
            problems.append("wrong_ticket_operation")
        if match.get("ticket"):
            appended.add(match["ticket"])
            if action.get("ticket_id") != match["ticket"]:
                problems.append("wrong_existing_ticket")
        for mail in subset["outgoing"]:
            notice = routing_notice(report["message"]["id"], issue["id"], action["ticket_id"],
                                    match["target"], mail["id"])
            notice["recipient"] = store.get("recipients", match["target"])["address"]
            if mail != notice:
                problems.append("unexpected_automatic_content")
    if remaining:
        problems.append("missing_expected_action")
    for original in initial_tickets:
        allowed = dict(original)
        if original["id"] in appended:
            allowed["message_ids"] = list(dict.fromkeys(original["message_ids"] + [report["message"]["id"]]))
        if store.get("tickets", original["id"]) != allowed:
            problems.append("unexpected_existing_ticket_change")
    if len(store.all("tickets")) != len(initial_tickets) + sum(not r.get("ticket") for r in expected):
        problems.append("wrong_ticket_count")
    return sorted(set(problems))


def metrics(report):
    end = report["events"][-1]["payload"]
    tools = [e["payload"] for e in report["events"] if e["kind"] == "tool"]
    responses = [e["payload"] for e in report["events"] if e["kind"] == "model_response"]
    usage = Counter()
    for response in responses:
        usage.update({k: v for k, v in response.get("usage", {}).items()
                      if k in {"prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(v, int)})
    return {"model_calls": end["model_calls"], "tool_calls": end["tool_calls"],
            "elapsed_seconds": end["elapsed_seconds"], "tokens": dict(usage) if usage else None,
            "actual_models": sorted({r["model"] for r in responses}),
            "blocked_attempts": sum(t["result"]["status"] in {"blocked", "conflict", "invalid_arguments"} for t in tools),
            "human_handoffs": sum(i["state"] == "human_review" for i in report["issues"]),
            "staff_content_reviews": sum(r.get("kind") == "critical_data_review" for r in report["reviews"])}


def save_summary(directory, summary):
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    rows = ["# Сравнение: " + summary["split"], "",
            "Реальная модель: " + str(summary["live_model"]), "",
            "| Письмо | Путь | Режим | Ожидается | Фактически | Успех | Ошибки | Вызовы модели | Токены | Время, с | Блокировки |",
            "|---|---|---|---|---|---|---|---:|---:|---:|---:|"]
    for r in summary["checks"]:
        m = r["metrics"]
        rows.append(f"| {r['message']} | {r['path']} | {r['mode']} | {r['expected']} | {r['actual']} | {r['passed']} | "
                    f"{', '.join(r['problems'])} | {m['model_calls']} | "
                    f"{(m['tokens'] or {}).get('total_tokens', '—')} | {m['elapsed_seconds']} | {m['blocked_attempts']} |")
    for mode in ("workflow", "agent"):
        subset = [c for c in summary["checks"] if c["mode"] == mode]
        rows.extend(["", f"{mode}: {sum(c['passed'] for c in subset)}/{len(subset)} запущенных; "
                     f"уникальных писем запланировано {summary['cases_per_mode']}."])
    rows.extend(["", "Повторные попытки включены в число запусков. Исходные трассы доступны по полю trace в summary.json.",
                 "Пауза между началами запросов, с: " + str(summary.get("request_interval_seconds", 0))])
    (directory / "comparison.md").write_text("\n".join(rows) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--suite", type=Path, default=ROOT / "data/evaluation-dev.json")
    parser.add_argument("--seed", type=Path, default=ROOT / "data/seed.json")
    parser.add_argument("--continue-from", type=Path, help="Previous comparison directory; keep all attempts and retry unfinished checks")
    parser.add_argument("--request-interval", type=float, default=5,
                        help="Seconds between API request starts, 0..10; counted in the existing run budget (default: 5)")
    args = parser.parse_args(argv)
    if not 0 <= args.request_interval <= 10:
        parser.error("--request-interval must be between 0 and 10")
    suite = json.loads(args.suite.read_text())
    seed = json.loads(args.seed.read_text())
    if seed.get("data_classification") != "synthetic" or any(
            row.get("data_classification", "synthetic") != "synthetic"
            for rows in seed.values() if isinstance(rows, list) for row in rows):
        raise ValueError("Evaluation exports are for explicitly synthetic fixtures only")
    if suite["split"] not in {"development", "held_out"} or not suite["cases"]:
        raise ValueError("Expected a nonempty development or held_out suite")
    identifiers = {m["id"] for m in seed["messages"]}
    if len({c["message"] for c in suite["cases"]}) != len(suite["cases"]):
        raise ValueError("Duplicate case IDs; use a fresh run for repeated attempts")
    for case in suite["cases"]:
        if (not re.fullmatch(r"mail-\d+", case["message"])
                or case["message"] not in identifiers or not case.get("path")):
            raise ValueError("Unknown message or missing path")
        if bool(case.get("routes")) == (case.get("outcome") in {"identity", "forbidden"}):
            raise ValueError("Each case needs routes or an identity/forbidden outcome")
    load_env()
    try:
        # Fresh scripted model per case; no automatic offline fallback.
        model = None if args.offline else ChatModel()
    except ModelError as exc:
        print(str(exc))
        return 2
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "email_router").glob("*.py"))}
    prior = continuation(args.continue_from.resolve(), suite, seed, hashes, not args.offline,
                         DemoModel() if args.offline else model) if args.continue_from else []
    passed_checks = {(c["message"], c["mode"]) for c in prior if c["passed"]}
    if model is not None:
        model = PacedModel(model, args.request_interval)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "artifacts" / (("offline" if args.offline else "live") + "-comparison-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "expectations.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2))
    (directory / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2))
    for check in prior:
        check["trace"] = os.path.relpath(check["trace"], directory.resolve())
    summary = {"split": suite["split"], "live_model": not args.offline,
               "source_hashes": hashes, "cases_per_mode": len(suite["cases"]), "checks": prior, "stopped": False,
               "previous_run": str(args.continue_from.resolve()) if args.continue_from else None,
               "request_interval_seconds": 0 if args.offline else args.request_interval}
    save_summary(directory, summary)
    for case in suite["cases"]:
        for mode in ("workflow", "agent"):
            if (case["message"], mode) in passed_checks:
                continue
            name = f"{mode}-{case['message']}"
            store = Store(directory / (name + ".sqlite"))
            try:
                store.seed(args.seed)
                initial = store.all("tickets")
                report = Runner(store, DemoModel() if args.offline else model, mode).run(case["message"])
                (directory / (name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
                problems = assess_result(case, store, report, initial)
                check = {"message": case["message"], "path": case["path"], "mode": mode,
                         "passed": not problems, "problems": problems, "trace": name + ".json",
                         "expected": ", ".join(r["target"] for r in case.get("routes", [])) or case["outcome"],
                         "actual": ", ".join(a.get("recipient_ref") or a.get("reason") or
                                             ("clarification:" + "+".join(a.get("missing_fields", [])))
                                             for a in report["actions"]),
                         "metrics": metrics(report),
                         "model_errors": [e["payload"] for e in report["events"] if e["kind"] == "model_error"]}
                summary["checks"].append(check)
                # Preserve failures, stop further quota consumption after provider/transport errors.
                summary["stopped"] = any(e["kind"] == "model_error" for e in report["events"])
                save_summary(directory, summary)
                print(encode(check), flush=True)
            finally:
                store.close()
            if summary["stopped"]:
                if any(e.get("http_status") == 429 for e in check["model_errors"]):
                    print("HTTP 429: проверь текущие квоты проекта в Google AI Studio. Пауза не устраняет исчерпание дневной квоты.")
                print("Запуски остановлены после ошибки модели. Результаты: " + str(directory))
                return 1
    print("Результаты: " + str(directory))
    # Historical failures stay in the denominator, including in the exit status.
    return int(any(not c["passed"] for c in summary["checks"]))


if __name__ == "__main__":
    raise SystemExit(main())
