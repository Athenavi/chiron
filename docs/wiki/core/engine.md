# 核心组件：Python 引擎

> **权威来源**：`docs/architecture.md` · [会话运行时 spec](../../session-runtime-spec.md) ·
> [子 Agent 设计](../../subagent-design.md) · [钩子协议设计](../../hook-protocol-design.md)。
> 本页只给「它负责什么、代码在哪、有哪些契约」。

## 职责

**能力主要长在这里**：agent 循环、工具执行、子 Agent 编排、RAG、工作流、记忆、钩子。

## 代码在哪

```
python-engine/app/
  agent/          agent 循环与运行时
                  runtime.py（3274 行，最大）· loop.py · event_sink.py · resume.py
                  subagent_runner.py（1258 行）· multi_agent.py · collaboration.py
                  checkpoint.py · guards.py · prompt_engine.py · message_codec.py
  tools/          工具实现（39 个 .py）
                  core.py · run_code.py · sandbox.py · skill.py · memory.py · media.py
                  browser.py · terminal.py · jobs.py · graph.py · exec_audit.py
  subagent/       调度与账本：scheduler · store · registry · affinity · receipts · inherit · remote
  hooks/          钩子：manager · runner · config · events · audit
  queue/          任务队列：worker · producer · idempotency · dlq
  rag/            检索增强：builder · parser · retriever · hybrid_search · context_injector
  memory/         记忆：service · layers · manager · summary_store · profile_card · conflict_manager
  knowledge/ · skill/ · workflow/ · mcp/ · plugins/ · providers/ · llm/ · prompts/
  observability/ · trace/ · chaos/ · middleware/ · core/ · context/ · media/
```

顶层另有 `main.py`（装配）· `session_store.py` · `run_registry.py` · `event_bus.py` ·
`redis_client.py` · `audit_log.py` · `batch_processor.py`。

## 它身上的契约

| 契约 | 文档 |
|---|---|
| 会话运行时状态与遥测 | [session-runtime-spec.md](../../session-runtime-spec.md) |
| 子 Agent（Profile 化 + 三级消息模型） | [subagent-design.md](../../subagent-design.md) |
| 钩子协议 | [hook-protocol-design.md](../../hook-protocol-design.md) |
| run checkpoint 续跑 | [run-checkpoint-design.md](../../run-checkpoint-design.md) |
| 安全与可靠性（威胁模型） | [agent-safety-and-reliability.md](../../agent-safety-and-reliability.md) |
| 工具策略（与 Go 成对） | 同上 + `internal/api/tool_policy.go` |

**引擎侧的 `mypy app/` 是 strict 的**，`ruff check .` 零容忍 —— 改引擎前先看
[贡献与验收](../contributing/overview.md) 的 `python` job。

## 两个已知的"大文件"

- `app/agent/runtime.py` **3274 行**：其中 `AgentRuntime` 本体高度内聚在「一次 run 的状态机」上，
  **拆它必然触碰行为** ⇒ 拆分评估里明确**暂不动**；已拆出的是类型（`runtime_types.py`）。
- 上下文/消息处理函数簇**待按现状重新定界**再拆 —— 启动前务必重新实测行号，
  不要照搬文档里的旧数字。

## 延伸阅读

- [架构](architecture.md) · [会话](sessions.md) · [工具与审批](tools.md) · [子 Agent](subagents.md)
- [高级用法：钩子](../advanced/hooks.md) · [checkpoint](../advanced/checkpoints.md)
