# 学习：概念速查

> 每个概念都指向**权威文档** —— 本页只做"一句话 + 去哪读"。

## 运行时

| 概念 | 一句话 | 去哪读 |
|---|---|---|
| **会话（session）** | 一段持久对话 + 它的运行状态 | [会话](../core/sessions.md) · [spec](../../session-runtime-spec.md) |
| **run** | 会话里的一次执行；有归属、有锁、可续跑 | [会话](../core/sessions.md) · [checkpoint](../advanced/checkpoints.md) |
| **回合（turn）** | 一次"模型请求 → 流式响应 → 工具执行"的完整循环 | [会话运行时 spec](../../session-runtime-spec.md) |
| **事件（event）** | 会话内可广播、可补发、可去重的有序消息 | [会话](../core/sessions.md) |
| **checkpoint** | 回合末落盘的 run 现场，用于恢复 | [checkpoint](../advanced/checkpoints.md) |

## 呈现

| 概念 | 一句话 | 去哪读 |
|---|---|---|
| **transcript** | 消息列表的渲染契约（几何 / 窗口化 / 单写者） | [transcript](../core/transcript.md) |
| **projection（投影）** | 把事件流折算成"该显示什么" | [transcript](../core/transcript.md) |
| **notice** | 非消息类提示行（护栏拦截 / 压缩 / 中断） | [工具与审批](../core/tools.md) |

## 能力

| 概念 | 一句话 | 去哪读 |
|---|---|---|
| **工具（tool）** | 模型可调用的动作；有策略与审批 | [工具与审批](../core/tools.md) |
| **护栏（guardrail）** | 拦住不该发生的动作；**任何授权模式下都在** | [安全与护栏](../advanced/guardrails.md) |
| **审批（approval）** | 人在回路；决策落审计 | [工具与审批](../core/tools.md) |
| **`toolsMode`** | 工具授权模式；**放宽的是审批，不是护栏** | [安全与护栏](../advanced/guardrails.md) |
| **子 Agent** | 委派出去的执行单元；有账本与边界 | [子 Agent](../core/subagents.md) |
| **hook（钩子）** | 在固定挂载点插入外部逻辑 | [钩子协议](../advanced/hooks.md) |
| **skill（技能）** | 目录型资产（`{name}/SKILL.md`），内置源在 `market/skills/` | [部署](../production/deploy.md) |
| **KB / RAG** | 外部文档 → 检索 → 注入上下文 | [知识库](../core/knowledge.md) |
| **记忆（memory）** | 关于用户/会话的事实，**与 RAG 不同源** | [记忆](../advanced/memory.md) |

## 平台

| 概念 | 一句话 | 去哪读 |
|---|---|---|
| **租户（tenant）** | 隔离单位；贯穿鉴权、配额、审计 | [架构](../core/architecture.md) |
| **归属路由** | 请求被路由到持有该会话的实例 | [多实例](../production/multi-instance.md) |
| **错误码** | 跨语言契约（Go ↔ 三语言 `errors.ts`） | [错误码](../production/error-codes.md) |

## 判据类（读差距分析时会遇到）

| 词 | 含义 |
|---|---|
| **可比性** | 只有"同一层"才可比；交付形态与 UI **不列入差距** |
| **切口** | 可"超越"对标项目的具体方向（见 [DSH 差距分析](../../dsh-gap-analysis.md)） |
| **证据阶梯** | 【枚举】<【文档】<【grep】<【实测】；没实测的要标出来 |

## 延伸阅读

- [概述](../overview.md) · [设计理念](../getting-started/philosophy.md) · [教程索引](tutorials.md)
