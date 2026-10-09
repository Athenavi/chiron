# 生产使用：可观测性

> **权威来源**：`internal/monitor/`（metrics.go / trace.go）· `python-engine/app/observability/` ·
> `python-engine/app/trace/`。本页只给「看哪里、什么算信号」。

## 三样东西

| 面 | 位置 | 看什么 |
|---|---|---|
| **指标（metrics）** | `internal/monitor/metrics.go` | 计数器/仪表：配额拒绝、连接池拒绝、运行数… |
| **链路（trace）** | `internal/monitor/trace.go` · `app/trace/` | 一次请求穿过网关 → 引擎 → 存储 |
| **审计流水** | `app/tools/exec_audit.py` · `app/hooks/audit.py` · `app/agent/approval_audit.py` | **谁在什么租户下做了什么**（JSONL，UTC+毫秒） |

**审计不是日志**：它是**证据**，带 tenant/user/session 维度，不受日志级别影响。

## 几个值得盯的指标

- **配额与限流拒绝**：`quota_exceeded` 之类的计数（`internal/monitor/metrics.go` 里有专门的
  `QuotaExceeded` 计数器）。
- **MCP 连接池拒绝**：`mcp_pool_rejected_total` —— 多租户下的连接经济学信号。
- **运行/会话状态**：在跑多少 run、锁过期多少次（多副本健康度的直接指标）。

## 健康检查的两个端点

```bash
curl -sS localhost:8080/health   # 进程活着
curl -sS localhost:8080/ready    # 依赖就绪
```

**编排系统应该只把 `/ready` 当就绪探针** —— `/health` 200 不代表能服务。

## 一条纪律

**能变成机器可读的码，就不要只写日志句子**。例如压缩"为什么没发生"应该有明确的状态码，
而不是让人去 grep 日志（这是差距分析里"值得追"的低成本项）。

## 延伸阅读

- [多实例](multi-instance.md) —— 这些指标在多副本下才有意义
- [错误码](error-codes.md) · [安全与护栏](../advanced/guardrails.md)（审计流水）
