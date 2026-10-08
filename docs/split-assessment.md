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
| `migrations/versions/0001_authoritative_baseline.py`（1475 行） | 已发布迁移**不可变**，拆分等于改写历史 |
| `shared/models/**` | ORM 生成物；要改就改生成器（见 L4-3） |
| `node_modules/`、`dist/`、`__pycache__/` | 构建产物 |

实测体量（`symbols` = 顶层 `func/type` 或 `def/class` 或 `function/const ref|computed` 计数）：

| 文件 | 行数 | KB | symbols | 主要矛盾 |
|---|---|---|---|---|
| `frontend-vue/src/views/ChatView.vue` | 3047 | 137.8 | 173 | 单文件承载整条对话链路 + 侧栏 + 导出 |
| `python-engine/app/agent/runtime.py` | 1841 | 98.6 | 25 | 一个 `AgentRuntime` 类占 1557 行 |
| `frontend-vue/src/views/MediaView.vue` | 1672 | 58.6 | 110 | 列表/筛选/多选/面包屑混在一起 |
| `frontend-vue/src/views/WorkflowView.vue` | 1649 | 63.6 | 91 | 画布 + 模板 + 实例 + 执行日志 |
| `python-engine/app/main.py` | 1509 | 82.1 | 24 | `lifespan` 单函数 559 行 |
| `internal/api/mail_handler.go` | 1308 | 51.0 | 45 | 认证流程 / 密码重置 / 配置三簇并存 |
| `python-engine/app/memory/service.py` | 1264 | 54.0 | 10 | `MemoryService` 单类 1240 行 |
| `frontend-vue/src/components/chat/ChatInput.vue` | 1193 | 50.7 | 67 | 输入 + 附件 + 模型选择 + 提及 |
| `frontend-vue/src/components/chat/ChatSidePanel.vue` | 1116 | 47.4 | 47 | 用户菜单 + 轨迹 + 会话列表 + 抽屉拖拽 |
| `internal/session/manager.go` | 1098 | 44.1 | 41 | 会话 CRUD 与消息持久化同文件 |
| `python-engine/app/queue/worker.py` | 981 | 49.4 | 4 | `QueueWorker` 单类 844 行 |
| `internal/api/gateway_router.go` | 970 | 60.7 | 18 | `NewGatewayRouter` 单函数 372 行 + 10 个 `registerXxxRoutes` |

## 2. 边界建议（按收益排序）

### 2.1 `gateway_router.go` — 最低风险，**建议先做**

实测结构：middleware 基础设施 `32–131`；`NewGatewayRouter` `132–503`（372 行，只做装配）；`registerXxxRoutes` 共 10 个 `504–1148`（`public` 504 / `agent` 565 / `auth` 814 / `system` 832 / `conversation` 881 / `media` 913 / `userMarket` 943 / `plugin` 950 / `billing` 961 / `proxy` 979 / `admin` 1149）。

- 边界：**按业务域拆成 `routes_<domain>.go`**，`gateway_router.go` 只留 middleware + `NewGatewayRouter`。
- 契约：各 `registerXxxRoutes` 签名与调用点**一字不改**，仅换文件；同包（`package api`）→ 无需改调用方。
- 风险：极低（纯移动、同包私有符号仍可互访）；唯一注意点是别顺手改函数体。

**✅ 已完成（批次 1）**：拆成 11 个 `routes_<domain>.go`（public / agent / auth / system / conversation / media / market / plugin / billing / proxy / admin），`gateway_router.go` 只留 imports + middleware + `NewGatewayRouter`（**1254 → 442 行**）。验收：**逐函数文本与拆分前逐字一致**（16/16 个顶层函数，无丢失 / 无内容变化 / 无新增）· `go build/vet/test -mod=mod ./...` 全绿 · `check_source_encoding.py` 通过。

> 坑：`goimports` 在**整包编译不过时不会补 import**（它需要包可加载）—— 拆完就是编译不过的状态，于是它静默无输出。改用「按限定符静态推导 import + `go build` 兜底」。

### 2.2 `runtime.py` — 压缩工具簇天然独立

> **行数已过期**：本文写作时 `runtime.py` 1841 行，**当前 3370 行**（`AgentRuntime` 从 1557 → 2315 行）。
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
- `ChatInput.vue`（67）/`ChatSidePanel.vue`（47）/`WorkflowView.vue`（91）/`MediaView.vue`（110）同法。
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
| 4 | `main.py` 依赖取用器 + `lifespan` 分步（① 校验部分 ✅ 2170 → 2061；② 关闭段 ✅ 抽成 `_shutdown_lifespan`（句柄显式传递 + 3 条用例），`lifespan` 740 → 664 行；启动段仍待分段） | 启动顺序敏感，需逐行对照 |
| 5 | 前端抽 composable（一次一簇，从有测试的 `WorkflowView` 起） | 隐式耦合最多，放最后 |

每批的验收口径：

1. **纯移动 + 零行为变更**：不夹带改名、不改文案、不动错误处理分支；
2. `go build/vet/test -mod=mod ./...` 或 `pytest -q -m "not integration"` + `vue-tsc -b` + `vitest run` 全绿；
3. `check_source_encoding.py` 通过；
4. 提交信息写清"本批只移动了什么、为什么边界在此"。

## 5. 与路线图的关系

- 本项**无功能收益**，排在所有功能项之后（见路线图 §4 批次 D）。
- 启动前建议先确认 §2.1 是否值得做：它是唯一"零风险 + 立刻降低装配复杂度"的一批；其余批次收益递减，可只做 1–2 批。
