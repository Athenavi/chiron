# 变更记录

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。
版本号采用 `v0.1.YYMMDD.NN`：`YYMMDD` 为发布日期，`NN` 为当日第几次发布。

## [Unreleased]

### 修复

- **修注册 500：`set_config('app.current_tenant_id', $1, true)` 的 `int16` 参数**
  `auth.go` 有三处调用（注册 1 处 + 登录/其它入口 2 处），其第二参是 `text`（OID 25），
  而 `tenant_id` 改型后传的是 `int16`，pgx 报 `unable to encode 1 into text format for text (OID 25)`，
  整个注册事务在第一步就失败 → `POST /v1/auth/register` 500。三处统一包 `tenantStr(...)`。
  已验证全仓 `set_config` 仅这三处，且无 `current_setting('app.current_tenant_id')` 读取方与 RLS 策略，
  故数字字符串 `"1"` 不改变任何既有语义。**这类 SQL 参数类型问题编译与单测都发现不了，
  只有真实请求才暴露**；已在 8090 端口实跑 Gateway 验证注册返回 201 并正确落库。

- **修 `tenant_id` 改 `smallint` 后的两处 `COALESCE` 空串遗留**：`run_usage_reconciler` 的
  `runUsageScanSQL` 与 `middleware` 的 API-Key 校验查询都写了 `COALESCE(tenant_id, '')`——
  列是 `varchar` 时合法，改 `smallint` 后 PG 会报 `invalid input syntax for type smallint: ""`
  (`SQLSTATE 22P02`)。两处加 `::text`，保持 Go 侧 `string` 契约与「tenant_id 为空直接拒绝」
  的判断不变。**这类问题编译与单测都发现不了，只有真库运行才暴露**（reconciler 每 5 分钟
  打一条 WARN）；已在重建库上实跑 Gateway 确认该 WARN 消失。

- **统一 Python 侧 `tenant_id` 的消费契约**：`get_tenant_id()` 的调用方绝大多数是「格式化」场景
  （沙箱路径拼接 `sandbox_root()/tenant`、审计条目的 `tenant` 字段、`kb_access_allowed` 的租户比较、
  `SkillStore` 目录段）。此前把它改成 `int` 会让 `Path / int` 直接抛 `TypeError`、审计字段变成 `""`。
  现恢复 `get_tenant_id() -> str`（值规约为数字串，未绑定返回 `""`），另加 `get_tenant_id_int()`
  供 SQL 参数与 Pydantic 请求体使用。
- **修复 `app/knowledge/settings_store.py` 的循环导入**：`from app.tools.context import to_tenant_int`
  放在模块顶部会与 `app.tools.__init__` 形成环（`ImportError: partially initialized module`），
  改为函数内延迟导入。
- **同步 53 个因此失配的 Python 测试**（断言仍按字符串租户、SQL 期望仍写 `$N = ''`、请求 query 仍是
  `tenant_id=t1` 导致 FastAPI 422 等）。
- **线账表 DDL 契约测试迁到基线**：`0022_run_lineage_ledger` 已收敛进 `0001_authoritative_baseline.py`，
  原测试读取的独立迁移文件不存在。改为从基线 `_DDL` 元组提取，建表列约束、主键（单独的
  `ADD CONSTRAINT`）、partial index（`WHERE paused`）与 downgrade 断言全部保留，skip 取消。
  验证：`pytest tests/ --ignore=tests/integration` **2803 passed / 42 skipped / 0 failed**，
  `ruff check .` 全绿。
- **修复前端 `tenant_id → number` 的类型遗留（8 处）**：`vue-tsc -b`（`npm run build` 的前置）
  此前是**失败**的 —— `stores/auth.ts` 两处恢复/回填仍赋 `''`（`User.tenant_id` 已是 `number`）·
  `auth.spec.ts` 的 mock 与断言仍用 `'t1'` / `'t2'` / `''` · `CostCenterView` / `MarketView` 的
  表单字符串未与 API 的 `number` 互转 · `CallChainTimeline` 的 `tenantId?: string` 与调用方
  （传 `number`）不一致。现已全部归位。
  验证：`npx vue-tsc -b` 通过 · `npm test` **81 files / 631 tests passed** · `eslint` 0 error。

### 重构

- **拆出 `useWorkflowCanvas.ts` + `useWorkflowPersistence.ts`**（L3-4 前端 composable 批次 4）：
  `WorkflowView` 的画布核心（Vue Flow 实例、`nodes` / `edges` / 选中态、节点 id 生成、拖拽落点、
  连线、自动布局、删除/复制/快捷键、VueFlow ↔ 后端格式互转）与持久化（保存 / 列表 / 载入 / 删除）
  分别抽出，`WorkflowView.vue` **1590 → 1362 行**。「节点配置表单」那一簇（`edit*` 字段 +
  `onNodeClick` / `applyNodeConfig`）与 16 个表单字段强耦合，保留在视图；`resetCanvas` 横跨画布与
  执行态，拆为 `clearCanvas()` + 视图侧重置。
  验证：`npx vue-tsc -b` 通过 · `npm test` **81 files / 631 tests passed** · `eslint` 0 problem。
- **拆出 `useChatShare.ts`**（L3-4 前端 composable 批次 3）：ChatView 的分享簇（弹窗状态 +
  选消息 → 生成链接 → 撤销/复制）原样迁到 composable，`ChatView.vue` **3846 → 3771 行**；
  会话状态经依赖注入（`items` / `sessions` / `activeSessionId` / `switchSession`），模板侧引用的
  名字不变。

### 新增

- **P1-1 数据模型与队列准入**：§6 问题 3 定为**串行排队**（Thread 只做事件来源分类，
  执行侧仍是 `agent:run-lock:<session_id>` 一把锁，不引入子会话）。`migrations/versions/
  0021_threads.py` 建 `threads` / `thread_subscriptions` / `thread_deliveries` 三张纯扩展表
  （真库验证 upgrade + downgrade 双向干净）；`internal/api/thread_delivery.go` 的
  `decideThreadDelivery` 是纯函数，钉死"上限 20 含第 20 条"与"负数不丢"两个边界 ——
  写成 `>` 会变成能排 21 条，而这种差一错误在评审时看不出来，只表现成"偶尔多一条"。
  队列的另一半是收尾认领：`claimNextThreadDelivery` 用 `FOR UPDATE SKIP LOCKED` 原子领走
  最早一条并置 `running`，排序按 `(created_at, thread_id, event_key)` 全序（`now()` 同事务
  同值，只按时间排会让"为什么这条先跑"没有答案）；`finishThreadDelivery` 的
  `WHERE status = 'running'` 让它天然幂等，重复收尾不会把已 `done` 的行改写成 `dropped`。
  真库验收 2 条：最早优先（乱序插入仍领对的）+ 收尾幂等。
  队列的入队写入是 `enqueueThreadDelivery`：计数与插入在同一个 advisory lock 临界区内完成
  （两个副本各自读到 19 再双双插入，上限就从 20 变成 21，而这类竞态靠测试很难复现，只能
  从结构上消掉）；`threads_idem` 与投递表主键两层幂等让多副本重投只落一行，且 `DO NOTHING`
  **不复活**已 `dropped` 的行；超限时落 `dropped` 行并写审计，而不是只写审计 —— 缺行就
  分不清"没发生"和"发生了但被丢"。真库验收 3 条：重投幂等 / 超限丢弃不复活 / 未注册来源报错。
  **尚未接线**：事件入口仍走 `agent_followup.go` 直投，投递层默认不生效。
- **`thread_deliveries.payload`（P1-1 接线先决）**：`migrations/versions/0022_thread_delivery_payload.py`
  给投递表加 `payload jsonb NOT NULL DEFAULT '{}'`。起因是核实接线前提时发现两表都存不下
  正文要素：`buildSubagentFollowupContent` 要 `RunID/Status/Profile/Summary`、
  `buildJobFollowupContent` 要 `RunID/Status/Summary`，而这些字段没有任何一处留底 ——
  引擎回投是一次性 HTTP 请求，drain 侧无从还原。选单列 jsonb 而非三列：`profile` 只有
  `subagent` 用得上，拆列会得到大部分格子为空的稀疏表。真库验证 upgrade + downgrade 双向干净。
  紧接着投递行承载 payload 与收件人字段：`threadEnqueueParams.Payload` 随行写入
  （`payloadArg` 把空值归一成 `{}`，因为列是 `jsonb NOT NULL` 而"无额外输入"是正常情形）；
  `claimNextThreadDelivery` 改用 CTE——先 `UPDATE ... RETURNING` 再 JOIN `threads`
  （多表 UPDATE 的 FROM 里"哪行被更新"与"返回哪行"不保证一一对应）。真库验收新增 1 条。
- **Super-agent 接管的真库验收（P1-2 验收 4/6/7/9）**：`internal/api/superagent_p12_live_test.go`。
  此前这四条只有静态检查 —— 读 SQL 能看出 `ends_at` 写成 `now() + make_interval(...)`，
  但"PG 真的这样算"、"拒绝时表里真的一行没多"、"A 真的停不了 B"只有跑一次才知道。四条用例：
  ①`(ends_at - started_at) = interval '600 seconds'` 精确成立（用精确整秒区分来源：Go 侧
  `time.Now().Add` 的微秒抖动会让它落在 599.9999~600.0001）；②跨用户会话被拒后接管表 0 行，
  同租户另一用户同样被拒（判据是 `user_id` 不是 `tenant_id`）；③A 用 B 的接管 id 调 stop
  得到 404 **且断言 B 仍为 `active`**（只查响应码的话，漏写 `AND user_id = $2` 但恰好 404
  的实现也能蒙混过关）；④第 6 个接管、第 11 条定时任务被拒且总数不变。
  沿用既有口径：`CHIRON_TEST_POSTGRES_DSN` 未设即 skip，`-run Live` 才执行。
  **踩到的坑**：`GetUserID`/`GetTenantID` 读的是 `TenantMiddleware` 写入的两个 ctx key，
  不是 `auth.Claims` —— 只挂 claims 会让四条全部 401，看起来像 handler 有问题。
  已在 `alembic upgrade head` 到 `0020_superagent` 的临时库上跑通。
- **同上，第二批（P1-2 验收 5/8）**：验收 5 直接调 `recordTakeoverUsage` —— 正是回合结束时
  被调用的那个函数，验"未超限只累加 / 跨线即置 `budget_exhausted` / 停止原因写 `token_cap` /
  终结后不再记账"四条纯 SQL 行为；另有一条 `token_cap = 0` 用例（0 表示不限，写反会让所有
  不设上限的用户第一轮就被停掉）。验收 8 走真实的 `session.Manager.CreateTurn`，断言接管回合
  `origin = 'superagent_takeover'`、手动回合**空串而非 NULL**（前端按 `=== ''` 判断不渲染标记，
  NULL 会漏出小字标签），且同一 turnID 重放不会把标记洗掉。
  口径说明：这两条**没有**跑真实引擎回合 —— "每轮用量真实回流到驱动"仍只有单测覆盖。
  顺带修了清理顺序：`turns` 也引用 `sessions`，漏删会撞 `turns_session_id_fkey` 并留残留行。
- **同上，第三批（P1-2 验收 1/10）**：验收 1 验"默认禁用真的什么都不碰"三件事 —— 开关读出
  为关、该会话上没有接管行、快照里没有运行中的接管。只验"开关为 false"不够：
  `loadTakeoverConfig` 的零值恰好是 `false`，那是有意为之还是巧合，得同时确认没有东西在跑。
  验收 10 造的是真实成因（任务指向的会话已经有接管在跑，这一拍一个窗口都开不出来），
  断言走 `ListSchedules` **响应体**而不是直接查表 —— 验收要的是"配置页可见"，
  只验库里那列证明不了投影层有没有把它带出来。
  至此 P1-2 十条验收里 8 条有真库证据；仍缺 2（同一拍用户赢）与 3（多副本打断只发生一次），
  都需要真实引擎回合与多副本环境。
- **steer 指令来源落库（P0-3 / P1-3 收口）**：`push_steer` 收 `source` 却从不落库，
  于是 watchdog 自动注入的提示在历史里和用户自己说的长得一模一样。现在来源一路带到
  `messages.source`：`steer._LOCAL` 由 `(ts, text)` 扩为 `(ts, text, source)`，Redis 条目由
  裸文本改为 `{"text":…,"source":…}` JSON（`ensure_ascii=False`）；`runtime.py` 注入时把
  `source` 挂到消息上，`history_writer.persist_messages` 的 payload 末位加 `source` 并写进
  INSERT。**兼容在途旧条目**：解码失败一律回落 `(raw, "user")` —— 滚动升级期旧副本还在写
  裸文本，读端不兼容会把用户指令整条丢掉，比标错来源严重得多。`source` 放在 payload
  **末尾**是因为 `_drop_existing_tool_calls` 按索引 3 去重、归档清理按索引 5 取 `blob_ref`。
  验收：`pytest tests/test_steer.py` 5 passed（编解码往返 / 旧裸文本兼容 / 跨副本读 Redis /
  本地保序且逐条带来源 / 缺省为 `user`）；`tests/test_history_writer.py` 的归档用例由
  "按参数个数认行"改为"按 SQL 内容认行"，避免每加一列就误伤。
  前端同步认这个来源：`MessageItem.vue` 新增 `.watchdog-badge`（warning 色，与接管的 primary
  色区分），三语言补 `workflow.watchdog_nudge_prompt`。只把来源写进库不够 —— 徽标漏渲染，
  用户照样会把系统的推测当成自己说过的话。渲染级用例 2 条（`MessageItem.spec.ts`）。
- **Super-agent Watchdog 骨架（P1-3）**：在主循环之外判断"这一轮是否还在推进"。判断器
  `app/agent/watchdog.py` 是**纯函数**：把当前回合最近 20 条 `messages` 与 20 次 `tool_calls`
  折成两个哈希加消息条数，看末尾连续多少拍三项都没变，输出三档 `CONTINUE` / `NUDGE` /
  `SUGGEST_STOP`。数据面 `app/queue/watchdog_probe.py` 只读摘要（内容可能很长，判断
  "有没有变化"不需要原文），调度面 `app/queue/watchdog_job.py` 用 Redis 租约互斥
  （多副本同时判断会把同一条提示注入两次），挂载点是 `queue/worker.py::_start_watchdog`。
  **`NUDGE` 复用 `agent/steer.py`** 在轮次边界注入（与 P0-3 同一条通道，不再造第二套
  "什么时候生效"的语义）；**`SUGGEST_STOP` 永不终止**，只写审计与日志 —— 终止不可逆，留给人。
  默认三层全关：`SUPERAGENT_WATCHDOG_ENABLED=false` 时 `worker.start()` **不创建任务**（关掉
  是真零影响，连采集都不跑），`SUPERAGENT_WATCHDOG_ACT=false` 时判断结果只进 `audit_logs`
  （`superagent.watchdog.nudge` / `.suggest_stop`，写失败只 warning）。fail-closed：样本不足、
  回合已不在 `running`、摘要为空、Redis 抢不到租约 —— 一律不干预。
  **明确未做**：判断层仍是确定性结构判断，不是原文 §6 的"模型层判断"；`subagent_runs` 未纳入采集。
  验收：`pytest tests/test_watchdog.py` 21 passed（含与 `loop_guard` 的"更早报警"对照、
  关闭时采集闭包未被调用、审计写入断言、`act=False` 不注入）。
- **接管预算硬闸门（P1-2 第 8 步 / 验收第 5 条）**：接管回合的真实用量现在会记进
  `superagent_takeovers.token_spent`，越过 `budget.token_cap` 即置终态
  `budget_exhausted`（`stop_reason='token_cap'`）并广播 `superagent.takeover_ended`，
  不再起下一拍。**口径与 `TrueUpTenantQuota` 一致**（`usage.input + usage.output`，
  `cached` 不进池也不计预算），否则面板读数会和账单对不上。`budget_exhausted` 单列成
  独立终态而不是复用 `stopped`：一个是用户点停、一个是花到上限，用户据此决定"调预算还是收工"。
  用量经 `ctx` 上的原子 sink（`withTakeoverUsageSink` / `takeoverUsageFromCtx`）回传，
  **不改 `HandleSubmit` 的签名**，也不塞进会被序列化进引擎请求体的 `workbenchCtx`；
  记账发生在归还租约**之前**，避免另一副本用旧读数抢到下一拍。前端：事件到达时插一行
  warning notice（遮罩只会突然消失、不给说明），配置页把已花展示成「已花 / 上限」，
  用掉 80% 起标警示色。三语言同步。
- **运行中补充指令的前端入口（P0-3 收口）**：`POST /v1/agent/steer` 此前只有引擎与网关
  两侧，前端没有调用方。现补 `api/index.ts::steerRun`，入口在 `ChatInput` 操作行的
  steer 按钮上（**只在"正在生成 + 有草稿"时出现**），**不与 Enter 共用** —— 生成中的
  Enter 已经是"打断并发送"，两条相反语义挤一个键必然误触。投递失败把原文退回输入框
  （`ChatInput.setDraft`）而不是静默清空：这条通道的拒绝多是临时的（429 限流 / 409 正在
  收尾），用户多半想重试一次。成功不额外插行 —— 引擎的 SSE `steer` 事件已经落了一条小字。
  验收：`vitest run ChatInput.spec.ts` 9 passed（新增 3 条：按钮出现条件 / 发出 steer 不打断
  且不写 ↑ 召回历史 / `setDraft` 回填失败原文）· `vue-tsc -b` 通过。
- **注册邮箱判重布隆过滤器**：注册时先用按租户分片的布隆过滤器快速否定"邮箱未注册"，
  从而跳过 `users` 上无索引的存在性查询（原先是一次全表扫描）。它**只做快速否定**，
  真相源是新增的 `users(tenant_id, email)` 唯一索引（迁移 `0002_users_tenant_email_unique`，
  对存量重复数据 fail-fast）；预热在后台异步完成，完成前与跨副本同步滞后时一律回落 DB 判重。
  开关 `EMAIL_BLOOM_FILTER_ENABLED`（默认开）、`EMAIL_BLOOM_CAPACITY`、`EMAIL_BLOOM_FP_RATE`。

### 变更

- **清理 22 张零引用死表**：基线中 84 张表里有 22 张在生产代码中零引用（11 张 `admin_*`
  影子表族 + 11 张未落地业务表），已从 `0001_authoritative_baseline.py`、`init.sql`、
  `configs/orm/V1/models.yaml` 中一并移除。迁移链保持单 head。
- **`tenant_id` 统一为 `smallint`**：44 个 `tenant_id` 列（原混用 `varchar(36/50/64)`）与
  `tenants.id`（改为 `smallserial`）统一为 2 字节整数，并补 `NOT NULL`。Go 侧
  `DefaultTenantID`/JWT `Claims.TenantID`/各结构体字段改为 `int16`；Python 引擎的 SQL 参数与
  Pydantic 字段、前端 TS 类型同步跟进。
- **`system_settings` 补 `UNIQUE(category, key)`**（迁移 `0003_system_settings_key_unique`）：
  原先只有 `id` 主键、靠代码"先查后写"保证唯一，并发写可能插出重复键。迁移先检测存量重复、
  有则 **fail-fast 并列出冲突**，不静默删数据。全链已在真实 PostgreSQL 18.1 上实跑验证
  （空库 `alembic upgrade head` 到 0003；两条 fail-fast 路径确认生效且不删数据）。
- **界面精修（P0–P3）**：明度阶别名层（`:root` 的
  `--elev-0..3` / `--hairline` / `--ring-inner/outer`，8 个主题块一行未改）；暗色模式用
  `html.dark` 一次性降字重（400/500/600/700 → 360/460/560/660）与字号（14 → 13px，`--fs-xs/sm`
  偏移同步收窄）；思考面板改为 `grid-template-rows: 0fr → 1fr` 撑开；数字统一
  `tabular-nums`（`.tnum` / `.tnum-fixed` 锁 `3ch`，覆盖思考时长、用量、成本、时间戳）；
  流式期水墨逐字（≤ 2000 字才拆、`--i` 只算本批新增字符并封顶 12 档）；点赞/点踩弹簧
  （按下 `scale(.86)` 走 60ms 过阻尼，松开由 `--ease-spring` 回弹，图标 pop 1.18 / 1.09）。
  三处动画均有 `prefers-reduced-motion` 降级。
- **新增 `utils/notify.ts`**：统一提示封装 —— 成功/提醒/信息 **2.5s**、错误 **4s**（错误要能读完）。
  `MessageItem` / `ChatView` / `ChatInput` 共 **50 处** `message.*` 改为 `notify.*`。
  验收：`npm run check:ui` 八项守卫全绿 · `vue-tsc -b` 通过 · `npm test` **83 files / 635 tests**。
- **落地 desktop-agent-ui 界面规约（四项）**：
  ① **12px 最小字号门禁** —— `--fs-xs` 在 `:root` 与 `html.dark` 都改为 12px，全仓 35 个文件
  共 **122 处** 9/10/11px 硬编码字号统一改用 `var(--fs-xs)`（此项**覆盖**了上一轮 UI 精修里
  「暗色小字号收窄到 11px」的决策）；② **移除中点「·」分隔符** —— 代码 14 处 + 三语言 i18n
  共 24 个文件，改用布局留白（flex 容器内由 `gap` 承担）；③ **同心圆圆角** —— 新增约定
  （内层 = `max(0, 外层 − padding)`；外层用主题 token 时以 `calc` 跟随，不写死像素），
  应用于 `ChatInput` 的 `.input-card` / `.paste-preview` 与 `ChatSidePanel` 的 `.session-menu`；
  ④ **0.5px 精细微边框** —— 新增 `--hairline-width` token，56 个文件共 **194 处** `1px solid` 改用它。
- **新增三项桌面端防错机制**：① **CJK 输入法伴生 Enter 守卫** —— `compositionend` 之后 500ms 内的
  Enter 视为「选词确认」的幽灵键，拦截并复位时间戳（`frontend-vue/src/components/chat/ime.ts`；
  此前只判 `e.isComposing`，高负载下该事件可能在 `isComposing` 转 false 后才到达，导致只想选词却发出消息）；
  ② **分栏拖拽 Escape 取消闭环** —— 复位状态机并把宽度还回拖拽前基准（`SessionPreviewPane.vue`），
  防止手柄 DOM 丢失后无法再展开；③ **composer 局部滚动条收敛** —— 仅输入框内隐藏原生滚动条外观，
  保留滚轮 / 触控 / 选中 / 键盘导航。
  验收：`npm run build`（八项守卫 + `vue-tsc -b` + `vite build`）通过 · `npm test` **84 files / 639 tests**。

## [v0.1.261022.01] - 2026-10-22

首个公开准备版本。

### 新增

- **多副本部署**：会话运行锁、SSE 跨实例广播与断线重放、run 归属路由、队列消费组与 PEL 接管、
  实例存活键与在途预留清理
- **多租户**：租户/用户配额、租户级审计与可回放证据链、SSO、计费幂等（`turn_id` 唯一索引）
- **Agent 运行时**：工具授权链、人在环审批（可跨进程答复）、检查点续跑、子 Agent 委派、
  多层记忆、知识库检索、工作流引擎
- **前端**：模型配置页的分组 / 折叠 / 批量启停；管理后台；i18n 三语言

### 变更

- 数据库迁移链收敛为单条权威基线（`0001_authoritative_baseline`）：全新库一次
  `alembic upgrade head` 即达终态
- 连接预算改为经 PgBouncer 收敛；网关与引擎的观测端点统一要求 `METRICS_TOKEN`
- 生产环境判定统一到 `python-engine/app/envmode.py`
  （`CHIRON_ENV` / `APP_ENV` / `PYTHON_ENV` 任一命中即生产）
- 内部 SQL 端点（`/v1/internal/db/*`）统一只读校验（含多语句与 CTE DML 拦截）
- 内部特权端点收敛：gateway Service 改为 `ClusterIP`，并补 NetworkPolicy
- k8s 下 gateway 挂载共享卷（媒体、上传、编辑器工作区与插件配置）
- 优雅终止窗口 30s → 60s 并补 `preStop`；gateway 补反亲和；镜像拉取策略改为 `IfNotPresent`

### 修复

- 沙箱租户白名单的注释与启动日志曾与代码语义相反（空名单 = **全部租户**走服务）
- `SANDBOX_BACKEND` 取值非法时曾静默退回进程内执行（现在拒绝启动）
- CTE 内嵌 DML 的 SQL 闸门漏判（形如 `WITH x AS (DELETE …) SELECT …` 曾被放行）

### 安全

- 漏洞请按 [SECURITY.md](SECURITY.md) 的私有渠道报告；安全修复会在 GitHub advisory 中单独披露
