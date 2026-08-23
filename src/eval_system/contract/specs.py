"""Specs: 输入契约（可复现）— CONTRACT.md §1-3, §5.2.

三种 Spec（TaskSpec / AgentSpec / EnvironmentSpec）描述一次评测的输入：
「做什么 / 用谁跑 / 在哪里跑」。任何后端拿到同一组 Spec（同 checksum）
应产出可比的 Trial。TrialResult 只带 Spec 的引用 + checksum，不内嵌全文。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SPEC_SCHEMAS = {
    "task": "taskspec.v1",
    "agent": "agentspec.v1",
    "environment": "envspec.v1",
}


# ---------------------------------------------------------------------------
# checksum 工具（canonical JSON，跨版本稳定）
# ---------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化：sorted keys、紧凑分隔符。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_checksum(data: str | bytes) -> str:
    """内容 hash，格式 `sha256:<hex>`。"""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def model_checksum(model: BaseModel) -> str:
    """Spec 全字段的内容 hash（规范化序列化后）。"""
    return content_checksum(canonical_json(model.model_dump(mode="json", exclude_none=True)))


def validate_checksum(expected: str, actual: str, label: str) -> None:
    """校验 checksum，不符即抛错（防篡改 / 防 Spec 漂移）。"""
    if expected != actual:
        raise ChecksumMismatchError(
            f"{label} checksum mismatch: expected {expected}, got {actual}"
        )


class ChecksumMismatchError(ValueError):
    """specs 引用与内容不符（读取方必须报警而非静默继续）。"""


# ---------------------------------------------------------------------------
# TaskSpec — 一次评测要做什么（CONTRACT.md §1）
# ---------------------------------------------------------------------------
class ContentRef(BaseModel):
    """任务内容的可寻址引用，后端据此获取任务。"""

    type: Literal["git", "path", "registry"]
    url: str | None = None      # git: 仓库 URL
    path: str | None = None     # git: 仓库内路径 / path: 本地目录
    commit: str | None = None   # git: commit id
    name: str | None = None     # registry: org/name
    ref: str | None = None      # registry: tag/version

    model_config = {"extra": "forbid"}


class TaskSpec(BaseModel):
    schema_version: Literal["taskspec.v1"] = "taskspec.v1"
    task_id: str = Field(description="稳定唯一标识，如 eval/write-report@1.0.0")
    name: str = Field(description="短名")
    version: str = Field(description="语义化版本")
    instruction: str = Field(description="喂给 agent 的指令原文")
    instruction_checksum: str = Field(description="instruction 内容 hash（单独，检测指令变化）")
    source: str | None = Field(default=None, description="数据集来源 (provenance)")
    content_ref: ContentRef | None = Field(
        default=None, description="任务内容的可寻址引用，后端据此获取任务"
    )
    task_checksum: str = Field(description="任务目录整体内容 hash（含环境/测试定义）")
    metadata: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = Field(
        default=None, description="期望产物结构（提示 verifier/消费方）"
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# AgentSpec — 谁来跑（CONTRACT.md §2）
# ---------------------------------------------------------------------------
class AgentSpec(BaseModel):
    schema_version: Literal["agentspec.v1"] = "agentspec.v1"
    name: str = Field(description="agent 标识，如 claude-code / oracle / pi")
    version: str | None = None
    model: str | None = None
    provider: str | None = None
    config: dict[str, Any] = Field(
        default_factory=dict, description="agent 专属可序列化配置（kwargs/skills/mcp 等）"
    )
    config_hash: str = Field(
        default="",
        description="上述全部字段的内容 hash — agent 实现变了，TrialResult 能检测出来；"
        "构造后自动计算，无需手动指定",
    )
    supports_atif: bool = Field(
        default=False, description="是否产出标准轨迹（runtrace 能力预判）"
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _fill_config_hash(self) -> "AgentSpec":
        """未显式指定 config_hash 时，按其余全部字段自动计算。"""
        if not self.config_hash:
            payload = self.model_dump(mode="json", exclude_none=True, exclude={"config_hash"})
            self.config_hash = content_checksum(canonical_json(payload))
        return self


# ---------------------------------------------------------------------------
# EnvironmentSpec — 在哪里跑（CONTRACT.md §3）
# ---------------------------------------------------------------------------
class ImageRef(BaseModel):
    type: Literal["dockerfile", "compose", "image"]
    path: str | None = None     # dockerfile/compose: 环境定义目录
    content: str | None = None  # dockerfile/compose: 定义内容（可选内嵌）
    tag: str | None = None      # image: 镜像引用

    model_config = {"extra": "forbid"}


class Resources(BaseModel):
    cpus: int = 1
    memory_mb: int = 2048
    storage_mb: int = 10240
    gpus: int = 0
    timeout_sec: float | None = None

    model_config = {"extra": "forbid"}


class EnvironmentSpec(BaseModel):
    schema_version: Literal["envspec.v1"] = "envspec.v1"
    type: str = Field(description="docker | local | remote …（后端可支持子集）")
    image_ref: ImageRef | None = None
    resources: Resources = Field(default_factory=Resources)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    environment_hash: str | None = Field(
        default=None, description="环境定义内容 hash（镜像复用性的依据）"
    )

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# SpecRef — TrialResult.specs 里的引用条目（CONTRACT.md §5.2）
# ---------------------------------------------------------------------------
class SpecRef(BaseModel):
    schema_version: str = Field(
        alias="schema",
        description="Spec 的 schema_version，如 taskspec.v1",
    )
    checksum: str = Field(description="sha256:<hex>，读取方必须重新计算比对")
    ref: str = Field(
        description="定位符：相对路径（默认，相对 trial 根）/ file:// 绝对 / registry://<id>"
    )

    model_config = {"extra": "forbid", "populate_by_name": True}


SPEC_TYPE_TO_MODEL = {
    "task": TaskSpec,
    "agent": AgentSpec,
    "environment": EnvironmentSpec,
}
