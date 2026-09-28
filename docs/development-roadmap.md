# 开发路线图

本文是**待办账本**：只收录**尚未完成**的事项（每条标「依据」「验收」），已完成项只留提交号指针、明细见提交信息，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) 第 10 节。

## 0. 当前状态

主干全绿（最近一次本机核验）：Go build/vet/test 通过；`pytest -m "not integration"` 1259 passed + `pytest -m integration` 4 passed（真实 PG 18 + Redis 7）；`npm run test` 464 passed；`npm run check:ui` 通过；`npm run lint` 0 errors / 30 warnings（`no-explicit-any` 剩 3，见 L3-2）；`vue-tsc -b` 通过；`mypy app/` 0；alembic 单 head。**CI 尚未实际运行**（本分支未 push，见 §6）。

## 1. L1 国际化 ✅

- L1-3 译文补齐（`b7fea4f`/`84c6991`/`2dc7d81`）、L1-4 语义化 key 改造（`283c84b` + 收尾）已完成：`legacy.ts` 三语种清空。
- 仍然生效的护栏：`check-i18n-keys` 锁 `en-US/ar` 缺键 = 0；`check-i18n.mjs` 禁止**翻译调用里写中文字面键**（含 `tr`/`translate`/`i18n.t` 别名；注释与 `.test.ts` 除外）—— legacy 清空后 `t('中文')` 必然缺键且回显 key，靠人眼守不住。

## 2. L2 质量门禁 ✅（CI 首跑待确认）

- L2-1 `mypy --strict` 全量接入（`21a96ae`…`bf4860f`，1171→0 条 / 200 文件）；接线手册见 [贡献与验收流程](contributing.md)。
- L2-2 真实栈集成门禁（`578bc90`）：CI 新增 `integration` job —— pgvector PostgreSQL + Redis `services:` → `alembic upgrade head` → Go live 测试 → build 并启动网关（Python 的 unified DB/Redis 客户端走 `/v1/internal/*`）→ `pytest -m integration`；`APP_SECRET` 需 ≥32 字符。本机以真实栈验证全绿，并当场修掉两个真实缺陷（`RedisGet` 把 `redis.Nil` 当 500；workflow 实例写库传 aware datetime 撞 naive 列且异常被静默吞掉）。
- ⚠ 两者都还没在 CI 实跑过（本分支未 push）：空库建表路径、pgvector 镜像、网关在 runner 内启动都只能靠首跑确认，见 §6。

## 3. L3 技术债

### ✅ L3-2 收敛 ESLint warning（`no-explicit-any` 272 → 0，`de7cb61`…`4fb2e61`、`f0c6327`、`22ebf95`，本批收尾）

- `components/`、契约层、`views/` 全量清零；`catch (e: any)` 的取值动作统一收口到 `utils/apiError.ts`（`errorDetail`/`errorMessageText`/`serverErrorDetail`/`serverErrorMessage`/`errorStatus`/`apiErrorMessage`）。
- 收尾两条待决已落地（本批）：
  1. 删除 `ChatView` 三处塞给 `PUT /v1/conversations/{id}` 的无效 `llm_config`（后端 `Update` 只认 `title/pinned/tag/alias`）。其中 `persistRuntime` 那处**只带** `llm_config`，必然 400 且被 `.catch(() => {})` 静默吞掉 → 整行删除；rename/pin 两处去掉该字段。`buildPersistLlmConfig` 保留（`createSession` 仍用）。
  2. `catch` 取值口径定为**分层**并写入 `apiError.ts` 的文件头契约：终端用户即时反馈用 `describeApiError`（本地化）；管理/配置页与需按原文分支的场景保留原文类 helper。**不**全量改 `describeApiError` —— 它在 4xx 且状态码已知时会把后端原文换成通用文案（`400 invalid_request` → "请求失败，请稍后重试"），配置类操作恰恰需要那句原文。
- 剩余 27 条警告均为风格项（`no-unused-vars` 21 + `vue/require-default-prop` 5 + `vue/no-template-shadow` 1），无功能收益，不再动。
- 硬约束回顾：不为清零加 `eslint-disable`；每批单独跑 `vue-tsc -b` 与组件测试（本批 466 passed）。踩过的坑见提交信息（模板里 `as A | B` 会被判成 Vue2 filter、interface 不满足 `Record<string, unknown>` 形参等）。

### ✅ L3-4 巨型文件拆分（评估已产出 → `docs/split-assessment.md`）

- 实测现状（第一方代码，已排除 `vendor/`·`locales/`·迁移 baseline·ORM 生成物）：`ChatView.vue` 3047 行 / 173 符号、`runtime.py` 1841 行（`AgentRuntime` 单类 1557 行）、`MediaView.vue` 1672、`WorkflowView.vue` 1649、`main.py` 1509（`lifespan` 单函数 559 行）、`mail_handler.go` 1308、`memory/service.py` 1264（单类 1240 行）、`ChatInput.vue` 1193、`gateway_router.go` 970（`NewGatewayRouter` 372 行 + 10 个 `registerXxxRoutes`）。
- 结论：**建议拆**，但只按「纯移动 + 零行为变更」分 5 批走。批次 1（`gateway_router.go` → `routes_*.go`：同包纯移动、零 import 变更）风险最低，建议先做；三个大类的**内部**拆解（`AgentRuntime` / `MemoryService` / `QueueWorker` 各自是状态机/领域服务整体）属行为变更，明确排除。边界、契约、批次与验收口径见该文档。
- 未启动：本项无功能收益，仍排在功能项之后（§7 批次 D）。

### L3-7 `WorkflowView` 组件测试 ✅

- 本次提交：新增 `frontend-vue/src/views/__tests__/WorkflowView.spec.ts` —— `@vue-flow/*` 组件替身 + `useVueFlow` 最小桩（组件只渲染插槽、`getNodes/getEdges` 回吐 ref 形状的空图、`fitView` 用 spy），覆盖「工具栏入口 → 弹窗打开 → 列表按模板 payload 的 nodes/edges 数量渲染 → 点『使用』进入 loading → 成功后关窗并重新布局」。
- 两个坑（已写进测试注释）：① antd Modal 的组件名是 `AModal`，而 VTU 的 `stubs` 按**组件名**匹配 —— 只写 `Modal` 不生效；② `vi.mock('../api')` 这类**不带 `from`** 的路径不会被 `from '...'` 的批量替换命中（调试时 mock 实际指向不存在的 `src/views/api`，表现为「vi.fn 不是函数」）。
- 验收：vitest **466 passed**（464 + 2）、`eslint` 0、`vue-tsc -b` 通过。

### L3-8 引擎侧 `SSEProducer` 未接线（死代码）✅

- 本次提交：删除 `python-engine/app/sse/`（`producer.py` + `__init__.py`）与 `app/agent/runtime.py` 里随之永远为空的 `sse_producer` 参数和 `self._sse` 赋值（所有 `AgentRuntime` 构造点都用关键字传参，无人传它）；[架构与请求链路](architecture.md) §6 同步改为"曾经存在、已删除"。
- 验收：`ruff` 0、`mypy app/` 0（200 → 198 源文件）、`pytest -m "not integration"` 1259 passed。

## 4. L4 结构性后续（来源：多实例部署指南 §10）

### L4-1 run 现场 checkpoint 续跑 —— 设计已产出，待评审

- 本次提交：[run 现场 checkpoint 续跑设计](run-checkpoint-design.md) —— 状态机（running / checkpointed / resuming / 终态 / abandoned）、checkpoint 内容与**回合级**落点（依据 `app/agent/runtime.py` 的 `for turn in range(task.max_turns)`）、存储分层（PG `agent_runs` 为唯一事实源 + Redis 热镜像）、**幂等边界**（含 `checkpoint_ttl ≤ TASK_IDEMPOTENCY_RETENTION_DAYS` 的不等式）、恢复路径（用户重试 + reconciler）与 5 批分步落地；同时盘点了可复用的既有机制（workflow 节点级 checkpoint、run 租约、会话运行锁、SSE 重放）。
- 待评审拍板：设计文档 §8 的 5 个未决问题（对话 run 是否改走 `engine:tasks`、messages 快照形状、非幂等工具的中断判定、reconciler 接管风暴、checkpoint TTL 取值）。
- 实现拆批见设计文档 §7（批 2 只增表与写入、无行为变更；批 3 才改执行路径）。

### L4-2 `chiron-cli db` 迁移入口交互设计

- 现状：`chiron-cli db migrate` 实际 shell 出 `alembic upgrade head`，要求目标机有 python+alembic；`db status` 已读 `alembic_version`。
- 待决（用户决策）：CLI 完全不接触迁移（alembic 唯一入口）或保留「受控便捷封装」。
- 验收：产出决策记录，并统一 `chiron-cli db` 帮助文本/退出码/错误提示。

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据：本次实测 **142 列** `timestamp without time zone`（72 张表）、9 列 `with time zone`；唯一 VARCHAR 是已废弃 `schema_migrations.applied_at`，不涉及 VARCHAR 转换。
- ⚠ **新发现的前置（本次取证）**：原判「不涉及存量风险」只成立在「类型是 timestamp」这一层；**naive 值的时区语义未确认**，而它决定 `USING` 子句怎么写：
  - Go 侧 `time.Now().UTC()` 仅 **7** 处、`time.Now()`（**本地时间**）**102** 处；Python 侧 `datetime.now(UTC)` 19 处；
  - PG 侧 DDL 默认值 `now()` 写进 `timestamp without time zone` 列时按**会话时区**丢弃时区（本机实测 `now()` = `+08`，而 `now() at time zone 'UTC'` 差 8 小时）——即「写库时的本地时间」；
  - 结论：若照抄常见写法 `USING col AT TIME ZONE 'UTC'`，存量时间会**整体偏移 8 小时**。必须先抽样确认（取近期写入行与 `timestamptz` 列/UTC 时刻比对），再决定 `AT TIME ZONE '<会话时区>'` 还是分级处理。
- 剩余：提升为 `timestamptz`（含 `scripts/generate_orm_models.py` 生成模型与数十张表）——ORM 侧需让 datetime 列渲染 `DateTime(timezone=True)`（模板 `orm-template.jinja2` 目前无条件渲染 `DateTime`）。
- 前置：① 存量时区语义抽样确认（上述）；② 需可达 PostgreSQL（迁移是发布流程步骤）。
- 验收：新迁移追加到 `migrations/versions/`（单 head，CI schema job 校验）；ORM 重新生成；`alembic upgrade head --sql` 离线渲染通过；**并在迁移注释里写清 `USING` 的时区依据**。

## 5. L5 文档 ✅

- L5-2 架构与请求链路（`65b0558`）：新增 [架构与请求链路](architecture.md)（组件拓扑、一次对话请求的完整链路、事件回传与断线重放、其它入口、凭证与信任边界、自查命令），README 已链接；文中命令均在本机实测。

## 6. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖（beautifulsoup4/aiohttp 类）、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/enterprise`/`monitor`/`storage`/`id`/`model` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：建议按业务域拆 `routes_*.go`（同包纯移动、零 import 变更），见 [拆分评估](split-assessment.md) |

## 7. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-2 待用户决策 |
| D. 可维护性 | L3-4 | 无功能收益，放最后 |

> 批次 A（L2-2/L5-2）与批次 B（L1-4）已完成，见 §2/§5 与 §1。

## 8. 通用验收口径

（与 CI 一致；逐 job 说明见 [贡献与验收流程](contributing.md)，以 `.github/workflows/ci.yml` 为唯一事实来源）

```bash
# 仓库根
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1
python scripts/check_source_encoding.py
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# python-engine/
ruff check . && mypy app/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
