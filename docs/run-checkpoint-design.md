# run 现场 checkpoint 续跑设计

> 状态：**设计待评审**（路线图 L4-1）。本文只做设计与边界划定，**不含实现**；评审通过后再按 §7 分批落地。
> 背景见 [多实例部署指南](deployment-multi-instance.md) §10「已知边界」与「结构性后续」。

## 0. 结论摘要

1. 现状：实例故障时该 run **直接中断**，用户重试即在新实例重建（`python-engine/app/run_registry.py` 文件头写明"只登记归属，不做现场状态的持久化/迁移"）。
2. 目标：把"整轮重跑"降级为"**从最近一次 checkpoint 续跑**"——已完成的 LLM 回合与工具调用不重放，用户侧 SSE 无感（缺口由既有 `Last-Event-ID` 重放补齐）。
3. 复用而非新建：workflow 侧**已有**节点级 checkpoint（`app/workflow/executor.py` 的 `load_checkpoint` + `app/workflow/engine.py` 的 `resume_state/resume_done` + 队列重投幂等），本设计把它同构地搬到**对话 run**，并复用既有的 run 租约/会话锁语义。

**范围**：对话 run（`POST /submit` → 网关 SSE 直连引擎 `/v1/agent/submit`）。
**不做**：跨实例的现场内存迁移（术语上的"热迁移"）、工具级 checkpoint、把对话 run 改走队列（见 §8）。

## 1. 现状盘点（可复用的既有机制）

| 机制 | 位置 | 现状 | 对本设计的价值 |
|---|---|---|---|
| 会话运行锁（网关） | `internal/api/gateway_router.go` 的 `submitHandlerFunc`：`AcquireSessionRunLock` + 60s 心跳 / TTL 5min | 防同会话并发提交；持有实例崩溃后 ≤5min 自动过期 | **防脑裂**：接管必须等锁过期，语义已存在 |
| run 归属租约（引擎） | `app/run_registry.py`：`engine:run:{session_id}`，TTL 300s，心跳 100s，`run_token` + 释放用 Lua 比对 | 只回答"哪个实例持有该 run"；网关据此路由（`internal/engine/discovery.go` 的 `applyRunAffinity`） | **接管判定**：TTL 过期即"原主已死"，可安全换 `run_token` |
| SSE 事件缓冲 | `internal/broadcast/hub.go`：per-session Stream（key `sse:events:<sid>`）、**200 条 + 1h**、`XADD` 流 ID 即事件 ID、`ReplayAfter` | 断线重连按 `Last-Event-ID` 补发缺口 | **用户无感**：续跑重建连接后缺口自动补齐（前提：缺口 ≤ 200 条且在 1h 内） |
| **workflow checkpoint（先例）** | `app/workflow/executor.py` 的 `load_checkpoint`（读 `workflow_instances.checkpoint = {state, done_nodes}`）、`app/workflow/engine.py` 的 `run_workflow(resume_state=…, resume_done=…, on_node_done=…)`（**节点级跳过**）、`app/queue/worker.py` 的 `_handle_workflow_run`（"读 DB checkpoint 跳过已完成节点，终态写回"） | **已上线**：同 instance 消息被重投/被另一实例接管时自动续跑 | **同构模板**：状态形状、落点、幂等键位置都可以照搬 |
| 队列 `engine:tasks` + 幂等表 | `app/queue/idempotency.py`（`task_idempotency`：claim/complete/fail + attempt 递增 + 清理）、`worker.py` 消费组 + `retry_count` + DLQ | workflow run **走队列**，天然可重投 | 幂等键的存储与生命周期可复用（见 §5） |
| 提交去重 | `internal/api/submit_handler.go` 的 `client_msg_id` → Redis `SET … NX EX`（注释：RedisClient 无类型化 SetNX） | 同一条消息最多执行一次 | 续跑**不得**新造 `client_msg_id`，否则会绕过这层 |
| 保留清理 | `internal/api/retention.go` 清 `turns`（网关）；`task_idempotency` 由引擎侧同类清理 | 各 30 天（`TURN_RETENTION_DAYS` / `TASK_IDEMPOTENCY_RETENTION_DAYS`） | §5 的不等式约束 |

## 2. 与 workflow run 的关键差异（为什么不照抄）

| 维度 | workflow run | 对话 run |
|---|---|---|
| 触发路径 | `POST /v1/graphs/{id}/execute` → **落库 + `XADD engine:tasks`**（`app/api/workflows.py`），由 worker 消费 | 网关 `RunSSE("/v1/agent/submit")` **直连引擎**（`internal/api/submit_handler.go`），无队列 |
| 执行单元 | 节点（输入/输出边界清晰，天然可幂等跳过） | LLM 回合（`app/agent/runtime.py` 的 `for turn in range(task.max_turns)`），回合内可调任意多次工具 |
| 现场状态 | `state` dict + `done_nodes` 集合 | messages 列表 + 工具结果 + 计划/目标 + 累计 token + 待审批项 |
| 用户可见性 | 轮询 `GET /v1/workflows/{id}/status` | SSE 实时流（首字延迟敏感） |
| 既有防护 | 无会话锁 | 会话运行锁 + run 归属租约 + 幂等 `client_msg_id` |

**结论**：workflow 的"**落库 + 队列重投**"两个条件在对话链路上都不成立，所以对话 run 需要：① 自己写 checkpoint（落点见 §4）；② 自己决定"谁来续跑"（触发见 §6）。其余（状态形状、幂等键语义、跳过已完成单元）与 workflow 完全同构。

## 3. 状态机

run 状态（新增 PG 表 `agent_runs`，见 §4；`workflow_instances.status` 是同类先例）：

| 状态 | 含义 | 迁入条件 | 迁出 |
|---|---|---|---|
| `running` | 引擎正在执行（持 `run_token`） | 网关提交后、引擎侧 `RunLease.start()` 成功 | → `checkpointed`（回合末）、`completed`、`failed`、`cancelled` |
| `checkpointed` | 已有 checkpoint，但**当前无实例持有**（心跳/租约过期判定） | 租约 TTL 过期且进程未上报终态**或**进程优雅停机前落盘 | → `resuming`（被接管）、`abandoned`（超过续跑窗口） |
| `resuming` | 新实例已取得租约、正从 checkpoint 继续 | 接管方拿到会话运行锁 + 新 `run_token` | → `completed` / `failed` / `cancelled` |
| `completed` / `failed` / `cancelled` | 终态（与现有一致） | 引擎上报 | — |
| `abandoned` | 放弃续跑（超出窗口/checkpoint 不可用） | 见 §5 的不等式被打破时 | — |

要点：
- **`running` → `checkpointed` 的判定不在引擎内部**（故障进程无法自证死亡），而由**接管方**依据 `engine:run:{sid}` 租约过期 + 会话运行锁可获取来判定（两个既有信号，见 §1）。
- `resuming` 与 `running` 的区别只在**入口**（是否读 checkpoint），对 SSE/计费/审计应不可区分。

## 4. checkpoint 的内容与落点

### 4.1 落点：**回合边界**

- 依据：`app/agent/runtime.py` 的主循环是 `for turn in range(task.max_turns)`（工具调用发生在回合内）→ **回合末**是唯一既"粒度够粗"又"语义完整"的边界。
- 为什么不做到工具级：回合内工具可能是非幂等副作用（`shell_exec`、写文件、外部调用），逐工具落盘会把"不可续跑"的判断成本推给每一个工具；workflow 先例同样是**节点级**而非语句级。
- 为什么不整轮重跑：正是本设计要消除的成本（长回合 = 多轮 LLM + 多工具，重跑等于重复计费与重复副作用）。

### 4.2 内容（形状对齐 workflow 的 `{state, done_nodes}`）

```
agent_runs.checkpoint = {
  "turn_index":  <int>,          # 下一个待执行回合（已完成 0..turn_index-1）
  "messages":    <jsonb>,        # 截至上一回合末的消息快照（见 4.3）
  "done_tools":  [<tool_call_id>...],  # 已完成工具调用（跳过依据）
  "pending":     {approval_id|question_id: …},  # 未决的审批/提问（重启后按 §6 处理）
  "turn_id":     "<uuid>",       # 本轮 turn（计费/turns 表口径，续跑沿用同一个）
  "usage":       {input_tokens, output_tokens, cached_tokens},  # 累计（续跑不重复计入）
  "last_event_id": "<stream-id>",# 供续跑后重建 SSE 时从该 ID 继续
  "instance_id":  "<原主>",       # 诊断用
  "saved_at":     "<ts>"
}
```

### 4.3 存储

- **唯一事实源 = PostgreSQL**（部署指南 §7：PG 已外置、是唯一持久层）；新增表 `agent_runs`（`session_id, run_token, status, checkpoint jsonb, created_at, updated_at`），与 `workflow_instances` 同构。
- **热镜像 = Redis**：在既有 `engine:run:{sid}` 值里加一个 `checkpoint_at` 字段（不搬 payload），让接管方能 O(1) 判断"有没有可续的现场"。
- `messages` 快照的体积控制（长会话）：只存"尾部窗口 + 摘要"还是整段，见 §8 未决项 —— 建议初版存**尾部 N 条 + 既有压缩摘要**（与 `app/agent/runtime.py` 的压缩策略同源）。

### 4.4 写入时机与成本

- 回合末**异步**写（不阻塞 SSE 首字）；失败只降级恢复粒度（与 workflow 的 `persist_checkpoint` 注释一致：`workflow checkpoint persist failed (recovery granularity only)`）。
- 频率 = 回合数（`max_turns` 量级），不是 token 量级 → 对 PG 压力与 `turns` 表同阶。

## 5. 幂等边界（评审重点）

| 边界 | 规则 | 依据/风险 |
|---|---|---|
| 已完成回合 | **不重放**：从 `turn_index` 继续 | workflow 同款（`engine.py`："已完成节点跳过（其输出已在 resume_state 中）"） |
| 已完成工具调用 | **不重放**：按 `done_tools` 跳过 | 否则非幂等工具会二次副作用（重复写文件、重复外部调用） |
| 非幂等工具**中断在中间** | 保守策略：视为**已完成**，在续跑的首条事件里显式告知"该工具结果可能不完整" | 无法证明"未执行"；宁可少做不漏告知。选项见 §8 |
| 计费 | 沿用同一 `turn_id`；`usage` 累计写入 checkpoint，续跑只补差额 | 依据：`turns` 表按 turn 一行（`CreateTurn`）、`billing_records` 按 usage 落账 |
| 提交去重 | 续跑**不新造** `client_msg_id` | 否则绕过 `SET … NX EX` 去重（§1） |
| 审批/提问 | 未决项随 checkpoint 存；续跑后**不自动重放**，由用户重新确认（前端已有 approval 卡片协议） | 与"陈旧审批被拒"的现有语义一致（部署指南 §10 第一条） |
| **保留期不等式** | `checkpoint_ttl ≤ TASK_IDEMPOTENCY_RETENTION_DAYS`（30 天），且**只有幂等键仍有效时才允许续跑**；超出即 `abandoned` | 幂等表被清理后，同 `task_id` 的 claim 会被当成**新任务**（`app/queue/idempotency.py` 的 claim 语义）→ 必须让 checkpoint 先失效 |

## 6. 恢复路径

**谁触发**（两条，建议都做）：
1. **用户重试**（现状已有）：新实例拿到会话运行锁后，先查 `agent_runs` 是否处于 `checkpointed` → 是则续跑，否则新建。
2. **reconciler**（新增，可选）：周期扫描"`engine:run:{sid}` 租约已过期 + `agent_runs.status='running'`"的行 → 标记 `checkpointed` 并按会话排队接管。参照 workflow 侧的 `requeue_pending_workflows`（`app/api/workflows.py`，服务启动时跑一次的最小闭环）。

**接管不变量**：
- 必须同时满足：会话运行锁可获取（旧主 ≤5min 已过期）**且** 新 `run_token` 与 Redis 归属不一致（旧主确实不在）。
- 接管后**重建 SSE**：网关侧以 checkpoint 里的 `last_event_id` 作为 `Last-Event-ID` 起始，缺口由 hub 重放补齐（§1）。

**降级路径**（必须显式）：checkpoint 不可读 / 超出窗口 / messages 快照损坏 → 标记 `abandoned`，回落到现状语义（该 run 中断、用户可整轮重试），并在 SSE 上给出明确原因（不是静默）。

## 7. 分步落地（每步独立可验收）

| 批 | 内容 | 验收 |
|---|---|---|
| 1 | 本文（设计评审） | 评审通过，§8 的未决项有结论 |
| 2 | `agent_runs` 表 + 回合末写 checkpoint（**只写不读**） | 跑一轮对话后能读到合法 checkpoint；写入失败不影响对话（对照 `turns` 表落库方式） |
| 3 | 恢复路径（用户重试时续跑）：跳过已完成回合与工具 | 集成测试：跑到第 2 回合 kill 引擎 → 重试 → 断言第 1 回合**未重放**（日志/`done_tools`/计费三者一致） |
| 4 | reconciler 自动接管 + 指标（`abandoned`/接管次数/续跑省下的回合数） | 杀掉实例后**无需用户操作**即续跑完成；指标可查 |
| 5（可选，单独评审） | 工具级 checkpoint / 跨实例现场迁移 | —— |

每批都必须能独立回滚：批 2 只增表与写入（无行为变更），批 3 才改执行路径。

## 8. 未决问题（需评审拍板）

1. **对话 run 是否改走 `engine:tasks` 队列**？—— 那样能直接复用"队列重投 + 幂等表"，但会把首字延迟、审批往返、SSE 直连语义全部改掉，属更大重构；本设计**不假设**它成立。
2. `messages` 快照的形状：整段 vs 尾部窗口 + 摘要（体积 vs 恢复保真度）。倾向后者，但需给出"尾部 N 条"的 N 与摘要来源。
3. 非幂等工具"中断在中间"的判定：能否从既有 `tool_calls` 表反推"已开始未结束"？若可，则把该工具标记为"需用户确认后重放"，比 §5 的保守策略更精确。
4. reconciler 的扫描频率与"接管风暴"限流（多实例同时扫描同一批 `checkpointed`）。
5. checkpoint 的 TTL 具体值（§5 只给了上界 30 天；实际建议 1–3 天，与"用户还会回来重试"的时间窗对齐）。

## 9. 明确不做（避免范围蔓延）

- **不**迁移进程内现场（`AgentRuntime` 实例、`PersistentTerminal`、MCP 连接）——与部署指南 §10 的既有结论一致；
- **不**改 SSE 协议（事件 ID 已是 Stream ID，续跑只需沿用 `Last-Event-ID`）；
- **不**做工具级 checkpoint（批 5 才评估）；
- **不**引入新的存储组件（只用 PG + Redis）。
