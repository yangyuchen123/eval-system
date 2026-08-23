"""CLI: python -m eval_system <path> [options]

v1（默认）：把 Harbor trial 归一为 EvalSample 汇总 / 导出 JSONL。
v2（--v2）：读取/生成 TrialResult（CONTRACT.md 核心资产）并导出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval_system.contract.harbor_backend import HarborBackend
from eval_system.loader import HarborEvalLoader


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_system",
        description="Load Harbor trial outputs as unified EvalSamples / TrialResults.",
    )
    parser.add_argument("path", help="jobs root / job dir / single trial dir")
    parser.add_argument(
        "--dump",
        metavar="OUT",
        help="Write samples as JSONL to OUT (use - for stdout)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a per-sample one-line summary",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help="v2 模式：输出 TrialResult（CONTRACT.md），老目录自动补 specs/trial_result.json",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="v2 模式：读取时校验 specs checksum（防篡改 / 防 Spec 漂移）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.v2:
        return _main_v2(args)

    loader = HarborEvalLoader(args.path)
    samples = list(loader.iter_samples())
    if not samples:
        print(f"No trials found under {args.path}", file=sys.stderr)
        return 1

    if args.dump:
        target = sys.stdout if args.dump == "-" else open(args.dump, "w", encoding="utf-8")
        with target:
            for sample in samples:
                target.write(sample.model_dump_json() + "\n")
        if args.dump != "-":
            print(f"Wrote {len(samples)} samples to {args.dump}")

    if args.summary or not args.dump:
        _print_summary(samples)
    return 0


def _main_v2(args: argparse.Namespace) -> int:
    """v2：遍历 trial，生成/读取 TrialResult。"""
    path = Path(args.path)
    if (path / "trial_result.json").is_file() or (path / "result.json").is_file():
        jobs_root = path.parent.parent
        backend = HarborBackend(jobs_root)
        trial_ids = [path.name]
    else:
        backend = HarborBackend(path)
        trial_ids = backend.list_trials()
    return asyncio.run(_read_all_v2(backend, trial_ids, args))


async def _read_all_v2(backend, trial_ids, args) -> int:
    results = []
    for trial_id in trial_ids:
        try:
            results.append(await backend.read_trial(trial_id))
        except FileNotFoundError as exc:
            print(f"skip {trial_id}: {exc}", file=sys.stderr)

    if not results:
        print(f"No trials found under {args.path}", file=sys.stderr)
        return 1

    if args.dump:
        target = sys.stdout if args.dump == "-" else open(args.dump, "w", encoding="utf-8")
        with target:
            for r in results:
                target.write(r.model_dump_json() + "\n")
        if args.dump != "-":
            print(f"Wrote {len(results)} trial_results to {args.dump}")

    if args.summary or not args.dump:
        print(f"{'backend':<8}{'status':<10}{'reward':<7}{'trial_id'}")
        print("-" * 60)
        for r in results:
            reward = r.scoring.reward
            print(
                f"{r.producer.backend:<8}{r.status:<10}{str(reward):<7}{r.trial_id}"
            )
    return 0


def _print_summary(samples) -> None:
    for sample in samples:
        reward = sample.scoring.reward
        reward_s = "n/a" if reward is None else f"{reward:g}"
        n_steps = len(sample.runtrace.steps())
        n_artifacts = len(sample.artifacts.files())
        print(
            f"{sample.agent.name:<16} {sample.agent.model or '-':<28} "
            f"{sample.scenario.task_name or '-':<24} "
            f"reward={reward_s:<4} steps={n_steps:<3} artifacts={n_artifacts:<3} "
            f"{sample.trial_dir}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
