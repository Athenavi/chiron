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

### 2.3 面向人的交互：异步工具齐了，但结果不回头

- **全双工的一半已经落地**：`python-engine/app/tools/jobs.py` 注册了
  `run_in_background`（立即返回 job id）、`job_output`、`job_kill`；
  `python-engine/app/tools/job_runner.py` 是子进程执行器，带 kill flag 轮询与超时；
  `python-engine/app/tools/taskboard.py` 提供跨 Agent 共享任务板。文章 §4.1 描述的
  "异步工具 + 终止工具"在工具面上是对得上的。
- **断在"回投"**：`job_runner.py` 只把状态写进 Redis（`_write_meta` / `_write_result`），
  没有 notify/publish/XADD/emit，**后台任务完成后不会给会话发任何消息**。对照
  `subagent/followup.py`（有完整的"结束 → 队列 → 新回合"通道），这是明显的非对称缺口。
- **没有"打断"**：会话运行锁 `AcquireSessionRunLock`（`internal/api/session_coord.go`，
  TTL 5min、60s 续期）在锁被占时直接 400 拒绝，提示"stop it or wait"；用户没有
  "边跑边补充/改方向"的通道。注意 steer **原先两侧都没有**：它只写在
  `docs/context-and-delegation-design.md` §9.3，并且是那份文档「实施顺序」的第 8 步，
  `python-engine/app` 下无任何实现 —— 所以主会话这条通道是**新建**，不是"接到子 Agent 已有能力上"。
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
| 后台结果回投会话 | 无（只写 Redis） | **缺**，且零件已就绪 |
| 边跑边干扰（steer 主会话） | 锁占即 400 拒绝；引擎侧已建通道 | **部分**（引擎已落地，网关端点未接） |
| 入站 IM / GitHub 信道 | 无（只有 SSO 出向） | **缺** |
| 无状态计算 + DB 存 Session | 已是架构基线 | **有** |
| 隔离执行环境 | `sandbox-service`（受限容器） | **部分** |
| 判定/执行分信任域 | `tool_broker` + `guardian` | **有** |
| 端侧无权 daemon / BYOM | `local` / `cloud` 二选一一体栈 | **缺**（形态不同） |
| Watchdog（语义级进展判断） | 只有 `loop_guard`/`recovery`/reconciler | **部分** |

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
- 待接：SSE `steer` 事件的前端呈现。
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
2. **"主动"的边界**：`P0-1`/`P1-3` 都会让系统**未经用户触发**就产生新回合。
   今天 `internal/notify/notify.go` 的站内信"不可关闭"，主动 Agent 是否也要默认开？
   这是产品决策，不是技术决策。
3. **单会话串行假设**：`agentRunLock` 保证同会话只有一个回合在跑（TTL 5min）。
   Thread 直通会让"同一会话收到多个事件"，是沿用串行排队还是允许并行子会话？
   这一条决定 P1-1 的整个数据模型。
4. **事件入口的信任边界**：入站 Webhook 是外部可控输入。按
   `agent-safety-and-reliability.md` 的原则，它应该比用户输入更不可信——是否要求
   事件驱动出的回合默认降级到 `ask` 模式？

## 7. 复核方式

- 现状断言的代码位置均可在本文 §2 给出的路径下复核；文档口径本身按
  `CONTRIBUTING.md` 的"只写代码能证明的内容"执行。
- 文档链接：`python scripts/check_doc_links.py`（只扫 `.md`，新增断链即失败）。
- 实现阶段的验收沿用仓库既有门禁：`make check`、`go test ./...`、
  `python-engine` 侧按 `CONTRIBUTING.md` 的 CI 口径（Python 3.11 + `requirements*.txt`）。
- 数据库变更只走 Alembic，且必须向后兼容（扩展-迁移-收缩）。
