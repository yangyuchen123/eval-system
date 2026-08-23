# eval-system v2 契约草案 — Task × Agent → Trial → TrialResult

> 状态:**已实施落地**（2026-08-23）。实现位于 `src/eval_system/contract/`：
> `specs.py`（输入契约）、`trial.py`（TrialResult + 落盘/校验）、`backend.py`（后端协议）、
> `harbor_backend.py`（Harbor 后端）、`local_backend.py`（本地后端，验收用）。
> 验收 1/3/4/5 已实测通过（见 `tests/test_contract.py`）；v1 loader/schema 保留为兼容入口。
> 本文档定义 eval-system 升级为
> **通用 Execution Runtime** 后的唯一稳定公共接口。一旦冻结,后端
> (Harbor / 本地 / 远程)可换、Agent 可换、评测集生产者(benchagent /
> eval-tasks)可换、消费方(agent-eval / 打分层)可换,整个评测体系
> 不被任何一方拖着重写。
>
> 对应实施范围:重构 `src/eval_system/`(schema/loader/hooks),**不改变**
> 现有 Harbor 运行流程,老 trial 目录保持可读。

---

## 0. 契约总览

```
       输入契约(可复现)                   运行契约(有状态)            核心资产(不可变)
┌─────────────────────┐        ┌─────────────────────┐      ┌─────────────────────┐
│ TaskSpec     (v1)   │        │                     │      │ TrialResult  (v1)   │
│ AgentSpec    (v1)   │ ────►  │  Trial (句柄)        │ ──►  │  = 产物 + runtrace   │
│ EnvironmentSpec(v1) │        │  status/cancel/probe │      │    + scoring + 溯源  │
└─────────────────────┘        └─────────────────────┘      └─────────────────────┘
        ↑ 生产者: benchagent / eval-tasks / 手工                ↓ 消费者: agent-eval / 打分层 / 训练管线
```

**核心原则**

1. **输入契约保证可复现** —— 任何后端拿到同一组 Spec(同 checksum)应产出可比的 Trial。
2. **TrialResult 保证可消费** —— 下游只读它,不感知后端实现。
3. **TrialResult 只带 checksum + 引用** —— 不内嵌 Spec 全文;Spec 以独立文件落盘,
   由引用定位、由 checksum 校验。TrialResult 保持轻量,Spec 可被多 trial 共享、被
   registry 去重,审计完整性由 checksum 兜底。

---

## 1. TaskSpec — 一次评测要做什么

| 字段 | 类型 | 必填 | 说明 | 现状来源 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | `"taskspec.v1"` | 新增 |
| `task_id` | str | ✅ | 稳定唯一标识(如 `eval/write-report@1.0.0`) | result.task_name + version |
| `name` | str | ✅ | 短名 | result.task_name |
| `version` | str | ✅ | 语义化版本 | task.toml `[task].version` |
| `instruction` | str | ✅ | 喂给 agent 的指令原文 | instruction.md / benchagent question |
| `instruction_checksum` | str | ✅ | instruction 内容 hash(单独,便于检测指令变化) | 新增 |
| `source` | str \| null | | 数据集来源(provenance) | result.source |
| `content_ref` | dict \| null | | 任务内容的可寻址引用:`{type: git\|path\|registry, url/path, commit, checksum}` — **后端据此获取任务** | config.task(git_url/commit/path) |
| `task_checksum` | str | ✅ | 任务目录整体内容 hash(含 instruction 之外的环境/测试定义) | result.task_checksum |
| `metadata` | dict | | difficulty/category/tags 等 | task.toml `[metadata]` |
| `output_schema` | dict \| null | | 期望产物结构(可选项,给 verifier/消费方提示) | benchagent subtask.output_schema |

**边界**:TaskSpec 是"做什么",不含"怎么跑"(EnvironmentSpec)也不含"用谁跑"(AgentSpec)。
benchagent 的 `evaluation.json` 样本 → TaskSpec 时:context+question → `instruction`,
`answer_type/output_schema` → `output_schema`,`subtask_id` → `task_id`。

---

## 2. AgentSpec — 谁来跑

| 字段 | 类型 | 必填 | 说明 | 现状来源 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | `"agentspec.v1"` | 新增 |
| `name` | str | ✅ | agent 标识(如 `claude-code` / `oracle` / `my-agent`) | agent_info.name |
| `version` | str \| null | | agent 实现版本 | agent_info.version |
| `model` | str \| null | | 模型名(如 `anthropic/claude-sonnet-4-6`) | agent_info.model_info.name |
| `provider` | str \| null | | 提供商 | agent_info.model_info.provider |
| `config` | dict | | agent 专属可序列化配置(kwargs/skills/mcp 等) | config.agent |
| `config_hash` | str | ✅ | 上述全部字段的内容 hash — **agent 实现变了,TrialResult 里能检测出来** | 新增 |
| `supports_atif` | bool | | 是否产出标准轨迹(供 runtrace 能力预判) | Harbor `SUPPORTS_ATIF` |

**边界**:AgentSpec 是"用谁、用什么模型、什么配置"。换 agent = 换 AgentSpec,契约不动。

---

## 3. EnvironmentSpec — 在哪里跑

| 字段 | 类型 | 必填 | 说明 | 现状来源 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | `"envspec.v1"` | 新增 |
| `type` | str | ✅ | `docker` \| `local` \| `remote` …(后端可支持子集) | config.environment.type |
| `image_ref` | dict \| null | | `{type: dockerfile\|compose\|image, path/content, tag}` — 内容寻址 | environment/ 目录 |
| `resources` | dict | ✅ | `{cpus, memory_mb, storage_mb, gpus, timeout_sec}` | task.toml `[environment]` |
| `mcp_servers` | list | | MCP 服务声明 | task.toml |
| `environment_hash` | str | ✅ | 环境定义的内容 hash(镜像可复用性的依据) | Harbor 镜像名 sha256(隐式,现在显式化) |

**边界**:EnvironmentSpec 与 TaskSpec 分离的意义——同一任务可在不同环境复跑
(容器/本地/远程),也可不同任务共用环境。

---

## 4. Trial — 运行句柄(有状态,不落盘为契约)

| 字段 | 类型 | 说明 |
|---|---|---|
| `trial_id` | str | 一次运行的唯一 ID(如 `write-report__9BTHL4H`) |
| `run_id` | str | 一批运行的 ID(如 jobs 目录名 `2026-08-23__10-13-57`) |
| `specs_refs` | dict | 运行快照:三个 Spec 的引用 + checksum(见 §5.2) |
| `status` | enum | `pending → running → finished / cancelled / failed` |
| `timestamps` | dict | 各阶段时间(started/finished) |
| 方法(接口层) | | `status() / cancel() / result() -> TrialResult`(阻塞或异步) |

**边界**:Trial 是"运行态",给 hooks/流式/控制面用;打分层永远只拿 TrialResult。
这是现有 hooks(运行期)与 loader(离线)两条消费路径的统一。

---

## 5. TrialResult — 核心资产(不可变、自包含、版本化)

### 5.1 字段

| 字段 | 类型 | 必填 | 说明 | 现状映射 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | `"trialresult.v1"` | 新增 |
| `producer` | dict | ✅ | `{backend: "harbor", backend_version, schema_version}` — **回答"谁产的"** | 新增 |
| `trial_id` / `run_id` | str | ✅ | 溯源 | trial_name / jobs 目录 |
| `specs` | dict | ✅ | **只放引用 + checksum**,不内嵌 Spec 全文(见 §5.2) | EvalScenario + AgentRunInfo + config |
| `status` | str | ✅ | `finished \| failed \| cancelled` | exception_info 推断 |
| `timings` | dict | ✅ | environment/agent/verifier 各阶段耗时 | result.environment_setup 等 |
| `artifacts` | Artifacts | ✅ | manifest(文件级)+ host 路径 | 现 Artifacts 原样 |
| `runtrace` | RunTrace | ✅ | ATIF trajectory + 原生日志 + agent_context(token/cost) | 现 RunTrace 原样 |
| `scoring` | Scoring | ✅ | rewards + test stdout/stderr + verifier_result + step_results | 现 Scoring 原样 |
| `is_regrade` | bool | | 是否为只重打分不重跑 | 现字段 |
| `backend_raw` | dict \| null | | 后端原始结果(可选,保真溯源) | 现 raw_result |
| `created_at` | str | ✅ | 落盘时间 | 新增 |

> 设计意图:把现有 `EvalSample` 的五个子块(scenario/agent/artifacts/runtrace/scoring)
> 几乎原样搬进 TrialResult,**改名 + 加版本 + 加 producer** —— 迁移成本低,但契约从此
> 独立于 Harbor。

### 5.2 specs:引用 + checksum(本版修订的核心)

TrialResult 不重复携带 Spec 全文,而是每次运行把三个 Spec 序列化落盘,由引用定位、
由 checksum 校验:

```jsonc
// trial_result.json → specs
"specs": {
  "task":        { "schema": "taskspec.v1", "checksum": "sha256:4f8d…",
                   "ref": "specs/task.json" },
  "agent":       { "schema": "agentspec.v1", "checksum": "sha256:9ac2…",
                   "ref": "specs/agent.json" },
  "environment": { "schema": "envspec.v1",   "checksum": "sha256:c1e7…",
                   "ref": "specs/environment.json" }
}
```

- `ref`:定位符。相对路径(`specs/task.json`,相对 trial 根)为默认;
  允许 `file://` 绝对路径与 `registry://<id>`(Spec 注册表)两种扩展形式。
- `checksum`:读取方按 ref 取到 Spec 后**必须重新计算并比对**;不一致即报警
  (防篡改 / 防 Spec 漂移),不得静默继续。
- 落盘形态:

```
<trial>/
├── trial_result.json      # ★ 唯一稳定交付物(下游只读它)
├── specs/                 # 本次运行的三个 Spec 快照(checksum 校验)
│   ├── task.json
│   ├── agent.json
│   └── environment.json
├── artifacts/ …           # 文件仍落盘,JSON 里引用相对路径
├── agent/ …               # runtrace 原生日志
├── verifier/ …            # 打分现场(可选)
└── result.json, config.json, …   # ← 后端私有细节,不再有消费者
```

**收益**:TrialResult 变轻;同一 Spec 可被多 trial 共享、被 registry 去重;
审计完整性由 checksum 兜底,不依赖任何后端。

---

## 6. 后端协议(最小面)

```
ExecutionBackend
  run(TaskSpec, AgentSpec, EnvironmentSpec) -> Trial   # 启动运行(内部负责落盘 specs/)
  (Trial).result() -> TrialResult                       # 取终态(含写盘)
  (Trial).cancel()                                      # 可选
  read_trial(trial_id) -> TrialResult                   # 离线读取历史
```

- **只有这 4 个能力**。调度/并发/重试/队列**不在协议里** —— 它们是上层或后端内部的事,
  防止 eval-system 变成调度器。
- Harbor 后端 = 现有 `HarborEvalLoader` 的读侧(实现 `read_trial`)+ 一个薄的 `run()`
  适配(调 harbor run)。**现有 loader 不删,降级为 HarborBackend 内部实现**。
- 验收标准:**第二个后端(本地进程,无 docker)能在不触碰 TrialResult 任何字段的情况下
  接入** —— 加不出来就是抽象错了。

---

## 7. 版本化与兼容策略

| 规则 | 内容 |
|---|---|
| schema_version 粒度 | 每个契约独立版本(`taskspec.v1` ≠ `trialresult.v1`) |
| 变更规则 | **只允许增量加字段**;删字段/改语义 = 升主版本,下游按版本分发 |
| checksum 语义 | 每个 Spec 一个内容 hash;TrialResult.specs 的 checksum 是"复现审计"的依据 |
| 读取兼容 | 读取方按 `schema_version` 分支;未知版本 → 拒绝或降级,不静默猜 |
| 后端兼容 | `producer.backend_version` 变更不要求下游改动;`schema_version` 变更才需要 |

---

## 8. 与现状的映射速查(迁移不改消费方)

| 现状(eval-system v1) | 新契约 |
|---|---|
| `EvalSample` | → `TrialResult`(字段基本平移) |
| `EvalScenario` | → `TaskSpec`(+ instruction_checksum / content_ref) |
| `AgentRunInfo` | → `AgentSpec`(+ config_hash) |
| `config`(原始 TrialConfig) | → `EnvironmentSpec` + `specs/` 落盘 |
| `HarborEvalLoader.load_trial()` | → `HarborBackend.read_trial()`(内部实现不变) |
| `collect_sample_on_end` | → 不变,回调签名改收 `TrialResult` |

**向后兼容**:v1 的 loader 保留为 `harbor_read_v1` 兼容入口,老 trial 目录仍可读
(读取时补 `schema_version`、producer、specs 引用与 checksum 字段)。

---

## 9. 下游投影(桥,作为官方适配器)

| 消费者 | 投影规则 |
|---|---|
| agent-eval GDPVal | `TrialResult → {task_id: artifacts 首文件文本}`(`artifacts.read_text`) |
| agent-eval SWE-bench | `TrialResult → {instance_id: {model_patch}}`(产物为 diff 时) |
| 打分层/训练管线 | 直接消费 TrialResult(超集,免投影) |

投影器本身也带版本(`projection.v1`),随 TrialResult 一起发布 —— 这样 agent-eval 侧
的输入契约也进入版本管理。

---

## 10. 验收清单(这份契约"真正稳定"的标志)

1. 新增一个本地后端,**TrialResult 字段零改动**跑通 oracle 任务;
2. 某次 Harbor 升级改了 `result.json` 内部字段,**下游(agent-eval/打分)零改动**;
3. 老 trial 目录(2026-08 之前的)通过兼容读取仍能出 TrialResult;
4. 两份不同后端产出的 TrialResult 能直接进同一份 `history.jsonl` 对比;
5. specs 引用缺失或 checksum 不符时,读取方明确报警而非静默继续。
