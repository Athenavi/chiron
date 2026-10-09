# 子 Agent 设计（Profile 化 + L0/L1/L2 三级消息模型）

> **溯源（2026-10-09）**：本文件**原先缺失** —— 仓库里 **17 处**引用它，且多数**带小节号**
> （`§3.1`–`§3.4` / `§4.2`–`§4.4` / `§5.2` / `§7`）。现按**代码与注释**重建，**只写代码能证明的内容**；
> 与代码冲突时以代码为准。**小节编号沿用引用里的小节号**，方便逐条对照。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 总览（P0）

子 Agent 是**父 Agent 在工具调用内部发起的一次独立委派**：父 agent 调用
`subagent(task, profile="reviewer")`，引擎在**独立 session** 上跑一个完整的 AgentRuntime 循环
（独立消息历史、工具集、轮次与深度预算）。一次委派的完整生命周期由
`python-engine/app/agent/subagent_runner.py:1-16` 承载，它的五件事就是本设计的骨架：

1. 解析 Profile（`agents` 表 `kind='subagent'`，见 §3.1）；
2. 装配**独立** AgentRuntime（独立 session id / 消息历史、工具收窄、轮次与深度预算）；
3. 逐事件落 L0（`subagent_run_steps`，经脱敏）+ 汇总 usage；
4. 结束后生成 **L1 有效消息**（LLM 整理，失败回落提取式）并写 `subagent_runs`；
5. 返回 **L2** 回传文本（限长 + 不可信标记包装），供父 Agent 的当轮工具结果使用。

## 2. 三级消息模型（L0 / L1 / L2）

三条通道的**去向**是这套设计的核心约束（`app/tools/subagent.py:6-9`）：

| 级别 | 内容 | 落点 | 是否进父上下文 |
|---|---|---|---|
| **L0** 完整过程 | 每一步调用/输出 | `subagent_run_steps`（脱敏后落库） | **永不**（仅供审计/回放） |
| **L1** 有效消息 | LLM 整理后的摘要 | `subagent_runs.summary` | 后续 turn **只注入这一条** |
| **L2** 当轮回传 | 限长文本 + `<subagent-result>` 不可信包装 | 工具结果 | **仅当轮** |

## 3. 定义层与持久化

### 3.1 Profile（定义层）

`python-engine/app/agent/profile.py:1-19`：Profile **复用 `agents` 表**，用 `kind='subagent'` 区分。
承载列的选择是刻意的：

* **直接复用既有列**：`system_prompt` / `max_turns` / `timeout_seconds` / `skills` / `plugins` /
  `workflows` / `kb_id` / `visibility`；
* **放 `llm_config` JSON**：`model` / `effort` 与 Profile 专属项（`allowed_tools` /
  `disallowed_tools` / `read_only` / `write_paths` / `max_depth` / `output_max_chars` /
  `summary_max_chars` / `isolation`），形如
  `{"model": "deepseek-chat", "subagent": {"read_only": true, "allowed_tools": ["read_file"]}}`；
  也兼容顶层直接写这些键；
* **不放 `tools` 列**：该列的既有语义是"工具定义数组"（网关会作为 `tools` 透传给引擎），复用会冲突。

默认值（`profile.py:30-36`）：`max_turns=5` · `timeout_seconds=120` · **`max_depth=1`（默认禁止再委派）** ·
`output_max_chars=2000`（L2 上限）· `summary_max_chars=4000`（L1 上限）。

本模块**只做「DB 行 → ProfileSpec」的解析与校验，不涉及执行**。

### 3.2 运行记录落库

`python-engine/app/subagent/store.py:1-10` 的四个要点：

* **两张表分离**：`subagent_runs` 存 run 级元数据 + L1 精简摘要（要被父上下文读取）；
  `subagent_run_steps` 存 L0 完整过程（审计/回放，永不进上下文）；
* **批量写**：steps 先在内存缓冲，达阈值或结束时一次性 `executemany`，避免"每个 token 一条 SQL"；
* **脱敏**：所有写库文本先过 `app.subagent.redact.redact_text`（§7）；
* **缺表降级**：表未创建（老库）时只记 warning，**不让子 Agent 因持久化失败而失败**。

终态标签是**有界白名单**（`store.py:27-32`）：未知状态归 `other`；`partial`（部分完成，例如撞上预算）
与 `failed` / `cancelled` 都算终态 —— 标签无界会在 Prometheus 里分裂出新的时间序列。

### 3.3 数据分层（Redis = 运行期 · PostgreSQL = 权威）

`python-engine/app/subagent/runtime_cache.py:1-17`：

* **Redis**：状态、摘要、事件流、子节点索引、整树骨架；TTL **1h**（`DEFAULT_TTL=3600`，`:29`）自动回收；
  每 run 事件流上限 **500**（`DEFAULT_MAX_EVENTS=500`，`:30`）；
* **PostgreSQL**：终态与完整过程（§3.2）。

key 约定（**全部带 tenant 维度** —— 多租户隔离是硬要求，`:8-13`）：

```
subagent:{tenant}:run:{run_id}       Hash   status/summary/depth/parent_run_id/profile/usage/updated_at
subagent:{tenant}:ev:{run_id}        Stream 进度事件（MaxLen ≈ 500）
subagent:{tenant}:children:{parent}  Set    子 run 索引（递归树懒加载）
subagent:{tenant}:tree:{session}     Hash   run_id → 摘要 JSON（一次拉整棵树骨架）
```

写入点与 `EventSink`（§4.3）一一对应 —— sink 已做限流/合并，因此写 Stream 不会放大。
**Redis 不可用时全部静默降级（no-op），绝不影响子 Agent 执行**。

### 3.4 三级模型在代码里的落点

见 §2 的表；实现侧对应 `app/tools/subagent.py:1-13`（工具入口）与
`app/agent/subagent_runner.py:5-15`（执行器）。工具入口还保证：**Profile 缺失或解析失败时退回通用子 Agent**；
落库失败不影响执行；`mode` / `expert` / `max_turns` 参数与旧版语义一致（向后兼容）。

## 4. 事件与观测

### 4.1 子 Agent 在工具调用内部运行

这是 §4.3 的前提：子 Agent 跑在 `subagent` handler **内部**，它产生的 `AgentEvent` 默认被丢弃 ——
前端因此看不到子 Agent 在做什么（`app/agent/event_sink.py:3-5`）。

### 4.2 进度事件契约（`subagent.*`）

**线上契约**：前端按 `type` 的 `subagent.` 前缀识别（`app/agent/event_sink.py:7-17`）：

| 事件 | 含义 |
|---|---|
| `subagent.started` | 建树 + 卡片出现（载荷：`run_id` / `parent` / `depth` / `profile`） |
| `subagent.status` | 阶段：`queued` · `running` · `reasoning` · `responding` · `tool` · `retrying` · `completed` · `failed` · `cancelled` |
| `subagent.reasoning` | 思考增量（受限） |
| `subagent.text` | 回答增量（受限） |
| `subagent.notice` | 提示/告警（含"预览被截断"） |
| `subagent.approval` | 子 Agent 请求批准工具调用（载荷含 `tool_call_id` / `name` / `arguments`，**必达**） |
| `subagent.done` | **唯一终态**（载荷：`status` / `usage` / `result_ref`） |

事件帧上的结构化字段见 `internal/engine/python_client.go:565-573`：`run_id` / `parent_run_id` /
`depth` / `profile` / `status` / `truncated` —— 让前端按 `run_id` 建树、按 `depth`/`parent_run_id`
还原层级；**主对话事件不带这些字段**（`omitempty` 保证不污染既有帧）。

消费侧边界（`frontend-vue/src/views/ChatView.vue:1419`、`:2268`）：子 Agent 进度**只进侧边栏观测面板，
绝不混入主对话流**。

### 4.3 事件旁路（EventSink）

`app/agent/event_sink.py:1-5`：提供一条**有界旁路** —— 子事件经限流/合并写入队列，由
`app/main.py:1553-1561` 的事件生成器合并进**父 SSE 流**（那里经 contextvar 暴露 sink，
并把 runtime 事件与旁路事件合并输出；`session_id` 注入每条事件，否则会被投给所有订阅者）。

**必须遵守的边界（性能，§6）**（`event_sink.py:19-23`）：

* **绝不阻塞子 Agent**：队列有界，满时丢弃最早的**预览**事件并置 `truncated`；
* **限流**：同 `(run_id, 频道)` 的增量按 `merge_window` 合并，每 run 每秒事件预算有限；
* **终态不可丢**：`subagent.done` 绕过节流与丢弃策略，且终态前排空缓冲。

时钟（`event_sink.py:25-28`）：窗口判定用的时钟**可注入**（`clock`，默认 `time.monotonic`）——
既让"每秒预算"能被确定性测试（不 `sleep`），也修掉一处隐患：合并窗口与预算原用 `time.time()`（墙钟），
NTP 回拨会让窗口失效或卡住。注意 `SubagentEvent.ts` **仍是墙钟**（它是线上面，供前端排序/显示，不参与差值判定）。

### 4.4 运行观测 API（网关）

`internal/api/subagent_handler.go:3-15` —— **三个只读端点**，供前端画"递归层级树 + 侧边栏看运行中输出"：

```
GET /v1/subagent/runs?session_id=&parent_run_id=   整树/子节点列表
GET /v1/subagent/runs/{run_id}                     单 run 详情（状态/摘要/用量）
GET /v1/subagent/runs/{run_id}/events?limit=       输出过程（Redis Stream → DB steps 回落）
```

路由挂载见 `internal/api/routes_system.go:50-55`（都经 `authMW` + `rlMW`）。
数据源分层同 §3.3：**每个查询都"先 Redis 后 DB"**，Redis 不可用或键过期时自动回落；
响应里的 `source` 字段标明本次数据来自哪一层（`frontend-vue/src/api/subagent.ts:4-5`）——
排查"看不到进度"时先看它。

**安全**：全部要求鉴权；租户取自 claims（`TenantID` 回退 `UserID`）；DB 查询**强制 `tenant_id` 过滤**
⇒ 不存在跨租户读取子 Agent 输出的路径。中止类端点（取消 run / 取消会话下所有 run）走
"网关只做租户校验 + Redis 广播，真正取消由持有该 run 的引擎实例执行"（`routes_system.go:56-57`）。

## 5. 前端消费

### 5.1 侧边栏观测面板（不进主对话流）

子 Agent 事件与运行视图**只供侧边栏观测面板消费**（`ChatView.vue:1419`、`:2268`）；
主对话流只承载父 Agent 自己的 `text`/`tool_call` 等事件。

### 5.2 面板的三层数据来源

`frontend-vue/src/components/chat/SubAgentPanel.vue:5-12`：

1. **树骨架与状态**：`GET /v1/subagent/runs`（Redis → DB），**轮询**刷新；
2. **实时输出**：父组件（`ChatView`）把 SSE 的 `subagent.*` 事件按到达顺序传进来（`liveEvents`）；
3. **历史输出**：选中 run 时 `GET /v1/subagent/runs/{id}/events` 拉一次，再与实时事件拼接。

层级用**「`depth` 缩进 + 折叠」**呈现：后端返回的扁平列表已带 `depth`/`parent_run_id`，
折叠即按 `parent_run_id` 隐藏整棵子树 —— 比递归组件更省，且不会因深层嵌套而抖。

## 6. 性能边界

* 旁路**有界**：满时丢最早的预览事件并置 `truncated`，**终态不可丢**（§4.3）；
* 增量**合并**：同 `(run_id, 频道)` 按 `merge_window` 合并 + 每 run 每秒事件预算；
* L0 落库**批量写**（§3.2），避免逐 token 一条 SQL；
* Redis 侧静默降级（§3.3）—— 缓存不可用只影响"实时可见性"，不影响执行正确性。

## 7. 脱敏与列加密（D6 决策）

`python-engine/app/subagent/redact.py:1-12`。**为什么单独做**：子 Agent 的价值在于"主动读文件"，
因此它的 L0 过程里出现密钥的概率**远高于普通对话**（`.env`、CI 配置、证书、`kubeconfig` …）；
这些内容一旦落库，会通过备份、管理端查询、审计导出扩散。

策略：

* 只匹配**明确的密钥形态**，避免误伤正文（**宁可漏报，不做正则大扫除**）；
* 命中处替换为 `[REDACTED:<kind>]`，并返回命中计数写入 `subagent_runs.redacted_count`；
* 强要求部署可另开 **`SUBAGENT_STEPS_ENCRYPT`**，对 `content` 列做**列级加密**
  （复用 `internal/settings` 的 AES-256-GCM 机制）—— 本模块只负责脱敏层。

形态清单见 `redact.py:22-40`（`sk-ant-` / `sk-` / `gh[pousr]_` / `AKIA|ASIA` / `AIza` / `xox[abprs]-` /
`Bearer` / JWT / PRIVATE KEY 块 / **JSON 密钥字段**）。最后一条是必需的：三条审计流水（exec / approval / hooks）
都是先 `json.dumps` 再脱敏，此时键名带引号（`"password": "…"`），按 `password=` 写的那条**匹配不到**。

## 8. 已知边界（代码当前**不**保证的）

* **Redis 不是权威**：运行期视图可能因 TTL/不可用而缺失，此时靠 DB 回落（§3.3/§4.4）；
* **旁路可能丢预览**：队列满时丢最早的预览事件（置 `truncated`），**只有终态保证不丢**（§6）；
* **子 Agent 默认不能再委派**：`max_depth=1` 且工具集里剥离 `subagent`/`fleet`（§3.1）；
* **Profile 缺失不是错误**：退回通用子 Agent，不阻断委派（§3.4）。
