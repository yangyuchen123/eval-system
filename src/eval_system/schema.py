"""Unified data contract for agent evaluation on top of Harbor.

Maps Harbor's on-disk trial output (产物 + runtrace) to one object the
scoring layer can consume without knowing Harbor internals:

    EvalSample
    ├── scenario   # 评测场景 (task / instruction / config)
    ├── agent      # 哪个 agent / model 跑的
    ├── artifacts  # 产物: 收集自 agent 环境的文件 + manifest
    ├── runtrace   # runtrace: ATIF 轨迹 + agent 原生日志 + token/cost 上下文
    └── scoring    # 打分输入: verifier rewards / stdout / 异常 / 计时
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 场景 (scenario)
# ---------------------------------------------------------------------------
class EvalScenario(BaseModel):
    """评测场景：一次评测要完成的 task 及其上下文。"""

    scenario_id: str = Field(description="任务标识，如 harbor/hello-world@1.0.0")
    task_name: str | None = None
    task_id: str | None = Field(
        default=None, description="Harbor 任务 ID (Local/Git/Package) 的字符串形式"
    )
    source: str | None = Field(
        default=None, description="数据集来源 (dataset source)"
    )
    instruction: str | None = Field(
        default=None, description="评测指令原文 (instruction.md，尽力解析)"
    )
    task_checksum: str | None = None
    config: dict[str, Any] = Field(
        default_factory=dict, description="原始 TrialConfig (JSON)"
    )


class AgentRunInfo(BaseModel):
    """谁跑了这次评测。"""

    name: str
    version: str | None = None
    model: str | None = None
    provider: str | None = None
    trial_name: str
    trial_uri: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


# ---------------------------------------------------------------------------
# 产物 (artifacts)
# ---------------------------------------------------------------------------
class ArtifactFile(BaseModel):
    """manifest.json 中的一条产物记录 + 可读的宿主路径。"""

    source: str = Field(description="容器内原始路径")
    destination: str = Field(description="host 相对路径 (artifacts/ 之下)")
    type: str = Field(description="file | directory")
    status: str = Field(description="ok | failed | empty | skipped")
    service: str | None = Field(default=None, description="来源 compose 服务")
    host_path: str = Field(description="宿主机绝对路径，可直接打开")
    size_bytes: int | None = None


class Artifacts(BaseModel):
    """一次 trial 收集到的全部产物。"""

    root: str = Field(description="artifacts 目录绝对路径")
    manifest: list[ArtifactFile] = Field(default_factory=list)

    def files(self, status: str | None = "ok") -> list[ArtifactFile]:
        """成功收集的产物文件；status=None 时返回全部。"""
        if status is None:
            return self.manifest
        return [a for a in self.manifest if a.status == status]

    def expand_directories(self) -> "Artifacts":
        """把 manifest 中收集成功的目录条目展开为其内部文件列表（就地）。

        Harbor 的 manifest 以目录为收集粒度（如约定目录 /logs/artifacts），
        打分层通常需要文件级粒度，因此扫描目录内容补充 file 条目。
        """
        if getattr(self, "_expanded", False):
            return self
        expanded: list[ArtifactFile] = []
        for entry in self.manifest:
            expanded.append(entry)
            if entry.type != "directory" or entry.status != "ok":
                continue
            root = Path(entry.host_path)
            if not root.is_dir():
                continue
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                rel = f.relative_to(root).as_posix()
                expanded.append(
                    ArtifactFile(
                        source=f"{entry.source.rstrip('/')}/{rel}",
                        destination=f"{entry.destination.rstrip('/')}/{rel}",
                        type="file",
                        status="ok",
                        service=entry.service,
                        host_path=str(f),
                        size_bytes=_safe_size(f),
                    )
                )
        self.manifest = expanded
        self._expanded = True
        return self

    def find(self, rel_path: str) -> ArtifactFile | None:
        """按路径查找产物，兼容两种写法：

        * 相对 artifacts 根:   ``logs/artifacts/output.txt``
        * 相对 trial 目录:     ``artifacts/logs/artifacts/output.txt``
        """
        rel = rel_path.lstrip("/")
        rel = rel.removeprefix("artifacts/")
        for a in self.manifest:
            if a.destination.lstrip("/").removeprefix("artifacts/") == rel:
                return a
        return None

    def read_text(self, rel_path: str, **kwargs: Any) -> str | None:
        """直接读取某产物内容（目录或失败项返回 None）。"""
        entry = self.find(rel_path)
        if entry is None or entry.status != "ok" or entry.type != "file":
            return None
        path = Path(entry.host_path)
        if not path.is_file():
            return None
        return path.read_text(**kwargs)


# ---------------------------------------------------------------------------
# runtrace
# ---------------------------------------------------------------------------
class RunTrace(BaseModel):
    """一次 agent 运行的轨迹：ATIF 标准轨迹 + 原生日志 + 结构化上下文。"""

    agent_logs_dir: str = Field(description="agent 日志目录绝对路径")
    trajectory: dict[str, Any] | None = Field(
        default=None,
        description="ATIF 轨迹 (agent/trajectory.json)：steps / tool_calls / "
        "observation / final_metrics，跨 agent 统一格式",
    )
    trajectory_path: str | None = None
    native_session_files: list[str] = Field(
        default_factory=list,
        description="agent 原生会话文件 (jsonl 等，保真但 agent 相关)",
    )
    agent_context: dict[str, Any] | None = Field(
        default=None,
        description="结构化执行上下文：n_input/cache/output_tokens、cost_usd、"
        "rollout_details、metadata (来自 result.agent_result)",
    )
    bridge_trajectory: dict[str, Any] | None = Field(
        default=None,
        description="模拟用户桥接轨迹 (bridge-trajectory.json，如有)",
    )

    def steps(self) -> list[dict[str, Any]]:
        """ATIF 轨迹的步骤列表；无轨迹时返回空。"""
        if not self.trajectory:
            return []
        steps = self.trajectory.get("steps") or []
        return list(steps)

    def total_cost_usd(self) -> float | None:
        """优先取 ATIF final_metrics，其次取 agent_context。"""
        if self.trajectory and self.trajectory.get("final_metrics"):
            cost = self.trajectory["final_metrics"].get("total_cost_usd")
            if cost is not None:
                return float(cost)
        if self.agent_context:
            cost = self.agent_context.get("cost_usd")
            if cost is not None:
                return float(cost)
        return None


# ---------------------------------------------------------------------------
# 打分输入 (scoring)
# ---------------------------------------------------------------------------
class Scoring(BaseModel):
    """供后续打分层使用的全部现场信息。"""

    rewards: dict[str, float | int] | None = Field(
        default=None, description="verifier 给出的 reward 映射"
    )
    reward: float | None = Field(
        default=None, description="主 reward (rewards['reward'] 的快捷方式)"
    )
    test_stdout: str | None = Field(default=None, description="verifier stdout")
    test_stderr: str | None = Field(default=None, description="verifier stderr")
    verifier_result: dict[str, Any] | None = Field(
        default=None, description="原始 VerifierResult"
    )
    exception: dict[str, Any] | None = Field(
        default=None, description="异常信息 (type/message/traceback)"
    )
    timings: dict[str, Any] = Field(
        default_factory=dict, description="environment/agent/verifier 各阶段计时"
    )
    step_results: list[dict[str, Any]] | None = Field(
        default=None, description="多步任务的分步结果"
    )

    @property
    def passed(self) -> bool | None:
        """主 reward == 1.0 视为通过；无 reward 时返回 None。"""
        if self.reward is None:
            return None
        return float(self.reward) == 1.0


# ---------------------------------------------------------------------------
# 统一视图
# ---------------------------------------------------------------------------
class EvalSample(BaseModel):
    """一次 trial 的完整统一视图：场景 + agent + 产物 + runtrace + 打分输入。"""

    sample_id: str = Field(description="trial name，样例唯一标识")
    trial_dir: str = Field(description="trial 目录绝对路径")
    scenario: EvalScenario
    agent: AgentRunInfo
    artifacts: Artifacts
    runtrace: RunTrace
    scoring: Scoring
    raw_result: dict[str, Any] = Field(
        default_factory=dict, description="原始 TrialResult JSON，完整保真"
    )
    is_regrade: bool = False
