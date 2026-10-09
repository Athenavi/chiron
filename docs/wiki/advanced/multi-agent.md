# 高级用法：多智能体编排

> **权威来源**：[子 Agent 设计](../../subagent-design.md) + 代码
> （`python-engine/app/agent/multi_agent.py` · `collaboration.py`）。

## 三个层次，别混为一谈

| 层次 | 是什么 | 代码 |
|---|---|---|
| **子 Agent（委派）** | 主 Agent 派一个执行单元出去，带回结果；有账本与边界 | `app/agent/subagent_runner.py` · `app/subagent/*` |
| **多智能体（协作）** | 多个 Agent 之间有**拓扑**（谁和谁协作、怎么汇总） | `app/agent/multi_agent.py` |
| **协作机制** | 协作的具体手段（消息传递、共享状态、仲裁） | `app/agent/collaboration.py` |

**先分清你要的是哪一个** —— 大多数需求是"委派"，不是"多智能体"。

## 调度与天花板

- **并发有明确上限**：调度器（`app/subagent/scheduler.py`）限制同时跑多少个子 Agent。
- **写路径有仲裁**：避免多个子 Agent 同时改同一份状态。
- **归属与亲和**：`affinity.py` 决定子 Agent 跑在哪个实例上（多副本下必需）。

## 账本（别只靠日志）

`app/subagent/store.py` 持久化子 Agent 的运行账本：状态、结果、终态。
`receipts.py` 是回执。**取消 / 超时 / 失败都必须落到账本**，否则多副本下无法收敛。

## 上下文继承是显式的

`app/subagent/inherit.py` 决定带过去多少上下文 —— **默认不是"全带"**。
「收窄是否对模型可见」是一个**产品决定**，不是实现细节。

## 什么时候不该用多智能体

- 任务能被一次工具调用解决 ⇒ 用工具，不要起 Agent。
- 只是为了"并行" ⇒ 先看 `app/tools/jobs.py`（后台任务）够不够。
- 需要确定性 ⇒ 多 Agent 的调度会引入不确定性，先问是否真的需要。

## 延伸阅读

- [子 Agent](../core/subagents.md) · [会话](../core/sessions.md)
- [工具与审批](../core/tools.md)
