# 会话运行时状态与会话遥测（spec）

> **溯源（2026-10-08）**：本文件**原先缺失** —— 仓库里 7 处引用它（`internal/api/session_runtime.go`
> 文件头、`internal/api/routes_proxy.go` §5、`internal/api/gateway_router.go`、`frontend-vue/src/api/sessionRuntime.ts`、
> `frontend-vue/src/components/chat/contextChips.ts`、`frontend-vue/src/views/ChatView.vue`、`python-engine/app/core/task_router.py`），
> 但文件不在仓库中。现按**代码与注释**重建，**只写代码能证明的内容**；与代码冲突时以代码为准。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 为什么需要它（单一事实源）

这些选择一度散落在**三处**：会话的 `llm_config`、前端组件 `ref`、URL query；而**两条提交链路**
（SSE `/submit` 与统一 `/v1/chat/submit`）各自带一份。结果是：只改了其中一处时，"切换模式/模型"
**看起来生效、实际不生效**（或刷新即回退默认）。

所以这里的核心诉求是：让「模型 / provider / 上下文激活项」**只存在一处**，并让两条链路读**同一份解析结果**。

## 2. 存储分层与键

| 层 | 位置 | 作用 |
|---|---|---|
| 热 | Redis Hash `session:runtime:{tenant}:{sid}`（经 `db.RedisKey`） | 低延迟读写；TTL **24h** |
| 权威 | `unified_sessions.runtime`（jsonb） | 持久；Redis 缺失/过期时由它回填 |

读路径（`loadRuntime`）：先取 DB 权威值，再用 Redis 热层覆盖它已有的字段 ⇒ **热层优先、权威层兜底**。
写路径（`saveRuntime`）两处都写。新会话（尚未创建）**允许**读写热状态 —— 首次提交时才建会话。

**工具授权模式（`ask`/`auto`/`yolo`）与对话模式（`normal`/`minimal`/`ptc`/`creative`）不在其中**：
它们是**请求级参数**（前端实时状态，每次提交随请求携带），服务端只校验与兜底。
曾把它们写进 runtime 时，同一份状态散落三处，任一处不一致就表现为"切换了却不生效"。
只有"用户选定后希望下次继续沿用"的项（**model / provider**）才落会话状态。

## 3. 解析优先级（唯一实现）

```
mode        : 请求显式 > 用户默认 > 全局默认 > normal
tools_mode  : 请求显式 > 用户默认 > 全局默认 > auto
model       : 请求显式 > runtime > 用户默认 > 全局默认 > deepseek-chat
provider    : 请求显式 > runtime > 空（自动路由）
```

注意 **`mode` / `tools_mode` 没有 runtime 这一层**（见 §2）。`validAgentModes` =
`normal | minimal | ptc | creative`，与引擎 `app/agent/modes.py` 的 `_BASE_MODES` 对齐。

每个解析结果都带**来源**（`resolvedValue{value, source}`），`source` 取值：
`request`（本次请求显式）· `session`（会话 runtime）· `default`（用户/全局默认）·
`system`（系统兜底）· `auto`（未指定 provider，自动路由）。前端据此展示"当前生效值 + 来自哪里"，排障同样用它。

## 4. HTTP 契约

全部需要登录；会话归属校验：会话属于他人 ⇒ **403**；会话不存在（新会话）⇒ 放行。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/sessions/{session_id}/runtime` | → `{runtime, defaults, resolved:{model,provider}}` |
| `PUT` | `/v1/sessions/{session_id}/runtime` | **PATCH 语义**：只改传入字段；**显式 `null` = 清除该项**，回落默认链。→ `{runtime, resolved:{model,provider}}` |
| `GET` | `/v1/sessions/{session_id}/metrics` | → 会话遥测汇总（见 §6） |

`runtime` 的字段：`model` · `provider` · `compaction` · `context` · `updated_at` · `updated_by`。

**PATCH 里的 `mode` / `tools_mode` 会被忽略**（`applyRuntimePatch` 不处理它们）—— 它们不是 runtime
字段。前端类型里仍留着这两个键，属**历史残留**，不要据此以为"写进去了"。
`context` 必须是对象，否则 400。

## 5. 统一链路注入（proxy `mutateBody`）

反向代理把请求转发给 Python 引擎前，用 `mutateBody(r, body)` 钩子**改写请求体**，
把会话运行时的解析结果注入**统一链路**（`/v1/chat/submit`），从而保证两条提交链路取到同一份值。
钩子返回 `false` 表示"已自行写出响应，代理应中止"（例如解析失败时直接回错误）。

引擎侧对应读取点：`python-engine/app/core/task_router.py` 从**会话运行时状态**取
`model` / `provider_hint` / `tenant_id`（P1-d）。

## 6. 遥测（**读取时聚合**，幂等）

- 热层：Redis Hash `session:metrics:{tenant}:{sid}`，`field = turn_id`，`value = JSON 明细`（TTL 24h）。
- 汇总**在读取时计算**（`aggregateMetrics`）—— 幂等、无增量漂移；明细缺失时回落 DB
  （`turns` + `billing_records`，与计费同口径）。
- 返回形状（前端 `SessionMetrics`）：`source`（`redis` | `db`）· `totals`
  （`turns` / `input_tokens` / `output_tokens` / `cached_tokens` / `cache_hits` / `cache_hit_rate` / `cost_cents`）·
  `throughput`（`ttft_ms_p50` / `output_tps_p50` / `output_tps_p95` / `sample_turns`）· `turns`。

## 7. 实现落点

| 环节 | 位置 |
|---|---|
| 读写 + 解析 + 遥测 | `internal/api/session_runtime.go`（`GetRuntime` / `PutRuntime` / `GetMetrics` / `loadRuntime` / `saveRuntime` / `resolveAgentConfig`） |
| 路由 | `internal/api/gateway_router.go`（三条 `authMW` 路由） |
| 统一链路注入 | `internal/api/routes_proxy.go`（`mutateBody`，见 §5） |
| 引擎侧取用 | `python-engine/app/core/task_router.py`（P1-d） |
| 前端契约 | `frontend-vue/src/api/sessionRuntime.ts`（`ResolvedValue` / `SessionRuntime` / `SessionMetrics`） |
| 上下文激活项 → 芯片 | `frontend-vue/src/components/chat/contextChips.ts`（`runtime.context` 是唯一来源） |

## 8. 已知边界

- 热层 TTL 24h：超过后回落到 DB 权威层（会话级选择不丢；只在"Redis 与 DB 都不在"时才失效）。
- `saveRuntime` **先写 DB 权威层、再写 Redis 热层**；**Redis 写失败只告警、不阻断**（DB 已落）——
  所以热层缺失不会丢会话级选择。
- `defaults` 只回**用户级 / 全局级**默认值，不含请求级显式值（请求级只体现在 `resolved.source=request`）。
- 遥测的 `throughput` 基于样本（`sample_turns`）；样本不足时该组字段可能缺失，调用方需要容忍。
