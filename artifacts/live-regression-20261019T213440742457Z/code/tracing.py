"""Local self-contained JSONL views of current and historical run reports."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

from .policy import public_report


VERSION_FIELDS = ("prompt_version", "tool_version", "code_version", "dataset_version", "model_config_version")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run_versions(store, model, mode):
    code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).parent.glob("*.py"))}
    data = [tuple(r) for r in store.db.execute("SELECT kind,id,payload FROM entities ORDER BY kind,id")]
    roles = {"extractor": model.model}
    if mode == "agent":
        roles["planner"] = model.model
    config = {"provider": getattr(model, "provider", "offline"), "roles": roles,
              "generation": getattr(model, "generation_config", {"temperature": 0, "max_tokens": 2400}),
              "request_interval_seconds": getattr(model, "interval", 0)}
    return {"trace_schema_version": "1", "code_version": digest(code), "code_files": code,
            "dataset_version": digest(data), "model_config_version": digest(config),
            "model_config": config, "model_roles": roles}


def records(report, run_id):
    report = copy.deepcopy(report)
    run = report["run"]
    if run["id"] != run_id:
        raise ValueError("Requested run_id does not match the report")
    start = next((e for e in report["events"] if e["kind"] == "run_start"), None)
    current_is_synthetic = report["message"].get("data_classification") == "synthetic"
    if start is None:
        snapshot = report["message"]
        snapshot_source = "available_report_message"
    else:
        snapshot = start["payload"].get("message")
        snapshot_source = "run_start"
        if not isinstance(snapshot, dict):
            # A redacted run_start cannot be restored from a later source state.
            snapshot = {k: v for k, v in report["message"].items() if k in {"id", "thread_id"}}
            snapshot_source = "run_start_redacted"
    report["message"] = snapshot
    # Respect both original classification and current export restrictions.
    if (report.get("redacted_for_staff") or not current_is_synthetic
            or snapshot.get("data_classification") != "synthetic"):
        report = public_report(report)
        if start is not None:
            snapshot_source = "run_start_redacted"
    config = run["config"]
    common = {"run_id": run_id, "case_id": config.get("case_id", run["message_id"]),
              "mode": run["mode"], "live_model": config.get("live_model"),
              "trace_schema_version": config.get("trace_schema_version"),
              **{key: config.get(key) for key in VERSION_FIELDS}}
    yield dict(common, kind="input_snapshot", event_index=None, snapshot_source=snapshot_source,
               snapshot_redacted=bool(report.get("redacted_for_staff")), payload=report["message"])
    config_fields = ("provider", "requested_model", "model_roles", "temperature", "max_tokens", "reasoning_effort",
                     "adapter_version", "data_policy_version", "code_files", "model_config", "faults")
    yield dict(common, kind="run_config_snapshot", event_index=None,
               payload={key: config.get(key) for key in config_fields})
    fields = ("call_id", "provider_call_id", "started_at", "ended_at", "duration_seconds",
              "model_role", "requested_model", "actual_model")
    for index, event in enumerate(report["events"], 1):
        metadata = {key: event.get(key) for key in fields}
        if event["kind"] == "model_response" and metadata["actual_model"] is None:
            metadata["actual_model"] = event["payload"].get("model")
        yield {**common, **metadata,
               "kind": event["kind"], "event_index": event.get("event_index", index),
               "at": event["at"], "payload": event["payload"]}
    yield dict(common, kind="effects_snapshot", event_index=None,
               payload={key: report[key] for key in ("issues", "actions", "outgoing", "reviews")})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--event-index", type=int, help="Show one original event; snapshot rows are omitted")
    parser.add_argument("--output", type=Path, help="Save JSONL to a new file; existing files are refused")
    args = parser.parse_args(argv)
    try:
        rows = list(records(json.loads(args.report.read_text()), args.run_id))
        if args.event_index is not None:
            rows = [r for r in rows if r["event_index"] == args.event_index]
            if not rows:
                raise ValueError("Event index not found")
        output = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        if args.output:
            with args.output.open("x") as target:
                target.write(output)
        else:
            print(output, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Do not print arbitrary source content or invalid JSON fragments.
        print("Trace unavailable: check report format, run_id and event index", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
