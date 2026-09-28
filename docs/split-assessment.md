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
- 契约：各 `registerXxxRoutes` 签名与调用点**一字不改**，仅换文件；同包（`package api`）→ 无 import 变更。
- 风险：极低（纯移动、同包私有符号仍可互访）；唯一注意点是别顺手改函数体。

### 2.2 `runtime.py` — 压缩工具簇天然独立

实测结构：上下文/消息处理函数簇 `50–468`（`_restrict_tools_to_plugins`…`_ensure_valid_tool_sequence`，约 420 行，彼此只依赖 `messages` 结构）；数据类型 `AgentTask` 469 / `AgentEvent` 505 / `ApprovalTicket` 530；**`AgentRuntime` 574–2131（1557 行）**；模块级入口 `run_agent` 2132 / `register_pending_approval` 2193。

- 边界（分两步，先易后难）：① 拆出 `runtime_compaction.py`（`50–468` 那簇，纯函数、无状态）；② 拆出 `runtime_types.py`（三个 dataclass）。
- 契约：`runtime.py` 保留同名再导出（`from .runtime_compaction import *` 或显式 `__all__` 转发），**避免改动任何既有 import 路径**；测试 `tests/` 中引用旧路径的地方不动。
- `AgentRuntime` 本体暂不动：1557 行内聚在"一次 run 的状态机"上，拆它必然触碰行为，不属于本项范围。

### 2.3 `main.py` — `lifespan` 分步化

实测结构：依赖取用器 `53–202`（`get_plugin_pool`/`get_redis`/`get_gateway`/`touch_user`/`verify_sandbox_root`/`verify_media_store`/`_is_inside_cwd`）；**`lifespan` 203–761（559 行）**；`_get_instance_id` 762、`_resolve_attachments` 779–914（136 行）、`_setup_middleware` 915、`_setup_routes` 936–1086；处理器 `agent_run` 1087、`_workflow_context_stream` 1136、`agent_submit` 1181–1571（390 行）、`agent_approval` 1572。

- 边界：① 依赖取用器 + 校验搬到 `app/deps.py`（`main.py` 再导出）；② `lifespan` 内部按"启动步骤"抽成私有 async 函数（如 `_start_pool`/`_start_redis`/`_start_worker`），**执行顺序与异常语义保持逐行等价**。
- 契约：`app.state` 上挂的键名、`lifespan` 的 `yield` 前后顺序、`logging` 输出顺序都不动；顺序一乱就是行为变更。
- 风险：中。启动顺序有隐含依赖（Redis → 网关 → 队列 worker），必须整段对照后再动。

### 2.4 `mail_handler.go` / `session/manager.go` — 按流程/职责切

- `mail_handler.go`：`redisResetTokenStore` `76–100`（重置令牌存储）→ `mail_reset_store.go`；处理器按 **认证（`SendCode` 237 / `Login` 338 / `provisionMailUser` 415）** 与 **密码重置（`RequestPasswordReset` 462 / `ConfirmPasswordReset` 570）**、**配置（`GetConfig` 646 / `configResponse` 674）** 分三个文件。
- `session/manager.go`：会话 CRUD（`GetSession` 51 / `CreateSession` 102 / `ListSessions` 148 / `DeleteSession` 198 / `UpdateSession` 281 与 `SessionUpdate` 238 / `buildSessionUpdate` 253）与消息持久化（`SaveMessage` 305 / `SaveUserMessage` 386 / `ensureSessionOwned` 365）分开。
- 契约：同包私有符号（`isMissingColumn` 337、`defaultMailConfigRow` 660）仍可用；`Manager` 的公开方法集**不变**。

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
| 1 | `gateway_router.go` → `routes_*.go` | 同包纯移动，零 import 变更，风险最低 |
| 2 | `runtime.py` → `runtime_compaction.py` + `runtime_types.py` | 纯函数/数据类，旧路径再导出 |
| 3 | `mail_handler.go`、`session/manager.go` 按流程/职责切 | 同包，公开方法集不变 |
| 4 | `main.py` 依赖取用器 + `lifespan` 分步 | 启动顺序敏感，需逐行对照 |
| 5 | 前端抽 composable（一次一簇，从有测试的 `WorkflowView` 起） | 隐式耦合最多，放最后 |

每批的验收口径：

1. **纯移动 + 零行为变更**：不夹带改名、不改文案、不动错误处理分支；
2. `go build/vet/test -mod=mod ./...` 或 `pytest -q -m "not integration"` + `vue-tsc -b` + `vitest run` 全绿；
3. `check_source_encoding.py` 通过；
4. 提交信息写清"本批只移动了什么、为什么边界在此"。

## 5. 与路线图的关系

- 本项**无功能收益**，排在所有功能项之后（见路线图 §7 批次 D）。
- 启动前建议先确认 §2.1 是否值得做：它是唯一"零风险 + 立刻降低装配复杂度"的一批；其余批次收益递减，可只做 1–2 批。
