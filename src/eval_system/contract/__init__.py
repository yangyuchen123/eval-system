"""contract: eval-system v2 通用 Execution Runtime（CONTRACT.md）。

输入契约（可复现）：TaskSpec / AgentSpec / EnvironmentSpec
运行句柄（有状态）：Trial
核心资产（不可变）：TrialResult（specs 引用 + checksum）
后端协议（最小面）：ExecutionBackend

验收标准 §10：新增本地后端不触碰 TrialResult 字段 —— 见 local_backend.py。
"""

from eval_system.contract.backend import AsyncTrial, ExecutionBackend, Trial
from eval_system.contract.specs import (
    AgentSpec,
    ChecksumMismatchError,
    ContentRef,
    EnvironmentSpec,
    ImageRef,
    Resources,
    SpecRef,
    TaskSpec,
    content_checksum,
    model_checksum,
    validate_checksum,
)
from eval_system.contract.trial import (
    Producer,
    TrialResult,
    load_spec,
    read_trial_result,
    write_specs,
    write_trial_result,
)

__all__ = [
    # 输入契约
    "TaskSpec",
    "AgentSpec",
    "EnvironmentSpec",
    "ContentRef",
    "ImageRef",
    "Resources",
    # 运行句柄 / 后端协议
    "Trial",
    "AsyncTrial",
    "ExecutionBackend",
    # 核心资产
    "TrialResult",
    "Producer",
    "SpecRef",
    # 校验
    "ChecksumMismatchError",
    "content_checksum",
    "model_checksum",
    "validate_checksum",
    # 落盘/读取
    "write_specs",
    "write_trial_result",
    "read_trial_result",
    "load_spec",
]
