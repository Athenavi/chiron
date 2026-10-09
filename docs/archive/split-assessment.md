# 归档：split-assessment.md

> 全文归档（2026-10-09）。`docs/split-assessment.md` 现为**结论+行动项摘要**，正文在此。
> 移动原因：`docs/` 只保留绝对核心且当前有效的文档；这类对照/评估的完整过程留档备查。

---

# 巨型文件拆分评估（L3-4）

> 结论：**建议拆**，但只按「纯移动 + 零行为变更」分步做，每步测试绿。
> 本文只给边界与契约，**不含代码改动**（评估项，未启动）。
> 所有体量与行号均为本机实测（2026-09-28，`X:\project\Chiron`）。

## 1. 扫描口径

按体积降序取第一方源码；以下排除项**明确不改**：

| 排除 | 理由 |
|---|---|
| `vendor/**` | 第三方源码（`DeepSeek-Reasonix`、`ZCode`）；占满体积榜前 18 名，不归本项目维护 |
| `frontend-vue/src/locales/**`（约 1000 行/语言） | 文案数据，按语言分文件本就是正确形态 |
| `migrations/versions/0001_authoritative_baseline.py`（**1,495 行**，2026-10-09 复测；原记 1475 行） | 已发布迁移**不可变**，拆分等于改写历史 |
| `shared/models/**` | ORM 生成物；要改就改生成器（见 L4-3） |
| `node_modules/`、`dist/`、`__pycache__/` | 构建产物 |

实测体量（`symbols` = 顶层 `func/type` 或 `def/class` 或 `function/const ref|computed` 计数）：

| 文件 | 行数 | KB | symbols | 主要矛盾 |
|---|---|---|---|---|
| `frontend-vue/src/views/ChatView.vue` | 3436 | 145.1 | 155 | 单文件承载整条对话链路 + 侧栏 + 导出 |
| `python-engine/app/agent/runtime.py` | 3275 | 155.6 | 27 | 一个 `AgentRuntime` 类占大头（旧表的 1841/1557 已过期） |
| `frontend-vue/src/views/MediaView.vue` | 1770 | 56.8 | 88 | 列表/筛选/多选/面包屑混在一起 |
| `python-engine/app/memory/service.py` | 1667 | 63.9 | 10 | `MemoryService` 单类 |
| `frontend-vue/src/views/WorkflowView.vue` | 1598 | 57.8 | 73 | 画布 + 模板 + 实例 + 执行日志（**执行/轮询簇已抽**，见 §2.5） |
| `frontend-vue/src/components/chat/ChatInput.vue` | 1259 | 49.5 | 66 | 输入 + 附件 + 模型选择 + 提及 |
| `frontend-vue/src/components/chat/ChatSidePanel.vue` | 1180 | 46.3 | 44 | 用户菜单 + 轨迹 + 会话列表 + 抽屉拖拽 |
| `python-engine/app/queue/worker.py` | 1174 | 49.7 | 4 | `QueueWorker` 单类 |
| `python-engine/app/main.py` | 2141 | 96.0 | 9 | 启动/关闭集中（已抽 7 个函数；`lifespan` 现 **540 行** —— 2026-10-09 用 `ast` 复测定义在 `88-627`；原记 ~560 行） |
| `internal/api/mail_handler.go` | 696 | 25.3 | 32 | ✅ **已拆**（1488 → 695，4 个文件） |
| `internal/api/gateway_router.go` | 500 | 21.7 | 7 | ✅ **已拆**（1254 → 442，11 个 `routes_*.go`；现 500 含后续新增路由） |
| `internal/session/manager.go` | 129 | 3.5 | 8 | ✅ **已拆**（1198 → 128，4 个文件） |

> **测得 2026-10-08（round 43）**：符号口径同上（Go `^func|^type`、Python `^(def|class)`、Vue/TS `^(function|const X = ref|computed)`）。此前表里的数字有多处过期到**不可用**（`runtime.py` 1841 → 实际 3238、`session/manager.go` 1098 → 已拆成 129），**引用规模前必须现场测量**。
>
> **复测 2026-10-09**：行数与 KB **逐条复测**，**4 处已过期并已更正** —— `ChatView.vue` 3429→**3436** / 141.6→**145.1 KB** · `runtime.py` 3238→**3275** / 150.7→**155.6** · `WorkflowView.vue` 1637→**1598** / 58.1→**57.8**（又抽走了一批） · `main.py` 2132→**2141** / 93.3→**96.0**；其余 **8 条逐字节对上**（顺带确认了两个口径：**KB = 字节数 ÷ 1024**、**行数 = 换行符数 + 1**）。**`symbols` 列未能复现**：按上表字面规则复测系统性偏低（`ChatView.vue` 155 vs 130 · `MediaView.vue` 88 vs 74 · `WorkflowView.vue` 73 vs 56 · `ChatInput.vue` 66 vs 60 · `ChatSidePanel.vue` 44 vs 37），说明当时那条口径比字面更宽 ⇒ **该列仅作参考，引用前请按自己的规则重测**（Go/Python 三列复测一致，差异只在 Vue/TS）。

## 2. 边界建议（按收益排序）

### 2.1 `gateway_router.go` — 最低风险，**建议先做**

实测结构：middleware 基础设施 `32–131`；`NewGatewayRouter` `132–503`（372 行，只做装配）；`registerXxxRoutes` 共 10 个 `504–1148`（`public` 504 / `agent` 565 / `auth` 814 / `system` 832 / `conversation` 881 / `media` 913 / `userMarket` 943 / `plugin` 950 / `billing` 961 / `proxy` 979 / `admin` 1149）。

- 边界：**按业务域拆成 `routes_<domain>.go`**，`gateway_router.go` 只留 middleware + `NewGatewayRouter`。
- 契约：各 `registerXxxRoutes` 签名与调用点**一字不改**，仅换文件；同包（`package api`）→ 无需改调用方。
- 风险：极低（纯移动、同包私有符号仍可互访）；唯一注意点是别顺手改函数体。

**✅ 已完成（批次 1）**：拆成 11 个 `routes_<domain>.go`（public / agent / auth / system / conversation / media / market / plugin / billing / proxy / admin），`gateway_router.go` 只留 imports + middleware + `NewGatewayRouter`（**1254 → 442 行**）。验收：**逐函数文本与拆分前逐字一致**（16/16 个顶层函数，无丢失 / 无内容变化 / 无新增）· `go build/vet/test -mod=mod ./...` 全绿 · `check_source_encoding.py` 通过。

> 坑：`goimports` 在**整包编译不过时不会补 import**（它需要包可加载）—— 拆完就是编译不过的状态，于是它静默无输出。改用「按限定符静态推导 import + `go build` 兜底」。

### 2.2 `runtime.py` — 压缩工具簇天然独立

> **行数会持续过期**：本文写作时 `runtime.py` **1841 行**；**2026-10-09 用 `ast` 复测 = 3274 行**
> （`AgentRuntime` **2352 行** · `run_agent` 在 `:3163` · `register_pending_approval` 在 `:3224`）——
> 原写「当前 3370 行（`AgentRuntime` 2315 行）」也已过期。
> 下面 ② 的**类型部分已完成**（`runtime_types.py`）；① 的压缩簇必须先按现状**重新定界**再动 ——
> 启动前务必重新实测行号，不要照搬旧数字。

实测结构：上下文/消息处理函数簇 `50–468`（`_restrict_tools_to_plugins`…`_ensure_valid_tool_sequence`，约 420 行，彼此只依赖 `messages` 结构）；数据类型 `AgentTask` 469 / `AgentEvent` 505 / `ApprovalTicket` 530；**`AgentRuntime` 574–2131（1557 行）**；模块级入口 `run_agent` 2132 / `register_pending_approval` 2193。

**✅ 已完成（批次 2 · 类型部分）**：`AgentTask` / `AgentEvent` / `ApprovalTicket` / `ApprovalDecision` 与审批决策取值（`DECISION_*` / `VALID_DECISIONS`）移到 `app/agent/runtime_types.py`（**3370 → 3198 行**，新模块 207 行）。验收：AST 取源码段逐字比对 —— **HEAD 34 个顶层定义 → 4 个移动、30 个留在原处，全部文本一致、常量完整**；`ruff` 0 · `mypy app/ acp_adapter/` 0（242 文件）· `pytest -m "not integration"` 全绿。

> 坑：`runtime.py` 里的再导出必须是**逐条 `X as X`** —— mypy 的 `no-implicit-reexport` 只认 `as` 别名，而本仓库 ruff 的 isort（`combine-as-imports=false`）会把带别名的再导出拆成逐条；写成一行或多行合并都会被其中一方判红。另外拆分后 `dataclasses.field` 在 `runtime.py` 里已无人使用，需要随之删掉（ruff F401 会指出）。

- 边界（分两步，先易后难）：① 拆出 `runtime_compaction.py`（`50–468` 那簇，纯函数、无状态）；② 拆出 `runtime_types.py`（三个 dataclass）。
- 契约：`runtime.py` 保留同名再导出（`from .runtime_compaction import *` 或显式 `__all__` 转发），**避免改动任何既有 import 路径**；测试 `tests/` 中引用旧路径的地方不动。
- `AgentRuntime` 本体暂不动：1557 行内聚在"一次 run 的状态机"上，拆它必然触碰行为，不属于本项范围。

### 2.3 `main.py` — `lifespan` 分步化

实测结构：依赖取用器 `53–202`（`get_plugin_pool`/`get_redis`/`get_gateway`/`touch_user`/`verify_sandbox_root`/`verify_media_store`/`_is_inside_cwd`）；**`lifespan` 203–761（559 行）**；`_get_instance_id` 762、`_resolve_attachments` 779–914（136 行）、`_setup_middleware` 915、`_setup_routes` 936–1086；处理器 `agent_run` 1087、`_workflow_context_stream` 1136、`agent_submit` 1181–1571（390 行）、`agent_approval` 1572。

- 边界：① 依赖取用器 + 校验搬到 `app/deps.py`（`main.py` 再导出）；② `lifespan` 内部按"启动步骤"抽成私有 async 函数（如 `_start_pool`/`_start_redis`/`_start_worker`），**执行顺序与异常语义保持逐行等价**。
- 契约：`app.state` 上挂的键名、`lifespan` 的 `yield` 前后顺序、`logging` 输出顺序都不动；顺序一乱就是行为变更。
- 风险：中。启动顺序有隐含依赖（Redis → 网关 → 队列 worker），必须整段对照后再动。

**✅ 已完成（批次 4 · ① 校验部分，2026-10-08）**：`verify_sandbox_root` / `verify_media_store` / `_is_inside_cwd` 移到 `app/deps.py`（`main.py` 再导出，既有 `app.main.verify_sandbox_root` 引用不变）。**`main.py` 2170 → 2061 行**。验收：AST 取源码段**逐字一致**（3/3）· `ruff` 0 · `mypy` 0（244 文件）· `pytest -m "not integration"` 1905 passed。

**⚠ ② `lifespan` 分段化此前被低估了两个坑（实测后写在这里，动手前必须读）**：

1. **`locals()` 探测**：关闭段用 `if '_metrics_task' in locals()` / `'_retention_task' in locals()` / `"_engine_registry" in locals()` 判断"启动时到底建没建"。把这些段**抽成函数**后，这些名字不再出现在 `lifespan` 的 locals 里 ⇒ 关闭逻辑会**静默跳过**（不报错、不告警，只是不再取消任务/注销实例）。正确做法是让 `_start_*` **返回句柄**（或返回一个 dataclass），由 `lifespan` 持有再传给关闭段 —— 即"① 引用变 ② 显式数据流"，不是纯移动。
2. **模块级状态靠 `global` 改写**：`_redis` / `_gateway` / `_plugin_pool` / `_queue_worker` 等是 `main.py` 的模块全局，`lifespan` 里用 `global` 赋值。**依赖取用器**（`get_redis`/`get_gateway`/`get_plugin_pool`/`touch_user`）不能单独搬到 `deps.py`：跨模块后"`global` 改的是哪个模块的名字"会变得难以判断（搬状态则要改所有写入点，是行为变更）。所以 ① 只搬了**不依赖这些状态**的校验函数。

> 结论：② 仍待做，且**必须先设计句柄传递**；把"分段"当成纯移动会静默破坏关闭路径。

**✅ 已完成（批次 4 · ② 的第一步：关闭段，2026-10-08）**：关闭清理抽成 `_shutdown_lifespan(*, reconciler_task, metrics_task, retention_task, engine_registry)`，**句柄显式传入**；`lifespan` 顶部把这四个句柄显式初始化为 `None`（此前三个只在条件分支里赋值）。**`lifespan` 664 行**（原 ~740）。关闭路径此前**零覆盖**，本次补了 `python-engine/tests/test_shutdown_lifespan.py`（3 条：句柄被取消/停止 + 传 None 安全通过 + **源码级护栏**：`lifespan` 里不得再出现 `locals()` 探测）。**启动段（约 560 行）仍待按同一模式分段** —— 它们之间也有跨段局部变量，抽取前先做依赖分析。

**✅ 已完成（批次 4 · ② 第二刀：6 个无耦合启动段，2026-10-08）**：用**双向依赖分析**（脚本 `.tmp` 已删，方法写在这里）挑出既无"外流局部名"也无"内依赖更早局部名"的段，抽成同名函数：`_install_global_exception_handler` · `_start_redis` · `_start_postgres` · `_register_workbench_capabilities` · `_start_mcp_pool` · `_start_subagent_governance`。**`lifespan` 664 → 561 行**（main.py 2085 → 2118，多出的是各函数表头与说明）。

- **关键坑**：段内给模块全局赋值时（`_redis` / `_plugin_pool` …），lifespan 顶部的 `global` **不会跟着搬** —— 新函数必须自己声明，否则赋值变成局部变量、**模块全局永远为空**，而启动路径未必有测试覆盖。抽取脚本已按"段内赋值 ∩ lifespan 的 global 声明"自动补上。
- **顺带发现（靠"真的进一次 lifespan"的尝试）**：关闭路径在调 `await _redis.close()`，而 redis-py ≥5.0.1 起该方法**已废弃**（要用 `aclose()`）。整段冒烟用例因**污染进程级单例**（记忆服务/能力注册表）会连累无关用例，已撤掉，改为在 `tests/test_shutdown_lifespan.py` 里用"未连接的真实客户端 + 捕获 DeprecationWarning"精准盯住。
- **仍待抽**（有真实耦合）：§1 可观测性（132 行，外流 `routes`）· §3 LLM Gateway（184 行，外流 `TenantRateLimiter`/`exc`）· §3.4 记忆服务（79 行，外流 `pool`）· §7 Queue Worker（24 行，外流 `_retention_task`）· §6.5 指标/实例注册/僵尸巡检（85 行，外流三个句柄）—— 抽它们需要**显式传参或返回句柄**。

### 2.4 `mail_handler.go` / `session/manager.go` — 按流程/职责切

- `mail_handler.go`：`redisResetTokenStore` `76–100`（重置令牌存储）→ `mail_reset_store.go`；处理器按 **认证（`SendCode` 237 / `Login` 338 / `provisionMailUser` 415）** 与 **密码重置（`RequestPasswordReset` 462 / `ConfirmPasswordReset` 570）**、**配置（`GetConfig` 646 / `configResponse` 674）** 分三个文件。
- `session/manager.go`：会话 CRUD（`GetSession` 51 / `CreateSession` 102 / `ListSessions` 148 / `DeleteSession` 198 / `UpdateSession` 281 与 `SessionUpdate` 238 / `buildSessionUpdate` 253）与消息持久化（`SaveMessage` 305 / `SaveUserMessage` 386 / `ensureSessionOwned` 365）分开。
- 契约：同包私有符号（`isMissingColumn` 337、`defaultMailConfigRow` 660）仍可用；`Manager` 的公开方法集**不变**。

**✅ 已完成（批次 3 全部）**：

- `mail_handler.go` → `mail_reset_store.go` + `mail_handlers_{auth,reset,config}.go`（**1488 → 695 行**；4 个新文件 63/267/208/303 行）；
- `session/manager.go` → `manager_{crud,messages,queries,branch}.go`（**1198 → 128 行**；4 个新文件 269/448/193/209 行）。

两批同一验收口径：**逐函数/方法文本与拆分前逐字一致**（mail **45/45**、session **34/34**，无丢失 / 无内容变化 / 无新增）· `go build/vet/test -mod=mod ./...` 全绿 · 既有 `mail_handler_test.go` 与 `internal/session` 测试全部通过。

> 坑：拆分脚本按"限定符 → import 路径"静态推导 import（`goimports` 在整包编译不过时**不会**补 import）时，**版本化路径的包名不是最后一段** —— `github.com/jackc/pgx/v5` 的包名是 `pgx` 而不是 `v5`；不特判就会漏 import、直接编译失败。

### 2.5 前端 `views/` 与 `components/chat/` — 抽 composable

- `ChatView.vue`（173 符号）按实测簇抽：工作流/Agent 导出 `57–190`、布局切换 `191–221`（`readLayoutSwap`/`toggleLayout`/`onToolbarMenu`）、搜索 `229–255`、统计与压缩 `256–271`、子代理面板 `272+`。
> ⚠ 下括号里的数字是**符号数**（顶层声明个数），**不是行数** —— 实测行数（**设计当时 2026-09-28**；**当前值见 §1 表**）：`ChatView.vue` **3429** · `MediaView.vue` 1769 · `WorkflowView.vue` 1746 · `ChatInput.vue` 1259 · `AgentsView.vue` 1190 · `ChatSidePanel.vue` 1179。引用规模前先现场测（本轮就有人把它误读成行数、差点去"更正"一份没错的文档）。

- ✅ **批次 1 已抽（2026-10-08）**：`WorkflowView.vue` 的**执行与状态轮询簇**（提交运行 → 轮询状态 → 落到画布与日志 → 刷新运行历史）→ `src/composables/useWorkflowExecution.ts`。做法是**依赖注入**视图自己的 `ref`（`workflowId`/`getNodes`/`isExecuting`/`executionLogs`/`executionResults`/`instances`），返回视图需要的名字并由视图解构 ⇒ 模板与既有函数一行未改。**1746 → 1635 行**；共享类型抽到 `src/types/workflow.ts`（`NodeRunResult` / `InstanceRecord`，避免两处定义漂移）。护栏：`WorkflowView.spec.ts`（2 用例）· `vue-tsc -b` · `eslint` 0 problems · 全量 `vitest` 477 passed。
- 下一簇候选（同文件）：持久化（`saveWorkflow`/`loadWorkflows`/`loadWorkflow`/`deleteWorkflow`/模板 `loadTemplates`/`useWorkflowTemplate`）约 120 行；节点编辑面板（`edit*` refs + `applyNodeConfig`/`onNodeClick`）分散在多处，跨簇依赖更多，放后面。
- ✅ **批次 2 已抽（2026-10-08）**：**模板市场**簇（列表状态 + 加载 + 一键使用）→ `src/composables/useWorkflowTemplates.ts`；画布回填用**回调注入**（`resetCanvas` / `fromBackendFormat` / `fitView`），composable 不碰画布状态。**1636 → 1597 行**；抽取后视图侧 4 个 import（`nextTick`/`listTemplates`/`useTemplate`/`TemplateItem`）变为未使用，已清掉（`eslint` 0 problems 是靠这条暴露的）。护栏：`WorkflowView.spec.ts` 2 用例 · `vue-tsc -b` · 全量 `vitest` **477 passed**。
- ⏸ **持久化簇（`saveWorkflow`/`loadWorkflows`/`loadWorkflow`/`deleteWorkflow` + `toBackendFormat`/`fromBackendFormat`）暂缓 —— 用"注入面"量出来的**：它的注入面是 **14 个** ref/回调（`getNodes`/`getEdges`/`nodes`/`edges`/`workflowId`/`workflowName`/`savedWorkflows`/`executionResults`/`executionLogs`/`getNodeColor`/`getNodeIcon`/`getUserIdFromToken`/`setNodeCounter`/`resetCanvas`），而模板簇只有 **3 个**。**注入面就是"这簇到底内不内聚"的度量**：14 个说明它与画布状态本身纠缠，正确顺序是先抽**画布状态**（`nodes`/`edges`/选中/`nodeCounter`/`genNodeId`），再谈持久化。
- ✅ **`ChatView` 冒烟网已补（2026-10-08）**：`src/views/__tests__/ChatView.spec.ts`（3 条：能挂载 / 初始加载真的跑（`api.get('/v1/conversations')`）/ 挂载→卸载可重复且不抛错）。此前它是**全仓最大文件（3429 行）却零测试**，按"没网不动手"的纪律，先补网再拆。做法：重子组件全部打桩 + `../../api` 替身放 `vi.hoisted`（`vi.mock` 工厂会被提升，不能引用文件后面的顶层变量）+ 复用 `test-setup.ts` 的全局 i18n。
- **下一簇（同文件）**：会话加载/切换 + SSE 生命周期（`loadSessions`/`switchSession`/`activeSSE`/`onUnmounted` 清理），这簇状态含 `items`/`activeSessionId`/`loading`，抽前先量注入面。
  ⏸ **2026-10-09 量完 —— 这一簇不建议抽，且"SSE 生命周期"根本不是一簇**：
  按同一口径实测（`ChatView.vue` 现 **3,458 行 / 218 个顶层声明**）：

  | 候选 | 行数 | **注入面** |
  |---|---|---|
  | `switchSession`（单独） | 89 | **23** |
  | `loadSessions` + `persistSessions` | 19 | **3** |
  | 会话加载/切换 + SSE 生命周期（原提的整簇） | 105+ | **27** |

  **判断**：持久化簇在 **14** 个时已判"暂缓"，而这一簇是 **27** ⇒ **更不该抽** ✓。
  更要紧的是第二条：**"SSE 生命周期"不是一簇，而是散在 9 处**（`activeSSE` `L463` · `appliedQueryKey` `L848` ·
  `applyRouteQuery` `L850` · `mapSessionStreams` `L1305` · `ensureSubagentStream` `L1477` · `switchSession` `L1770` ·
  `onSSEMessage` `L2152` · `sendMessage` `L2298` · `stopGeneration` `L2448`）—— **横跨 1,100+ 行、含 `sendMessage`
  与 `stopGeneration`** ⇒ 想"抽 SSE 生命周期"等于抽掉**半个视图**，那不是拆分、是搬家 ✗。
  **唯一可抽的是** `loadSessions`+`persistSessions`+`sortSessions`（**~25 行 / 注入面 3**）—— 但它**太小**
  （从 3,458 行里搬 25 行是**观感收益**），且 §2.5 自己要求"该簇有组件测试兜底"而它**没有** ⇒ **不做** ✓。
  **⇒ 结论：这个文件的正确拆法不是"按生命周期阶段"，而是"按能力"** —— 承重的函数是
  `sendMessage` / `switchSession` / `onSSEMessage`，要动它们得先有一层**能力级**的边界（例如把
  "一次提交的编排"整体做成 `useSubmit()`，而不是把 SSE 的**开关**抽出来）。**下一簇应另选**。
- `ChatInput.vue`（67 符号）/`ChatSidePanel.vue`（47）/`MediaView.vue`（110）同法。
- 契约：抽到 `src/composables/use*.ts`，入参/返回值保持现有 `ref`/`computed` 形状；模板改动限于改名调用。**props/emits 契约与 UI 输出不变**。
- 风险：中偏高（`<script setup>` 里的闭包与生命周期钩子有隐式耦合）；建议一次只抽一簇，且该簇有组件测试兜底（如 `WorkflowView.spec.ts` 已有 2 用例）。

## 3. 不建议动的

- `migrations/versions/**`：基线不可变。
- `locales/*/index.ts`：数据文件。
- `AgentRuntime`（1557 行）、`MemoryService`（1240 行）、`QueueWorker`（844 行）三个大类的**内部拆解**：它们各自是一个状态机/领域服务的整体，拆解必然重排状态与错误处理 → 属行为变更，单独评估，不进本轮。

## 4. 落地批次

| 批次 | 内容 | 为什么这个顺序 |
|---|---|---|
| 1 | `gateway_router.go` → `routes_*.go`（✅ 已完成：1254 → 442 行，逐函数文本一致） | 同包纯移动、仅换文件（各文件 import 由脚本静态推导），风险最低 |
| 2 | `runtime.py` → `runtime_compaction.py` + `runtime_types.py`（类型部分 ✅ 已完成：3370 → 3198 行，AST 逐字一致；压缩簇待**重新定界**后做） | 纯函数/数据类，旧路径再导出 |
| 3 | `mail_handler.go`、`session/manager.go` 按流程/职责切（✅ 已完成：mail 1488 → 695、session 1198 → 128，逐函数文本一致） | 同包，公开方法集不变 |
| 4 | `main.py` 依赖取用器 + `lifespan` 分步（① 校验 ✅ · ② 关闭段 ✅ + **6 个无耦合启动段 ✅**：`lifespan` 740 → 561 行；剩余 5 段有真实耦合，需显式传参/返回句柄） | 启动顺序敏感，需逐行对照 |
| 5 | 前端抽 composable（**批次 1 ✅** 执行/轮询簇 → `useWorkflowExecution.ts`，1746 → 1637 行；**批次 2 ✅** 模板市场簇 → `useWorkflowTemplates.ts`，1636 → 1597 行；持久化簇因注入面 14 个暂缓，见 §2.5） | 隐式耦合最多，放最后 |

每批的验收口径：

1. **纯移动 + 零行为变更**：不夹带改名、不改文案、不动错误处理分支；
2. `go build/vet/test -mod=mod ./...` 或 `pytest -q -m "not integration"` + `vue-tsc -b` + `vitest run` 全绿；
3. `check_source_encoding.py` 通过；
4. 提交信息写清"本批只移动了什么、为什么边界在此"。

## 5. 与路线图的关系

- 本项**无功能收益**，排在所有功能项之后（见路线图 §4 批次 D）。
- 启动前建议先确认 §2.1 是否值得做：它是唯一"零风险 + 立刻降低装配复杂度"的一批；其余批次收益递减，可只做 1–2 批。
