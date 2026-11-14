# 变更记录

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构。
版本号采用 `v0.1.YYMMDD.NN`：`YYMMDD` 为发布日期，`NN` 为当日第几次发布。

本文件只记录**对使用者可见**的变更：新增能力、行为变更、修复、安全。
内部重构、测试同步与逐文件验收记录留在 commit 与 PR 里。

## [Unreleased]

### 新增

- **Super-agent 接管**：在主循环之外判断"这一轮是否还在推进"，停滞时由接管者继续推进。
  接管回合的真实用量计入租户配额，并受独立的预算硬闸门约束（超限即置 `budget_exhausted`
  并广播结束事件，不再起下一拍）。默认关闭，三层开关分别控制采集、判断与干预。
- **运行中补充指令**：`POST /v1/agent/steer` 允许在 Agent 运行过程中插入新指令，
  前端已在输入区接入入口（生成中且有草稿时出现，不与 Enter 共用）。
  指令来源（用户 / 系统自动）随消息落库，历史里能区分"谁说的"。
- **Thread 串行排队**：同一会话的事件来源（用户提交、子 Agent 回投等）按全序串行执行，
  上限 20 条；超限落 `dropped` 记录并写审计，而不是静默丢弃。未抢到执行条件的请求
  留在队里返回 `queued`，由收尾钩子续接。
- **注册邮箱判重布隆过滤器**：按租户分片快速否定"邮箱未注册"，跳过 `users` 上的全表
  存在性查询。它只做快速否定，真相源是 `users(tenant_id, email)` 唯一索引；
  预热未完成或跨副本同步滞后时一律回落 DB 判重。

### 变更

- **`tenant_id` 统一为 `smallint`**：44 个 `tenant_id` 列与 `tenants.id` 由混用的
  `varchar(36/50/64)` 收敛为 2 字节整数并补 `NOT NULL`；Go / Python / 前端类型同步跟进。
  **这是一次破坏性 schema 变更**，滚动升级须走"扩展-迁移-收缩"。
- **清理 22 张零引用死表**：基线中 84 张表里有 22 张在生产代码中零引用（`admin_*` 影子
  表族与未落地业务表），已从基线、`init.sql` 与 ORM 模型定义中一并移除。
- **`system_settings` 补 `UNIQUE(category, key)`**：原先只靠代码"先查后写"，并发写可插出
  重复键。迁移先检测存量重复，有则 fail-fast 并列出冲突，不静默删数据。
- **界面精修**：明度阶别名层、暗色模式统一降字重与字号、思考面板改用网格撑开过渡、
  数字统一 `tabular-nums`、流式输出逐字水墨效果、点赞/点踩弹簧动画。
  三处动画均有 `prefers-reduced-motion` 降级。
- **界面规约落地**：最小字号统一为 12px（全仓 122 处硬编码小字号改用 token）、移除中点
  「·」分隔符改用布局留白、同心圆圆角约定、新增 `--hairline-width` 精细微边框 token。
- **统一提示封装**：成功/提醒/信息 2.5s、错误 4s（错误要能读完）。

### 修复

- **注册接口返回 500**：`set_config('app.current_tenant_id', $1, true)` 的第二参是 `text`，
  而 `tenant_id` 改型后传的是 `int16`，pgx 编码失败使整个注册事务在第一步中断。
  这类 SQL 参数类型问题编译与单测都发现不了，只有真实请求才暴露。
- **用量对账与 API-Key 校验的 `COALESCE(tenant_id, '')`**：列改 `smallint` 后该写法会让
  PostgreSQL 报 `invalid input syntax for type smallint: ""`，两处加 `::text`，保持
  「tenant_id 为空直接拒绝」的判断不变。
- **知识库设置模块的循环导入**：延迟到函数内导入，避免与 `app.tools` 形成初始化环。
- **前端 `tenant_id` 类型遗留**：`vue-tsc -b` 此前是失败的（认证 store 的恢复/回填、
  成本中心与市场页的表单互转、调用链组件的类型声明），现已全部归位。
- **CJK 输入法伴生 Enter 守卫**：`compositionend` 后 500ms 内的 Enter 曾被当作确认键，
  导致"只想选词却发出消息"。高负载下 `isComposing` 转 false 可能晚于事件到达。
- **分栏拖拽 Escape 取消闭环**：复位状态机并把宽度还回拖拽前基准，
  防止手柄 DOM 丢失后无法再展开。

### 安全

- 漏洞请按 [SECURITY.md](SECURITY.md) 的私有渠道报告；安全修复会在 GitHub advisory 中单独披露。

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
