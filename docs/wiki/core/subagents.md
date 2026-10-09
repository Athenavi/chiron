# 核心组件：子 Agent

> **权威来源**：[子 Agent 设计](../../subagent-design.md)（Profile 化 + **L0/L1/L2 三级消息模型**）。
> 本页只给「它是什么、代码在哪、边界在哪」。

## 它解决什么

主 Agent 把一段任务**委派**出去，由子 Agent 独立跑，再把结果**带回**。
难点不在"能起一个子进程"，而在**边界**：子 Agent 能看到什么上下文、能改什么、结果怎么合并、
失败/取消怎么收尾、以及**账本**怎么记。

## 代码在哪

```
python-engine/app/agent/
  subagent_runner.py    1258 行 —— 子 Agent 的执行器
  multi_agent.py         515 行 —— 多 Agent 编排
  collaboration.py       575 行 —— 协作
python-engine/app/subagent/
  scheduler.py      调度（并发天花板）
  store.py          账本持久化
  registry.py · registry_targets.py   注册与目标解析
  affinity.py       亲和/归属
  receipts.py       回执
  inherit.py        上下文继承（**边界就在这个文件**）
  remote.py         远端子 Agent
网关侧
  internal/api/subagent_cancel.go 等  取消与终态
前端
  frontend-vue/src/api/subagent.ts    前端调用面
```

## 三条边界（改之前先读）

1. **五元组边界由测试钉住**：子 Agent 能看/能改的范围是**断言过的**，不是约定俗成。
2. **继承是显式的**：`inherit.py` 决定带过去多少上下文 —— **默认不是"全带"**。
3. **终态必须明确**：取消 / 超时 / 失败都要落到账本（`store.py`），不能只靠日志。

## 与"多智能体"的区别

- **子 Agent**（本页）：一个主 Agent 委派出的执行单元，有账本、有边界。
- **多智能体编排**（[高级用法](../advanced/multi-agent.md)）：多个 Agent 之间的协作拓扑。

两者共用 `multi_agent.py` / `collaboration.py`，但**边界不同**：前者是"委派"，后者是"协作"。

## 延伸阅读

- [会话](sessions.md) —— 子 Agent 会话与主会话的关系
- [工具与审批](tools.md) —— `subagent` 工具在工具面上的位置
- [高级用法：多智能体](../advanced/multi-agent.md)
