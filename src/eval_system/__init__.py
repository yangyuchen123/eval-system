"""eval_system: Harbor-backed agent evaluation — unify artifacts + runtraces
for downstream scoring.

Public API (v1, 兼容入口):
    HarborEvalLoader   # 离线读取 harbor jobs/trials 目录 -> EvalSample
    collect_sample_on_end  # 运行期 TrialEvent.END hook -> EvalSample
    EvalSample / EvalScenario / Artifacts / RunTrace / Scoring  # 数据契约

Public API (v2, CONTRACT.md):
    contract.TaskSpec / AgentSpec / EnvironmentSpec  # 输入契约
    contract.TrialResult                             # 核心资产（不可变）
    contract.ExecutionBackend / Trial                # 后端协议 + 运行句柄
    contract.HarborBackend / LocalBackend            # 两个后端实现
    collect_trial_on_end                             # v2 运行期 hook
"""

from eval_system.contract import (  # noqa: F401
    AgentSpec,
    ChecksumMismatchError,
    ContentRef,
    EnvironmentSpec,
    ExecutionBackend,
    ImageRef,
    Producer,
    Resources,
    SpecRef,
    TaskSpec,
    Trial,
    TrialResult,
    AsyncTrial,
    content_checksum,
    load_spec,
    model_checksum,
    read_trial_result,
    validate_checksum,
    write_specs,
    write_trial_result,
)
from eval_system.contract.harbor_backend import HarborBackend  # noqa: F401
from eval_system.contract.local_backend import LocalBackend  # noqa: F401
from eval_system.loader import HarborEvalLoader
from eval_system.schema import (
    AgentRunInfo,
    ArtifactFile,
    Artifacts,
    EvalSample,
    EvalScenario,
    RunTrace,
    Scoring,
)

# hooks 依赖 harbor 包（运行期接口）。loader/schema 保持零 harbor 依赖，
# 因此这里条件导出：harbor 可用时顶层可直接 import。
try:  # pragma: no cover - 取决于环境
    from eval_system.hooks import collect_sample_on_end as _collect_sample_on_end
    from eval_system.hooks import collect_trial_on_end as _collect_trial_on_end

    collect_sample_on_end = _collect_sample_on_end
    collect_trial_on_end = _collect_trial_on_end
except ImportError:
    collect_sample_on_end = None  # type: ignore[assignment]
    collect_trial_on_end = None  # type: ignore[assignment]

__all__ = [
    "HarborEvalLoader",
    "collect_sample_on_end",
    "collect_trial_on_end",
    # v2 contract
    "TaskSpec",
    "AgentSpec",
    "EnvironmentSpec",
    "ContentRef",
    "ImageRef",
    "Resources",
    "SpecRef",
    "Producer",
    "TrialResult",
    "Trial",
    "AsyncTrial",
    "ExecutionBackend",
    "HarborBackend",
    "LocalBackend",
    "ChecksumMismatchError",
    "content_checksum",
    "model_checksum",
    "validate_checksum",
    "write_specs",
    "write_trial_result",
    "read_trial_result",
    "load_spec",
    "benchmark_to_task_specs",
    "materialize_benchmark_tasks",
    "trial_results_to_agent_eval",
    # v1
    "EvalSample",
    "EvalSample",
    "EvalScenario",
    "AgentRunInfo",
    "Artifacts",
    "ArtifactFile",
    "RunTrace",
    "Scoring",
]

__version__ = "0.1.0"

from eval_system.integrations import (  # noqa: F401,E402
    benchmark_to_task_specs,
    materialize_benchmark_tasks,
    trial_results_to_agent_eval,
)
