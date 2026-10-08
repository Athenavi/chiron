# Chiron 与 Reasonix 的差距分析

> **依据**：`vendor/DeepSeek-Reasonix`（5151 个 Go 文件、100+ 内部包；形态是 Electron 桌面 + Go TUI +
> Go CLI + npm 包）+ Chiron 自身代码。**静态对照**，不是跑分。
>
> 对照面：**平台 vs 同类产品**（不是 vs agent 库）—— 可比性更高，因此差距也更该认真对待。
> （原先这里指向一份"平台 vs agent 库"的对照文档，但**该文件从未入库**；断链已由
> `scripts/check_doc_links.py` 记录，见 [开发路线图](development-roadmap.md) §3。）
>
> 聚焦用户指定的三块：**核心 agent 循环事件** → **子 agent 交互** → **UI/UX**。

## 0. 先分清可比性

| | Chiron | Reasonix |
|---|---|---|
| 形态 | 多租户 SaaS：Go 网关 + Python 引擎 + Vue Web | **本地产品**：Electron 桌面 + Go TUI + Go CLI |
| 语言重心 | Python（引擎）+ Go（网关）+ TS（前端） | **纯 Go**（5151 文件）+ TS（桌面前端，也是 Vite） |
| 交付面 | Web / HTTP API | CLI / TUI / Desktop / npm |
| 文档形态 | `docs/` 8 份（诚实但少） | **契约文档体系**：`TRANSCRIPT_*` 7 份 + `TOOL_CONTRACT`(19.3KB) + `REASONING_CONTRACT` + `TASK_CONTRACT` + `THEME_PACK` + `GUIDE`(94KB)，**每份中英双语** |

**可比性判断**：核心循环与子 agent 是**同一层的东西**，逐项可比；UI/UX 是**不同战场**（Web SaaS vs 本地
TUI/桌面），对比的价值在**做法**而不在功能条数。

---

## 1. 核心 agent 循环事件（首要）

### 1.1 Reasonix 的模型：`Kind` 枚举 + `Sink` 解耦 + 回合账本

`internal/event/event.go` 开头那段注释把设计意图说得比我转述更清楚：

> *"decouples 'what happened'（模型产生了推理、工具被派发、回合用了 N tokens）from 'how to show it'
> （终端的 ANSI 回滚、webview 里的卡片）。agent 只依赖 `Sink`，每个前端实现一个。**这取代了旧的
> `io.Writer` 契约** —— 那时 agent 写预格式化 ANSI、消费方靠匹配行前缀重建结构，脆弱且对任何比终端
> 更丰富的 UI 都是有损的。"*

事件类型（`Kind`，强类型枚举，共 **14 种**）：

| Kind | 语义 | Chiron 对应 |
|---|---|---|
| `TurnStarted` | 一个顶层 Run 开始（sink 据此重置每回合渲染状态） | 无（靠首个事件推断） |
| `Reasoning` | 思考增量（**独立通道**） | `text` 事件里内联 `[thinking]` 包裹 |
| `Text` | 回答增量 | `text` |
| `Message` | **回合消息完成**：`Text`/`Reasoning` 各持全文，供 sink 把流式原文重渲染为 styled markdown | 无 |
| `ToolDispatch` | 工具将执行（含 `ReadOnly` 标志） | `tool_call` |
| `ToolResult` | 工具完成（`Output`/`Err`/`Truncated`） | `tool_result` |
| `Usage` | **每回合 token 遥测 + Pricing**（成本） | `done` 里带 token；`trace_span` 带明细 |
| `Notice` | 带 **Level** 的带外消息（警告/截断/拦截/压缩） | `guardrail_blocked` / 各自的事件 |
| `Phase` | **协调器边界**（如 planner→executor handoff，Text = "deepseek · planning"） | 无 |
| `ApprovalRequest` | 审批请求（run 阻塞至 `Approve(ID,…)`） | `approval` |
| `AskRequest` | **结构化多选提问**（run 阻塞至 `AnswerQuestion`） | `ask` |
| `TurnDone` | 回合结束（`Err` 非 nil = 失败；**nil 也用于用户取消，取消不是错误**） | `done` / `error` / `cancelled`（三种分开） |
| `CompactionStarted` | 压缩开始（前端显示"compacting…"占位） | `compaction` |
| `CompactionDone` | 压缩完成（**与 ToolDispatch/ToolResult 同构**） | 同上 |

**回合账本**（`internal/turnevent/ledger.go`）是另一层：

* 定位声明极清楚：*"**local lifecycle ledger … projection/recovery artifact only and never contributes
  to model input**"* —— 账本**只做投影与恢复**，绝不进模型输入；
* `schemaVersion = 2` + `legacySchemaVersion = 1`，且有 `UnsupportedSchemaError`：
  *"**deliberately distinct from corruption**；更新的 Reasonix 可能持有该文件，因此当前进程必须
  **leave it untouched**"* —— 旧版本不会破坏新版本的数据；
* 压缩阈值：`8MB` / `4096` 事件 / 关闭时 `256KB`；重放硬上限 `512` 事件 / `2MB`。

### 1.2 对照结论

| 维度 | Chiron | Reasonix | 谁强 |
|---|---|---|---|
| 事件类型丰富度 | ~13 种（text/tool_call/tool_result/trace_span/compaction/todo_updated/rubric/guardrail_blocked/approval/ask/cancelled/done/error） | 14 种（含 `TurnStarted`/`Message`/`Phase`/`Usage`） | **相当** |
| 渲染与语义解耦 | SSE 事件 → 前端自行渲染 | **`Sink` 接口**（TUI / headless ANSI / GUI 各实现一个），且明确"取代 io.Writer" | Reasonix 的**抽象层次更清晰** |
| 思考通道 | 内联在 `text` 里（`[thinking]…[/thinking]`），前端/评测各自切分 | **独立 `Reasoning` 事件** | **Reasonix**（Chiron 把切分成本摊给了每个消费者 + ACP 适配层） |
| 消息完成事件 | 无 | `Message`（流式 + 最终 styled 重渲染两不误） | **Reasonix** |
| 用户取消 | `cancelled` 事件（本轮才补上，见 §32） | `TurnDone` 的 `Err == nil` | 相当（语义一致：**取消不是错误**） |
| 事件持久化 | **无事件级持久化**：checkpoint 是**消息级快照**；SSE 重放靠网关 Redis TTL（1h） | **本地账本**：有序号、压缩、schema 版本、重放上限 | **Reasonix**（崩溃后可重放事件，Chiron 只能从消息快照继续） |
| 版本兼容 | checkpoint 快照无 schema 版本概念 | `schemaVersion` + 拒绝写入未知版本 | **Reasonix** |

### 1.3 值得学的三点

1. **思考用独立事件**，而不是内联标记 + 每个消费者自己切分。Chiron 现在有三处各自切分
   （`evals/observe.py` 剥掉、`acp_adapter/mapping.py` 切成 thought 增量、前端再切一次）——同一份逻辑三份实现。
2. **`Message` 事件**："流式给过程、完成给样式"两不误，避免了"要么只能流式裸文本、要么等完整消息"的二选一。
3. **账本的 schema 版本策略**：未知版本**拒绝写入而不是覆盖**。Chiron 的 checkpoint 快照没有版本概念，
   一旦换代就是"旧引擎读到新快照"的静默风险。

---

## 2. 子 agent 的交互（其次）

### 2.1 Reasonix：五个**带边界声明**的类型

`internal/agent/profile_spec.go` 把一个委派拆成五元组，**每个字段都注明它归谁决定**：

```go
// TaskSpec —— 本次调用要达成什么
//   "Every field is decided per call by the delegating parent, never by the worker's identity."
// WorkerSpec —— 谁来执行（身份与运行时）
//   "Fields here follow from the worker chosen, not from what this particular call asks for."
// CapabilityGrant —— 能碰什么
//   "Profile frontmatter supplies a ceiling and call arguments may only narrow it,
//    so the effective grant is always the intersection of the two."   ← 天花板 ∩ 收窄
// ContextRequest —— 从什么上下文开始
//   "the only parent facts a child should start from. The parent transcript is not copied."
// SchedulerPolicy —— 何时如何执行
//   "It never changes what the child is asked to do or what it is allowed to touch."
```

配套的是 `profile_boundary_test.go` —— **边界本身有测试**（"每次调用的值不许进 profile"是断言，不是约定）。

### 2.2 子 agent 遥测：7 个生命周期阶段 vs 8 种事件

`internal/event/subagent_lifecycle.go` 定义的是**content-free 宿主遥测**：

* 阶段：`child_created | child_running | child_completed | child_partial | child_failed |
  child_cancelled | child_resume`（**7 个**）；
* 字段：Ref / ParentToolCallID / Skill / Model / Effort / Status / ErrorCode / **Retryable** /
  **OutputBytes** / Start-End / **ValidatorMode/Outcome/Attempt** / ProviderRequestID；
* 注释点明取舍：*"**intentionally excludes prompts, reasoning, tool output, and paths** so it can be
  forwarded to diagnostics without leaking transcript data"* —— 可转发诊断而**不泄漏会话内容**；
* 投递方式是**可选接口**（`SubagentLifecycleAuditSink` + 类型断言）：sink **显式 opt-in 才收**。

Chiron 侧（`app/agent/event_sink.py`）是**面向渲染的事件**，共 8 种：`subagent.started` / `.status` /
`.reasoning` / `.text` / `.notice` / `.approval` / `.ask` / `.done`，其中
`IMPORTANT_EVENTS = {started, done, approval, ask}` 不受每秒预算约束。

**两者不是同一层**：Chiron 有流式/审批/提问（**渲染面更全**），Reasonix 有生命周期遥测（**可观测面更全**）。

> **修正**：Chiron 的状态面比"8 种事件"更全 —— 除事件外还有 `subagent_runs` 表的 `status`，取值含
> `cancelled`（取消）与 **`lost`（孤儿恢复）**，后者与 Reasonix 的 `orphan_recovery` 对位。真正缺的是
> `partial`（部分完成）与 `resume`（恢复）这两个语义，以及 `retryable` / `output_bytes` / `validator_*`
> 这类遥测字段。这条修正已带进 `vendor/规划.md`（A5：口径见 §4，未决项见 §3.3）。

### 2.3 Chiron 的实际状况（核实过，不是臆断）

`app/agent/subagent_runner.py` 的 `SubAgentRunner.__init__` 有 **21 个参数**，实际混了六类东西：

| 类别 | 参数 |
|---|---|
| per-call 值 | `task` / `profile_ref` / `mode` / `max_turns` / `run_id` / `rerun_of`（在 `run()` 里，已部分分开） |
| worker 身份 | `profile_ref` / `expert_prompt` |
| 授权 | `allow_write` / `inherit_context` / `budget` / `response_schema` |
| 归属 | `tenant_id` / `user_id` / `parent_session_id` / `parent_run_id` / `depth` |
| 上下文 | `parent_messages` / `parent_system` |
| 基础设施 | `gateway` / `store` / `pool` / `sink` / `cache` |

**能力面 Chiron 不缺**（`app/subagent/` 13 个模块 64 KB：`inherit` / `redact` / `registry`+
`registry_targets`（白名单）/ `remote`（远端）/ `affinity` / `budget` / `store` / `reporting` /
`followup` / `runtime_cache` / `target`）。缺的是**边界纪律的机械化**：Reasonix 用类型 + 边界测试让
"per-call 值泄漏到 worker 身份"成为编译期/测试期错误，Chiron 靠命名与注释。

`CapabilityGrant` 的"**天花板 ∩ 收窄**"里，Chiron **已有天花板**（`ProfileSpec.allowed_tools` /
`disallowed_tools`，见 `subagent_runner._resolve_tools`），缺的是**"调用侧收窄"这一层**与"取交集"的
显式表达（当前是 `allow_write` 布尔 + `mode` 字符串）。

> **修正**：本节初稿写成"Chiron 没有天花板 ∩ 收窄的显式模型"。核实后发现**天花板已有** —— 差距比初判更小，
> 也更具体（只要加"每调用的白名单"并取交集）。这条修正已带进 `vendor/规划.md`（A4：未决项见 §3.3）。

### 2.4 Reasonix 的成熟度差距（规模）

`internal/agent/` 里子 agent 相关的**单文件规模**：`subagent_store.go`(28.3KB) ·
`subagent_progress.go`(23.4KB) · `subagent_result.go`(10.5KB) · `subagent_outcome.go`(8.7KB) ·
`subagent_report.go`(8.5KB) · `scheduler.go`(12.1KB)；测试同样厚（`subagent_store_test` 33.8KB ·
`subagent_progress_test` 34.2KB · `subagent_registry_test` 29.6KB）。

Chiron 的对应物是 `subagent_runner.py` 的**单个类**（约 700 行）。这是**成熟度**差距，不是能力缺失。

---

## 3. UI/UX

### 3.1 不同战场

| | Chiron | Reasonix |
|---|---|---|
| 形态 | **Web SaaS**（Vue 3 + Ant Design Vue） | **Electron 桌面**（Vite + TS）+ **Go TUI** |
| 聊天组件 | 35 个（`ChatInput` 50.7KB · `ChatSidePanel` 47.4KB · `SubAgentPanel` 43.4KB · `MessageItem` 42.5KB · `MessageList` 30.1KB） | `chat_tui.go` **166.6KB**（单文件）+ `chat_tui_paste`(22.9KB) + `chat_tui_events`(17.4KB) |
| transcript 体系 | 10 个模块：`transcriptProjection`(15.4KB) · `transcriptWindow`(6.9) · `transcriptViewport`(5.1) · `transcriptFolds` · `transcriptAnchor` · `transcriptMeasurementLedger`(2.3) · `transcriptSearch` | **7 份契约文档** + `TRANSCRIPT_V2` |
| 渲染测试 | 组件测试（vitest） | `chat_render_test.go` **32.9KB** |
| 主题 | Ant Design token 契约（`check:ui` 的四个契约脚本） | `THEME_PACK`(11.6KB) + `THEME_ASSETS` + `desktop/themes/` |
| 国际化 | 3 语言（zh-CN / en-US / ar）+ 键集检查 | 文档中英双语 + `internal/i18n` |

### 3.2 一个好消息：transcript 这块 Chiron 已经学到了

`transcriptMeasurementLedger.ts` 的注释里写着：

> *"为什么不能边测边改：前缀偏移（`transcriptWindow.buildGeometry` 的输入）是所有窗口与锚点计算的共同
> 输入，逐条写入会让同一帧内的不同计算读到"半更新"的几何（**参照项目称之为测量代际混用**），表现为滚动中
> 偶发跳动或空白。因此：`stage()` 只暂存，`publish()` 原子提交；无实际变更时返回 null。"*

"测量代际混用"正是 Reasonix 那边的说法 —— **这块是已经对位过的**，且实现形态（暂存 → 原子发布 → 无变更返回 null）
与它的契约一致。

### 3.3 真正的差距：**文档化**，不是实现

Reasonix 的 UI/UX 是**契约驱动**的：`TRANSCRIPT_ARCHITECTURE`(10KB) · `TRANSCRIPT_PROJECTION`(9.9KB) ·
`TRANSCRIPT_SCROLL_CONTRACT`(7.3KB) · `TRANSCRIPT_OUTLINE_NAVIGATION`(7KB) · `TRANSCRIPT_V2`(8.5KB) ·
`TRANSCRIPT_ACCEPTANCE_9777`（验收）· `WINDOWS_CLOSE_TRANSCRIPT_VALIDATION`（平台特定验收），
**全部中英双语**；外加 `TOOL_CONTRACT`/`REASONING_CONTRACT`/`TASK_CONTRACT`/`THEME_PACK`/`GUIDE`(94KB)。

Chiron 这边：`transcriptMeasurementLedger.ts` 的**代码注释**写得同等清楚，但**没有对应的契约文档**；
`docs/` 8 份里没有一份是"transcript 契约"。

**这是可执行的差距**：不是"要重写渲染"，而是"把已经写对的不变量写成文档 + 验收清单"。
对 UI/UX 这种"只能靠肉眼与手感验证"的领域，契约文档的价值比代码更持久 —— 它让人知道**哪些行为是承诺**。

---

## 4. 值得追 / 不值得追

### 值得追（按性价比）

1. **思考用独立事件**（改 `AgentEvent` + 三处消费者收敛为一处切分）—— 消除"同一逻辑三份实现"，且
   直接改善 ACP/评测的准确性；
2. **transcript 契约文档化**（现有不变量 + 验收清单）—— 纯文档工作，收益是对 UI/UX 承诺的可追溯；
3. **子 agent 的生命周期遥测**（7 阶段 + content-free 字段 + 可选 opt-in sink）—— 与既有
   `subagent_runs` 审计互补，能让"子 agent 出问题时"可诊断而不泄漏内容；
4. **`CapabilityGrant` 式的显式授权模型**（天花板 ∩ 收窄）—— 把 `allow_write` 布尔升级为可组合的表达；
5. **checkpoint 快照的 schema 版本**（未知版本拒绝写入）—— 小改动，防"旧引擎读新快照"的静默风险。

### 不值得追

* **不改成纯 Go**（语言是交付形态的选择，不是能力）；
* **不做 Electron 桌面**（与"多租户 SaaS"的定位冲突）；
* **不照搬 TUI**（166KB 单文件是一种取舍，不是标杆；且 Chiron 的交互面在 Web）；
* **不引 LangGraph/LangSmith**（Reasonix 也不用 —— 它是自研 Go 运行时，与 Chiron 的取向一致）。

---

## 5. 一句话总结

Reasonix 在**工程纪律的机械化**上明显更强：事件的 `Sink` 抽象与账本、子 agent 的五元组边界与边界测试、
UI/UX 的契约文档体系 —— 这三样都指向同一个习惯：**把"约定"变成"类型、测试或文档"**。

Chiron 的能力面不弱（记忆基建、技能执行、多租户生产面、运行恢复甚至更强），差距主要在
**把纪律固定下来的形式**。这也是最划算的追赶方向 —— 它不需要新依赖，只需要把已经写对的东西写清楚、
或用类型/测试钉住。
