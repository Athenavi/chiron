# 核心组件：Go 网关

> **权威来源**：`docs/architecture.md` · [错误码契约](../../error-codes.md) · [会话运行时 spec](../../session-runtime-spec.md)。
> 本页只给「它负责什么、代码在哪、有哪些契约」。

## 职责

**网关薄、引擎厚**：网关做**多租户相关的那些事**，不做 agent 逻辑。

| 职责 | 说明 |
|---|---|
| 鉴权与租户隔离 | JWT / SSO / 企业身份；租户与用户维度贯穿全链路 |
| 路由与转发 | `/v1/**`、`/submit`、`/cancel`、`/events`、`/media` |
| **SSE 广播** | 会话事件流；`Last-Event-ID` 补发与去重 |
| **会话运行锁与归属路由** | 跨实例取消、`engine:run:*` 归属（多副本一致性的基础） |
| 计费与配额 | 用量事件按**类型**分流，防重复计费 |
| 媒体与存储 | 分片上传合并、对象存储与签名 URL |

## 代码在哪

```
cmd/chiron           网关入口（main.go 485 行）+ chiron-cli 的管理子命令
internal/api/        138 个 .go —— HTTP 面；**路由按业务域拆成 routes_<domain>.go**
                     （public / agent / auth / system / conversation / media / market /
                      plugin / billing / proxy / admin，见拆分评估）
internal/auth/       鉴权与身份
internal/broadcast/  SSE 广播中枢
internal/session/    会话运行状态、锁、归属
internal/billing/    计费与配额
internal/engine/     与引擎之间的统一客户端
internal/storage/    对象存储
internal/monitor/    指标与 trace（metrics.go / trace.go）
internal/{db,id,model,settings,enterprise}/
```

**几个值得先读的文件**：`internal/api/submit_handler.go`（提交与事件落库）、
`internal/api/error_codes.go`（错误码常量，被门禁守）、`internal/api/tool_policy.go`（与引擎成对）、
`internal/monitor/metrics.go`。

## 它身上的契约（改之前先读）

| 契约 | 文档 | 有门禁吗 |
|---|---|---|
| 错误码 | [error-codes.md](../../error-codes.md) | **有**：Go `Code*` ↔ 三语言 `errors.ts` 键集必须一致 |
| 工具策略 | [agent-safety-and-reliability.md](../../agent-safety-and-reliability.md) | **有**：与 Python 侧同构（分级表 / 模式 / 提供商目录 / 常量） |
| 会话运行时 | [session-runtime-spec.md](../../session-runtime-spec.md) | **有**（对话模式与工具授权模式对齐） |
| 多副本一致性 | [deployment-multi-instance.md](../../deployment-multi-instance.md) §9 | 对外承诺已成文 |

## 延伸阅读

- [架构](architecture.md) —— 三层与请求链路
- [会话](sessions.md) · [工具与审批](tools.md) · [子 Agent](subagents.md)
- [生产：可观测性](../production/observability.md)
