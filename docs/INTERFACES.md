# eval-system 接口文档

基于 Harbor 的 agent 评测系统底座。本文档定义系统的**对外接口**（数据契约、Python API、CLI、Harbor 对接点），供打分层/其他系统集成使用。

---

## 1. 系统边界

```
┌─────────────────────────── 你的评测系统（本包） ───────────────────────────┐
│                                                                          │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────────────────────┐   │
│  │ 场景定义      │   │ Harbor 运行    │   │ 统一数据契约 EvalSample       │   │
│  │ task.toml   │ → │ harbor run   │ → │ ┌──────────┐ ┌──────────┐  │   │
│  │ instruction │   │ (agent×task) │   │ │ artifacts│ │ runtrace │  │   │
│  │ environment │   └──────────────┘   │ └──────────┘ └──────────┘  │   │
│  └─────────────┘                      └───────────┬─────────────────┘   │
│                                                   ▼                     │
│                                      ┌──────────────────────────────┐   │
│                                      │ 打分层（你的评分系统）           │   │
│                                      │ 只依赖 EvalSample，不碰 Harbor │   │
│                                      └──────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

**分层原则**：Harbor 负责「跑」与「产出」，eval-system 负责「统一」与「交付」。
打分层**只消费 `EvalSample`**，不感知 Harbor 内部实现。

---

## 2. 核心数据契约：`EvalSample`

一次 trial（一个 agent 在一个任务上的一次运行）的完整统一视图。
所有接口（loader / hooks / CLI）最终都产出此对象。

```python
EvalSample
├── sample_id: str              # 唯一标识 = trial 目录名（如 "write-report__9BTHL4H"）
├── trial_dir: str              # trial 目录绝对路径
├── is_regrade: bool            # 是否为 regrade（只重打分不重跑 agent）
│
├── scenario: EvalScenario      # ① 评测场景
│   ├── scenario_id             # "task@version"
│   ├── task_name / task_id     # 任务标识
│   ├── source                  # 数据集来源
│   ├── instruction             # 评测指令原文（尽力解析 instruction.md）
│   ├── task_checksum
│   └── config                  # 原始 TrialConfig (dict)
│
├── agent: AgentRunInfo         # ② 谁跑的
│   ├── name / version          # agent 名与版本
│   ├── model / provider        # 模型与提供商
│   ├── trial_name / trial_uri
│   └── started_at / finished_at
│
├── artifacts: Artifacts        # ③ 产物
│   ├── root                    # artifacts 目录绝对路径
│   └── manifest: ArtifactFile[]  # 文件级清单（目录自动展开）
│
├── runtrace: RunTrace          # ④ runtrace
│   ├── agent_logs_dir          # agent 日志目录
│   ├── trajectory              # ATIF 标准轨迹 (dict | None)
│   ├── trajectory_path
│   ├── native_session_files[]  # agent 原生日志文件
│   ├── agent_context           # token/成本 (dict | None)
│   └── bridge_trajectory       # 模拟用户桥接轨迹 (dict | None)
│
├── scoring: Scoring            # ⑤ 打分输入（原生 verifier 结果）
│   ├── rewards                 # {"reward": 1.0, ...}
│   ├── reward                  # 主 reward 快捷方式
│   ├── passed                  # property: reward == 1.0
│   ├── test_stdout / test_stderr
│   ├── verifier_result / exception / timings / step_results
│
└── raw_result: dict            # 原始 result.json（完整保真，可溯源）
```

### ArtifactFile（清单条目）

```python
ArtifactFile
├── source: str        # 容器内原始路径，如 "/logs/artifacts/report.md"
├── destination: str   # host 相对路径，如 "artifacts/logs/artifacts/report.md"
├── type: str          # "file" | "directory"
├── status: str        # "ok" | "failed" | "empty" | "skipped"
├── service: str|None  # 来源 compose 服务（None=main）
├── host_path: str     # 宿主绝对路径（可直接打开）
└── size_bytes: int|None
```

> 注：Harbor 的 `manifest.json` 以**目录**为收集粒度；`Artifacts.expand_directories()`
> 在加载时把目录展开为文件级条目（`read_text`/`files()` 才能按文件工作）。

---

## 3. 读取接口：`HarborEvalLoader`（离线）

把 Harbor 落盘结果（jobs/trials 目录）读取为 `EvalSample`。**不依赖 Harbor 包**（纯读磁盘契约）。

```python
from eval_system import HarborEvalLoader

# 三种输入均可：
#   - jobs 根目录（含多个 job）
#   - 单个 job 目录
#   - 单个 trial 目录
loader = HarborEvalLoader("jobs/")

# 发现 trial 目录（递归）
trial_dirs: list[Path] = loader.discover_trial_dirs(recursive=True)

# 迭代所有 EvalSample
for sample in loader.iter_samples():
    ...

# 加载单个 trial（无 result.json 时返回 None）
sample: EvalSample | None = loader.load_trial("jobs/<job>/<trial>/")
```

判定规则：目录含 `result.json` **且** `trial.log` 才视为 trial（job 目录也有 result.json，靠 trial.log 区分）。

### Artifacts 便捷方法

```python
sample.artifacts.files(status="ok")          # 成功收集的产物文件列表
sample.artifacts.find("logs/artifacts/report.md")  # 按路径查（兼容两种前缀写法）
sample.artifacts.read_text("logs/artifacts/report.md")  # 直接读内容（文件缺失→None）
```

### RunTrace 便捷方法

```python
sample.runtrace.steps()                      # ATIF 轨迹步骤列表（无轨迹→[]）
sample.runtrace.total_cost_usd()             # 优先 ATIF metrics，其次 agent_context
```

---

## 4. 运行期接口：Hooks（实时）

在 Harbor Job 的 trial 生命周期上挂回调，trial 结束即得 `EvalSample`，可实时推送打分层。

```python
import asyncio
from harbor.job import Job
from harbor.trial.hooks import TrialEvent
from eval_system import collect_sample_on_end

async def my_scorer(sample):
    # 你的打分层：产物 + runtrace 都已就绪
    print(sample.agent.name, sample.artifacts.files(), sample.runtrace.trajectory)

async def main():
    job = await Job.create(config)
    job.add_hook(TrialEvent.END, collect_sample_on_end(scorer=my_scorer))
    await job.run()
```

```python
def collect_sample_on_end(
    scorer: Callable[[EvalSample], Awaitable[Any]] | None = None,
    *,
    trials_root: str | Path | None = None,   # 非本地 trial 时指定根目录
) -> Callable[[TrialHookEvent], Awaitable[EvalSample | None]]
```

回调收到的事件类型（`harbor.trial.hooks.TrialEvent`，也可自行挂）：

| 事件 | 时机 | result 完整度 |
|---|---|---|
| `START` | trial 开始 | 初始化完成 |
| `ENVIRONMENT_START` | 环境启动 | 部分 |
| `AGENT_START` / `AGENT_END` | agent 运行前后 | 部分 |
| `VERIFICATION_START` | 打分开始 | agent 部分已就绪 |
| `END` | trial 结束 | **完整**（产物+runtrace+打分） |
| `CANCEL` | 取消 | 部分（含异常） |

---

## 5. CLI 接口

```bash
# 汇总视图：一行一个 sample（agent / model / task / reward / steps / artifacts）
python -m eval_system <path>

# 导出 JSONL（- 表示 stdout）
python -m eval_system <path> --dump samples.jsonl
python -m eval_system <path> --dump -

# 参数
#   <path>    jobs 根 / job 目录 / 单 trial 目录
#   --dump   输出 JSONL 文件
#   --summary 打印汇总（默认行为）
```

---

## 6. Harbor 对接接口（底座契约）

打分层通常不需要，但「定义评测场景 / 接入新 agent」时需要。

### 6.1 评测场景定义（task 目录）

```
task_dir/
├── task.toml          # 场景配置（schema_version、task、verifier、agent、environment）
├── instruction.md     # 评测指令（喂给 agent）
├── environment/       # 容器定义（Dockerfile 或 docker-compose.yaml 或 docker_image）
├── tests/             # 打分脚本（test.sh 写 reward 到 /logs/verifier/reward.txt）
└── solution/          # 参考实现（oracle agent 用，可选）
```

### 6.2 产物收集声明

产物是「不同 agent 的产物」的核心交付。三来源：

```toml
# task.toml（或 job.yaml 的 artifacts 字段）
artifacts = [
    "/app/hello.txt",                                  # 简单形式：容器路径
    { source = "/var/log/api/requests.log", service = "api" },  # sidecar 证据
    { source = "/data/results", destination = "results" },       # 指定宿主落点
]
```

**约定目录（零配置）**：agent 写入 `/logs/artifacts/` 的文件自动收集，落在
`<trial>/artifacts/logs/artifacts/`。

### 6.3 runtrace 契约

- **标准轨迹（推荐）**：agent 把 ATIF 轨迹写到 `<trial>/agent/trajectory.json`
  （`populate_context_post_run` 中实现），跨 agent 统一，`EvalSample.runtrace.trajectory` 直接可用。
- **原生日志**：`<trial>/agent/` 下任意文件（pi.txt、sessions/*.jsonl 等），保真但 agent 相关。
- **结构化上下文**：`result.json → agent_result`（token 数/成本），由 agent 回填。

### 6.4 自定义 agent 接口

```python
from harbor.agents.base import BaseAgent

class MyAgent(BaseAgent):
    SUPPORTS_ATIF = True          # 声明支持 ATIF 轨迹

    @staticmethod
    def name() -> str: return "my-agent"
    def version(self) -> str | None: return "0.1.0"

    async def setup(self, environment): ...            # 容器内安装
    async def run(self, instruction, environment, context):
        ...  # 执行 agent；写产物到 /logs/artifacts/ 或声明的 artifacts 路径
    def populate_context_post_run(self, context):
        ...  # 写 logs_dir/trajectory.json（ATIF）+ 回填 token/cost
```

### 6.5 打分接口（原生）

- **test.sh**：跑测试，写 `1`/`0` 到 `/logs/verifier/reward.txt`（或 `reward.json` 多维度）
- **rewardkit**：规则/LLM judge/agent judge，`uvx harbor-rewardkit@0.1 /tests`
- `EvalSample.scoring` 即原生打分结果的统一视图（rewards/stdout/异常/计时）

### 6.6 落盘契约（读取方的真相源）

```
<jobs>/<job>/<trial>/
├── result.json       # TrialResult：agent_info / agent_result / verifier_result / timings / exception
├── config.json       # TrialConfig：可复现
├── lock.json         # 已解析输入
├── artifacts/        # 产物 + manifest.json
├── agent/            # runtrace（trajectory.json + 原生日志）
├── verifier/         # reward.txt / test-stdout.txt / test-stderr.txt
└── trial.log
```

---

## 7. 镜像复用机制（Harbor 行为速查）

| 机制 | 说明 |
|---|---|
| 内容寻址命名 | 镜像名 = `hb__{sha256(environment/ 内容)}`；环境改动 → 新镜像名 → 强制重建 |
| Docker layer 缓存 | 未变层复用（基础镜像/已装包），重建秒级 |
| Job 内并发 | 多 trial 共享一次构建（asyncio.Lock） |
| 生命周期 | `delete: true`（默认）跑完 `down --rmi local` 删镜像；`false` 保留供跨 job 复用 |
| 预构建镜像 | task 声明 `docker_image` → 直接使用不构建 |

---

## 8. 快速参考

```python
from eval_system import (
    HarborEvalLoader,      # 离线读取
    collect_sample_on_end, # 运行期 hook
    EvalSample,            # 数据契约
    EvalScenario, AgentRunInfo,
    Artifacts, ArtifactFile,
    RunTrace, Scoring,
)
```

依赖：`pydantic`（loader/schema 不依赖 harbor）；`harbor`（仅 hooks 需要）。

---

# 附：v2 契约接口（CONTRACT.md 实施版）

> 完整设计见 [CONTRACT.md](CONTRACT.md)。本节是已落地的 Python 接口速查。
> v1 接口（上文）保留为兼容入口；新代码建议直接用 v2。

## A. 输入契约（可复现）

```python
from eval_system import TaskSpec, AgentSpec, EnvironmentSpec

task = TaskSpec(
    task_id="eval/write-report@1.0.0",
    name="eval/write-report",
    version="1.0.0",
    instruction="...",                        # 喂给 agent 的指令
    instruction_checksum=content_checksum("..."),  # 指令内容 hash
    task_checksum="sha256:...",               # 任务目录整体 hash
    content_ref={"type": "path", "path": "/path/to/task"},  # git|path|registry
)
agent = AgentSpec(name="oracle")              # config_hash 自动计算
env = EnvironmentSpec(type="docker")
```

## B. 运行句柄 + 后端协议

```python
from eval_system import HarborBackend, LocalBackend, ExecutionBackend, Trial

backend = HarborBackend("jobs/")              # Harbor 后端（自动发现 harbor CLI）
# backend = LocalBackend("local-trials/")     # 本地后端（无 docker，验收用）

trial: Trial = await backend.run(task, agent, env, run_id="run-001")
result = await trial.result()                 # -> TrialResult
```

## C. 核心资产 TrialResult（不可变）

```python
result.schema_version    # "trialresult.v1"
result.producer          # {backend: "harbor"|"local", backend_version, schema_version}
result.trial_id / run_id
result.specs             # {task: SpecRef, agent: SpecRef, environment: SpecRef}
result.status            # finished | failed | cancelled
result.artifacts / result.runtrace / result.scoring   # 与 v1 同构
result.backend_raw       # 原始 result.json（保真溯源）
```

落盘形态（`read_trial` 自动为老目录生成）：

```
<trial>/
├── trial_result.json    # ★ 唯一稳定交付物
├── specs/{task,agent,environment}.json   # Spec 快照（checksum 校验）
├── artifacts/ agent/ verifier/          # 产物 + runtrace
└── result.json config.json …            # 后端私有细节（不再有消费者）
```

## D. checksum 校验

```python
from eval_system import read_trial_result, ChecksumMismatchError

try:
    result = read_trial_result(trial_dir)   # 默认校验 specs checksum
except ChecksumMismatchError:
    ...  # Spec 被篡改/漂移：必须报警而非静默继续
```

## E. v2 运行期 hook

```python
from eval_system import collect_trial_on_end

job.add_hook(TrialEvent.END, collect_trial_on_end(scorer=my_scorer))  # 回调收 TrialResult
```

## F. CLI

```bash
python -m eval_system <path> --v2 [--summary|--dump -]   # v2 输出 TrialResult
python -m eval_system <path>                              # v1 输出 EvalSample
```

## G. 版本化规则（CONTRACT.md §7）

- 每个契约独立版本：`taskspec.v1` / `agentspec.v1` / `envspec.v1` / `trialresult.v1`
- 只允许增量加字段；删字段/改语义 = 升主版本
- 读取方按 `schema_version` 分支；未知版本 → 拒绝或降级，不静默猜
- `producer.backend_version` 变更不要求下游改动

## H. 验收结果（2026-08-23 实测）

| 验收项 | 结果 |
|---|---|
| §10.1 本地后端零字段改动接入 | ✅ LocalBackend 产出 13 字段与 Harbor 完全一致 |
| §10.3 老 trial 目录兼容读取 | ✅ 7 个老 trial 自动生成 specs + trial_result.json |
| §10.4 双后端产物进同一 history | ✅ 字段集合一致 |
| §10.5 checksum 篡改报警 | ✅ 篡改 specs/task.json 后读取抛 ChecksumMismatchError |
| HarborBackend.run 端到端 | ✅ 从 Spec 起跑真实 harbor 评测（oracle → reward=1.0） |
