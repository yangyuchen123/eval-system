"""Octagon backend: run agents through OpenAgentOctagon adapters and envs.

Bridges octagon's ``LoadedEnv`` + adapter + scorer into the eval-system
``TrialResult`` contract. The runtrace uses octagon's unified schema
(``octagon-trajectory-v1``, backend/wire/trajectory_schema.py) directly —
no forced ATIF conversion; downstream may map it if a cross-backend
canonical trajectory is needed.

The backend is thin: eval-system stays backend-agnostic (Harbor / local /
octagon all produce the same TrialResult), matching the "轨迹转统一结构" goal.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from eval_system.contract.backend import AsyncTrial, ExecutionBackend, Trial
from eval_system.contract.trial import (
    Producer,
    TrialResult,
    write_specs,
    write_trial_result,
)
from eval_system.schema import ArtifactFile, Artifacts, RunTrace, Scoring


class OctagonBackend(ExecutionBackend):
    """ExecutionBackend backed by OpenAgentOctagon (envs + adapters + scorer).

    Parameters
    ----------
    trials_root : path to write eval-system trial dirs.
    octagon_root : the open-agent-octagon repository (sys.path source).
    envs_path : octagon envs dir (81 environments or framework examples).
    agent_name : octagon adapter id ("fake", "kimi-code", "opencode", ...).
    model : optional provider-prefixed model ("openrouter/glm-5.2", ...).
    env_name : pin a specific octagon environment; default = first loaded.
    config_path : optional octagon.yaml (Settings); default loads env vars.
    """

    name = "octagon"

    def __init__(
        self,
        trials_root: str | Path,
        *,
        octagon_root: str | Path,
        envs_path: str | Path,
        data_path: str | Path | None = None,
        config_path: str | Path | None = None,
        agent_name: str = "fake",
        model: str | None = None,
        env_name: str | None = None,
    ) -> None:
        self.trials_root = Path(trials_root)
        self.trials_root.mkdir(parents=True, exist_ok=True)
        self.octagon_root = Path(octagon_root)
        self.envs_path = Path(envs_path)
        self.data_path = Path(data_path) if data_path else self.trials_root / "octagon-data"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(config_path) if config_path else None
        self.agent_name = agent_name
        self.model = model
        self.env_name = env_name
        self._octagon_imported = False

    # ------------------------------------------------------------- octagon runtime
    def _import_octagon(self) -> None:
        if self._octagon_imported:
            return
        root = str(self.octagon_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        self._octagon_imported = True

    def _load_env_and_task(self):
        self._import_octagon()
        from backend.env_loader import EnvLoader

        loader = EnvLoader(self.envs_path)
        envs = loader.load_all(allow_unavailable_core=True)
        if not envs:
            raise RuntimeError(f"no octagon environments under {self.envs_path}")
        env = envs.get(self.env_name) if self.env_name else next(iter(envs.values()))
        if env.load_error:
            raise RuntimeError(f"env {env.name} failed to load: {env.load_error}")
        if not env.tasks:
            raise RuntimeError(f"env {env.name} has no tasks")
        return env, env.tasks[0]

    def _build_adapter(self):
        self._import_octagon()
        from backend.config import load_settings
        from backend.run_dispatch import build_adapter

        settings = load_settings(self.config_path)
        return build_adapter(self.agent_name, settings, model=self.model), settings

    # ------------------------------------------------------------- ExecutionBackend
    async def run(
        self,
        task: Any,
        agent: Any,
        environment: Any,
        *,
        run_id: str | None = None,
    ) -> Trial:
        trial_id = f"{task.name.replace('/', '__')}__{agent.name}"
        trial_dir = self.trials_root / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "artifacts").mkdir(exist_ok=True)
        (trial_dir / "agent").mkdir(exist_ok=True)
        spec_refs = write_specs(trial_dir, {"task": task, "agent": agent, "environment": environment})

        async def _runner() -> TrialResult:
            self._reset_run_state()
            await self._execute_octagon(trial_dir, task, agent)
            result = self._build_result(trial_dir, task, agent, spec_refs, run_id)
            write_trial_result(trial_dir, result)
            return result

        return AsyncTrial(trial_id=trial_id, run_id=run_id, task=asyncio.ensure_future(_runner()))

    async def read_trial(self, trial_id: str) -> TrialResult:
        from eval_system.contract.trial import read_trial_result
        return read_trial_result(self.trials_root / trial_id)

    def list_trials(self) -> list[str]:
        return sorted(
            p.name for p in self.trials_root.iterdir()
            if p.is_dir() and (p / "trial_result.json").is_file()
        )

    # ------------------------------------------------------------- execution
    async def _execute_octagon(self, trial_dir: Path, task: Any, agent: Any) -> None:
        self._import_octagon()
        from backend.adapters.base import AdapterRunInput

        env, octagon_task = self._load_env_and_task()
        adapter, settings = self._build_adapter()

        attempt_id = f"{trial_dir.name}__{self.agent_name}"
        attempt_dir = self.data_path / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)

        run_input = AdapterRunInput(
            attempt_id=attempt_id,
            task_id=octagon_task.id if hasattr(octagon_task, "id") else str(task.name),
            task_prompt=getattr(octagon_task, "prompt", None) or task.instruction,
            task_context=dict(getattr(octagon_task, "context", None) or {}),
            timeout_seconds=int(getattr(octagon_task, "timeout_seconds", 600) or 600),
            env_name=env.name,
            env_skill_id=env.skill_id,
            env_token="octagon-backend",
            env_base_url="http://127.0.0.1:1",  # unused for fake / CLI workspace tasks
        )
        result = await adapter.run(run_input, env, self.data_path)
        self._status = getattr(result, "status", "completed")

        # scorer
        score_rows: list[dict[str, Any]] = []
        if env.scorer_module is not None:
            scorer = getattr(env.scorer_module, "score", None)
            if scorer:
                import inspect
                try:
                    kwargs = {"attempt_id": attempt_id, "task": run_input}
                    if "env_db" in inspect.signature(scorer).parameters:
                        kwargs["env_db"] = None
                    score_rows = scorer(**kwargs) or []
                except Exception as exc:  # noqa: BLE001 - scorer failure must not sink the trial
                    score_rows = [{"dimension": "error", "value": 0, "detail": repr(exc)[:300]}]
        self._score_rows = score_rows

        # copy workspace -> artifacts
        workspace = attempt_dir / "skill_workspace"
        if workspace.is_dir():
            for item in workspace.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(workspace)
                    dest = trial_dir / "artifacts" / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
        # keep native session / trace files
        for cand in ("events.jsonl", "trace.jsonl", "trajectory.json"):
            src = attempt_dir / cand
            if src.is_file():
                shutil.copy2(src, trial_dir / "agent" / cand)

    # ------------------------------------------------------------- result
    def _build_result(self, trial_dir: Path, task: Any, agent: Any,
                      spec_refs: dict[str, Any], run_id: str | None) -> TrialResult:
        # artifacts
        artifacts_root = trial_dir / "artifacts"
        manifest: list[ArtifactFile] = []
        for item in sorted(artifacts_root.rglob("*")):
            if item.is_file():
                rel = item.relative_to(artifacts_root).as_posix()
                manifest.append(ArtifactFile(
                    source=rel, destination=rel, type="file",
                    status="ok", host_path=str(item), size_bytes=item.stat().st_size,
                ))
        artifacts = Artifacts(root=str(artifacts_root), manifest=manifest)

        # runtrace: octagon unified schema (octagon-trajectory-v1) if produced,
        # else native files only.
        agent_dir = trial_dir / "agent"
        trajectory_path = agent_dir / "trajectory.json"
        trajectory: dict[str, Any] | None = None
        if trajectory_path.is_file():
            try:
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                trajectory = None
        native = [str(p) for p in sorted(agent_dir.iterdir()) if p.is_file()]
        runtrace = RunTrace(
            agent_logs_dir=str(agent_dir),
            trajectory=trajectory,
            trajectory_path=str(trajectory_path) if trajectory_path.is_file() else None,
            native_session_files=native,
        )

        # scoring: octagon dimension rows -> rewards
        rewards: dict[str, float | int] = {}
        reward: float | None = None
        if self._score_rows:
            total = sum(float(r.get("value") or 0) for r in self._score_rows)
            rewards = {"octagon_total": round(total, 4)}
            reward = round(total, 4)
        scoring = Scoring(rewards=rewards, reward=reward)

        return TrialResult(
            producer=Producer(backend="octagon", backend_version="octagon-trajectory-v1"),
            trial_id=trial_dir.name,
            run_id=run_id,
            specs=spec_refs,
            status=self._status if self._status in ("finished", "failed", "cancelled") else "finished",
            timings={},
            artifacts=artifacts,
            runtrace=runtrace,
            scoring=scoring,
        )

    def _reset_run_state(self) -> None:
        self._status = "finished"
        self._score_rows = []
