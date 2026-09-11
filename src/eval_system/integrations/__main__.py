"""CLI for cross-project adapters."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .agent_eval import trial_results_to_agent_eval
from .feedback import build_feedback_events, load_runtime_samples, write_feedback_events
from .benchagent import materialize_benchmark_tasks


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m eval_system.integrations")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("materialize-tasks")
    p.add_argument("evaluation_json")
    p.add_argument("output_root")
    p.add_argument("--clean", action="store_true")
    p = sub.add_parser("write-feedback")
    p.add_argument("trials_root")
    p.add_argument("summary_json")
    p.add_argument("output")
    p.add_argument("--report-path", default=None)
    p.add_argument("--low-threshold", type=float, default=0.4)
    p.add_argument("--high-threshold", type=float, default=0.9)
    p.add_argument("--exclude-normal", action="store_true")
    p.add_argument("--backend", default="harbor")
    p = sub.add_parser("replay", help="thin frozen-fixture stage replay entrypoint")
    p.add_argument("replay_args", nargs=argparse.REMAINDER)
    p = sub.add_parser("export-agent-eval")
    p.add_argument("trials_root")
    p.add_argument("output_dir")
    p.add_argument("--exclude-failed", action="store_true")
    args = parser.parse_args()
    if args.command == "replay":
        from eval_system.replay_cli import main as replay_main
        return replay_main(args.replay_args)
    if args.command == "materialize-tasks":
        paths = materialize_benchmark_tasks(args.evaluation_json, args.output_root, clean=args.clean)
        print(f"materialized {len(paths)} task(s) under {args.output_root}")
    elif args.command == "write-feedback":
        samples = load_runtime_samples(args.trials_root)
        report = json.loads(Path(args.summary_json).read_text(encoding="utf-8"))
        events = build_feedback_events(
            samples, report, report_path=args.report_path or args.summary_json,
            low_threshold=args.low_threshold, high_threshold=args.high_threshold,
            include_normal=not args.exclude_normal, backend=args.backend,
        )
        write_feedback_events(args.output, events)
        print(f"feedback: {args.output} ({len(events)} event(s))")
    else:
        paths = trial_results_to_agent_eval(args.trials_root, args.output_dir, include_failed=not args.exclude_failed)
        cases_path = paths["cases"]
        cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases", [])
        if not cases:
            raise SystemExit(f"no trials found under {args.trials_root}; refusing empty export")
        for key, path in paths.items():
            print(f"{key}: {path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
