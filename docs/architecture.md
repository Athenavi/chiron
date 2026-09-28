# 架构与请求链路

本文回答一个问题：**一次对话请求从浏览器发出，到流式回复回到浏览器，中间经过了什么。**

每个环节都给出代码位置（`文件:符号`，行号只在指常量时给出以免漂移）与可复现的验证命令 —— 结论都能自己跑一遍。横向扩容与多副本一致性见 [多实例部署指南](deployment-multi-instance.md)，红线与验收口径见 [贡献与验收流程](contributing.md)。

## 0. 组件与拓扑

| 组件 | 位置 | 职责 | 地址 |
|---|---|---|---|
| 前端 SPA | `frontend-vue/`（Vite 构建产物进 nginx 静态目录） | 对话 UI、SSE 订阅、`Last-Event-ID` 续传 | 同源 |
| nginx | `frontend-vue/nginx.conf` | 同源反代 `/v1/`、`/events`、`/submit`、`/cancel`、`/media/`、`/ws/`；`/health` 本地返回 200 | `:80` |
| Go 网关 | `cmd/chiron`，路由 `internal/api/gateway_router.go` | 认证、限流、计费/配额闸门、SSE 汇聚、引擎代理、管理面 | `PORT`，默认 `8080`（`config/config.go` 的 `getEnv("PORT", "8080")`） |
| Python 引擎 | `python-engine/app/main.py` | Agent 执行、工具/记忆/知识库、工作流、队列 worker | `HTTP_PORT`，默认 `8000`（容器内 `python-engine:8000`，见 compose 的 `PYTHON_ENGINE_ADDRESS`） |
| PostgreSQL | — | **唯一持久层**（对话、设置、计费、工作流实例…） | `POSTGRES_DSN` |
| Redis | — | 会话运行锁、限流计数、**SSE 事件缓冲流**、任务队列（`engine:tasks`）、缓存、Pub/Sub | `REDIS_URL` |

```
浏览器 ──HTTP──> nginx ──> Go 网关 ──HTTP(SSE)──> Python 引擎
   ^                          │  ▲                    │
   └──────SSE(/events)────────┘  └──Redis(Pub/Sub)────┘
                               │
                     Redis(Streams) + PostgreSQL
```

两条容易搞错的边界：

- **PostgreSQL 不在 compose 里**（`docker-compose.yml` 顶部注释：生产使用托管实例或由 DBA 维护，schema 迁移由发布流程执行）。
- **网关也是 PG/Redis 的客户端**，不只是反代：它自己跑迁移版本校验、限流计数、事件缓冲、会话运行锁。连接预算见多实例指南 §。 

## 1. 一次对话请求（SSE 模式）

前端 `ChatView.sendMessage`（`frontend-vue/src/views/ChatView.vue`）做两件事：**先开流，再提交**。

1. **订阅事件流**：`createSSEConnection(sessionId, …)`（`frontend-vue/src/api/index.ts`）打开
   `GET {API_URL}/events?session_id=<sid>`（`EventSource`，带 cookie；`API_URL` 由 `VITE_API_URL` 决定，同源部署时为空串）。跨轮重建连接时回传 `last_event_id`，服务端据此补发缺口。

2. **提交**：`api.post('/submit', body)`，body 含 `content`、`session_id`、`llm_config`（模式/模型/思考档位/`tools_mode`/`client_msg_id`）、`context`、`attachments`。

3. **nginx 分流**（`frontend-vue/nginx.conf`）：
   `location /submit` → 网关；`location /events` → 网关并**关闭缓冲**（`proxy_buffering off`、`proxy_read_timeout 3600s`）—— 这是 SSE 能实时到达的前提；`location /ws/` 只服务 RPA 桥（见 §3）。

4. **网关注册的入口**（`internal/api/gateway_router.go`）：`POST /submit` 与 `POST /v1/agent/submit` 都指向同一中间件链
   `authMW(sanitizeMW(submitHandlerFunc))`；`POST /cancel`、`POST /v1/agent/cancel` 走 `cancelMW`。

5. **认证**（`internal/api/middleware.go` 的 `AuthMiddleware`）：JWT 取 cookie `chiron_token`，或 API 客户端用 `Authorization: Bearer <token>`。失败即 401，链路上没有"匿名放行"的旁路。

6. **闸门按固定顺序拒绝**（`gateway_router.go` 的 `submitHandlerFunc`，注释逐条写了理由）：

   | 顺序 | 检查 | 失败响应 |
   |---|---|---|
   | 1 | **会话归属**：已存在的 `session_id` 必须是本人的（新会话放行） | 403 |
   | 2 | **计费预检**：超出每日免费额度且余额 ≤ 0 | 402 |
   | 3 | **租户 token 配额预检**（`EnforceTenantQuota`，fail-open） | 429 |
   | 4 | **会话运行锁**：`AcquireSessionRunLock`（Redis，跨实例） | 400 |
   | 5 | **全局并发信号量** `agentSem.TryAcquire` | 429 |
   | 6 | **租户并发闸门**（`ent_quota_pools`，跨副本） | 429 |

   第 4 步顺带起了**锁心跳续期**（60s 一次，TTL 5min）：持有实例崩溃后锁自动过期，用户可重试。

7. **立即返回 202**（`{"status":"accepted","session_id":…}`），真正的执行在后台 goroutine 里，且用 `context.WithoutCancel(r.Context())` + 独立超时（`AGENT_SUBMIT_TIMEOUT`，兜底 300s）—— 否则客户端断开会把整条链路取消。

8. **网关 → 引擎**（`internal/api/submit_handler.go` 的 `HandleSubmit`）：
   - **幂等**：`llm_config.client_msg_id` 走 Redis SETNX（同一条消息最多执行一次，防网络重试重复计费）；
   - 先把用户消息**落库**（SSE 中断也不丢历史）；
   - `h.python.RunSSE(ctx, "/v1/agent/submit", …)` —— 以 SSE 方式调用引擎（`internal/engine/python_client.go` 的 `RunSSE`，请求头注入 `X-Internal-Token`）；引擎侧入口是 `python-engine/app/main.py` 的 `app.post("/v1/agent/submit")(agent_submit)`；
   - 边读边转发：`text` 事件按 50ms 合帧，其余事件原样 `h.eventHub.Publish(...)`。

9. **引擎执行**：AgentRuntime 解析上下文（Agent/技能/知识库/记忆，网关的 `resolveAgentContext` 会按 `agent_id` 补全配置）→ 调 LLM 与工具 → 以 SSE 块返回。

10. **浏览器收到事件**：见 §2。

计费**预检**在第 6 步（只挡"完全付不起"），真实用量结算走计费链路（`internal/billing`，回合结束时按 usage 落账）。

## 2. 事件回传与断线重放

汇聚点是 `internal/broadcast/hub.go` 的 `Hub`：

- **事实源是 per-session Redis Stream**：key 为 `RedisKey("sse:events:") + sessionID`（`sessionEventsKey`），`RedisKey(name)` 只是 `REDIS_KEY_PREFIX + name`（`internal/db`），所以默认前缀为空时 key 就是 `sse:events:<session_id>`。
- **XADD 的流 ID 就是 SSE 的 `id:` 行**（跨实例单调递增），每会话保留最近 200 条 + 1 小时滑动 TTL（`sseEventsMaxLen`/`sseEventsTTL`）。
- **发布路径**：`Publish` 只入队，后台 worker 串行完成 `XADD 取 ID → 本地 fanout → 跨实例 PUBLISH`（同会话固定分片，保证顺序）。队列满时回退同步发布 —— 宁可让调用方等，也不丢事件。
- **断线重放**：浏览器重连时带 `Last-Event-ID`（或 `?last_event_id=`），网关 `Hub.ReplayAfter` 用 `XRange(key, "(" + after, "+")` 补发缺口，再进入实时转发；已发过的旧事件按流 ID 去重（`internal/api/events.go` 的 `handleSSE`）。
- **订阅必须带 `session_id`**：`SSEHandler` 强制校验（`session_id is required`），并核对会话归属 —— 否则会订阅到全站事件流（含他人对话内容）。
- **15s ping**：无事件时发 `: ping` 注释行，防中间设备掐长连接。

## 3. 其它入口

| 入口 | 路由 | 链路要点 |
|---|---|---|
| 统一任务模式 | `POST /v1/chat/submit` | 网关代理到引擎 unified executor；前端 `sendUnified`，结果一次性返回后追加为消息 |
| 工作流执行 | `POST /v1/graphs/{id}/execute` → `GET /v1/workflows/{id}/status` | 引擎**异步提交**：先落库 `workflow_instances`（`status='running'`），再把任务 `XADD` 到 `engine:tasks`（`python-engine/app/api/workflows.py`），由队列 worker 消费；前端轮询 status |
| Agent 派发 | `POST /v1/agents/{id}/run`、`POST /v1/agents/dispatch` | 与对话同构：闸门 + 异步执行 + 事件流 |
| 审批 / 提问 | `POST /v1/agent/approval`、`POST /v1/agent/answer` | 工具需确认时前端回传布尔/文本，网关转引擎 |
| RPA 插件桥 | `GET /ws/rpa` | **全站唯一的 WebSocket**（`internal/api/rpa_ws.go`，nginx `location /ws/`）；对话流**不用** WS |
| 公开分享 | `GET /v1/share/{id}`、`GET /media/s/{assetID}` | 仅 `rlMW`，无登录；内容由分享记录限定 |

> 会话流统一为 SSE 是有意为之：`gateway_router.go` 在注册 `/events` 的注释里写明"批 B-3′：下线 /ws/{sessionId} 与 WebSocketHub（前端仅 EventSource）；RPA 插件通道 /ws/rpa 保留"。

## 4. 凭证与信任边界

| 凭证 | 载体 | 谁校验 | 用途 |
|---|---|---|---|
| 用户 JWT | cookie `chiron_token` / `Authorization: Bearer` | 网关 `AuthMiddleware` | 终端用户身份；**httpOnly cookie，JS 读不到** |
| 内部 token | `X-Internal-Token` | 网关 `internalTokenMW`；引擎 `app/middleware/auth.py` | 网关 ↔ 引擎、网关 ↔ 自身的 `/v1/internal/*`（DB/Redis/模型路由/工具授权）；由 `INTERNAL_TOKEN` 或 `APP_SECRET` 派生，两侧必须一致 |
| 运行锁 token | Redis key | 网关 `RefreshSessionRunLock` / 释放 | 会话级 run 唯一标识，防旧 run 误删新锁 |
| 引擎身份 | query `?user_id=&tenant_id=` | 引擎 `AuthMiddleware` | 真实链路上由网关注入，客户端伪造同名头会被剥离 |

`/v1/internal/*` 一律 `internalTokenMW`（外加 `rlMW`），**不暴露给浏览器**；`/metrics` 另走 `metricsAuthMW`。

## 5. 自查命令

```bash
# 1) 网关就绪（无需鉴权）
curl -fsS http://127.0.0.1:8080/health          # {"success":true,"data":{"status":"ok"}}
curl -fsS http://127.0.0.1:8080/ready           # {"success":true,"data":{"postgres":"up","redis":"up"}}

# 2) 内部端点（网关 ↔ 引擎的同一把钥匙；$INTERNAL_TOKEN 取自 .env）
curl -s -H "X-Internal-Token: $INTERNAL_TOKEN" http://127.0.0.1:8080/v1/internal/db/health
curl -s -H "X-Internal-Token: $INTERNAL_TOKEN" http://127.0.0.1:8080/v1/internal/redis/health

# 3) 未带凭证必须被挡（链路无匿名旁路）；内部端点拿错 token 同样 401
curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:8080/events?session_id=demo'   # 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8080/submit                                                    # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Internal-Token: wrong' \
  http://127.0.0.1:8080/v1/internal/db/health                                             # 401

# 4) 订阅事件流（-N 关掉 curl 自己的缓冲；需已登录用户的 cookie）
curl -N -H "Cookie: chiron_token=$JWT" 'http://127.0.0.1:8080/events?session_id=<sid>'
#    断线重放：加 -H "Last-Event-ID: <id>"

# 5) 提交一轮对话 → 202 后事件从上面的流里出来
curl -s -X POST http://127.0.0.1:8080/submit \
  -H "Cookie: chiron_token=$JWT" -H 'Content-Type: application/json' \
  -d '{"content":"你好","session_id":"<sid>","llm_config":{"client_msg_id":"'"$(uuidgen)"'"}}'

# 6) 事件缓冲流（XADD 的流 ID 就是 SSE 的 id: 行；前缀默认空，见 REDIS_KEY_PREFIX）
#    该会话还没产生过事件时这条返回空 —— 缓冲流只在有事件时存在
redis-cli XRANGE "sse:events:<sid>" - + COUNT 5

# 7) 引擎侧同一条链路的入口（容器内直连，需同款内部 token + 身份 query）
curl -N -H "X-Internal-Token: $INTERNAL_TOKEN" \
  'http://localhost:8000/v1/agent/submit?user_id=u&tenant_id=u' \
  -H 'Content-Type: application/json' -d '{"content":"hi","session_id":"<sid>"}'
```

网关的路由清单可以直接从代码里导出（比对着读文档可靠）：

```bash
grep -o 'mux\.Handle("[A-Z]* [^"]*"' internal/api/gateway_router.go | sort -u
grep -o 'mux\.HandleFunc("[A-Z]* [^"]*"' internal/api/gateway_router.go | sort -u
```

## 6. 现状与坑

- **`python-engine/app/sse/producer.py` 的 `SSEProducer` 是未接线的遗留**：全仓只有定义与 `app/sse/__init__.py` 的导出，没有构造点。它写的 `sse:<task_id>` stream 也无人消费。真实链路是"引擎 HTTP SSE 流 → 网关 hub → per-session stream"，别照它理解架构（本项待清理，见路线图）。
- **会话流是 SSE，不是 WebSocket**：`/ws/rpa` 是 RPA 插件桥专用（见 §3）。
- **网关与引擎都会连 PG/Redis**：扩容时连接预算 = `POSTGRES_MAX_CONN × 网关副本数 + DB_POOL_MAX_SIZE × 引擎副本数 ≤ PG max_connections`；引擎启动时会把这个算式打出来（`app/db.py` 的 `_log_pool_capacity`）。
- **长回合超时**：`AGENT_SUBMIT_TIMEOUT` 默认 5 分钟且不低于 `DefaultAgentTimeout`(300s)；比它短的硬编码曾导致"多轮 LLM + 工具调用做一半就断"。
- **SSE 事件只保证"最近 200 条 + 1h"**：更久之前的断线无法靠流补发，前端需按 DB 状态自愈（历史消息接口）。
