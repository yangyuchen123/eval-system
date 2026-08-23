"""TrialResult: 核心资产（不可变、自包含、版本化）— CONTRACT.md §5.

TrialResult 是下游唯一稳定交付物：只带 specs 引用 + checksum，不内嵌 Spec
全文；artifacts/runtrace/scoring 与 v1 EvalSample 共享模型（原样平移）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from eval_system.contract.specs import (
    SPEC_TYPE_TO_MODEL,
    SpecRef,
    model_checksum,
    validate_checksum,
)
from eval_system.schema import Artifacts, RunTrace, Scoring

TRIAL_RESULT_FILENAME = "trial_result.json"
SPECS_DIRNAME = "specs"


class Producer(BaseModel):
    """回答「谁产的」— 后端可换，下游凭此追溯。"""

    backend: str = Field(description="后端标识，如 harbor / local")
    backend_version: str | None = Field(
        default=None, description="后端实现版本（变更不要求下游改动）"
    )
    schema_version: str | None = Field(
        default=None, description="后端输出 schema 版本（供后端自述）"
    )

    model_config = {"extra": "forbid"}


class TrialResult(BaseModel):
    schema_version: Literal["trialresult.v1"] = "trialresult.v1"
    producer: Producer
    trial_id: str = Field(description="一次运行的唯一 ID（如 write-report__9BTHL4H）")
    run_id: str | None = Field(default=None, description="一批运行的 ID（jobs 目录名）")
    specs: dict[str, SpecRef] = Field(
        default_factory=dict,
        description="三个 Spec 的引用 + checksum（不内嵌全文）",
    )
    status: Literal["finished", "failed", "cancelled"]
    timings: dict[str, Any] = Field(
        default_factory=dict, description="environment/agent/verifier 各阶段计时"
    )
    artifacts: Artifacts
    runtrace: RunTrace
    scoring: Scoring
    is_regrade: bool = False
    backend_raw: dict[str, Any] | None = Field(
        default=None, description="后端原始结果（可选，保真溯源）"
    )
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# 落盘 / 读取
# ---------------------------------------------------------------------------
def write_trial_result(trial_dir: str | Path, result: TrialResult) -> Path:
    """把 TrialResult 写到 `<trial>/trial_result.json`。"""
    trial_dir = Path(trial_dir)
    path = trial_dir / TRIAL_RESULT_FILENAME
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def write_specs(trial_dir: str | Path, specs: dict[str, BaseModel]) -> dict[str, SpecRef]:
    """把三个 Spec 序列化落盘到 `<trial>/specs/`，返回引用条目。

    每个 Spec 一个文件（task.json / agent.json / environment.json），
    ref 为相对 trial 根的路径，checksum 为内容 hash。
    """
    trial_dir = Path(trial_dir)
    specs_dir = trial_dir / SPECS_DIRNAME
    specs_dir.mkdir(parents=True, exist_ok=True)

    refs: dict[str, SpecRef] = {}
    for key, spec in specs.items():
        if key not in SPEC_TYPE_TO_MODEL:
            raise ValueError(f"Unknown spec type: {key}")
        filename = f"{key}.json"
        (specs_dir / filename).write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        refs[key] = SpecRef(
            schema=spec.schema_version,
            checksum=model_checksum(spec),
            ref=f"{SPECS_DIRNAME}/{filename}",
        )
    return refs


def _resolve_spec_path(trial_dir: Path, ref: str) -> Path | None:
    if ref.startswith("registry://"):
        return None  # registry 定位需 SpecRegistry，本版返回 None（调用方处理）
    if ref.startswith("file://"):
        path = Path(ref.removeprefix("file://"))
        return path if path.is_file() else None
    candidate = trial_dir / ref
    return candidate if candidate.is_file() else None


def read_trial_result(
    trial_dir: str | Path,
    *,
    verify_checksums: bool = True,
) -> TrialResult:
    """读取 `<trial>/trial_result.json`。

    verify_checksums=True 时，对 specs 引用的每个 Spec 重新计算内容 hash 并比对；
    不符抛 ChecksumMismatchError（不得静默继续）。
    """
    trial_dir = Path(trial_dir)
    path = trial_dir / TRIAL_RESULT_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"No {TRIAL_RESULT_FILENAME} in {trial_dir}")

    result = TrialResult.model_validate_json(path.read_text(encoding="utf-8"))

    if verify_checksums:
        for key, ref in result.specs.items():
            spec_path = _resolve_spec_path(trial_dir, ref.ref)
            if spec_path is None:
                # registry:// 定位或文件缺失：报警但不中断（由调用方决定策略）
                raise ChecksumMismatchError(
                    f"spec '{key}' ref {ref.ref!r} cannot be resolved for checksum "
                    f"verification (expected {ref.checksum})"
                )
            spec_model = SPEC_TYPE_TO_MODEL[key]
            spec = spec_model.model_validate_json(spec_path.read_text(encoding="utf-8"))
            validate_checksum(ref.checksum, model_checksum(spec), f"spec '{key}'")

    return result


def load_spec(trial_dir: str | Path, key: str) -> BaseModel | None:
    """按 TrialResult.specs 的引用读取某个 Spec 全文（已校验）。"""
    result = read_trial_result(trial_dir)
    ref = result.specs.get(key)
    if ref is None:
        return None
    spec_path = _resolve_spec_path(Path(trial_dir), ref.ref)
    if spec_path is None:
        return None
    return SPEC_TYPE_TO_MODEL[key].model_validate_json(spec_path.read_text(encoding="utf-8"))


# re-export for callers
from eval_system.contract.specs import ChecksumMismatchError  # noqa: E402

__all__ = [
    "TRIAL_RESULT_FILENAME",
    "SPECS_DIRNAME",
    "Producer",
    "TrialResult",
    "write_trial_result",
    "write_specs",
    "read_trial_result",
    "load_spec",
    "ChecksumMismatchError",
]
