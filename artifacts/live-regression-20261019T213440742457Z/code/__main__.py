"""CLI: run a real model, inspect persisted effects, or exercise an offline fixture."""

import argparse
import json
import sys
from pathlib import Path

from .llm import ChatModel, ModelError, load_env
from .runner import Runner
from .store import Store, encode


ROOT = Path(__file__).resolve().parent.parent


def execute(db, seed, model, mode, message, output):
    store = Store(db)
    try:
        store.seed(seed)
        report = Runner(store, model, mode).run(message)
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(encode({"run_id": report["run"]["id"], "mode": mode,
                      "live_model": model.is_live, "message": message,
                      "issues": [{"id": i["id"], "state": i["state"]} for i in report["issues"]],
                      "actions": report["actions"], "report": str(output) if output else None}))
        return report
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local email router; all outgoing mail stays in SQLite")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Real model run (requires local API configuration)")
    run.add_argument("--mode", choices=("agent", "workflow"), required=True)
    run.add_argument("--message", required=True)
    run.add_argument("--db", required=True, help="Use a fresh path for a new experiment")
    run.add_argument("--output", required=True)
    run.add_argument("--seed", default=str(ROOT / "data/seed.json"))
    demo = sub.add_parser("demo", help="OFFLINE scripted fixture, not a real model evaluation")
    demo.add_argument("--directory", default="artifacts/offline-demo")
    demo.add_argument("--message", default="all")
    demo.add_argument("--mode", choices=("agent", "workflow", "both"), default="both")
    inspect = sub.add_parser("inspect", help="Inspect actual stored effects")
    inspect.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    load_env()
    try:
        if args.command == "run":
            model = ChatModel()  # Fail before creating a run if configuration is missing.
            report = execute(args.db, args.seed, model, args.mode, args.message, args.output)
            return 0 if report["events"][-1]["payload"]["status"] == "completed" else 1
        if args.command == "demo":
            from .demo_model import DemoModel
            modes = ("agent", "workflow") if args.mode == "both" else (args.mode,)
            messages = [f"mail-{n:02}" for n in range(1, 9)] if args.message == "all" else [args.message]
            failed = False
            for mode in modes:
                for message in messages:
                    root = Path(args.directory) / mode / message
                    root.mkdir(parents=True, exist_ok=True)
                    report = execute(root / "state.sqlite", ROOT / "data/seed.json", DemoModel(),
                                     mode, message, root / "trace.json")
                    failed |= report["events"][-1]["payload"]["status"] != "completed"
            return int(failed)
        if not Path(args.db).is_file():
            raise ValueError("Database does not exist")
        store = Store(args.db)
        try:
            print(json.dumps({"tickets": store.all("tickets"),
                  "outgoing": [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM outgoing")],
                  "reviews": [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM reviews")]},
                  ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    except (ModelError, ValueError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
