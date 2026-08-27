"""LocalBackend: 无 Docker 的本地执行后端（验收标准 §10.1 的第二个后端）。

演示「不触碰 TrialResult 任何字段就能接入新后端」：
- 在本地建 trial 目录（artifacts/agent/specs/）
- 「执行」= 把 instruction 与 agent 信息落为产物；若 TaskSpec.metadata 声明
  `local_command`，则执行并把 stdout 写入产物，reward 由退出码决定（0 → 1.0）
- 产出与 HarborBackend 完全同构的 TrialResult（可直接进同一份 history.jsonl）
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from eval_system.contract.backend import AsyncTrial, ExecutionBackend, Trial
from eval_system.contract.specs import AgentSpec, EnvironmentSpec, TaskSpec, model_checksum
from eval_system.contract.trial import (
    Producer,
    TrialResult,
    write_specs,
    write_trial_result,
)
from eval_system.schema import ArtifactFile, Artifacts, RunTrace, Scoring

ARTIFACT_INSTRUCTION = "instruction.txt"
ARTIFACT_AGENT = "agent.txt"
ARTIFACT_OUTPUT = "output.txt"


class LocalBackend(ExecutionBackend):
    name = "local"

    def __init__(self, trials_root: str | Path):
        self.trials_root = Path(trials_root)
        self.trials_root.mkdir(parents=True, exist_ok=True)

    def list_trials(self) -> list[str]:
        return sorted(
            p.name for p in self.trials_root.iterdir()
            if p.is_dir() and (p / "trial_result.json").is_file()
        )

    async def run(
        self,
        task: TaskSpec,
        agent: AgentSpec,
        environment: EnvironmentSpec,
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
            await self._execute_async(trial_dir, task, agent)
            result = self._build_result(trial_dir, task, agent, spec_refs, run_id)
            write_trial_result(trial_dir, result)
            return result

        return AsyncTrial(trial_id=trial_id, run_id=run_id, task=asyncio.ensure_future(_runner()))

    async def read_trial(self, trial_id: str) -> TrialResult:
        from eval_system.contract.trial import read_trial_result

        trial_dir = self.trials_root / trial_id
        return read_trial_result(trial_dir)

    # -- 本地执行 -----------------------------------------------------------
    async def _execute_async(self, trial_dir: Path, task: TaskSpec, agent: AgentSpec) -> None:
        """Execute the optional local command without blocking the event loop."""
        artifacts = trial_dir / "artifacts"
        (artifacts / ARTIFACT_INSTRUCTION).write_text(task.instruction, encoding="utf-8")
        (artifacts / ARTIFACT_AGENT).write_text(
            f"{agent.name} {agent.version or ''} {agent.model or ''}".strip(),
            encoding="utf-8",
        )
        command = (task.metadata or {}).get("local_command")
        if not command:
            return
        # LocalBackend is a deliberately small fallback backend. Use the
        # synchronous subprocess API here: this environment's sandbox can
        # leave asyncio subprocess transports/waiters unresolved, even for
        # ``echo``. HarborBackend remains the async production backend.
        try:
            completed = subprocess.run(
                str(command),
                shell=True,
                executable="/bin/bash",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("local command exceeded 120 seconds") from exc
        (artifacts / ARTIFACT_OUTPUT).write_text(
            completed.stdout.decode("utf-8", errors="replace"), encoding="utf-8"
        )
        (trial_dir / "exit_code.txt").write_text(str(completed.returncode), encoding="utf-8")

    def _build_result(
        self,
        trial_dir: Path,
        task: TaskSpec,
        agent: AgentSpec,
        spec_refs: dict[str, Any],
        run_id: str | None,
    ) -> TrialResult:
        artifacts_dir = trial_dir / "artifacts"
        entries = [
            ArtifactFile(
                source=f"/workspace/{p.name}",
                destination=f"artifacts/{p.name}",
                type="file",
                status="ok",
                host_path=str(p),
                size_bytes=p.stat().st_size,
            )
            for p in sorted(artifacts_dir.iterdir())
            if p.is_file()
        ]
        exit_code = self._exit_code(trial_dir)
        rewards = {"reward": 1.0} if exit_code == 0 else {"reward": 0.0}

        return TrialResult(
            producer=Producer(backend="local", backend_version="0.1.0", schema_version="trialresult.v1"),
            trial_id=trial_dir.name,
            run_id=run_id,
            specs=spec_refs,
            status="finished",
            timings={"agent_execution": {"started_at": None, "finished_at": None}},
            artifacts=Artifacts(root=str(artifacts_dir), manifest=entries),
            runtrace=RunTrace(
                agent_logs_dir=str(trial_dir / "agent"),
                trajectory={
                    "schema_version": "ATIF-v1.7",
                    "agent": {"name": agent.name, "model_name": agent.model},
                    "steps": [
                        {
                            "step_id": 1,
                            "source": "user",
                            "message": task.instruction,
                        },
                        {
                            "step_id": 2,
                            "source": "agent",
                            "message": f"{agent.name} completed locally",
                        },
                    ],
                },
                native_session_files=[],
                agent_context={
                    "n_input_tokens": None,
                    "n_output_tokens": None,
                    "cost_usd": None,
                },
            ),
            scoring=Scoring(rewards=rewards, reward=rewards["reward"]),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _exit_code(trial_dir: Path) -> int:
        try:
            return int((trial_dir / "exit_code.txt").read_text().strip() or "1")
        except (OSError, ValueError):
            return 1
