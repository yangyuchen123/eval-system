"""Runtime hooks: stream trial outputs to your scoring layer as they finish.

Attach to a Harbor Job before running:

    from harbor.job import Job
    from eval_system.hooks import collect_sample_on_end

    job = await Job.create(config)
    job.add_hook(TrialEvent.END, collect_sample_on_end(scorer=my_scorer))
    await job.run()
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from harbor.models.trial.result import TrialResult
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from eval_system.loader import HarborEvalLoader
from eval_system.schema import EvalSample
from eval_system.contract.harbor_backend import HarborBackend
from eval_system.contract.trial import TrialResult

logger = logging.getLogger(__name__)

Scorer = Callable[[EvalSample], Awaitable[Any]]
TrialScorer = Callable[[TrialResult], Awaitable[Any]]


def collect_sample_on_end(
    scorer: Scorer | None = None,
    *,
    trials_root: str | Path | None = None,
) -> Callable[[TrialHookEvent], Awaitable[EvalSample | None]]:
    """Build a TrialEvent.END hook that loads the finished trial and scores it.

    Args:
        scorer: optional async callable receiving the EvalSample (your 打分层).
        trials_root: if given, used to resolve trial dirs that are not local
            (e.g. hosted jobs); otherwise the trial dir comes from
            TrialResult.trial_uri.
    """

    async def _hook(event: TrialHookEvent) -> EvalSample | None:
        if event.event is not TrialEvent.END:
            return None
        trial_dir = _resolve_trial_dir(event.result, trials_root)
        if trial_dir is None:
            logger.warning(
                "Cannot locate trial dir for %s; skipping scoring",
                event.result.trial_name,
            )
            return None

        loader = HarborEvalLoader(trial_dir)
        sample = loader.load_trial(trial_dir)
        if sample is None:
            logger.warning("No result.json for trial %s", event.result.trial_name)
            return None

        if scorer is not None:
            await scorer(sample)
        return sample

    return _hook


def _resolve_trial_dir(
    result: Any, trials_root: str | Path | None
) -> Path | None:
    """Locate a finished trial's directory on this host.

    ``result`` 同时接受 Harbor 的 TrialResult（v1 hook）与 eval-system 的
    TrialResult 事件对象——二者都带 trial_name / trial_uri。
    """
    if trials_root is not None:
        candidate = Path(trials_root).expanduser() / result.trial_name
        if candidate.is_dir():
            return candidate
    if result.trial_uri:
        uri = result.trial_uri
        if uri.startswith("file://"):
            path = Path(uri.removeprefix("file://"))
            if path.is_dir():
                return path
    return None


# ---------------------------------------------------------------------------
# v2: collect_trial_on_end — 回调收 TrialResult（CONTRACT.md §8）
# ---------------------------------------------------------------------------
def collect_trial_on_end(
    scorer: TrialScorer | None = None,
    *,
    jobs_dir: str | Path | None = None,
) -> Callable[[TrialHookEvent], Awaitable[TrialResult | None]]:
    """TrialEvent.END hook：把结束的 trial 升级为 TrialResult（v2 契约）后回调。

    v1 的 collect_sample_on_end 保留（收 EvalSample）；v2 统一收 TrialResult，
    打分层只消费 TrialResult，不感知后端。

    Args:
        scorer: 可选异步回调，接收 TrialResult。
        jobs_dir: Harbor jobs 根目录（用于定位/惰性生成 trial_result.json）。
    """

    async def _hook(event: TrialHookEvent) -> TrialResult | None:
        if event.event is not TrialEvent.END:
            return None
        trial_dir = _resolve_trial_dir(event.result, jobs_dir)
        if trial_dir is None:
            logger.warning(
                "Cannot locate trial dir for %s; skipping v2 scoring",
                event.result.trial_name,
            )
            return None

        jobs_root = Path(jobs_dir) if jobs_dir else trial_dir.parent.parent
        backend = HarborBackend(jobs_root)
        trial_result = await backend.read_trial(trial_dir.name)
        if scorer is not None:
            await scorer(trial_result)
        return trial_result

    return _hook
