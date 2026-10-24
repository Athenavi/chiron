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

- **注册邮箱判重布隆过滤器**：注册时先用按租户分片的布隆过滤器快速否定"邮箱未注册"，
  从而跳过 `users` 上无索引的存在性查询（原先是一次全表扫描）。它**只做快速否定**，
  真相源是新增的 `users(tenant_id, email)` 唯一索引（迁移 `0002_users_tenant_email_unique`，
  对存量重复数据 fail-fast）；预热在后台异步完成，完成前与跨副本同步滞后时一律回落 DB 判重。
  开关 `EMAIL_BLOOM_FILTER_ENABLED`（默认开）、`EMAIL_BLOOM_CAPACITY`、`EMAIL_BLOOM_FP_RATE`；
  设计说明见 `docs/email-bloom-filter-design.md`。

### 变更

- **清理 22 张零引用死表**：基线中 84 张表里有 22 张在生产代码中零引用（11 张 `admin_*`
  影子表族 + 11 张未落地业务表），已从 `0001_authoritative_baseline.py`、`init.sql`、
  `configs/orm/V1/models.yaml` 中一并移除。迁移链保持单 head。
- **`tenant_id` 统一为 `smallint`**：44 个 `tenant_id` 列（原混用 `varchar(36/50/64)`）与
  `tenants.id`（改为 `smallserial`）统一为 2 字节整数，并补 `NOT NULL`。Go 侧
  `DefaultTenantID`/JWT `Claims.TenantID`/各结构体字段改为 `int16`；Python 引擎的 SQL 参数与
  Pydantic 字段、前端 TS 类型同步跟进。设计说明见 `docs/tenant-id-smallint-migration.md`。
- **`system_settings` 补 `UNIQUE(category, key)`**（迁移 `0003_system_settings_key_unique`）：
  原先只有 `id` 主键、靠代码"先查后写"保证唯一，并发写可能插出重复键。迁移先检测存量重复、
  有则 **fail-fast 并列出冲突**，不静默删数据。全链已在真实 PostgreSQL 18.1 上实跑验证
  （空库 `alembic upgrade head` 到 0003；两条 fail-fast 路径确认生效且不删数据）。
- **界面精修（P0–P3，见 `docs/ui-refinement-design.md`）**：明度阶别名层（`:root` 的
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
- **落地 desktop-agent-ui 界面规约（四项，见 `docs/ui-refinement-design.md` §14）**：
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
