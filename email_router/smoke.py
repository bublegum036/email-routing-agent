"""Run four real-model checks in fresh databases and inspect saved effects.

Usage: python3 -m email_router.smoke
No key is accepted through arguments or printed in output.
"""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

from .__main__ import ROOT, execute
from .llm import ChatModel, ModelError, load_env


def assess(report, expected_target):
    problems = []
    if report["events"][-1]["payload"].get("status") != "completed":
        problems.append("run_failed")
    if not any(e["kind"] == "model_response" for e in report["events"]):
        problems.append("no_model_response")
    if len(report["issues"]) != 1 or any(i["state"] != "routed" for i in report["issues"]):
        problems.append("expected_one_routed_issue")
    actions, outgoing = report["actions"], report["outgoing"]
    if len(actions) != 1 or actions[0].get("recipient_ref") != expected_target:
        problems.append("wrong_action_or_recipient")
    if len(outgoing) != 1 or outgoing[0].get("recipient_ref") != expected_target:
        problems.append("wrong_saved_mail")
    required_review_ids = {a["review_id"] for a in actions if a.get("content_delivery") == "requires_staff"}
    staff_reviews = [r for r in report["reviews"] if r.get("kind") == "critical_data_review"
                     and r["id"] in required_review_ids and r.get("queue_ref") == expected_target
                     and r.get("state") == "awaiting_staff"]
    if len(staff_reviews) != len(report["reviews"]) or {r["id"] for r in staff_reviews} != required_review_ids:
        problems.append("unexpected_human_review")
    if len(actions) == len(outgoing) == 1:
        if actions[0].get("outgoing_message_id") != outgoing[0]["id"]:
            problems.append("receipt_mail_mismatch")
        if actions[0].get("ticket_id") != outgoing[0].get("ticket_id"):
            problems.append("receipt_ticket_mismatch")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Real-model checks with fresh databases; no key is printed")
    parser.add_argument("--mode", choices=("both", "agent", "workflow"), default="both")
    parser.add_argument("--message", choices=("both", "mail-01", "mail-02"), default="both")
    args = parser.parse_args(argv)
    load_env()
    try:
        model = ChatModel()
    except ModelError as exc:
        print(str(exc))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "artifacts" / ("live-check-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    modes = ("agent", "workflow") if args.mode == "both" else (args.mode,)
    messages = [("mail-01", "marina"), ("mail-02", "order_operations")]
    if args.message != "both":
        messages = [(message, target) for message, target in messages if message == args.message]
    total = len(modes) * len(messages)
    summary = {"requested_model": model.model, "provider": model.provider, "live_model": True,
               "requested_checks": total,
               "note": "Initial implementation checks, not the final architecture comparison",
               "checks": []}
    for mode in modes:
        for message, target in messages:
            prefix = directory / (mode + "-" + message)
            report = execute(prefix.with_suffix(".sqlite"), ROOT / "data/seed.json", model,
                             mode, message, prefix.with_suffix(".json"))
            problems = assess(report, target)
            actual_models = sorted({e["payload"]["model"] for e in report["events"]
                                    if e["kind"] == "model_response"})
            summary["checks"].append({"mode": mode, "message": message, "passed": not problems,
                                     "problems": problems, "actual_models": actual_models,
                                     "trace": prefix.with_suffix(".json").name})
            (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            # Avoid consuming more free quota when the endpoint has never responded.
            rate_limited = any(e["kind"] == "model_error" and e["payload"].get("http_status") == 429 for e in report["events"])
            if "no_model_response" in problems or rate_limited:
                if rate_limited:
                    if model.provider == "gemini":
                        print("HTTP 429: дальнейшие запросы остановлены. Проверь лимиты проекта в Google AI Studio.")
                    else:
                        print("HTTP 429: дальнейшие запросы остановлены. Проверь квоту: python3 -m email_router.quota")
                else:
                    print("Нет ответа модели. Остальные запросы остановлены; смотри summary.json и трассу.")
                print("Результаты: " + str(directory))
                return 1
    passed = sum(c["passed"] for c in summary["checks"])
    print(f"Проверки с реальной моделью: {passed}/{total}. Результаты: {directory}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
