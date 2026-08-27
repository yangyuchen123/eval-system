"""CLI for cross-project adapters."""
from __future__ import annotations
import argparse
from .agent_eval import trial_results_to_agent_eval
from .benchagent import materialize_benchmark_tasks


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval_system.integrations")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("materialize-tasks")
    p.add_argument("evaluation_json")
    p.add_argument("output_root")
    p.add_argument("--clean", action="store_true")
    p = sub.add_parser("export-agent-eval")
    p.add_argument("trials_root")
    p.add_argument("output_dir")
    p.add_argument("--exclude-failed", action="store_true")
    args = parser.parse_args()
    if args.command == "materialize-tasks":
        paths = materialize_benchmark_tasks(args.evaluation_json, args.output_root, clean=args.clean)
        print(f"materialized {len(paths)} task(s) under {args.output_root}")
    else:
        paths = trial_results_to_agent_eval(args.trials_root, args.output_dir, include_failed=not args.exclude_failed)
        cases_path = paths["cases"]
        import json
        cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases", [])
        if not cases:
            raise SystemExit(f"no trials found under {args.trials_root}; refusing empty export")
        for key, path in paths.items():
            print(f"{key}: {path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
