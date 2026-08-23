# Eval System on Harbor

用 [Harbor](https://github.com/harbor-framework/harbor) 作为 agent 评测系统的**运行底座**：
评测场景产出「不同 agent 的产物 + runtrace」，统一交给后续打分层消费。

```
┌─────────────────────────────────────────────────────────────────────┐
│  你的评测系统                                                         │
│                                                                     │
│  ┌──────────────┐   ┌───────────────┐   ┌────────────────────────┐  │
│  │ 场景定义       │ → │ Harbor 运行    │ → │ 产物 + runtrace 数据契约 │  │
│  │ task.toml     │   │ harbor run    │   │ EvalSample (本包)       │  │
│  │ instruction   │   │ (agent×task)  │   │ ┌────────┐ ┌────────┐  │  │
│  │ environment   │   └───────────────┘   │ │产物     │ │runtrace│  │  │
│  └──────────────┘                        │ └────────┘ └────────┘  │  │
│                                          └───────────┬────────────┘  │
│                                                      ▼               │
│                                          ┌────────────────────────┐  │
│                                          │ 打分层 (你的 scoring)    │  │
│                                          │ verifier / rewardkit /  │  │
│                                          │ 你的 LLM judge / 规则     │  │
│                                          └────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## 1. 抽象映射

| 你的抽象 | Harbor 概念 | 位置 |
|---|---|---|
| 评测场景 (scenario) | `Task`（task.toml: instruction + environment/ + tests/）+ `JobConfig` | 任务目录 / job config |
| agent 产物 (artifacts) | 收集到的产物: 约定目录 `/logs/artifacts/` + 配置路径 + sidecar 证据 | `<trial>/artifacts/` (+ `manifest.json`) |
| 打分现场 (verifier 产物) | verifier 输出: 测试 stdout/stderr、reward | `<trial>/verifier/` |
| runtrace | ATIF 轨迹（agent 无关统一格式） | `<trial>/agent/trajectory.json` |
| runtrace (原生) | agent 原生会话日志 (jsonl 等) | `<trial>/agent/` |
| runtrace (结构化上下文) | token 数 / 成本 / rollout details | `result.json → agent_result` |
| 打分结果 | verifier 的 rewards + 异常 + 计时 | `result.json → verifier_result` |

### 一次运行 (trial) 的磁盘契约

Harbor 每次运行 = 一个 `Trial`，输出目录结构固定，天然就是「产物 + runtrace」的交付物：

```
<jobs_dir>/<job>/<trial>/
├── result.json        # ★ 结构化总纲: TrialResult (agent_info / agent_result /
│                      #   verifier_result / timings / exception / step_results)
├── config.json        # 可复现的 TrialConfig（agent、model、env、artifacts 声明）
├── lock.json          # 已解析的输入（任务版本等）
├── artifacts/         # ★ 产物: 从 agent 环境收集的文件
│   ├── manifest.json  #   收集清单 (source/destination/service/status)
│   └── ...            #   约定目录 logs/artifacts/ 及配置路径的镜像
├── agent/             # ★ runtrace: agent 日志
│   ├── trajectory.json   # ATIF 标准轨迹（若 agent 支持，跨 agent 可比）
│   └── ...               # agent 原生会话文件
├── verifier/          # ★ 打分现场: test-stdout.txt / test-stderr.txt /
│                      #   reward.txt / reward.json / 其他 verifier 产物
└── trial.log
```

多步任务 (multi-step) 时，`agent/ verifier/ artifacts/` 的内容归档到
`steps/<step_name>/` 下，结构相同。

## 2. 本包: 统一数据契约

`eval_system.schema.EvalSample` 把上述磁盘内容归一为打分层直接可用的对象：

```python
from eval_system.loader import HarborEvalLoader

loader = HarborEvalLoader(jobs_dir=Path("~/.harbor/jobs"))
for sample in loader.iter_samples():
    # 场景
    sample.scenario.task_name          # "harbor/hello-world"
    sample.scenario.instruction        # 评测指令原文
    # 是谁跑的
    sample.agent.name / sample.agent.model
    # 产物
    sample.artifacts.files()           # ArtifactFile 列表(带 host_path 可直接读)
    sample.artifacts.read_text("logs/artifacts/output.txt")
    # runtrace
    sample.runtrace.trajectory         # ATIF dict (steps/tool_calls/observation)
    sample.runtrace.agent_context      # token 数 / cost
    # 打分输入
    sample.scoring.rewards             # {"reward": 1.0, ...}
    sample.scoring.test_stdout
    sample.scoring.exception
```

三条接入路径（可按需选择，可叠加）：

1. **离线读取（推荐起步）** — `HarborEvalLoader` 扫 jobs/trials 目录，把已跑完的
   结果统一成 `EvalSample`，喂给任何打分器（LLM judge / 规则 / 训练数据管线）。
2. **运行期 hook（实时收集）** — 在 `harbor run` 的 Job/Trial hook 里挂回调
   （`TrialEvent.START/AGENT_END/END`），trial 结束即得到产物+runtrace 就绪通知，
   可实时推送打分（见 `eval_system.hooks`）。
3. **原生打分** — 直接使用 Harbor 的 verifier / rewardkit / `harbor analyze`：
   无需自行实现沙箱内打分；`EvalSample.scoring` 即为原生结果的统一视图。

## 3. 快速开始

```bash
# 1) 安装 harbor（运行底座）
uv tool install harbor          # 或 pip install harbor

# 2) 跑一个评测（示例: 用 claude-code 跑 hello-world）
export ANTHROPIC_API_KEY=...
harbor run -d harbor/hello-world@1.0.0 -a claude-code -m anthropic/claude-sonnet-4-6

# 3) 用本包把结果归一成 EvalSample
uv run python -m eval_system ~/.harbor/jobs/<job> --dump sample.jsonl
```

## 4. 定制 agent

Harbor 已内置 30+ agent（claude-code、codex、openhands、aider、cursor 等）。
接入你自己的 agent：实现 `BaseAgent` 的 `setup/run`，并在 `populate_context_post_run`
里写 `logs_dir/trajectory.json`（ATIF），runtrace 即自动进入统一契约：

```python
from harbor.agents.base import BaseAgent

class MyAgent(BaseAgent):
    SUPPORTS_ATIF = True
    @staticmethod
    def name() -> str: return "my-agent"
    def version(self) -> str | None: return "0.1.0"
    async def setup(self, environment): ...
    async def run(self, instruction, environment, context):
        ...  # 跑你的 agent
    def populate_context_post_run(self, context):
        ...  # 写 logs_dir/trajectory.json + 回填 token/cost
```

## 5. 目录

```
eval-system/
├── README.md
├── docs/
│   └── INTERFACES.md     # ★ 完整接口文档（数据契约 / Python API / CLI / Harbor 对接点）
├── pyproject.toml
└── src/eval_system/
    ├── __init__.py       # 公共 API 导出
    ├── schema.py         # EvalSample / Artifacts / RunTrace / Scoring 数据契约
    ├── loader.py         # 离线读取: harbor jobs/trials 目录 → EvalSample
    ├── hooks.py          # 运行期 hook: 实时收集 + 通知打分
    └── __main__.py       # CLI: dump / 汇总
```

完整接口规范见 [docs/INTERFACES.md](docs/INTERFACES.md)。
