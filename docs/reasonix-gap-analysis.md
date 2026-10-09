# Chiron 与 Reasonix 的差距分析（对照分支：`studio` = Reasonix 2.x）

> **本版换了对照目标。** 上一版对照的是 `main-v2`（**1.x**，现已冻结为维护线）；本版对照 `studio`
> （**2.x**，活动开发线，出 Reasonix Studio 桌面端与 `reasonix` CLI）。两线已严重分叉：
> `git rev-list --left-right --count main-v2...studio` = **2684 / 3235**，
> `git diff --shortstat main-v2 studio` = **13,300 文件、+826,096 / −1,447,823 行**。
> 因此旧版基于 1.x 的 vendor 证据路径（`internal/event`、`internal/turnevent`、`internal/agent`、
> `TRANSCRIPT_*` 系列）在 2.x **全部不存在** —— 见 §11 的过期清单。
>
> **取证基线**：`vendor/DeepSeek-Reasonix` @ 分支 `studio`，HEAD `c47bdfd84`（2026-10-08）。
> ⚠️ 该仓库的 `origin` 是 `Athenavi/DeepSeek-Reasonix`（fork），**没有 `studio` 分支**；
> `studio` 只在上游 `esengine/DeepSeek-Reasonix` ⇒ 已加 `upstream` remote 后 `git merge --ff-only upstream/studio`。
>
> **取证强度**：每条结论标来源等级 —— **【枚举】**逐文件/逐符号读出 · **【文档】**读契约文档原文 ·
> **【grep】**关键词检索（只证明词的有无，不证明运行期生效）。
> **本次没有跑任何构建、测试或运行时**：全部是静态对照。"机制在源码里" ≠ "运行期生效"，
> 这条纪律见 [与 DSH 的差距分析](dsh-gap-analysis.md) 与 `vendor/规划.md` §4。

## 0. 结论摘要

| # | 结论 | 强度 |
|---|---|---|
| 1 | **目标线变了**：Reasonix 2.x（`studio`）是活动线，1.x（`main-v2`）只做修复。旧版本分析对照的是一条已停止演进的线 | 【枚举】`docs/ROADMAP.md`、`docs/MIGRATING.md` |
| 2 | **Chiron 的能力面不落后，且平台面是它独有**：多租户、配额、计费、多副本协调、身份/会话隔离在 2.x **没有对应物**（2.x 是单机单用户） | 【枚举】+【grep】§9 |
| 3 | **差距集中在"把纪律固定下来的形式"**，且 2.x 比 1.x 更极端：事件契约 34 种强类型 + 线协议层、委派边界由**反射测试**钉住、文档规格由 **repolint 门禁**执行 | 【枚举】§3/§4/§8 |
| 4 | **测试厚度是最大的系统性差距**：2.x `internal/` 测试字节 **14.13 MB > 实现 11.86 MB（1.19×）**；Chiron Go 侧 0.20×、Python 侧 0.58× | 【枚举】§2 |
| 5 | **旧版本文件里 23 条 Chiron 侧断言，只有 10 条仍成立**（3 条错、9 条过期、1 条部分过期）—— 差距分析本身已经烂了一轮 | 【枚举】§11 |
| 6 | **Chiron 侧查出 5 处接线缺口**（不是能力缺失）—— **2026-10-09 已逐条收口**：**1 / 2 / 3 / 4 复现并修好**（最要紧的 native `thinking` 未落库 ⇒ 已修 + 真 PG 用例「读回来」断言；`partial` 半接入 ⇒ **两半都补**并升级为三方比对机械化；ORM 生成物未跟上迁移 ⇒ 补 yaml + 重跑生成器；一处引用命中断链基线 ⇒ 改就地说明 + **基线清零**），**5 复核后不成立**（清单自身过期）。**2026-10-09 更正**：本行原只写"最要紧的是…刷新后思考丢失"，读起来像**仍待办** ✗，与 §12 的收口结论不符 | 【枚举】§12 |

---

## 1. 可比性

| | Chiron | Reasonix 2.x（`studio`） |
|---|---|---|
| 形态 | 多租户 SaaS：Go 网关 + Python 引擎 + Vue Web | **本地单机产品**：Electron 桌面 + Go TUI + Go CLI + `serve` Web + ACP 编辑器 |
| 租户模型 | 租户/用户/session 三级隔离，配额与计费 | **一个 OS 用户**（威胁模型只列一个 `User` 主体） |
| 语言重心 | Python（引擎）+ Go（网关）+ TS（前端） | **纯 Go 内核**（静态单二进制）+ TS（桌面 SPA） |
| 存储 | PostgreSQL + Redis + Milvus + MinIO | 本地文件 + 本地 SQLite（usage 汇总） |
| 交付 | 容器多副本 + nginx | 签名安装包（自更新）+ npm/Homebrew CLI |

**可比性判断**：§3（事件契约）、§4（子 agent）是**同一层的东西，逐项可比**；
§5（执行信任）**做法可比、结论须换算**（2.x 面对"用户自己的机器"，Chiron 面对"不受信租户"）；
§8（UI/UX）是**不同战场**，价值在做法不在功能条数；
§9（平台面）**不可比 —— 那是 Chiron 独有的一段**。

---

## 2. 量表（本机实测，2026-10-08）

| 维度 | Chiron | Reasonix 2.x | 说明 |
|---|---|---|---|
| Go 文件 | — | **4,528**（2,216 非测试 / 2,312 测试） | `git ls-tree` 枚举；1.x 是 5,183 ⇒ 2.x **变小了**（重写而非堆叠） |
| Go 实现 vs 测试（字节） | `internal/` **1.50 MB / 0.29 MB = 0.20×** | `internal/` **11.86 MB / 14.13 MB = 1.19×** | 2.x **测试比实现多** |
| 子 agent 域测试厚度 | `app/subagent/` 157 KB vs 相关测试 174 KB = **1.10×** | `internal/runtime/delegation/` 243 KB vs 399 KB = **1.65×** | 同域对比才是公平尺子 |
| Python | `app/` 2.39 MB / 239 文件；测试 1.40 MB / 183 文件 = **0.58×** | — | |
| 前端 | `src/` 2.20 MB / 206 文件；测试 0.24 MB / 57 文件 | `desktop/frontend-next/` 16.31 MB（含 10 MB 字体） | 体量不可直接比 |
| 文档 | `docs/` **17** 份 `.md` | `docs/` 71 文件 / 1.73 MB，**40** 份顶层 `.md`；全仓 297 份 | 见 §8 的规格门禁 |
| 基准/评测 | `python-engine/evals/` 9 `.py` + 4 固件 | `benchmarks/` **14 套件**，`benchmarks/e2e/tasks` **82** 个任务 | |
| CI | 2 个 workflow；**CI 从未实跑** | **24** 个 workflow（发布/签名/多平台/部署） | 见 §10 |

> ⚠️ Go 的 0.20× 不能单独当结论：Chiron 的网关层薄、`internal/` 里大量是路由与胶水，
> 而 2.x 的 `internal/` 是**整个产品内核**。真正的信号是**同域**那一行（1.10× vs 1.65×）。

---

## 3. 核心循环与事件契约

### 3.1 2.x 的模型：34 种强类型 `Kind` + 独立的线协议层

`internal/contract/event/event.go`（**33,038 B / 678 行**）的包注释把意图写得比转述清楚：

> *"It decouples 'what happened' … from 'how to show it' … This replaces the old `io.Writer` contract,
> where the agent wrote pre-formatted ANSI and the consumer had to re-derive structure by matching line
> prefixes — fragile, and lossy for any frontend richer than a terminal."*

`Kind` 枚举 **34 个真实取值（iota 0..33）+ `KindCount` 哨兵**（= 34）。**1.x 的 14 个在 0..13 原序保留**
⇒ 2.x 是**严格追加式扩展**，每一处新增都带注释 *"Appended last to keep the Kind values before it wire-stable"*，
哨兵注释写明 *"New event kinds must be inserted above it so completeness tests cover them automatically."* 【枚举】

新增的 20 种（iota 14..33）：`ToolProgress` · `MCPSurfaceReady` · `Retrying` · `Steer` ·
`GuardianAssessment` · `ExtensionSurface` · `ExtensionStatus` · `StreamAttempt` · `ContextMaintenanceEvent` ·
`TodoProgressEvent` · `WorkspaceLeaseEvent` · `WorkspaceChanged` · `TurnPhase` · `CompletionSummary` ·
`CompactionProgress` · `InboxChanged` · `GraphDelta` · `AdjudicationsChanged` · `BrowserTabsChanged` ·
`ProgressWatchEvent`。

其中两类**思路值得单独记**：

* **`NonPersistable` + `Seq` 的分层**（`internal/contract/eventwire/wire.go`，**33,682 B / 800 行**）：
  线协议名是 snake_case 的**稳定身份**（Go 标识符重命名不是协议破坏），逐 `Kind` 投影载荷，
  并且显式标注「**只用于实时投递与提示词重放，永不写进持久事件日志**」（`NonPersistable`），
  以及 *"Seq numbers frames a client must not miss … Streaming deltas carry none — the same reason SSE
  omits `id:`"*。
* **内容无关（content-free）的失效事件**：`InboxChanged` / `AdjudicationsChanged` / `BrowserTabsChanged`
  只发"变了"，值**回读内核** —— 注释：*"one authority answers every client instead of each rebuilding it
  from the frames it happened to see."* 这直接对应 Chiron 的教训「**消费端有分支 ≠ 生产端在发**」。

### 3.2 归属、恢复与压缩

* **归属只发生一次**：`internal/session/control/turn_orchestrator.go` 的 `announceAuthoredTurn` 是唯一
  命名回合的地方，同时盖 `AuthoredTurn` / `MsgIndex` / `Text`；合成续跑走 `continueAnnouncedTurn`
  「不开边界也不关边界」。【枚举】
* **准入是一个受限状态机**：`turn_gate.go`（75 行）把 6 个字段收进一个类型，注释：*"rather than as five
  loose bools whose combinations include states no controller ever reaches"*；`active() = running || finishing`
  「**这正是裸 `running` 检查会漏掉竞态的原因**」。【枚举】
* **崩溃恢复**：回合开始写持久 in-flight 标记，转写落盘后才清除 —— *"A crash can therefore leave either a
  recoverable marker or a durable completed transcript, never an unmarked in-memory-only suffix."*；
  恢复有四个分支（含「标记跨过更晚的已完成回合 ⇒ 保留整段 WAL 并发 warn」）。【枚举】
* **审批屏障日志**：*"A barrier is a historical fact … written before anyone can be asked, and never
  rewritten"*，写入 `Sync()` 后才提问（**durable before observable**）；`interrupted` 刻意**不落盘**
  （由"开记录 + 无存活属主"推导）。**重连重放** `ReplayPendingPrompts` 会跳过"从未显示过的排队 ask"。【枚举】
* **压缩**：`CompactionNoopReason` 有 **7 个码**（`no_new_closed_prefix` / `fold_below_economics` / …），
  注释：*"'Already folded this turn' and 'nothing left to fold' read alike and mean opposite things"*；
  核心不变量 **"The canonical transcript is never rewritten"** —— 折叠装的是**投影**，
  所以 resume / rewind 不受影响。【枚举】

### 3.3 对照

| 维度 | Chiron | Reasonix 2.x | 谁强 |
|---|---|---|---|
| 事件类型数 | **15**（`runtime.py` 枚举：text / thinking / tool_call / tool_result / usage / done / error / cancelled / approval / ask / compaction / todo_updated / rubric / trace_span / guardrail_blocked） | **34** + 哨兵，且**追加式**、有完整性测试 | 2.x（**不是数量，是"加类型不破坏线协议"**） |
| 思考通道 | **已是独立事件**（`runtime.py:1542-1545`，注释写明 A1 "不再包装成 `[thinking]` 混进 `text`"） | `Reasoning` 独立 Kind + 独立线名 | **已对位**（但落库有缺口，见 §12.1） |
| 消息完成事件 | **缺**：`done` 只带 usage/model/trace_id | `Message`（全文 Text + Reasoning，供重渲染 styled markdown） | 2.x |
| 线协议身份 | 事件 `type` 字符串即协议 | 枚举 → snake_case 稳定名的**独立映射层** + 只实时字段 | 2.x |
| 回合归属 | 隐式（靠事件顺序） | `TurnStarted` 带 `AuthoredTurn`/`MsgIndex`/`ModelRef`，**只发一次** | 2.x |
| 取消语义 | `cancelled` 事件 | `TurnDone` 的 `Err == nil` + `Cancelled` 位 | 相当（**取消不是错误**） |
| 事件级持久化 | **无事件表**（~80 张表里没有）；checkpoint 是消息级快照 | `internal/state/trajectory/`：JSONL 审计流，`SchemaVersion=1`、合并上限 256、逐条 flush | 2.x（但**无重放读取路径**，只写） |
| 重连重放 | SSE + Redis TTL 1h + `Last-Event-ID` | `ReplayPendingPrompts`（只重放**仍阻塞**的提示，跳过排队 ask） | 各自解一个问题 |
| 压缩可解释性 | `compaction` 事件 | 7 个结构化 noop 码 + 进度事件（*"a placeholder that says nothing for a minute is indistinguishable from one that has hung"*） | 2.x |

---

## 4. 子 agent / 委派

### 4.1 五元组边界仍在，且**由测试钉住**

`internal/runtime/delegation/profile_spec.go`（14,547 B）保留 `ProfileExecSpec` 五元组
（`Task` / `Worker` / `Grant` / `Context` / `Sched`），注释：*"Its members are the delegation boundary:
place a new field in the member that decides its value"*。`CapabilityGrant` 仍是**天花板 ∩ 收窄**
（`IntersectToolLists`，空交集报错）。【枚举】

**关键是"谁来保证"**：`profile_boundary_test.go`（5,923 B）用**反射断言每个类型的精确字段集**，
并把规则文本嵌进测试 —— 也就是说"per-call 值不许泄漏进 worker 身份"是**测试期错误**，不是注释约定。

2.x 还新增了 worker 身份层的 **`DeliveryContract` / `AuthorityContract`**，注释 *"a call may never widen it"*。

### 4.2 遥测换了赛道：1.x 的 lifecycle 通道被**删除**

1.x 的 `internal/event/subagent_lifecycle.go`（7 阶段 + `Retryable`/`OutputBytes`/`Validator*`）在 2.x
**没有生产者也没有消费者**（grep 只剩一个测试函数名）。取而代之是三条通道【枚举】：

1. `internal/contract/agentgraph/graph.go`：`NodeState` = pending/queued/running/completed/**adopted**/failed/cancelled/skipped，
   带 `Grant`/`WaitCause`/`EdgeKind`；持久形态 `internal/state/execjournal/journal.go`（含派生的
   `interrupted-before-start` / `interrupted-during-execution`）；
2. 进度 9 阶段（queued/running/reasoning/responding/tool/retrying/终态），线名 `reasonix.subagent.*`；
3. store 元状态（含 `interrupted`）。

"resume" 不再是阶段，而是 `ContextRequest.ContinueFrom/ForkFrom` + `ResumedFrom`。

### 4.3 调度：写路径仲裁 + 明确的并发天花板

`internal/runtime/writeclaim/`：同路径串行（`conflictLocked`）、**未声明路径的写者按整工作区独占**
（`WholeWorkspaceWriteClaim`）、嵌套**快速失败**（注释：*"nested subagents fail fast to avoid
parent/child slot deadlock"*）、等待者**按优先度排序**（v3 新，v2 是纯 FIFO）。
默认 **6 个并发 / 3 个写者**，上限 32。【枚举】

**洪水抑制是三层**（这是 Chiron 侧最可直接对照的一块）：250 ms 合并窗口 + 每组 32 事件/秒令牌桶 +
每子 8 KiB 待发预算（丢弃顺序 notice→reasoning→text）+ 子通道 8/8/2 KiB 上限，
且**只有初始态与终态绕过预算**。【枚举】

### 4.4 对照

| 维度 | Chiron | Reasonix 2.x | 判断 |
|---|---|---|---|
| 边界模型 | `ProfileSpec.allowed_tools/disallowed_tools` 天花板 + `call_tools` 收窄（`subagent_runner.py:249,1041-1061`，注释写明"只能收窄、永不放宽"） | 五元组 + **反射边界测试** | **能力已对位；差的是机械化**：Chiron 靠命名与注释 |
| 收窄是否对模型可见 | **否** —— `app/tools/subagent.py` 只收 allow_write/response_schema/inherit_context/target | 是（`profile=` 参数） | 2.x；Chiron 侧是**未决产品决定**（`vendor/规划.md` §3.3-1） |
| 生命周期遥测 | **已有** `app/subagent/lifecycle.py` 7 阶段 + `retryable`/`output_bytes`/`validator_*` + 0006/0007 迁移 | 换了形态（agentgraph/execjournal） | **已对位**（旧版说"缺"，是过期） |
| `partial` / `resume` 语义 | **已有**（`partial` 在 `store.py:33` 终态集；`resume` 走 `rerun_of` → `PHASE_RESUME`） | `adopted` 节点 + ContinueFrom/ForkFrom | 已对位 |
| 写路径仲裁 | `app/subagent/scheduler.py`（**默认关**，`SUBAGENT_WRITE_ARBITRATION`） | 默认开，且并入调度准入 | 相当；2.x **默认值更激进** |
| 并发天花板 | 由预算/配置约束 | **6 / 3，硬上限 32**，有优先级 | 2.x 更明确 |
| 洪水抑制 | `event_sink` 每秒预算 + `IMPORTANT_EVENTS` 免限 | 三层（合并窗口 + 令牌桶 + 字节预算） | 2.x 更细 |
| 测试厚度 | **1.10×** | **1.65×** | 2.x |
| 新能力拼图 | — | `bestof`（N 候选 worktree 择优）、`goaleval`、`schedrun`、`verdict`、`taskcontract`（1.x 该路径仅 1.4 KB → 2.x 46 KB） | 2.x 有新拼图，但**大部分与"本地多 worktree"形态绑定**，不可直接搬 |

> **对 `vendor/规划.md` §3.5 的影响**：R1–R4 的判据已全部落地（本节逐条复核），**R5（Fork / ContinueFrom）
> 需要按 2.x 的形态重新定义** —— 它的对应物现在是 `ContextRequest.ContinueFrom/ForkFrom` +
> `adopted` 节点，而不是 1.x 的 `child_resume` 阶段。

---

## 5. 执行信任与安全

### 5.1 2.x：策略引擎 + OS 沙箱，**两层**

| 层 | 包 | 做什么 |
|---|---|---|
| 策略引擎 | `internal/safety/permission`（26 文件） | 逐调用 allow/ask/deny；判定序 **deny > SessionAllow > ask > allow > 只读⇒Allow > 需人类⇒Ask > Mode**；写栅栏 |
| OS 沙箱 | `internal/safety/sandbox`（53 文件） | macOS Seatbelt / Linux bubblewrap；写根、禁读根、宿主权限；**无后端时 fail closed** |
| 静态 shell 分析 | `internal/safety/shellsafe`（20） | 只读命令表、递归删除拒绝 —— 文档自己声明 *"it is not an OS sandbox"* 并列出绕过 |
| 出站代理 | `internal/safety/egress`（8） | 域允许/拒绝 + 类型化拒绝原因 |

**Windows 没有 OS 沙箱**：`seatbelt_windows.go`（1,106 B）直接返回 `Available() == false`、
命令不加包装、`EgressSupported() == false`。【枚举】

审批模式 5 值（ask/auto/dontAsk/yolo/readOnly）+ 别名归一；**没有 per-tool 的 ask/never/auto 矩阵**，
取而代之是三条规则表 + 一组**任何 posture 都绕不过的硬编码"必须现问人类"集合**
（exit_plan_mode / remember / forget / sandbox_escape / 托管配置写 / 网络出站）。【枚举】

扩展是 **FULL TRUST**：`RuntimeTrustText` 明写 "runtime: FULL TRUST"，
`sidecar/doc.go` 声明启动只能来自"已安装且已启用"的插件包状态（**安装即授权**），
框架自己也说这不构成对恶意 MCP 的遏制。

**最重要的一条**：那套最宏大的设计 `docs/design/TRUSTED_EXECUTION.md`（37,051 B / 545 行）
开头写着 **"Status: proposed. Nothing in this document is shipped unless §11 says so."**，
并且**刻意放在 `docs/design/` 下**，理由是 *"so the product-doc retrieval corpus (`docs/*.md`) cannot
present it to a model as a feature."* —— 我核对了原文。【文档】

### 5.2 对照

| 维度 | Chiron | Reasonix 2.x |
|---|---|---|
| 隔离模型 | **多租户**：进程内强化为默认 + 独立 sandbox 服务为升级路径；明确排除 docker socket | **单机单用户**：策略引擎 + OS 沙箱；Windows 无沙箱 |
| 内核级强隔离 | **没有**（无 namespace/seccomp），文档已写明上限 | 有（Seatbelt/bubblewrap），但仅在 macOS/Linux |
| 审计 | 每条执行路径入审计 + 多副本集中摄取（`POST /v1/internal/audit/exec`），有覆盖清单门禁 | 本地证据/义务账本（`internal/safety/evidence`，67 文件），**设计仍是 proposed** |
| 审批 | 审批票据 + Plan 模式 | 5 模式 + 硬编码"必须现问"集 + 屏障日志 |
| 扩展信任 | MCP 池/租约，按租户计预算 | **FULL TRUST，安装即授权** |

**结论**：这一项上"2.x 更强"只在**单机场景**成立；Chiron 面对的是不受信租户，
`sandbox-service` 的剩余工作是**部署形态**（需真机验证，见 [开发路线图](development-roadmap.md) §2）。
**不要把 2.x 的 OS 沙箱读成"Chiron 该抄"** —— Chiron 的边界问题不是内核级逃逸，是租户间隔离。

---

## 6. 状态、检查点与"回退"

2.x 有 Chiron **确实没有**的一项能力：**文件级回退（rewind）**。

* 粒度：**一个用户回合一个 checkpoint**，存**逐文件的改动前镜像**（`tool.Previewer` 接缝，
  `bash` 天然排除）；可选范围 code / conversation / both。
* 落盘：会话旁挂 `<session-id>.ckpt/turn-<n>.json` + blob，**刻意保持向后可读**。
* **回退是一次带日志的事务**（`TransactionManifest`：prepared/committing/committed/aborted/undone，
  逐文件 compensate，第一次 rename 之前先持久化 `Published` 意图），
  冲突是类型化的（`manual_edit`/`external_change`/`deleted_and_recreated`/`stale_plan`/…）。
* **覆盖率是显式的**：`CoverageGap` 逐条列出"回退不了什么"（`bash_side_effect`/`hook_write`/
  `mcp_external`/`symlink`/`oversized`/…）。保留策略：100 个 checkpoint / 1 GiB / 单文件 32 MiB。
* 版本策略：**向后容忍、无向前拒绝**（v1 读作 `Legacy` + `CoverageLegacy`）；
  **向前拒绝在别处存在**（`sessionstore/session_event_probe.go`、`sessionv4`、`ext/theme`）。【枚举】

**Chiron 的对应物不是同一个东西**：`app/agent/{checkpoint,resume}.py` 是 **run 现场续跑**
（消息级尾部快照 + `SNAPSHOT_SCHEMA_VERSION=1`，`resume.py` **拒绝**更高版本 ⇒ 这一条**已对位**，
旧版说"没有 schema 版本概念"是过期）。Chiron 缺的是「**按回合撤销已被编辑工具改过的文件**」。

---

## 7. 扩展与生态

* **边车协议**：`reasonix.extension.v2`，严格 JSON-RPC 2.0 over NDJSON（stdin/stdout），8 MiB 帧，
  最多 4 个边车共享 30 s 启动预算；未安装 runtime 时走"nil-dispatcher 路径"（零进程、零编码）。【文档】【枚举】
* **插件包**：`reasonix-plugin.json`，`apiVersion` 必须恰为 `reasonix.io/plugin/v2`，**严格解码拒绝未知字段**；
  生成的 `schema.generated.json`（20,367 B）有**漂移检查测试**；市场带审核记录的内容摘要；导出会**剥离凭据**。
* **钩子**：`internal/ext/hook/hook.go` **53,862 B**，测试 **78,854 B**（测试比实现大）；
  17 个冻结挂载点（session/agent/context/provider/tool/permission/compaction/frontend）；
  退出码即裁决（0 通过 / 2 阻断）；**项目钩子需要用户批准**才生效。【枚举】【文档】
* **技能**：`SKILL.md` 目录型，`runAs: inline|subagent`，`allowed-tools`、`read-only`、`requires`；
  **`authority` 字段被显式禁止**（声明即拒绝）；技能自身**不执行任何东西**，
  `scripts/` 只是列出来让模型走正常 `bash` 路径（⇒ 继承权限与沙箱）。

| 维度 | Chiron | Reasonix 2.x |
|---|---|---|
| 插件形态 | MCP 池 + owner 租约 + 租户预算；市场技能（6 个内置 SKILL.md） | 边车进程协议 + 版本化插件包 + 市场客户端；**安装即授权** |
| 钩子 | **无**（`vendor/规划.md` §3.2 已由用户 2026-10-09 立项"追平 DSH 钩子协议"） | 17 个冻结挂载点，测试比实现大 |
| 技能能力面 | 内置技能走 `CHIRON_BUILTIN_SKILLS_PATH`，容器需挂载 | 技能 + 插件包 + 市场 + 主题包，同一套清单 |

**钩子是两个对标物（DSH 与 Reasonix 2.x）共同指向的缺口** —— 它已在 Chiron 的待办里。

---

## 8. 客户端形态与 UI/UX 契约

> **本节是提要**：UI/UX 的逐项对照、量化与被验证过的清单见
> [UI/UX 差距分析](reasonix-ui-ux-gap-analysis.md)。

**四个界面同核**：桌面（Studio）/ TUI / `reasonix web` / ACP 编辑器 —— 都是同一个 Go 内核，
`internal/frontend/`（748 文件 / 4.29 MB）分别实现 `tui`(87) `cli`(205) `serve`(337) `acp`(47) 等。

**桌面不是"Electron 里嵌 Go"**：`desktop/`（1,039 文件 / 17.33 MB）是**嵌套 Go module**
（`reasonix/desktop`，`replace reasonix => ../`，保持 CLI 的 `CGO_ENABLED=0` 静态保证），
Electron 主进程**spawn 并持有** `cmd/reasonix-studio-host`，SPA 经 **127.0.0.1 回环**访问内核的
HTTP/SSE；启动凭据是每次启动的 `crypto/rand` 32 字节 + 严格 Host/Origin 校验。
前端是 React 19 + TS 7 + Vite 8。【枚举】

**真正的差距是"把契约变成门禁"**（这与旧版结论一致，但 2.x 把它制度化了两层）：

1. **`docs/DOCS_STANDARD.md`（4,286 B）是文件级规格**：必备头部（owner/backup/status/reviewed）、
   按类型规定的必备章节（Contract → Scope/Rules/Enforcement；Runbook → Owners/…/Recovery）、
   ≤320 列、MUST/SHOULD/MAY、**"只描述当前行为，历史进 commit"**、
   **"行为被删则同一次改动删文档"（C1）**、`tools/repolint/baseline.json` **只许下降**（C3）。
   **中文副本被限定为恰好三份**：`README.zh-CN.md`、`docs/GUIDE.zh-CN.md`、`docs/CLI.zh-CN.md`
   —— 实测全仓 `.zh-CN.md` **正好 3 份**。由 `make check` + CI `repolint` 执行。
2. **UI 走向机器可验**：`desktop/frontend-next/tools/ui-census/`（`gate.mjs` + `report.mjs` 42 KB +
   `golden/fx-*.txt`）接 `pnpm census` / `census:verify` —— 用**金标准快照**而不是肉眼比对。
3. **手工台账也有明确边界**：`docs/STUDIO_PARITY.md` 逐行登记 1.x 桌面与 Studio 的差异
   （Have / Have differently / Missing / Removed on purpose / Unverified），
   并**自陈 "Held by review, not by a gate: no check reads this file."** —— 诚实，但也可搬运。
4. **1.x 的 `TRANSCRIPT_*` 契约文档族在 2.x 全部消失**，transcript 语义并入 `docs/SPEC.md` §3.6；
   主题族拆成 `docs/THEME_PACK.md` + `docs/THEME_ASSETS.md` + `docs/THEME_AUTHOR_GUIDE.md` +
   机器 schema。【枚举】

| 维度 | Chiron | Reasonix 2.x |
|---|---|---|
| 聊天组件 | `components/chat/` 70 文件（34 测试 + 36 非测试，20 个 `.vue`）/ 542,914 B | 桌面 SPA `src/ui` 570 文件 / 3.29 MB |
| transcript | `transcript*.ts` **7** 个（[transcript 契约](transcript-contract.md) §0 的模块地图共 10 行 = 7 个 transcript + `chat-types`/`chat-history` + 2 个组件；旧版把这 10 行读成了"10 个模块"） | 语义进 `docs/SPEC.md`；无常驻契约文档族 |
| UI 契约门禁 | `check:ui` **7** 个脚本（旧版写 4 个、中间版写 6 个）+ `docs/transcript-contract.md` | DOCS_STANDARD + repolint + ui-census 金标准 |
| 语言 | 3（zh-CN / en-US / ar）+ 键集检查 | 3（en / zh / zh-TW）+ 字面量 parity 测试 |
| 文档规格 | 无（`docs/` 17 份，风格自定） | **有，且入 CI** |
| 中文文档 | 项目主语言 | **限定 3 份**（其余必须英文） |

> 依 `vendor/规划.md` §6：**不把 UI/UX 差距读成"要重写渲染"**。这里的可执行项是**文档化 + 机器化**。

---

## 9. 平台面：多租户 / 配额 / 计费 / 身份 —— **Chiron 独有**

**结论：2.x 没有多租户隔离、没有产品级配额、没有计费。它是严格的单机单用户**
（一个 OS 用户；威胁模型只列一个 `User` 主体）。【枚举】+【grep】

需要纠正的一个直觉：**`workers/accounts/` 看着像"多租户后端"，其实是身份服务**。

* 它是 `id.reasonix.io` 上的 Cloudflare Worker（Hono + D1），做邮箱/密码注册、邮箱验证、
  session、密码重置、公开资料、**RFC 8628 风格的设备登录**（CLI/桌面免浏览器跳转）。
  D1 只有 4 张表（users/sessions/email_tokens/device_grants）；
  grep `quota|billing|tenant|plan|subscription|seat` 于其 `src/` ⇒ **0 命中**。
* Go 侧只有**客户端**（`internal/platform/account`），包注释：*"An account is never required to run Reasonix."*
  唯一的"配额"是配置**备份**的服务端限制（客户端只读元数据）。
* `docs/BILLING.md` 讲的是**成本展示**（费率卡、显示币种、钱包余额查询），不是收费；
  `docs/USAGE_CATALOG.md` 是**本机 SQLite** 的用量汇总，自陈非权威。
* Go 侧的 `tenant` 命中 15 处，全是 MCP URL 夹具与脱敏键表；`quota` 命中里没有产品配额。
* Studio 的 shell 边界文档写明：内核只监听 `tcp4 127.0.0.1:0`，每次启动的随机凭据
  ⇒ **回环单用户**。（`docs/STUDIO_SHELL_BOUNDARIES.md`）

**因此**：Chiron 的租户中间件、配额池（`ent_quota_pools`/`ent_quota_allocations`）、
计费记录、雪花 ID、多副本 run 归属、SSE 逻辑事件去重、MCP owner 租约
**在这一对标物里找不到参照** —— 它们是 Chiron 自己的工程积累，应当按 `docs/dsh-gap-analysis.md` 的
"切口"方式自证，而不是拿 Reasonix 当尺子。

---

## 10. 值得追 / 不值得追

### 值得追（按性价比，且**都不引新依赖**）

| 项 | 依据（§） | 成本 | 与既有约束的关系 |
|---|---|---|---|
| **钩子协议** | §7（17 个挂载点，测试比实现大） | 中 | **已立项**（`vendor/规划.md` §3.2，2026-10-09 用户指示）；落地前先出事件清单与执行边界设计 |
| **文件级回退（rewind）** | §6（事务化 + 覆盖率显式 + 冲突类型化） | 中高 | 新能力，需迁移；形态须改成"引擎侧持久化 + 网关端点"，**不要照搬会话旁挂目录** |
| **事件的"可持久化/仅实时"分层** | §3.1（`NonPersistable` + `Seq`） | 低 | 直接服务 Chiron 已有的 SSE 去重与重放；无需新表 |
| **压缩的"结构化 noop 码"** | §3.2（7 个码 + 进度事件） | 低 | 把"为什么这次没压缩"从日志句子变成机器可读码 |
| **子 agent 生命周期的机器化边界** | §4.1（反射断言字段集） | 低 | 对应 `vendor/规划.md` §3.3-1（收窄是否对模型可见）的产品决定 |
| **文档规格门禁** | §8（DOCS_STANDARD + repolint + C1/C3） | 低 | 纯文档；与既有 `scripts/check_doc_links.py`（基线只应缩小）同构，可共用"基线化 + 拦增量"的成熟做法 |
| **UI 的机器契约（金标准快照）** | §8（ui-census） | 中 | 补足"只能靠肉眼验证"的领域；**不重写渲染** |
| **扩展/插件的信任披露文本** | §5.1（`RuntimeTrustText`） | 低 | 与 Chiron 的 MCP 审批面互补 |

### 不值得追（与 `vendor/规划.md` §6 一致，此处只补 2.x 的新证据）

* **不做 Electron 桌面**：2.x 的 `desktop/` 是 17.3 MB / 1,039 文件 + 独立 host 进程 + 签名自更新
  —— 与"多租户 SaaS + Web"定位冲突，且**收益全在本地分发**；
* **不改写成纯 Go**：2.x 用 Go 是为了**单静态二进制分发**（`CGO_ENABLED=0`），
  Chiron 的引擎价值在被租户共享的 Python 生态（MCP/RAG/工具），语言不是能力；
* **不照搬 TUI**：`internal/frontend/tui` 87 文件是本地交互取舍；
* **不引 LangGraph / LangSmith**：2.x 也是自研内核，没有引入；
* **不引向量库**：2.x 的记忆检索是 **Markdown 事实 + BM25（含 CJK bigram）**，
  **没有 embedding/向量库**（`docs/SESSION_MEMORY_RETRIEVAL.md` 明说不需向量库）
  —— Chiron 已有 Milvus，这不是差距；
* **不照搬 `bestof`/worktree 类能力**：它们建立在"本地 git 工作区"形态上，与多租户 SaaS 不同构；
* **不追"1.x 桌面功能对齐"**：`docs/STUDIO_PARITY.md` 是 2.x 自己的内部台账，与 Chiron 无关。

---

## 11. 旧版本文档的过期清单（23 条断言复核）

旧版 `docs/reasonix-gap-analysis.md`（对照 1.x）的 Chiron 侧断言：**10 确认 / 3 错 / 9 过期 / 1 部分过期**。

| 旧断言 | 判定 | 今天的证据 |
|---|---|---|
| `docs/` 8 份，且无 transcript 契约 | **过期** | `docs/` **17** 份 `.md`；`docs/transcript-contract.md`（**10,335 B**，2026-10-09 复测；原记 8,926 B）存在 |
| 引擎事件"~13 种" | **过期** | `python-engine/app/agent/runtime.py`（**159,375 B / 3,274 行**，2026-10-09 复测；原记 157,589 B / 3,237 行）**15** 个 `type` 字面量 |
| 思考内联在 `text` 的 `[thinking]…[/thinking]` | **过期** | `runtime.py:1542-1545` 独立 `type="thinking"`；前端 `ChatView.vue:2157-2160` |
| 三处各自切分思考 | **部分过期** | 三处仍在（`evals/observe.py` / `acp_adapter/mapping.py` / `chat-types.ts`），但**只针对模型自产的标记**；native reasoning 不再需要切分。实际切分点已增至 **5** 处（+`internal/cli/run.go`、`internal/api/submit_handler.go`） |
| "无消息完成事件" | **确认** | 15 个字面量里没有 `message`；`done` 只带 usage/model/trace_id |
| "取消是 `cancelled` 事件" | **确认** | `runtime.py:1421`；`acp_adapter/mapping.py:200-201` |
| "无事件级持久化；checkpoint 是消息级快照" | **确认** | `migrations/versions/0001_authoritative_baseline.py` ~80 张表无事件表 |
| "SSE 重放靠 Redis TTL 1h" | **确认** | `internal/broadcast/hub.go:37` `sseEventsTTL = time.Hour` |
| "checkpoint 快照无 schema 版本概念" | **过期** | `app/agent/checkpoint.py:67` `SNAPSHOT_SCHEMA_VERSION = 1`；`resume.py:202-212` **拒绝**更高版本 |
| `SubAgentRunner.__init__` 21 个参数 | **错** | `subagent_runner.py:225-262` = **20** 个（1 位置 + 19 关键字） |
| `subagent_runner.py` 单类"约 700 行" | **错** | 文件 **1,258** 行；`class SubAgentRunner` = L219–L1170，**952** 行 |
| `app/subagent/` 13 模块 64 KB | **过期** | **17** 个 `.py` / **157,249 B** |
| `event_sink` 8 种事件 + `IMPORTANT_EVENTS` | **确认** | `app/agent/event_sink.py:43-56` |
| "缺 `partial`/`resume` 与 `retryable`/`output_bytes`/`validator_*`" | **过期（全部已有）** | `app/subagent/lifecycle.py:36-52`、`store.py:33`、`subagent_runner.py:795/317-327`、迁移 `0006`/`0007` |
| `subagent_runs.status` 含 cancelled 与 lost | **确认** | `internal/api/subagent_cancel.go:253-258` |
| `ProfileSpec.allowed_tools/disallowed_tools` 天花板 | **确认** | `app/agent/profile.py:58-60` |
| "缺调用侧收窄与取交集" | **过期** | `subagent_runner.py:249` `call_tools`；L1041-1061 `names &= self._call_tools`（**但未对模型暴露**） |
| 聊天组件字节（50.7/47.4/43.4/42.5/30.1 KB） | **确认** | 51,961 / 48,589 / 44,446 / 43,498 / 30,851 B |
| "35 个聊天组件" | **过期** | `components/chat/` 70 文件；**36** 个非测试文件 |
| "transcript 体系 10 个模块" | **错（口径）** | 旧文只点名 **7** 个，且 `transcript*.ts` 恰好 **7** 个；"10"是 [transcript 契约](transcript-contract.md) §0 地图的行数（含 3 行非 transcript） |
| transcript 模块字节（15.4/6.9/5.1/2.3 KB） | **确认** | 15,817 / 7,025 / 5,177 / 2,327 B |
| `check:ui` 四个契约脚本 | **过期** | `frontend-vue/package.json` → **7** 个 |
| 3 语言 + 键集检查 | **确认** | zh-CN / en-US / ar，各 14 文件 |

**vendor 侧证据全部失效**（这是"换分支"的代价）：`internal/event/event.go`、`internal/turnevent/ledger.go`、
`internal/agent/profile_spec.go`、`internal/event/subagent_lifecycle.go`、
`TRANSCRIPT_ARCHITECTURE/PROJECTION/SCROLL_CONTRACT/OUTLINE_NAVIGATION/V2/ACCEPTANCE_9777`、
`REASONING_CONTRACT`、`chat_tui.go`(166.6 KB) —— 在 `studio` 上**均不存在**。
（`internal/turnevent` 不只是被移动：它在 2.x 的祖先里就**没有**；"ledger 的后继"这一说法**未获证实**。）

---

## 12. 复核时新发现的 Chiron 侧接线缺口（静态读出，**未跑运行时**）

这五条不是"比 Reasonix 少什么"，而是**复核过程中撞见的自家不一致**，按 `docs/development-roadmap.md` 的口径
属于"有实现 ≠ 有验证"。**最初五条均未做运行时复现**；**2026-10-09 已逐条收口**：
**1 / 2 / 3 / 4 复现并修好**（见各条里的"已修"与取证），**5 复核后不成立**（清单自身过期）。

1. **native `thinking` 事件没有落库 —— 刷新后思考丢失** ✅ **已修（2026-10-09）**
   引擎发独立 `thinking` 事件（`python-engine/app/agent/runtime.py:1542-1545`），网关**通用转发**
   （`internal/api/submit_handler.go:454` 发布每个 `evt.Type`），
   但累计落库只处理文字：`submit_handler.go:439` 用 `strings.HasPrefix(evt.Content, "[thinking]")`
   判思考、`:451-453` 只把 **`evt.Type == "text"`** 的思考累加进 `finalContent`，
   **没有 `case "thinking"`**。⇒ 实时看得到，历史回放只还原模型自产的 `[thinking]` 标记。
   **修法**：`submit_handler.go` 增加 `if evt.Type == "thinking" && evt.Content != ""` 分支，
   按**既有历史格式**包回 `[thinking]…[/thinking]` 再累加（前端 `splitThinking(loose)` 据此还原，回放格式不变）。
   **取证**：`internal/api/submit_thinking_persist_live_test.go`（假引擎发 `thinking`/`text`/`done` + **真 PG** 落库，
   把消息**读回来**断言含 `[thinking]先想一下[/thinking]`）；**变异验证**：去掉该分支 ⇒ 用例红并报
   `思考没有落库（刷新后会丢）—— content="答案"`。
2. **`partial` 是"半接入"的终态** ✅ **已修（2026-10-09，两半都修）**
   引擎 `app/subagent/store.py:33` 把 `partial` 列为终态，而 Go 侧 `internal/api/subagent_cancel.go:253-258`
   的 `terminalRunStatuses` 与 Python 侧 `app/subagent/reporting.py:38` 的 `REPORTABLE` 都**没有** `partial`
   ⇒ 该 run 在库里是终态，却既不可判终、也不会进上报。
   **修法**：两处都补上 `partial`（Go 那半的后果是"取消已终态的 run 会假成功"；
   上报那半的后果是"**有产出的部分完成永不上报**"）。**已机械化**：`scripts/check_tool_policy_parity.py`
   第八族现在做**三方比对**（Go `terminalRunStatuses` ↔ `_TERMINAL_STATUSES` ↔ `REPORTABLE`），
   加它的当场就红（`只在 Python ['partial']`）；变异验证：任一处删/加状态 ⇒ 均 exit 1。
3. **生成的 ORM 模型未跟上迁移** ✅ **已修（2026-10-09）**：`shared/models/subagent_run.py` 里
   `retryable` / `output_bytes` / `validator_*` / `error_code` / `inherited_messages` **均无**
   （grep 0 命中），它们只存在于迁移 `0004`/`0006`/`0007` 与手写 SQL ⇒ ORM 与迁移漂移。
   **修法**：ORM 是**生成物**（`scripts/generate_orm_models.py` ← `configs/orm/V1/models.yaml`），
   所以先按迁移里的**权威定义**把这 7 列补进 yaml（`inherited_messages` integer ·
   `retryable` boolean default false · `output_bytes` bigint default 0 ·
   `validator_mode`/`validator_outcome` varchar(16) · `validator_attempt` integer default 0 ·
   `error_code` varchar(32)），再跑生成器。
   **注意**：生成器会**给全部 71 个模型文件刷新"生成时间"表头**（69 文件被改，实为噪声）⇒
   只保留 `subagent_run.py` 的真实变化、其余从备份回退（现 diff = **1 文件 / 26 插入**）。
   **影响面**：引擎**不 import `shared.models`**（`python-engine/` 零命中，走手写 SQL）⇒ 这是
   "**生成物与 schema 不一致**"的诚实性问题，不是运行时缺陷；`shared/` 也不在 ruff/mypy 门禁内。
4. **一处代码引用命中"已知断链基线"** ✅ **已解决（2026-10-09）**：`internal/api/subagent_cancel.go`
   那处引用已改为**就地说明**（指向活着的 `app/subagent/store.py` 终态白名单）；
   `scripts/doc_link_baseline.txt` 现已**清空（0 条）** —— 19 条存量债务全部还清（6 份按代码重建、
   2 条剔除为守卫误报、其余删冗余引用）。
5. **`docs/dsh-gap-analysis.md` 引用的 `internal/api/session_cancel.go` 已不存在** ✅ **复核后不成立（2026-10-09）**：
   该文档**当前并不引用**那个路径（全仓库只有本条自己写着它）—— 它引的是
   `internal/api/session_cancel_test.go`（**存在**）。取消逻辑确实在
   `internal/api/session_coord.go`（`CancelSessionBroadcast`）与 `routes_agent.go`，与描述一致。
   ⇒ 这条是**清单自身过期**（该引用在清单写下之后已被修掉），不是待办。

---

## 13. 复现方式与证据索引

### 13.1 复现本次对照

```bash
# 1) studio 只在上游；fork 的 origin 没有该分支
cd vendor/DeepSeek-Reasonix
git remote add upstream https://github.com/esengine/DeepSeek-Reasonix
git fetch upstream --prune
git merge --ff-only upstream/studio      # 本次 HEAD: c47bdfd84

# 2) 两条线的分叉量
git rev-list --left-right --count upstream/main-v2...upstream/studio   # 2684  3235
git diff --shortstat upstream/main-v2 upstream/studio                  # 13300 files, +826096 -1447823

# 3) 量表（避免 Get-ChildItem -Recurse：仓库外有 junction 会走到 X:\.pnpm-store）
git ls-tree -r --name-only upstream/studio | Select-String '\.go$'      # 4528（_test 2312）
```

> ⚠️ **本机陷阱**：控制台按 GBK 渲染中文 ⇒ **别信回显**；`Get-ChildItem -Recurse` 会被 junction
> 带出仓库（本次实测走散到 `X:\.pnpm-store`）。核数字一律用 `git ls-tree` / `read` 工具。

### 13.2 2.x 侧关键证据（`vendor/DeepSeek-Reasonix/`）

| 路径 | 字节 | 用途 |
|---|---|---|
| `internal/contract/event/event.go` | 33,038（678 行） | 34 Kind 枚举 + `Sink` |
| `internal/contract/eventwire/wire.go` | 33,682（800 行） | Kind → 线名（34 项）+ 投影 + `NonPersistable`/`Seq` |
| `internal/session/control/turn_orchestrator.go` | 27,162 | 回合归属唯一入口 |
| `internal/session/control/turn_gate.go` | 2,932（75 行） | 准入状态机 |
| `internal/session/control/adjudication.go` | 11,278 | 屏障日志（fsync → 才提问） |
| `internal/runtime/agent/compact.go` / `compact_projection.go` | 28,625 / 30,994 | 压缩状态机与拒绝码 |
| `internal/state/trajectory/recorder.go` | 21,741（558 行） | 审计流（`SchemaVersion=1`，合并上限 256） |
| `internal/runtime/delegation/` | 32 实现 242,570 + 55 测试 399,307 | 委派全域（1.65×） |
| `internal/runtime/delegation/profile_spec.go` | 14,547 | `ProfileExecSpec` 五元组 |
| `internal/runtime/delegation/profile_boundary_test.go` | 5,923 | **反射断言字段集** |
| `internal/runtime/writeclaim/scheduler.go` | 11,087 | 写路径仲裁（6/3，上限 32） |
| `internal/contract/agentgraph/graph.go` | 7,790 | `NodeState`/`EdgeKind` |
| `internal/state/execjournal/journal.go` | 14,736 | 委派执行日志（含派生 interrupted） |
| `internal/safety/{permission,sandbox,shellsafe,egress}` | 26 / 53 / 20 / 8 文件 | 双层执行安全 |
| `internal/safety/sandbox/seatbelt_windows.go` | 1,106 | **Windows 无 OS 沙箱**（`Available()==false`） |
| `internal/state/checkpoint/types.go` / `transaction.go` | 12,520 / 51,788 | rewind（事务化） |
| `internal/ext/hook/hook.go` | 53,862（测试 78,854） | 17 个冻结钩子点 |
| `internal/ext/extension/sidecar/doc.go` | 25 行 | "安装即授权"的启动构造 |
| `internal/frontend/`（tui/cli/serve/acp） | 748 文件 / 4.29 MB | 四界面同核 |
| `desktop/`（electron + frontend-next） | 1,039 文件 / 17.33 MB | Electron 壳 + React 19，非 CGO |
| `workers/accounts/` | 106 文件 | **身份**服务（非租户/计费） |
| `docs/DOCS_STANDARD.md` | 4,286 | 文档规格（入 CI） |
| `docs/STUDIO_PARITY.md` | ~3.5 KB | 自陈"无门禁，靠评审"的对齐台账 |
| `docs/design/TRUSTED_EXECUTION.md` | 37,051（545 行） | **Status: proposed** |

### 13.3 Chiron 侧相关

[架构与请求链路](architecture.md) · [运行现场 checkpoint 续跑设计](run-checkpoint-design.md) ·
[拆分评估](split-assessment.md) · [贡献与验收流程](contributing.md) ·
[多实例部署指南](deployment-multi-instance.md) · [开发路线图](development-roadmap.md) ·
[与 DSH 的差距分析](dsh-gap-analysis.md) · [transcript 契约](transcript-contract.md) ·
[UI/UX 差距分析](reasonix-ui-ux-gap-analysis.md)

---

## 14. 一句话总结

2.x 与 1.x 的共同点比差异更说明问题：**它把"约定"尽量变成"类型、测试、门禁或机器可读的码"**
（34 种追加式事件 + 独立线协议层、反射钉住的委派边界、DOCS_STANDARD + repolint + ui-census 金标准、
7 个压缩拒绝码、显式的覆盖率缺口）。Chiron 的**能力面并不落后**（记忆基建、技能执行、多租户生产面、
运行恢复、平台计量在 2.x 找不到对应物），差距在**把纪律固定下来的形式**，以及**测试厚度**
（同域 1.10× vs 1.65×，全仓 0.20× vs 1.19×）。

最划算的方向仍是同一个：**不引新依赖，把已经写对的东西写成类型、测试或文档**；
外加两条本轮新增的可执行项 —— **钩子协议**（**批次 1 / 2a 已落地**，2b/3/4 待拍板，见 `docs/hook-protocol-design.md`）与**文件级回退**（新能力，需设计）。
