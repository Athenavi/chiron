# 核心组件：会话与运行时状态

> **权威来源**：[会话运行时 spec](../../session-runtime-spec.md) ·
> [会话地图与分支设计](../../session-map-branch-design.md) ·
> [多实例部署](../../deployment-multi-instance.md)。本页只给「状态放在哪、谁写它」。

## 一次会话由四样东西构成

| 组成 | 存哪 | 谁写 |
|---|---|---|
| **持久历史**（消息 / 工具调用 / 思考 / 用量） | PostgreSQL | 网关（提交路径） |
| **事件流**（SSE，可补发） | Redis Stream | 引擎产出、网关广播 |
| **运行状态**（本轮是否在跑、工具授权模式、对话模式） | 会话运行时 + Redis 锁/心跳 | 网关（唯一仲裁） |
| **run 现场**（可续跑的快照） | checkpoint | 引擎（回合末落盘） |

## 代码在哪

```
网关侧
  internal/session/                 会话运行状态、锁、归属
  internal/api/session_runtime.go   运行时状态与模式（被工具策略门禁守）
  internal/api/session_coord*.go    跨实例协调（运行锁心跳 / 归属路由）
  internal/broadcast/               SSE 广播与 Last-Event-ID 补发
引擎侧
  python-engine/app/session_store.py   会话读写
  python-engine/app/run_registry.py    运行注册表
  python-engine/app/event_bus.py       事件总线
  python-engine/app/queue/             worker · producer · idempotency · dlq
  python-engine/app/agent/resume.py    恢复
```

## 四条不变量（都值得先读再改）

1. **事件有序且可补发**：SSE 带 `Last-Event-ID`，客户端重连不丢不重 —— 去重靠**事件序号**，
   不靠时间戳。
2. **运行归属唯一**：一个会话的 run 归属某个实例（`engine:run:*`），跨实例取消要**先校验会话属主**。
3. **写者唯一**：会话状态的写路径收敛在网关；引擎侧通过统一客户端走 `/v1/internal/*`，
   不各自直连数据库。
4. **恢复有明确分支**：checkpoint 恢复含"标记跨过更晚的已完成回合 ⇒ 保留整段 WAL 并发 warn"这类分支，
   **不是简单的取最新**。

## 会话的"形态"

- **会话地图与分支**：会话之间可以有派生/分支关系（见 [session-map-branch-design.md](../../session-map-branch-design.md)）。
- **子 Agent 会话**：子 Agent 有自己的会话与账本，见 [子 Agent](subagents.md)。
- **文件级回退（rewind）**：**尚未实现** —— 是差距分析里"值得追"的新能力（需迁移），
  形态要改成「引擎侧持久化 + 网关端点」，**不照搬会话旁挂目录**。

## 延伸阅读

- [工具与审批](tools.md) —— 工具授权模式是运行状态的一部分
- [高级用法：checkpoint](../advanced/checkpoints.md)
- [生产：多实例](../production/multi-instance.md)
