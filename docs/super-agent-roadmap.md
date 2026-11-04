# Super-agent 开发方向企划

> 依据：《Super-agent 设计探索：迈向更自主的 Agent 系统》（bilibili 专栏
> `opus/1265361021143351318`，作者 Yujia Qiao / DeepSeek Harness 团队；文首明确声明
> "并非对 DeepSeek Harness 现有产品功能的介绍"）。
>
> 本文是**从该文到 Chiron 的落地企划**，不是对该文的转述。书写口径遵循本仓库约定：
> 引用文章处标"文中"，描述 Chiron 处一律给出可 `grep` 的文件位置；**以代码为准**，
> 凡代码证不了的（例如"计划中""应该能"）都不写进"现状"。
>
> 本文档不改变任何既有契约；它引用的既有文档小节编号未作改动。

## 1. 文章的目标模型

文中把 Super-agent 拆成四个基础特征 —— **连续、高效、主动、持久** —— 再用五个模块去实现：

| 模块 | 文中要点 |
|---|---|
| §2 双层架构 | 常驻 **Router 层**（全局序点、协调、记忆，不执行具体任务，单常驻 LLM Session + 批处理）＋ **Worker 层**（上下文隔离、挂全工具、只向 Router 汇报、不直接对用户） |
| §3 事件驱动 | 事件过滤 Fast/Slow Path；Thread 订阅**直通**（Router 只做冷启动与跨任务协调，Thread↔Worker 多对多）；上下文 Inline & Spill ＋ 链式召回；投机执行 |
| §4 面向人的交互 | 全双工/异步 ReAct Loop（`run_in_background` 类异步工具 + `job_kill` 类终止）；多信道（IM ≤100 词、GitHub/邮件结构化） |
| §5 计算/状态/环境分离 | 无状态计算容器 + DB 存 Session；端侧轻量 daemon 只执行命令不带状态，BYOM |
| §6 Watchdog | 主系统之外独立的 Watchdog Session，定时消息驱动，判断 Worker 有无实质进展 |

文中 §7 的实验给出两条对企划有直接影响的结论：Super-agent 的 p50/p95 为 12.4/15.7 分钟；
**去掉"订阅直通"后 Router 决定数 +≈30%、读缓存 token 从 2323 万涨到 3177 万（+≈37%）**。
即"直通"是省 token 的主因，不是美学选择。

## 2. 现状盘点

### 2.1 双层架构：有 Worker，没有 Router

- **Worker 的形态已经存在**：`python-engine/app/subagent/` 下的子 Agent 上下文隔离、
  只向父 Agent 汇报、不直接对用户，并有 L0（步骤永不进父上下文）/ L1（只注入一条摘要）/
  L2（当轮工具结果，`<subagent-result>` 不可信包装）三层注入约定，见
  [subagent-design.md](subagent-design.md)。默认 `max_turns=5`、`timeout_seconds=120`、
  `max_depth=1`。
- **没有 Router**。全仓没有"常驻会话"概念：`grep router` 命中的
  `python-engine/app/core/task_router.py` 是**单次请求内**的多工作台任务编排
  （能力匹配 → 子任务 DAG → 聚合），随请求结束而消失，跨回合不留状态；
  `python-engine/app/agent/event_sink.py` 的常驻投递器只负责把子 Agent 事件送出去，不做决策。
- 三个驱动入口都是"一次性"的：HTTP `POST /submit`、cron（`internal/api/cron_scheduler.go`，
  60s 重载 `cron_jobs`）、子 Agent 结束后的 `agent_followup`
  （`python-engine/app/subagent/followup.py` → `internal/api/agent_followup.go`，复用
  `HandleSubmit`）。没有一个"一直在线的协调者"。

### 2.2 事件驱动：有队列与投影，没有 Trigger

- **事件源是出向的**：`python-engine/app/event_bus.py` 把
  `agent.start/agent.complete/agent.error/knowledge.*/system.alert` 推给网关 Webhook；
  `internal/api/ent_webhook_dispatcher.go` 用消费组 + 幂等头 + 退避/DLQ 投递出去。
  这些是**告诉外部发生了什么**，不是**外部发生了什么来驱动我们**。
- **唯一的入站触发是窄形态**：`internal/api/gateway_router.go` 的
  `POST /v1/hooks/{jobID}` → `HandleCronWebhook`，只能触发一条预注册的 `cron_jobs.task`，
  凭据是 `cron_jobs.webhook_token`（`migrations/sql/init.sql` 的 `cron_jobs` 表；
  列表接口只下发末 4 位提示，见 `internal/api/admin_ops.go`）。它不是通用事件入口：
  没有事件类型注册、没有过滤、没有"这条事件该进哪个会话"的路由。
- **没有 Fast/Slow Path 之外的任何事件过滤**，也没有 Thread 订阅与直通。
- **上下文侧反而是最接近的**：[context-and-delegation-design.md](context-and-delegation-design.md)
  已把 `messages`（`seq` 单调，迁移 `0016_messages_seq`）、归档
  （`0018_message_archives`、`session_folds` 见 `0019`）与 `read_archive`
  （`python-engine/app/tools/archive.py`）落成 Inline & Spill + 可召回的形态；
  缺的是文中那种"以 Trigger/Thread/Worker 唯一 ID 组成的引用图"，现在是按会话维度组织。

### 2.3 面向人的交互：异步工具齐了，回投也接通了

- **全双工的一半已经落地**：`python-engine/app/tools/jobs.py` 注册了
  `run_in_background`（立即返回 job id）、`job_output`、`job_kill`；
  `python-engine/app/tools/job_runner.py` 是子进程执行器，带 kill flag 轮询与超时；
  `python-engine/app/tools/taskboard.py` 提供跨 Agent 共享任务板。文章 §4.1 描述的
  "异步工具 + 终止工具"在工具面上是对得上的。
- **"回投"原本断在中间，现已接通**：`job_runner.py` 只把状态写进 Redis
  （`_write_meta` / `_write_result`），本身不发任何消息；缺口在网关侧补上 ——
  `queue/worker.py::_handle_tool_job` 在终态后调 `_notify_job_finished`，投递
  `kind="job"` 的回投信号，经 `internal/api/agent_followup.go` 的 `followupKinds`
  注册表回到 `HandleSubmit`，与会话里正常的回合走同一条路。**默认关闭**
  （`users.settings.followup_on_job_finished`）：这条通道会在用户没动手时自己烧 token。
  取消**不**发 —— 那是用户自己停的，再唤起一轮只是噪音。
- **打断与改动分成了两条路**：会话运行锁 `AcquireSessionRunLock`（`internal/api/session_coord.go`，
  TTL 5min、60s 续期）在锁被占时仍直接 400 拒绝新**提交**（提示"stop it or wait"）——
  这条没有变，也不该变：同一个会话并发两个 runtime 是不变量。变的是"跑着的时候想改方向"
  不再只有"停掉重开"：`POST /v1/agent/steer`（`submit_handler.go::SubmitSteer`）把文本注入
  正在跑的 run，引擎在**轮次边界**消费（工具执行到一半插消息会让 `tool_calls` 与结果错位）。
  这一条是**新建**的：它此前只写在 `docs/context-and-delegation-design.md` §9.3，且那份文档
  写的是子 Agent 的 `chiron:subagent:steer:{run_id}`，`python-engine/app` 下无实现 ——
  所以主会话这条通道不是"接上子 Agent 已有能力"，是照 `agent/interrupt.py` 的形状另做的一条。
  前端入口见 P0-3。
- **多信道不存在**：全仓无入站 IM（`feishu/lark/dingtalk/wecom/telegram/slack` 只在
  `internal/auth/oauth2_exchanger.go` 里作为 SSO 换 token 出现）；通知只有出向的
  `internal/notify/notify.go`（站内信不可关闭）+ 邮件。前端交互集中在
  `frontend-vue/src/components/chat/`（含 `TodoPanel.vue` 的任务投影）。

### 2.4 计算/状态/环境分离：这一条最接近满分

- **无状态计算 + DB 存 Session 已是现状**：引擎不持有回合状态，状态在 PostgreSQL；
  Redis 只做锁、限流、SSE 事件流与有界缓冲（见 [architecture.md](architecture.md)）；
  SSE 事实源是 `sse:events:<sid>`，断线可用 `ReplayAfter` 续读。
- **隔离执行有独立进程**：`sandbox-service/` 提供 `/v1/internal/exec/*`，未配内部令牌即
  503 fail-closed，基线名单复用引擎 `app/tools/sandbox.py`；但**无命名空间/seccomp**，
  它是"受限容器"而不是"不可逃逸沙箱"。
- **判定与执行已分信任域**：`internal/api/tool_broker.go` 的
  `POST /v1/internal/tool-authorize` 由服务端独立判定，`delete`/`external` 即使 `yolo`
  也要求人工确认 + 二次校验（[agent-safety-and-reliability.md](agent-safety-and-reliability.md)）；引擎只连
  `read_only:true` 的 MCP server（fail-closed）。
- **端侧 daemon 不存在**：`desktop-local/` 是"本地一体栈"（`CHIRON_LOCAL_*` 全套，
  含内嵌 PG/Redis/迁移），与云端是 `local | cloud` **二选一的设备级模式**
  （[desktop-local/contracts.md](desktop-local/contracts.md)），不是文章 §5.2 说的
  "云端是脑、端侧只当手"的 BYOM 形态。

### 2.5 Watchdog：有机制层自愈，没有模型层判断

- **进程级**：`python-engine/app/instance_liveness.py` 心跳 + `participates()` 安全阀；
  `agent/checkpoint.py` 状态机（`running/checkpointed/resuming/completed/failed/cancelled/
  abandoned/awaiting_input`）+ `agent/resume.py` + `main.py` 启动的 run reconciler，
  能接管"进程没了但 run 还活着"的情况；`awaiting_input` 刻意不锁会话、不被接管。
- **回合级**：`agent/loop_guard.py`（`repeat`/`no_progress`/`oscillation`，命中先回灌提示、
  连续两次才终止）；`agent/recovery.py`（纯函数 `decide`，`SAFE_RETRY_BUDGET=1`，
  指纹而非计数，fail-closed 走 `REVIEW`）；`agent/guardian.py` 只对 `write` 级可放行，
  评审失败回退人工。
- **缺的是文中 §6 的那一层**：以上都是"事件发生后的机械反应/阈值判断"，没有独立于主循环的
  会话去回答"这个 Worker 是在推进，还是在兜圈子/偏离目标"。`loop_guard.py` 只检测不恢复，
  且判据是工具参数指纹，不是任务语义。

## 3. 差距矩阵

| 文中能力 | Chiron 现状 | 结论 |
|---|---|---|
| 常驻 Router 会话 | 无 | **缺** |
| Worker 上下文隔离、只上报 | `subagent/` L0/L1/L2 | **有** |
| 事件过滤 Fast/Slow Path | 无 | **缺** |
| Thread 订阅直通 | 无 Thread 概念 | **缺**（文中实验表明它是省 token 主因） |
| Inline & Spill | `messages.seq` + 归档 + `read_archive` | **有** |
| 链式召回（ID 引用图） | 按会话维度召回 | **部分** |
| 投机执行 | 无 | **缺**（研究性） |
| 异步工具 + 终止 | `run_in_background`/`job_output`/`job_kill` | **有** |
| 后台结果回投会话 | `queue/worker.py::_notify_job_finished` → `agent_followup.go` → `HandleSubmit` | **有**（按用户开关，默认关闭；见 P0-1） |
| 边跑边干扰（steer 主会话） | 锁占即 400 拒绝；steer 是并行的第二条通道 | **有**（引擎 / 网关 / 前端三侧已接通，见 P0-3） |
| 入站 IM / GitHub 信道 | 无（只有 SSO 出向） | **缺** |
| 无状态计算 + DB 存 Session | 已是架构基线 | **有** |
| 隔离执行环境 | `sandbox-service`（受限容器） | **部分** |
| 判定/执行分信任域 | `tool_broker` + `guardian` | **有** |
| 端侧无权 daemon / BYOM | `local` / `cloud` 二选一一体栈 | **缺**（形态不同） |
| Watchdog（语义级进展判断） | `loop_guard`/`recovery`/reconciler 之外，另有 `agent/watchdog.py`：结构级停滞判断（三档，默认只观察） | **部分**（结构判断已落地，模型层判断仍缺；见 P1-3） |

## 4. 开发方向

排序原则：**先接零件（无 schema 变更、可独立上线），再补对象（要迁移），最后做研究项。**
文中 §7 的实验说明"直通"收益最大，但直通的前提是"有 Thread 对象"——Chiron 现在连事件入口
都只有一个 cron 专用的窄形态，所以直通排在 P1 而不是 P0。

### P0：把已有零件接通（不动 schema）

#### P0-1 后台任务完成回投会话

- 动机：文中 §4.1 的"全双工"要求工具结果能自己回到对话里；Chiron 已有
  `run_in_background`，但 `job_runner.py` 只写 Redis，用户必须主动轮询 `job_output`。
- 做法：给 `tool_job` 加一条"终态回投"分支，**复用** `agent_followup` 已验证的通道
  （`engine:tasks` → `internal/api/agent_followup.go` → `HandleSubmit`），而不是新造一套。
  必须继承该通道的既有约束：每小时上限（当前 `DEFAULT_MAX_PER_HOUR=3`）、
  `SET NX` 幂等、**禁级联**、Redis 不可用则不投递。
- 已落地：
  - 引擎侧 `queue/worker.py::_handle_tool_job` 在终态后调 `_notify_job_finished`，
    投递 `kind="job"` 的回投信号（`run_id` 用 `job:{job_id}`，与子 Agent 的回投键不撞）。
    **取消不发**：那是用户自己停的，再唤起一轮只是噪音。
  - 网关侧 `agent_followup.go` 新增 `jobFollowupSwitch`（`users.settings.followup_on_job_finished`），
    **默认关闭**，按用户显式开启；开关判定放在幂等与资源闸门**之前** —— 否则开关关着时
    先占了幂等位，用户之后打开开关，这些作业也永远不会再被回投。
  - 注入文案分 `job` / `subagent` 两种（`<job-result>` / `<subagent-result>`），
    都包成"数据而非指令"：命令输出是外部世界写进来的内容。
  - 限流键按 `kind` 分开（`agent_followup:rate:{kind}:{session_id}`）：否则没开 job 回投时，
    每次命令结束都白耗一次子 Agent 的额度。
  - 前端 `messages.source = 'job_followup'` 独立小字（"后台任务结束 主 Agent 自动续跑"）。
- 验收：一次 `run_in_background` 在 turn 结束后完成，会话里出现一条由任务结果驱动的新回合；
  重复投递被幂等键挡住；关掉回投开关时行为与今天完全一致。
- 风险：把"任务完成"变成"自动开新回合"会放大计费与 token 消耗，需要按租户/会话的开关与配额。

#### P0-2 统一入站事件入口（窄口径）

- 动机：文中 §3.1 要求外部事件能驱动系统；Chiron 的 `POST /v1/hooks/{jobID}` 只能触发
  预注册的 cron task，新增一个事件源就要新增一个端点。
- 做法：把 `HandleCronWebhook` 的鉴权、幂等、审计骨架抽成通用入口，事件类型与目标会话
  由**已注册的订阅**决定，而不是由 URL 决定。第一阶段只允许"投递到指定会话并开一轮"，
  不做 Fast/Slow Path 过滤。
- 验收：一个非 cron 的事件源接入不需要改路由；未知事件类型默认丢弃并记审计；
  幂等与 `task_idempotency`（`migrations/sql/init.sql`）口径一致。
- **第一阶段范围（已定）**：只接通**内部可信事件源**（`kind` ∈ {`subagent`, `job`}，
  走 `internalTokenMW` 保护的 `/v1/internal/agent-followup`）。**不开放**外部 webhook
  自注册订阅 —— 那会新增信任边界，凭据轮换/重放防护/限流必须连同
  `docs/error-codes.md` 契约一起设计，属于独立批次，不能与通道泛化混在一起做。
  泛化的落点是把 `agent_followup.go` 里硬编码的 `kind` 分支改成事件类型注册表，
  未知类型落审计后丢弃。
- 风险：这是**新的对外攻击面**。凭据轮换、租户隔离、重放防护、限流必须与
  `docs/error-codes.md` 契约一起设计，不能先放通再补。

#### P0-3 主会话的"边跑边干扰"

- 动机：文中 §4.1 的全双工不只是"能异步"，还包括"跑着的时候能改"。当前锁被占直接
  400 `this session already has a turn in progress`，用户只能等或停。
- 做法：**新建**一条 steer 通道（不是接子 Agent 已有能力 —— 它只存在于
  `docs/context-and-delegation-design.md` §9.3，代码里没有）。形状照 `agent/interrupt.py`：
  本地快路径 + Redis 跨副本、会话归属 + 调用者 `x-user-id` + `run_token` 三重校验、
  **只在轮次边界**消费（工具执行到一半插消息会让 `tool_calls` 与 tool 结果错位）、
  收尾清队列以免误伤下一次会话。差别只在读到的是一条待注入文本而非停止信号。
- 已落地（引擎侧）：`python-engine/app/agent/steer.py`（`push_steer`/`drain_steer`，
  限流 6 条/分钟、单条 ≤2000 字符、TTL 300s，超限**报错而非静默丢弃**）、
  `runtime.py` 轮次边界注入 `<user-steer>` 并落库、`POST /v1/agent/steer`
  （与 interrupt 共用 `_authorize_run_input`）。
- 已落地（网关侧）：`POST /v1/agent/steer`（`submit_handler.go::SubmitSteer`，照
  `SubmitInterrupt` 的 run-affinity 路由）。与 interrupt 的一处刻意偏离：引擎用
  `200 + ok:false` 表达拒绝，网关把它翻成 403/409/429/400 —— 照抄 200 会让"没送到"
  看起来像"送到了"，而 steer 的全部价值就是让用户相信话带到了。
- 已落地（前端侧）：SSE `steer` 事件在 `ChatView.vue` 的 `onSSEMessage` 里落一行
  `common.steer_applied` 小字（正文已作为 user 消息进历史，不再贴一遍）；发送入口是
  `ChatInput.vue` 操作行的 steer 按钮（`BranchesOutlined`，**只在"正在生成 + 有草稿"
  时出现**）→ `emit('steer')` → `ChatView.onSteer` → `api/index.ts::steerRun`。
  不与 Enter 共用：生成中的 Enter 已经是"打断并发送"，两条相反语义挤一个键必然误触。
  投递失败把原文退回输入框（`ChatInput.setDraft`）而不是静默清空 —— 这条通道的拒绝多
  是临时的（429 限流 / 409 那一轮正在收尾），用户多半想重试一次。
- 验收：turn 进行中发送补充，能在下一轮工具循环前生效；补充内容进 `messages`，
  可被 SSE 回放；无活跃 run 时显式失败而不是入队。
- 风险：注入点必须是有界检查点，否则会破坏 `history_writer.py`"每轮结束写一次"的约束。

### P1：补对象（需要 Alembic 迁移）

#### P1-1 Thread 作为一等对象 + 订阅直通

- 动机：文中 §7.2 的实测（去掉直通，Router 决定数 +30%、token +37%）说明这是收益最大的一项。
- 做法：新增 thread/subscription 对象，让事件按订阅**直接**进目标会话，不经过协调者；
  协调者只在冷启动与跨任务时介入。迁移必须遵循"扩展-迁移-收缩"，且要能回答
  "订阅丢失后能否从事实重建"（本仓库对缓存的一贯要求）。
- 验收：同一事件在"有订阅"与"无订阅"两种配置下的决定数与 token 有可对比的埋点；
  多副本下同一订阅不被重复执行。
- 风险：Chiron 是**多租户 SaaS**，thread 数量随租户线性增长；文章的场景是单团队 harness，
  成本模型不可直接照搬（见 §6）。

##### P1-1.1 数据模型（2026-11-04 按 §6 问题 3 的裁决冻结）

裁决是**串行排队**，因此 Thread 不承担执行职责，只承担"这条事件从哪来、要投给谁"。
执行侧一行不改：`agent:run-lock:<session_id>` 仍是唯一的并发护栏。这条边界是整个模型的
分界线 —— 凡是需要"并发跑两个"的需求都不进 P1-1。

三张表，全部是**纯扩展**（不改既有表、不动 `messages`）：

```sql
-- 事件来源。一条 thread = 一个外部事件的上下文，不是执行单元。
create table threads (
  id            varchar(36) primary key,
  tenant_id     smallint     not null references tenants,
  user_id       varchar(36)  not null references users,
  source_kind   varchar(32)  not null,  -- 'subagent' | 'job'（与 P0-2 的 followupKinds 同集合）
  external_key  varchar(128) not null default '',  -- 外部系统的幂等键，去重靠它
  created_at    timestamptz  not null default now()
);
-- 同一事件重复入站只应产生一条 thread：多副本 + 队列重投是常态。
-- 取值集合刻意**不含 webhook**：P0-2 第一阶段只接内部可信源，外部自注册
-- 会新增信任边界，属独立批次（见 P0-2 与 §5）。
create unique index threads_idem on threads (tenant_id, source_kind, external_key)
  where external_key <> '';

-- 订阅：事件按它直接找到目标会话，绕开协调者。
create table thread_subscriptions (
  id         varchar(36) primary key,
  thread_id  varchar(36) not null references threads(id) on delete cascade,
  session_id varchar(36) not null,
  status     varchar(16) not null default 'active',  -- 'active' | 'paused'
  created_at timestamptz not null default now()
);
create index thread_subscriptions_by_session on thread_subscriptions (session_id, status);

-- 投递事实：这是"订阅丢了能否重建"的答案 —— 订阅是**用户配置**（丢了要人重填），
-- 而"哪些事件已经投过"是**事实**，必须能查、能续、能去重。
create table thread_deliveries (
  thread_id  varchar(36) not null references threads(id) on delete cascade,
  event_key  varchar(128) not null,
  session_id varchar(36) not null,
  turn_id    varchar(36) not null default '',  -- 空 = 已入队未开跑
  status     varchar(16) not null,             -- 'queued' | 'running' | 'done' | 'dropped'
  created_at timestamptz not null default now(),
  primary key (thread_id, event_key)           -- 同一事件的第二次投递直接冲突，天然幂等
);
```

投递路径（全程复用既有语义，不新造并发模型）：

1. 事件进站 → 按 `(tenant_id, source_kind, external_key)` 取或建 thread。
2. 查 `thread_subscriptions` 得到目标会话；无订阅则该事件到此为止（**不是**回退给
   Router 猜 —— 那会让"有没有订阅"变得不可观测，也就没法做 §6 要的那组对照埋点）。
3. 写 `thread_deliveries(status='queued')`；主键冲突 = 已投过，直接返回。
4. 试抢 `agent:run-lock:<session_id>`：
   - 抢到 → 立刻开跑，`status='running'`。
   - 抢不到 → **留在队里**（`status='queued'`），不重试、不轮询；由当前持有者在 run 收尾时
     drain 该会话的队列，取最早一条接着跑。这与 P1-2 接管 `skipped_busy` 的区别是刻意的：
     用户提交被拒要立刻告诉人，而后台事件本来就没人盯，排队才是对的。

队列上限与丢弃（承认的代价，必须显式）：每会话待处理上限 `maxPendingThreadEvents = 20`。
超限时新事件落 `status='dropped'` 并写审计 `superagent.thread.dropped_overflow` ——
**不静默丢**。上限的理由是前面那 20 条正是一个 run 可能积压的量级，再多说明这个会话
根本消化不完，继续攒只会把延迟推到没人能接受。

不变量对照（这是选串行排队的全部理由）：

| 不变量 | 位置 | 串行排队下 |
| --- | --- | --- |
| `messages.seq` 是 identity 列 | `migrations/versions/0016_messages_seq.py` | 不变 —— 全会话单写 |
| `session_folds.covered_to` 单调 | roadmap §5 | 不变 —— 折叠仍按会话整段推进 |
| `turns` 外键到 `session_id` | `0001_authoritative_baseline.py` | 不变 —— 无子会话 |
| 前端按 `turnId` 分组渲染 | `chat-types.ts` | 不变 —— 一个回合仍是同一个身份域 |

**明确不做**：不引入 `session.parent_id`；不做事件级并行；Thread 不成为新的可寻址资源
（没有"打开某个 thread"的页面），它只是事件来源的归类。若将来真需要并行，那是一次独立的
架构变更，要先解决上面四条，而不是在 P1-1 里悄悄开个口子。

##### P1-1.2 落地进度

- **已落地**：三张表（`migrations/versions/0021_threads.py`，真库验证 upgrade 与
  downgrade 双向干净）；队列三段 —— 入站准入 `decideThreadDelivery`（纯函数 + 单测，
  钉死"上限 20 含第 20 条"）、入队写入 `enqueueThreadDelivery`（真库验收 3 条：重投幂等 /
  超限落 dropped 且不复活 / 未注册来源报错）、收尾认领 `claimNextThreadDelivery` 与
  `finishThreadDelivery`（真库验收 2 条：最早优先 + 收尾幂等）。
- **未落地**：事件入口接线（把 `agent_followup.go` 的直投改成"先入队、由收尾方 drain"）、
  订阅 CRUD、以及 §6 要的那组"有订阅 / 无订阅"对照埋点。投递层默认**不接线**，
  故当前无可观测行为变化。

#### P1-2 Router 接管（Super-agent 接管）

> **权威依据先说清楚**：文章是**需求来源**，不是权威。落地判据一律取自本仓已有契约 ——
> `docs/session-runtime-spec.md`（配置解析优先级）、`docs/error-codes.md`（可重试性表态）、
> `docs/agent-safety-and-reliability.md`（能力分级与 fail-closed）、
> `internal/api/session_coord.go` 的"同会话串行"不变量、`docs/deployment-multi-instance.md`
> 的多副本假设。凡与这四条冲突的做法，一律不改契约来迁就需求。

##### P1-2.1 能力边界：这是"受限接管"，不是"常驻 Router"

必须先把两件事分开，否则会做出一个仓库契约不允许的东西：

| | 文中 §2.1 的常驻 Router | 本节要做的接管 |
|---|---|---|
| 何时存在 | 一直在线 | 用户显式开启，到期即止 |
| 作用范围 | 所有会话 | 用户勾选的会话白名单 |
| 上限 | 无（靠 batch 摊薄） | 时长上限 + token 预算上限 |
| 停止 | 无自然停止点 | 随时可打断，且到点/超预算自动停 |
| 默认 | 常驻 | **默认禁用** |

结论：接管是**一次有边界的授权执行**，不是"永远在线的协调者"。这个性质差异让它可以落在
现有的安全与并发契约内，而文中那种形态不行（见 §5）。

##### P1-2.2 配置模型：用户意图与运行态分离

配置（用户想要什么）与运行态（此刻正在发生什么）**不是一回事**，混在一处会出现
"改了配置但旧接管的到期时间没变"这类漂移。分开存：

- **用户意图** → `users.settings.superagent_takeover`，走已有的
  `PUT /v1/auth/profile`（`internal/api/auth.go:574`，`settings` 是**局部合并**
  `settings = COALESCE(settings::jsonb,'{}'::jsonb) || $n::jsonb`，零迁移）。
  形状：

  ```json
  {
    "enabled": false,
    "session_ids": ["<session_id>", "..."],
    "window": {
      "kind": "countdown",
      "duration_seconds": 3600,
      "tick_seconds": 180,
      "max_rounds": 0
    },
    "budget": { "token_cap": 200000 }
  }
  ```

  `window.kind` 两种取值：
  - `countdown`：开启时算 `ends_at = now() + duration_seconds`，一次性。
  - `schedule`：指向**用户级 super-agent 定时任务**（见 P1-2.10），每次触发时开一个
    `duration_seconds` 长的窗口。表达式语法仍用 `robfig/cron`（与
    `internal/api/cron_scheduler.go` 的 `cron.WithLocation(time.UTC)` 同一实现、同一时区口径），
    但**任务记录与 CRUD 入口独立**，不复用 `/v1/admin/cron-jobs`。

  `window.tick_seconds` 是**节拍**：两拍之间隔多久。默认 `180`（3 分钟），
  可配置，取值限制在 `60..3600`。定这个默认值的依据是接管的价值在"周期性推进"而非
  "毫秒级响应"；比 3 分钟更密只会让账单涨得快，而不改变推进结果。

  `window.max_rounds` 是**单窗口回合数硬上限**，默认 `0` 表示不设限（已拍板）。
  它与 `budget.token_cap` 互补而非重复：token 预算防"单轮很贵"，轮数上限防
  "每轮都很便宜但轮数很多"。默认不限的理由是费用已有 `token_cap` 兜底，
  再叠一个默认生效的上限会让"窗口还没到点就停"变成用户难以解释的行为 ——
  想收紧的人可以显式配置。

  **默认值 `enabled: false`**，且读侧必须把"字段缺失 / 解析失败 / Redis 与 DB 都读不到"
  一律当 `false` —— 这是 fail-closed，与仓库对 `read_only` MCP、SSRF 判定的一贯口径一致。

- **运行态** → 新表（**必须落 PG，不能只在 Redis / 进程内存**）：
  单一事实源是 PostgreSQL 是本仓基线，而"是否正在被接管"必须跨副本可见、按时间自动失效、
  可被审计。Redis 只做热缓存，读不到就回落 PG；PG 也读不到就当**没有接管**。

  ```sql
  create table superagent_takeovers (
      id           varchar(36) primary key,
      tenant_id    smallint    not null references tenants,
      user_id      varchar(36) not null references users,
      session_id   varchar(36) not null references sessions,
      status       varchar(16) not null,   -- active | stopping | stopped | expired | budget_exhausted
      started_at   timestamptz not null default now(),
      ends_at      timestamptz not null,
      stop_reason  varchar(64),
      token_spent  bigint      not null default 0,
      lease_token  varchar(64),
      lease_at     timestamptz
  );
  -- 同一会话同时只允许一个未终结的接管：靠部分唯一索引兜住并发开启
  create unique index ux_superagent_active on superagent_takeovers (session_id)
      where status in ('active', 'stopping');
  ```

  这条部分唯一索引是**并发正确性的第一道闸**：两个浏览器/两个副本同时点"启用"，
  数据库直接拒掉第二个，不需要应用层加锁。

  租户列必须 NOT NULL 且与 `sessions.tenant_id` 同源校验 —— 写入前
  `SELECT 1 FROM sessions WHERE id = $1 AND tenant_id = $2`，与
  `history_writer.py` 的写前归属校验同口径，防止跨租户接管。

  **配额硬上限：同一用户同时生效的接管最多 5 个会话。**校验放在开启那一刻、写表之前：
  `SELECT count(*) FROM superagent_takeovers WHERE user_id=$1 AND status IN ('active','stopping')`，
  达到 5 就整批拒绝（不静默丢弃超额的那几个 —— 用户会以为全都接管了）。
  与 10 个定时任务的配额一样，判据取 `user_id` 而非 `tenant_id`：这是个人授权行为的上界。

##### P1-2.3 驱动：接管不抢占，抢不到就跳过

这是整套方案最关键的一条，也是唯一能避免并发态的做法：

> **接管回合与用户提交走同一条通道，争同一把 `agent:run-lock`；拿不到就让，不抢。**

具体：接管驱动直接复用 `internal/api/agent_followup.go` 的无人值守回合骨架 ——
`AcquireSessionRunLock` → `agentSem.TryAcquire` → `HandleSubmit`。
理由：这条路径已经把"messages 落库 / SSE 推送 / turn 状态 / 计费"做对了
（引擎侧自己跑一轮会让这一轮在对话里看不见、不计费），再造一条等于重犯它的既有坑。

**抢不到锁 = 本次窗口作废，不排队等。**（已拍板）`AcquireSessionRunLock` 失败时：

- **不**用 `RetryLaterError` / `_defer_message` 退避重试。延迟重试不消耗失败预算
  （`retry_count` 不递增），确实能表达"等锁"，但它会让接管在用户毫不知情的情况下
  随时落地 —— 用户正聊着，几分钟后突然被接管，比"这次不接管"更难接受。
- 记 `last_status = 'skipped_busy'` 到 `superagent_schedules`，然后结束本次触发。
  用户在下个调度周期自然重试；同一个 `window_seconds` 内不补跑。
- 为什么这个取舍是安全的：接管的价值在**周期性**，不在**精确到秒**。跳过一拍的代价
  是"这次少跑一轮"，而排队等锁的代价是"用户随时被不可预期地打断"，后者直接毁掉
  P1-2.4 第 2 条不变量想保证的可预期性。
- `countdown` 形态是用户当场点的，不存在"用户不知情"的问题，但它同样遵守这条：
  用户自己正忙时点启用，就当这次没启用，UI 提示"会话正忙，未接管"而不是排队。

多副本：每条接管用 `lease_token` + 定期续期（照抄 `cron_jobs.run_lease_at/run_lease_token`
的既有模式，`internal/api/cron_scheduler.go:177`），只有一个副本在驱动。
**不要**用进程内 goroutine 定时器 —— 另一个副本看不到它，扩容后会重复执行。

多副本：每条接管用 `lease_token` + 定期续期（照抄 `cron_jobs.run_lease_at/run_lease_token`
的既有模式，`internal/api/cron_scheduler.go:177`），只有一个副本在驱动。
**不要**用进程内 goroutine 定时器 —— 另一个副本看不到它，扩容后会重复执行。

##### P1-2.4 并发态：三条不变量

用户提到的"并发态问题"在这套设计里收敛为三条必须写进代码注释的不变量：

1. **同会话永不并发两个 runtime。** 唯一入口是 `agent:run-lock`（TTL 5min、60s 续期，
   `session_coord.go:35`）。接管、用户提交、`agent_followup` 三者共用它，谁都不许绕过。
2. **拿到锁之后必须重读接管状态（check-then-act）。** 从"决定接管"到"抢到锁"之间，
   用户可能已经打断或接管已到期。拿到锁的第一件事是
   `SELECT status FROM superagent_takeovers WHERE id = $1`，不是 `active` 就**立刻放锁退出**。
   少了这一步，就会出现"用户点了打断，但排队中的那一拍又把回合拉起来了"。
3. **打断必须先落事实源，再取消回合。** 顺序反过来（先 cancel，后改状态）会有窗口：
   接管循环看到状态仍是 `active`，下一拍重新拉起一轮 —— 这正是用户担心的"引发更多并发态"。
   正确顺序：`UPDATE ... SET status='stopping', stop_reason=...`（提交即事实）→
   再走 `handleCancel` 的取消通道。回合真正结束后才置 `stopped`。

`stopping` 这个中间态不是多余的：它表达"已决定停止，但当前回合还没收口"。
在此期间前端遮罩显示"正在停止…"，且**前后端都拒绝新提交**（后端接管态检查 + 前端禁用输入），
避免用户在这段窗口里手动发一条必然撞锁的消息。

##### P1-2.5 打断：直接复用既有取消通道

不要新造取消机制。`internal/api/handlers.go:59` 的 `handleCancel` 已经有这条路径需要的全部性质：
归属校验（`sc.userID != claims.UserID` 即 403）、`CompareAndDelete` 原子认领
（避免取消掉刚被新 run 替换掉的条目）、跨实例 `CancelSessionBroadcast`
（`session_coord.go:223` 的 pubsub 订阅端同样校验 userID）。

新增的只有一个动作：打断时先改 PG 状态，再调用既有取消。
端点建议与既有风格一致：`POST /v1/superagent/takeovers/{id}/stop`。

##### P1-2.6 前端：配置窗格 + 接管遮罩

**配置入口**：`frontend-vue/src/views/AgentsView.vue` 的 `Tabs` 新增第四个 `TabPane`
（key 建议 `superagent`，现有三个是 `agents` / `market` / `sessions`）。不新建路由、不加侧栏项 ——
避免为一个功能多开一个导航层级。

- 开关：默认关闭。
- 接管时长：两种形态各自的 UI 不同，不要混成一个输入框。
  - `countdown`：时长选择（如 30 分钟 / 1 小时 / 自定义），保存后立即生效。
  - `schedule`：**不是**在这里填 cron 表达式，而是从该用户已建的 super-agent 定时任务里选一个
    （见 P1-2.10）。定时任务的增删改是同一 TabPane 内的第二个区块（列表 + 新建/编辑/试跑/删除）。
    这样"何时开始接管"和"接管多久"两个概念不会挤在同一个表单里。
- 接管会话：**多选**。数据源必须用 `GET /v1/conversations`
  （`internal/api/routes_conversation.go:19`，返回 id/title/updated_at），
  **不是** `GET /v1/agents/sessions`（那是 `agent_sessions` 运行记录，`api/index.ts:300`）——
  这两个很容易混，用错会给用户列出一堆已完成的历史运行记录。
- 定时任务区块同样要显示"上次运行时间 / 上次状态"（对应
  `last_run_at` / `last_status`），否则用户无法判断自己的表达式是否真的生效；
  配一个"立即试跑"按钮（调 `POST /v1/superagent/schedules/{id}/trigger`），
  比让用户等下一次到点更实用。

**费用提示分两层**（见 P1-2.7，这里只说呈现）：

- 启用时的**强提示**：用 `Modal` 而非行内 `Alert`，要求用户显式勾选确认后按钮才可用。
  文案沿用既有费用警告的措辞风格（如
  `locales/zh-CN/agent.ts` 的 `sub_agents_consume_tokens_independently_cost_multiplies_when_several_run_in_parallel`）。
- 配置页**常驻** `Alert`（`type="warning"`），不随用户点过确认而消失。

**接管遮罩**（用户明确要求）：被接管会话中显示黑色半透明模态覆盖层
（`rgba(0,0,0,.72)` 级别，足以挡住底下的输入区），内容：

- 状态："Super-agent 正在接管中" / 停止中；
- 已运行时长、剩余时长、已消耗 token、剩余预算；
- **"手动打断"** 按钮 → **二次确认** `Modal` → 调 P1-2.5 的 stop 端点；
- 遮罩期间禁用输入框与发送 —— 这是"避免引发更多并发态"最直接的一道 UX 闸门：
  不给用户制造一条注定 409 的提交。

**数据来源顺序不能颠倒**：进入会话时先 `GET /v1/superagent/takeovers?session_id=...`
拉快照，再叠加 SSE 增量（`superagent.takeover_started` / `_stopping` / `_ended`）。
必须快照优先，因为 SSE 断线期间的事件是靠 `Hub` 的 `ReplayAfter` 补的，
而**接管态本身不在 SSE 流里**（它属于会话状态，不属于某一轮的 token 流）。
只信 SSE 会让刷新页面后的遮罩状态错位。SSE 分流点插在
`ChatView.vue:2415` 那一支之前（那里目前只处理 `subagent.*`）。

##### P1-2.7 费用：提示是软的，预算闸门是硬的

只做提示不够 —— 用户点了"我知道有费用"之后，账单不会因此有上限。所以必须两条都要：

- **软**：P1-2.6 的强提示 + 常驻提示。
- **硬**：`budget.token_cap`。接管回合结束后累加 `token_spent`（`bigint`），
  超限即置 `budget_exhausted` 并停止接管，**不再询问用户**。
- 落地形态（三处细节是刻意的，改之前先看代码里的注释）：
  - **口径**：`usage.input + usage.output`，与同一句 `TrueUpTenantQuota` 完全一致；
    `cached` 既不进池也不计预算 —— 否则面板上的读数会和账单对不上。
  - **`budget_exhausted` 是独立终态**，没有复用 `stopped`：同样是"停了"，但一个是用户点停、
    一个是钱花到了自己设的上限；用户据此判断"该调预算还是该收工"。
    `stop_reason` 写 `token_cap`。
  - **记账发生在归还租约之前**（`superagent_driver.go::recordTakeoverUsage`）：否则另一个
    副本可能先抢到下一拍，用旧的 `token_spent` 再起一轮。
  - **用量怎么回到驱动里**：不改 `HandleSubmit` 的签名（已有 7 参数、5 个调用点含 3 个
    测试），改成 `ctx` 上挂一个原子 sink（`withTakeoverUsageSink` / `takeoverUsageFromCtx`），
    与既有的 `WithQuotaReserve` 同手法。**不要**塞进 `workbenchCtx` —— 那张表会被原样
    序列化进引擎请求体的 `context` 字段。普通提交没有 sink ⇒ 零影响。
  - 判定用 `spent >= cap`（正好花到上限也算用尽）；`cap <= 0` 表示不限。
  - 前端：SSE `superagent.takeover_ended` 且 `status == 'budget_exhausted'` 时插一行
    warning notice（遮罩只会突然消失、不给说明）；配置页把已花展示成「已花 / 上限」，
    用掉 80% 起标警示色。
- **顺带获得的约束**：接管回合既然走 `HandleSubmit`，就天然受
  `EnforceTenantQuota`（`docs/architecture.md` 的闸门 3）与租户并发闸门约束。
  这一点必须写进文档，否则会被误以为接管开了一条"绕过计费"的后门。

##### P1-2.8 验收与实施顺序

验收（每条都要能对着具体命令或现象核）：

1. **默认禁用**：不写配置时，行为与今天逐字一致（无新表行、无新回合、无新 SSE 事件）。
2. **并发**：用户提交与接管同一拍发生，**用户赢**——用户回合正常进行，接管本次作废并记
   `skipped_busy`；全程只有一个回合在跑，且用户侧**看不到任何错误**（不是 409）。
   反方向同理：接管已在跑时用户提交，沿用既有的 409 `session_busy` 口径
   （`docs/error-codes.md`，`retryable=true`）。
3. **打断**：打断后不再有新回合被拉起；多副本部署下也只发生一次取消
   （`CancelSessionBroadcast` 的订阅端校验 userID）。
4. **到期**：`ends_at` 到点自动停。判据取 PG 时间，不取进程内存/本地时钟。
5. **预算**：token 超限自动停，且停止原因可查（`stop_reason`）。
6. **归属**：跨租户/跨用户的 `session_id` 被拒绝，且拒绝发生在写表之前。
7. **个人隔离**：A 用户看不到、也停不了 B 用户的接管与定时任务（列表接口与 stop 端点各一条）。
8. **产出标记**：接管产生的回合 `turns.origin = 'superagent_takeover'`，用户手动回合为空串；
   历史数据迁移后全部为空串且渲染不变。
9. **配额**：第 11 个定时任务、第 6 个并发接管被拒，且拒绝发生在写表之前、返回可读原因。
10. **跳过可观测**：`skipped_busy` 在配置页可见（定时任务的上次状态列），
    用户能分清"没跑"和"跑失败"。

**真库验收（`internal/api/superagent_p12_live_test.go`，`CHIRON_TEST_POSTGRES_DSN` 未设即 skip）**

前四条此前只有静态检查 —— 读 SQL 能看出 `ends_at` 写成 `now() + make_interval(...)`，
但"PG 真的这样算"、"拒绝时表里真的一行没多"、"A 真的停不了 B"只有跑一次才知道。
现在各有一条用例，均已在临时库（`alembic upgrade head` 到 `0020_superagent`）上跑通：

- 验收 4：`(ends_at - started_at) = interval '600 seconds'` **精确成立**。用精确整秒
  而非范围断言，是为了区分来源：Go 侧 `time.Now().Add(...)` 的微秒抖动会让它落在
  599.9999~600.0001，这条就会失败。
- 验收 6：跨用户会话被拒（403）后，`superagent_takeovers` 里该用户与该死会话都是 0 行；
  且同租户另一用户的会话同样被拒 —— 证明判据是 `user_id` 不是 `tenant_id`。
- 验收 7：A 用 B 的接管 id 调用 stop 得到 404，并且**断言 B 的状态仍是 `active`**
  （只查响应码的话，漏写 `AND user_id = $2` 但恰好返回 404 的实现也能蒙混过关）。
- 验收 9：开满 5 个接管后第 6 个被拒、已生效数仍为 5；建满 10 条定时任务后第 11 条
  被拒、总数仍为 10。

**尚未覆盖**：验收 2/3 仍靠单测与既有 `*_live_test.go` —— 它们需要真实引擎回合与多副本
（同一拍两个提交谁赢、打断在多个副本间只发生一次），本文件只覆盖
"纯 SQL 语义 + handler 归属判定 + 驱动侧记账 + 投影"这一段。

验收 1、5、8、10 是后补的，覆盖口径要说清，否则看名字会以为验了整条链路：

- 验收 1（默认禁用）验三件事：开关读出来是关的、该会话上没有接管行、快照里没有运行中的
  接管。只验"开关为 false"不够 —— `loadTakeoverConfig` 的零值恰好是 `false`，
  那是有意为之还是巧合，只有同时确认"没有东西在跑"才说得清。
- 验收 5（预算）直接调 `recordTakeoverUsage` —— 正是回合结束时被调用的那个函数，
  验的是"累加""跨线即终结""终结后不再记账"三条纯 SQL 行为；**没有**跑真实引擎回合，
  所以"每轮用量真实回流到驱动"这一环仍只有单测。
  断言 `stop_reason = 'token_cap'` 而不只看 status：`budget_exhausted` 是通用终态，
  不写原因的话用户不知道是钱花完还是轮数用完。
- 验收 5 另有一条 `token_cap = 0` 的用例：0 在别处是"未设置"的哨兵，写反会让所有
  不设上限的用户在第一轮就被停掉。
- 验收 8（回合来源）走真实的 `session.Manager.CreateTurn`，不是直接 INSERT ——
  迁移里的 `DEFAULT ''` 只保证老行不为 NULL，证明不了新行写对标记。断言手动回合
  origin 是**空串而非 NULL**：前端按 `=== ''` 判断不渲染标记，NULL 会漏出小字标签。
- 验收 10（`skipped_busy` 可见）造的是真实成因：任务指向的会话已经有接管在跑，这一拍
  一个窗口都开不出来。断言走 `ListSchedules` **响应体**而不是直接查表 ——
  验收要求的是"配置页可见"，只验库里那列证明不了投影层有没有把它带出来。

实施顺序（每步可独立合并，避免一个 PR 同时改 schema、后端与前端）：

1. PG 迁移（一个 Alembic 版本，纯扩展）：
   `superagent_takeovers` 建表 + 部分唯一索引、`superagent_schedules` 建表、
   `turns` 加 `origin` 列（`default '' not null`）。
2. 后端状态机 + 端点：`GET` 快照 / `PUT` 配置（复用 profile）/ `POST` stop。
3. 接管驱动循环：租约 + 复用 `agent_followup` 骨架；抢不到会话锁即作废本次窗口。
4. 回合来源标记：`CreateTurn` 写 `origin`，并让前端历史链路带上它。
5. 用户级定时任务：`CronScheduler.sync()` 多读一张表 + 四个 CRUD 端点 + 试跑。
6. 前端配置 TabPane（开关 / 时长 / 多选会话 / 定时任务区块）。
7. 前端遮罩 + SSE 事件（快照优先）+ 回合小字标记。
8. 预算累加与自动停止。
9. 文档同步：`docs/error-codes.md` 若新增错误码须单列并三语言同步。

##### P1-2.9 权限与归属（已拍板）

**决定：每个用户独立管理自己的接管配置与定时任务；`/v1/admin/cron-jobs` 保持"平台维护"定位，不复用。**

- 开启 / 停止的判据是 `user_id`，不是 `tenant_id`，也不需要任何 admin 权限。
  `POST /v1/superagent/takeovers/{id}/stop` 的条件是 `t.user_id = claims.UserID`。
- `superagent_takeovers.tenant_id` 仍然必填。理由不是权限，而是**多租户隔离兜底**：
  所有查询一律带 tenant 谓词，将来加"租户管理员查看全员接管"这类功能时不会漏掉隔离条件。
- 同租户的其他用户（含租户 admin）**默认不能停别人的接管**。接管是个人授权行为，
  越权停止等于替别人做决定。若将来需要"管理员强制终止"，应单开一个带
  `PermAdminWrite` 的端点并留审计，**不要为此放宽上面这条判据**。
- 与 `/v1/admin/cron-jobs` 的分工：那份是平台运维（跨租户巡检、健康检查），
  按 `tenant_id` 组织；用户这份是个人任务，列表**只返回自己**的。

##### P1-2.10 用户级 super-agent 定时任务

**新增独立表，不复用 `cron_jobs`。** 三条理由都是既有代码的硬约束：

1. `cronListQuery`（`internal/api/admin_ops.go:917`）按 `tenant_id` 过滤 —— 普通用户一调
   就是"整个租户的任务"，权限面无法收紧。
2. `cron_jobs.task` 的两种形态（`parseAndExecute`，`cron_scheduler.go:351-386`）都**直接打引擎**
   （`runQuick` 发 `/v1/chat/submit`、`runAgent` 发 `/v1/agents/dispatch`），
   既不过会话锁也不校验会话归属 —— 与接管"必须排队进同一会话"直接冲突。
3. 混入用户数据会让平台运维列表（`frontend-vue/src/views/admin/CronView.vue`）被噪声淹没。

```sql
create table superagent_schedules (
    id              varchar(36)  primary key,
    tenant_id       smallint     not null references tenants,
    user_id         varchar(36)  not null references users,
    name            varchar(128),
    schedule        varchar(64)  not null,   -- robfig/cron，UTC
    window_seconds  integer      not null,   -- 每次触发开多长的接管窗口
    session_ids     jsonb        not null,   -- 触发时接管哪些会话（可多选）
    enabled         boolean      not null default false,
    last_run_at     timestamptz,
    last_status     varchar(16),
    run_lease_at    timestamptz,
    run_lease_token varchar(64),
    created_at      timestamptz  not null default now(),
    updated_at      timestamptz  not null default now()
);
create index ix_superagent_schedules_owner on superagent_schedules (user_id, tenant_id);
```

四条必须遵守的实现约束：

- **配额硬上限：每用户 10 个定时任务。**超出时 `POST` 直接拒绝（`400` + 可读原因），
  不静默截断。判据取 `COUNT(*) WHERE user_id=$1 AND tenant_id=$2`，且在**同一事务内**
  与插入一起判 —— 只在应用层"先查后插"挡不住两个并发请求同时通过。
  上限不做成可配置项：它存在的前提只是"给费用一个上界"，可配置会让这件事变成一个需要维护的面。

- **复用同一套多副本防重语义**：`tryAcquireScheduleSlot`（Redis 分钟槽，`cron_scheduler.go:232`）
  + `run_lease_at/run_lease_token`（`tryAcquireLease` / `releaseLease` 的 CAS 归还）。
  这两个机制存在的唯一目的就是"多副本不重复执行"，绕开它们等于重犯已经被修过的 bug。
- **挂在同一个 `CronScheduler` 实例上**，在 `sync()`（`cron_scheduler.go:85`）里多读一张表，
  而不是起第二个 scheduler。两个实例各持一份 `robfig/cron`，容易出现"同一时刻两份调度互相看不见"，
  且时区口径必须一致（都是 `cron.WithLocation(time.UTC)`）。
- **写入时即校验表达式**（`cron.ParseStandard`）。`sync()` 的注释
  （`cron_scheduler.go:70-72`）已经踩过这个坑：一条必然 panic 的坏数据等于全平台 CrashLoop，
  不能靠运行时降级兜。
- **触发即一个待执行的接管窗口**：到期只插入一条待执行的窗口记录，由 P1-2.3 的同一驱动
  去争会话锁。凡是绕过 `agent:run-lock` 直接起回合的写法都不可接受 —— 那正是并发态的源头。
  争不到就按 P1-2.3 作废本次窗口（`skipped_busy`），**不排队**。

端点（全部 `authMW(rlMW(...))`，不带 admin 权限，查询与写入一律
`WHERE user_id = $1 AND tenant_id = $2`）：

- `GET|POST /v1/superagent/schedules`
- `PUT|DELETE /v1/superagent/schedules/{id}`
- `POST /v1/superagent/schedules/{id}/trigger`（手动试跑，用于验证表达式而不必等到点）

##### P1-2.11 接管产出的标记：标在回合上，不是标在消息上

这是核实后必须改口的一处。原方案想复用 `messages.source`，但它在本仓**只覆盖用户消息**：

- 写入口只有 `SaveUserMessage`（`internal/session/manager_messages.go:116-150`）带 `source` 列。
- assistant 产出走两条路径，**都不写 source**：`UpsertAssistantMessage`
  （`manager_messages.go:171-193`，Go 侧增量落库）与 `history_writer.persist_messages`
  （`python-engine/app/agent/history_writer.py:309-322`，引擎侧批量落库）。

补充说明（2026-11-03 已修）：`/v1/conversations/{id}` 的 `Message` 结构体此前**没有 `Source`
字段**，`GetMessagesPage` 虽然读了 `COALESCE(to_jsonb(messages)->>'source','')`
（`manager_queries.go:192-193`），转响应时却丢掉了 —— 导致
`source === 'subagent_followup'` 的徽标在普通聊天历史路径上从不触发。**这条读侧缺口已补**
（`conversation.go` 的 `Message` 加 `Source`、前端 `HistoryMessage` 加 `source` 并在
`mergeHistory` 透传）。但**写侧缺口仍在**：assistant 产出依然没有 source 写入路径。

因此"给 assistant 消息加 source"仍需改两处写入，且引擎侧要把"这轮是不是接管"
一路透传进 `persist_messages` —— 成本高、容易漏。

**做法：标在回合（turn）上。**

```sql
alter table turns add column origin varchar(24) not null default '';
-- '' = 用户手动；'superagent_takeover' = 接管驱动
```

选它的三条依据：

- `turns` 表已存在（`init.sql:999-1016`），加列是纯扩展，旧行自动为空串 = 视为用户手动，
  语义正好正确，**不需要** `isMissingColumn` 那套降级。
- 前端**已经按 `turnId` 分组渲染**（`chat-types.ts:37-40`：同一回合的思考/正文/工具卡片
  共享一个身份域）。标记附在分组上，天然覆盖该轮全部产出。
- 写入点单一：`CreateTurn`（`manager_messages.go:222`）本就是每回合必调的一次写入，
  在那里按 `workbenchCtx` 的标记键写下 `origin`，比改两条 assistant 落库路径可靠得多。

呈现：

- 在**回合末尾那一行**渲染小字，而不是每条消息都标 —— 一个回合动辄几十条工具卡片，
  逐条标会变成噪声。
- 视觉直接复用 `.followup-badge` 的现成规格（`MessageItem.vue:1267-1272`：
  `font-size: var(--fs-xs)` + `--text-tertiary` 弱化色 + 细边框），文案如
  "Super-agent 接管期间自动执行"。用户要求的"小字显示"由这条样式即为一致口径。
- 注入指令那条用户消息**仍用 `source = 'superagent_takeover'`**（走已有的
  `SaveUserMessage` 链路，零改动），让用户一眼看出"这条不是我问的"。
  两者互补：消息级标"指令来源"，回合级标"产出归属"。

验收补三条：接管产生的回合带 `origin` 标记；用户手动回合为空串；历史数据迁移后全部为空串
且渲染不变。（这三条已并入 P1-2.8 的第 8 条，此处不重复维护。）

#### P1-3 Watchdog（语义级进展判断）

- 动机：文中 §6 的 Watchdog 是主系统之外的独立会话；Chiron 的 `loop_guard.py` 只看参数指纹，
  `recovery.py` 只会路由重试，都答不出"是否偏离了用户目标"。
- 做法：独立后台循环（走 `queue/worker.py` 的既有租约/重领/幂等骨架，避免多副本重复执行），
  定时读取 `messages`/`subagent_runs` 的**摘要级**信息做判断；输出只有三档：
  继续 / 追加提示 / 建议终止。默认**只观察不动作**，观察期与人工确认档位对齐
  `agent-safety-and-reliability.md` 的分级口径。
- 验收：对已知的"兜圈子"回放样本能给出与 `loop_guard` 一致或更早的信号；
  关掉它时零影响；它的每次干预都有审计。
- 风险：这是"用一个 LLM 判断另一个 LLM"，会引入新的误判与成本；必须 fail-closed 到"不干预"。

**现状（已落地一版骨架，但判断层不是模型层）**

- 代码：`app/agent/watchdog.py`（判断器，纯函数）· `app/queue/watchdog_probe.py`（采集）·
  `app/queue/watchdog_job.py`（一拍的采集→判断→处置→审计）· `queue/worker.py::_start_watchdog`。
- 判断依据：把**当前回合**（`turns.status='running'`）的最近 20 条 `messages` 与 20 次
  `tool_calls` 折成两个哈希 + 消息条数，看末尾连续多少拍（内容、工具、条数）三项都没变。
  这是**确定性结构判断**，不是 LLM 判语义 —— 原文 §6 的"模型层判断"仍未做，
  当前版本只做到"能证明的停滞"。它仍比 `loop_guard` 早：后者要**3 次同参调用**才命中，
  本判断可配到**2 拍**（`superagent_watchdog_nudge_after`，默认 2）出信号。
- 三档：`CONTINUE` / `NUDGE`（经 `agent/steer.py` 在轮次边界注入，与 P0-3 同一条通道）/
  `SUGGEST_STOP`（**只写审计与日志，永不终止** —— 终止不可逆，留给人）。
- 注入的消息**来源可辨**：`source='watchdog'` 一路带到 `messages.source`，`MessageItem.vue`
  渲染 `.watchdog-badge`（warning 色，与接管的 primary 色区分）。只记"谁发的"不够 ——
  历史里长得跟用户自己说的一模一样时，用户会把系统的推测当成自己的判断。
- 开关（`app/config.py`，默认全关）：`superagent_watchdog_enabled=false` 连采集都不跑；
  `superagent_watchdog_act=false` 时判断结果只进 `audit_logs`，不注入、不终止。
  `enabled=false` 时 `worker.start()` **不创建任务**，"关掉零影响"是真的零。
- 审计：`superagent.watchdog.nudge` / `superagent.watchdog.suggest_stop`，`resource_type='agent_watchdog'`，
  写失败只 warning（旁路，与 `conflict_audit.py` 同款契约）。
- **未覆盖**：`subagent_runs` 不在采集范围内（只看主会话回合）；采样历史存进程内，
  租约漂到另一副本时归零 → 该拍因样本不足判 `CONTINUE`（这就是 fail-closed 的方向）。

### P2：研究项（先做原型，不进主线）

- **投机执行**（文中 §3.4）：需要先有稳定的"下一步可预测性"证据，否则只是浪费算力。
- **端侧无权 daemon / BYOM**（文中 §5.2）：与当前 `desktop-local` 的 `local|cloud`
  二选一模型冲突，属于**产品形态变更**，不是纯工程增量；且文中 §8.4 已指出
  "云端被攻破即可控端侧"的风险，必须与凭据隔离一起设计。
- **A2A 与上下文边界**（文中 §8.3）：`docs/subagent-design.md` 的 `max_depth=1` 是当前的
  安全边界，放开前需要先有跨 Agent 的上下文契约。

## 5. 明确不做 / 暂缓

- **不做**文中 §2.1 的"Router 单常驻 Session 全局批处理"的完整形态。它假设一个低并发、
  高价值的团队场景；Chiron 是多租户 SaaS，逐租户常驻会话的成本与隔离代价未评估前不落地。
  **注意与 `P1-2` 的区别**：`P1-2` 做的是**用户显式开启、限定会话、限定时长、默认禁用**
  的受限接管，它不违反本条 —— 本条拒绝的是"无边界常驻"。两者不可混为一谈：
  若把 `P1-2` 的开关默认打开、去掉时长与预算上限，就等于变相实现了被本条拒绝的形态。
- **不做** IM 多信道（§4.2）。它是渠道工程，不改变 Agent 能力；先把 `P0-1`/`P0-2`
  的通道打通，信道可以后挂。
- **不引入** `AgentHub` 式的新抽象：`docs/context-and-delegation-design.md` 已判定
  `python-engine/app/collaboration.py` 的 `AgentHub` 是死代码应删。新能力优先长在
  `subagent/` 与 `queue/worker.py` 上。
- **不动** `messages` 的"只追加"约束与 `session_folds.covered_to` 单调约束去换新功能。

## 6. 需要先决的开放问题

这些不是工程细节，是**必须先定口径**的产品/架构决策，落地前要单独讨论：

1. **接管回合的计费归属层级**：`P1-2.7` 已确定"接管回合走 `HandleSubmit`，因此受
   `EnforceTenantQuota` 与租户并发闸门约束，并另有 `budget.token_cap` 硬上限"。
   仍未决的是**层级**：`EnforceTenantQuota` 只是回合级 fail-open（见
   [architecture.md](architecture.md)），而接管是连续多回合。是按"租户日额度"还是
   "每个接管一个独立预算池"记账，会影响账单可解释性 —— 用户看到的应该是一个
   "接管花了多少"的数字，而不是散落在几十条回合里的 token。
   **已决（2026-11-04）：与手动回合共用租户日额度。** 接管回合走 `HandleSubmit`，
   因此和其它回合走同一个 `EnforceTenantQuota`，不另设预算池、不新增记账表。
   选它的依据是"同一次消耗不该有两套额度账本"：租户日额度是现有的成本护栏，
   再接管开一份独立预算就等于同一笔钱被扣两次，账单对不上时无从判断该信谁。
   可解释性的诉求由 `superagent_takeovers.token_spent` 单独满足 —— 那是"这次接管
   累计烧了多少"的唯一数字，与 `budget.token_cap` 的硬上限同源（`recordTakeoverUsage`
   累加、`budgetExhausted` 判定），不需要拆一份额度出来才能回答。
2. **"主动"的边界**：`P0-1`/`P1-3` 都会让系统**未经用户触发**就产生新回合。
   今天 `internal/notify/notify.go` 的站内信"不可关闭"，主动 Agent 是否也要默认开？
   这是产品决策，不是技术决策。
3. **单会话串行假设**：`agentRunLock` 保证同会话只有一个回合在跑（TTL 5min）。
   Thread 直通会让"同一会话收到多个事件"，是沿用串行排队还是允许并行子会话？
   这一条决定 P1-1 的整个数据模型。
   **已决（2026-11-04）：沿用串行排队。** Thread 只承担"事件来源"的分类职责，
   运行锁仍是 `agent:run-lock:<session_id>` 一把、session 级，不引入子会话。同一会话
   短时间收到多个事件时进**每会话待处理队列**，跑完一个接着下一个。
   选它的依据是四条既有不变量都建立在"会话内单线程"之上：`messages.seq` 是 identity
   列（[0016 迁移](../migrations/versions/0016_messages_seq.py)）、`session_folds.covered_to`
   单调递增、`turns` 以 `session_id` 为外键、前端按 `turnId` 分组渲染。开子会话意味着
   这四条只能在子会话内成立，历史/摘要/计费全部要跨父子聚合 —— 成本远大于直通收益，
   而直通的收益（省掉路由判断）**并不来自并行**。
   代价（承认）：事件密集时排队延迟随队列长度线性上升，需要队列上限与丢弃策略，见 P1-1。
4. **事件入口的信任边界**：入站 Webhook 是外部可控输入。按
   `agent-safety-and-reliability.md` 的原则，它应该比用户输入更不可信——是否要求
   事件驱动出的回合默认降级到 `ask` 模式？
   **已决（2026-11-04）：默认降级到 `ask`。** 事件驱动回合（`agent_followup.go` 的
   `subagent` / `job` 两类，以及 P1-1 的 thread 投递）不是人当场授权的，事件正文里
   完全可以出现诱导性文字，因此危险操作一律要人确认。用户若要放宽，走显式开关，
   不靠"事件来源看起来可信"来推断。
   **实现状态（据代码）**：该口径**尚未在代码里 enforce** ——
   `agent_followup.go` 调用 `HandleSubmit` 时没有设置审批档位，注入内容目前只是包成
   不可信数据块（`<subagent-result>` / `<job-result>`）。落地时要新增档位参数并补
   渲染级证据，不能只改这句文档就当完成。

## 7. 复核方式

- 现状断言的代码位置均可在本文 §2 给出的路径下复核；文档口径本身按
  `CONTRIBUTING.md` 的"只写代码能证明的内容"执行。
- 文档链接：`python scripts/check_doc_links.py`（只扫 `.md`，新增断链即失败）。
- 实现阶段的验收沿用仓库既有门禁：`make check`、`go test ./...`、
  `python-engine` 侧按 `CONTRIBUTING.md` 的 CI 口径（Python 3.11 + `requirements*.txt`）。
- 数据库变更只走 Alembic，且必须向后兼容（扩展-迁移-收缩）。
