"""HarborBackend: 现有 Harbor 评测流程的后端适配 — CONTRACT.md §6.

- read_trial：离线读取历史 trial；老目录（无 trial_result.json）自动补
  schema_version / producer / specs（惰性生成并落盘）。
- run：薄适配，生成 job.yaml 调 `harbor run`（subprocess），轮询等终态。

现有 HarborEvalLoader（v1）保留，降级为本后端内部实现（读侧复用）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval_system.contract.backend import AsyncTrial, ExecutionBackend, Trial
from eval_system.contract.extract import extract_specs
from eval_system.contract.specs import AgentSpec, EnvironmentSpec, TaskSpec
from eval_system.contract.trial import (
    Producer,
    TrialResult,
    read_trial_result,
    write_specs,
    write_trial_result,
)
from eval_system.loader import HarborEvalLoader


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _infer_status(result: dict) -> str:
    """finished | failed | cancelled（异常 → failed；否则 finished）。"""
    if result.get("exception_info"):
        return "failed"
    return "finished"


def _find_trial_dir(jobs_dir: Path, trial_id: str) -> Path | None:
    """按 trial 目录名（trial_id）在 jobs 树下递归定位。"""
    if not jobs_dir.is_dir():
        return None
    for candidate in jobs_dir.rglob(trial_id):
        if candidate.is_dir() and (candidate / "result.json").is_file():
            return candidate
    return None


def _iter_trial_dirs(root: Path):
    """递归遍历 trial 目录（result.json + trial.log 判定）。"""
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        if (
            p.is_dir()
            and (p / "result.json").is_file()
            and (p / "trial.log").is_file()
        ):
            yield p


def build_trial_result(
    trial_dir: Path,
    *,
    backend_version: str | None = None,
) -> TrialResult:
    """从 Harbor trial 目录构建 TrialResult（含 specs 落盘）。

    复用 v1 HarborEvalLoader 的 artifacts/runtrace/scoring 构建逻辑，
    然后提取三个 Spec 落盘到 specs/，生成 trial_result.json。
    """
    result = _load_json(trial_dir / "result.json")
    if result is None:
        raise FileNotFoundError(f"No result.json in {trial_dir}")
    config = _load_json(trial_dir / "config.json") or {}
    lock = _load_json(trial_dir / "lock.json")

    # v1 loader 复用：artifacts / runtrace / scoring
    sample = HarborEvalLoader(trial_dir).load_trial(trial_dir)
    if sample is None:
        raise FileNotFoundError(f"Cannot load trial {trial_dir}")

    # 提取三个 Spec 并落盘 specs/
    task_dir = Path(lock.get("task", {}).get("path")) if lock and lock.get("task", {}).get("path") else None
    specs = extract_specs(result, config, lock, task_dir=task_dir)
    spec_refs = write_specs(trial_dir, specs)

    trial_result = TrialResult(
        producer=Producer(
            backend="harbor",
            backend_version=backend_version,
            schema_version="trialresult.v1",
        ),
        trial_id=sample.sample_id,
        run_id=trial_dir.parent.name if trial_dir.parent.name else None,
        specs=spec_refs,
        status=_infer_status(result),
        timings=sample.scoring.timings,
        artifacts=sample.artifacts,
        runtrace=sample.runtrace,
        scoring=sample.scoring,
        is_regrade=sample.is_regrade,
        backend_raw=sample.raw_result,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    write_trial_result(trial_dir, trial_result)
    return trial_result


def _default_harbor_cmd() -> str:
    """自动发现 harbor CLI：优先当前 Python 环境的 console script，其次 PATH。"""
    try:
        import sysconfig
        from pathlib import Path as _P

        candidate = _P(sysconfig.get_path("scripts")) / "harbor"
        if candidate.is_file():
            return str(candidate)
    except Exception:
        pass
    return "harbor"


class HarborBackend(ExecutionBackend):
    name = "harbor"

    def __init__(
        self,
        jobs_dir: str | Path,
        *,
        harbor_cmd: str | None = None,
        keep_workdir: bool = False,
        agent_setup_timeout_multiplier: float | None = None,
    ):
        self.jobs_dir = Path(jobs_dir).expanduser()
        self._harbor_cmd = harbor_cmd or _default_harbor_cmd()
        self.keep_workdir = keep_workdir
        self.agent_setup_timeout_multiplier = agent_setup_timeout_multiplier
        self._loader = HarborEvalLoader(self.jobs_dir)

    # -- 读侧 --------------------------------------------------------------
    def list_trials(self) -> list[str]:
        return [
            p.name for p in self.jobs_dir.rglob("*")
            if p.is_dir() and (p / "result.json").is_file() and (p / "trial.log").is_file()
        ]

    async def read_trial(self, trial_id: str) -> TrialResult:
        trial_dir = _find_trial_dir(self.jobs_dir, trial_id)
        if trial_dir is None:
            raise FileNotFoundError(f"Trial {trial_id!r} not found under {self.jobs_dir}")

        if (trial_dir / "trial_result.json").is_file():
            return read_trial_result(trial_dir)  # 已有 v2 交付物：直接读（含 checksum 校验）
        return build_trial_result(trial_dir)  # 老目录：惰性生成

    def read_trial_sync(self, trial_id: str) -> TrialResult:
        """同步版本（CLI/脚本场景）。"""
        return asyncio.run(self.read_trial(trial_id))

    # -- 运行侧（薄适配 harbor run） ----------------------------------------
    async def run(
        self,
        task: TaskSpec,
        agent: AgentSpec,
        environment: EnvironmentSpec,
        *,
        run_id: str | None = None,
    ) -> Trial:
        workdir = Path(tempfile.mkdtemp(prefix="eval-system-"))
        job_config = workdir / "job.json"
        job_name = run_id or datetime.now(timezone.utc).strftime("%Y-%m-%d__%H-%M-%S")
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        self._write_job_config(job_config, task, agent, environment)

        async def _runner() -> TrialResult:
            cmd = [
                self._harbor_cmd, "run", "-c", str(job_config),
                "--jobs-dir", str(self.jobs_dir), "--job-name", job_name,
                "--quiet",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"harbor run failed (exit {proc.returncode}): "
                    f"{output.decode(errors='replace')[-4000:]}"
                )
            job_dir = self.jobs_dir / job_name
            trials = list(_iter_trial_dirs(job_dir))
            if not trials:
                raise RuntimeError(
                    f"harbor run finished but produced no trial dir under {job_dir}; "
                    f"output: {output.decode(errors='replace')[-1000:]}"
                )
            return await self.read_trial(trials[0].name)

        async def _cleanup() -> None:
            if not self.keep_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

        async def _run_and_cleanup() -> TrialResult:
            try:
                return await _runner()
            finally:
                await _cleanup()

        trial_id = f"{task.name.replace('/', '__')}__{agent.name}"
        return AsyncTrial(trial_id=trial_id, run_id=job_name, task=asyncio.ensure_future(_run_and_cleanup()))

    def _write_job_config(
        self,
        path: Path,
        task: TaskSpec,
        agent: AgentSpec,
        environment: EnvironmentSpec,
    ) -> None:
        """Write a Harbor ``JobConfig`` as JSON.

        JSON is intentional here: it avoids lossy hand-written YAML and is
        accepted by Harbor's public CLI. The adapter maps only serializable
        fields from the neutral specs; Harbor remains the execution authority.
        """
        content_ref = task.content_ref
        if content_ref is None:
            raise NotImplementedError("HarborBackend.run requires task.content_ref")
        if content_ref.type == "path":
            task_entry: dict[str, Any] = {"path": content_ref.path}
        elif content_ref.type == "registry":
            task_entry = {"name": content_ref.name, "ref": content_ref.ref}
        elif content_ref.type == "git":
            if not content_ref.url or not content_ref.path:
                raise ValueError("git tasks require content_ref.url and content_ref.path")
            task_entry = {
                "git_url": content_ref.url,
                "git_commit_id": content_ref.commit,
                "path": content_ref.path,
            }
        else:
            raise NotImplementedError(f"Unsupported task content_ref type: {content_ref.type}")

        agent_entry: dict[str, Any] = {"name": agent.name}
        if agent.model:
            agent_entry["model_name"] = agent.model
        if agent.config:
            for key in ("import_path", "kwargs", "env", "skills", "mcp_servers", "n_concurrent"):
                if key in agent.config:
                    agent_entry[key] = agent.config[key]

        env_entry: dict[str, Any] = {"type": environment.type, "delete": True}
        resources = environment.resources
        for source, target in (("cpus", "override_cpus"), ("memory_mb", "override_memory_mb"),
                               ("storage_mb", "override_storage_mb"), ("gpus", "override_gpus")):
            value = getattr(resources, source, None)
            if value is not None:
                env_entry[target] = value
        config = {
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "quiet": True,
            "environment": env_entry,
            "agents": [agent_entry],
            "tasks": [task_entry],
        }
        if self.agent_setup_timeout_multiplier is not None:
            # Harbor JobConfig top-level field (CLI --agent-setup-timeout-multiplier).
            # Slow local installs (nvm+pi) blew the 360s default; 指挥层指令20.
            config["agent_setup_timeout_multiplier"] = self.agent_setup_timeout_multiplier
        path.write_text(json.dumps(config, indent=2, default=str), encoding="utf-8")


def harbor_backend_from_env() -> HarborBackend:
    """从默认 jobs 目录构造后端（CLI 场景）。"""
    return HarborBackend(Path("jobs"))
