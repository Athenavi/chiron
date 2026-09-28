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

### L3-2 收敛 ESLint warning（`no-explicit-any` 272 → 3）

- 现状：`components/`、契约层、`views/` 均已清零（`de7cb61`…`4fb2e61`、`f0c6327`、`22ebf95`）；`utils/apiError.ts` 提供 `errorDetail`/`errorMessageText`/`serverErrorDetail` 与既有 `serverErrorMessage`/`errorStatus`/`apiErrorMessage`，`catch (e: any)` 的取值动作统一收口到它们（只取原文、不改文案）。
- 待决（行为变更，需单独定，勿顺手改）：
  1. `ChatView` 3 处把 `llm_config` 塞给 `PUT /v1/conversations/{id}`，而后端只认 `title/pinned/tag/alias`（`internal/api/conversation.go` 的 `Update`；`DecodeJSON` 不拒绝未知字段）。其中 `persistRuntime` 那处**只带 `llm_config`** → 必被 400 拒绝并被 `.catch(() => {})` 静默吞掉；rename/pin 两处被后端忽略。删除这层无效负载（连同失去调用者的 `buildPersistLlmConfig`）是行为变更。
  2. 全仓 136 处 `catch (e: any)` 是否统一改用 `describeApiError`（会把文案本地化）。
- 硬约束：不为清零加 `eslint-disable`；每批单独跑 `vue-tsc -b` 与组件测试。踩过的坑见提交信息（模板里 `as A | B` 会被判成 Vue2 filter、interface 不满足 `Record<string, unknown>` 形参等）。

### L3-4 巨型文件拆分（评估项）

- 依据：`internal/api/gateway_router.go` 62 KB；`python-engine/app/agent/runtime.py` 101 KB、`app/main.py` 85 KB、`app/memory/service.py` 54 KB、`app/queue/worker.py` 51 KB。
- 验收（若启动）：先出拆分边界与契约清单，再按「纯移动 + 零行为变更」分步提交，每步测试绿。

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

- 依据：实测 145 个时间列中 136 个 `timestamp without time zone`、9 个 `with time zone`；唯一 VARCHAR 是已废弃 `schema_migrations.applied_at`，不涉及 `USING` 转换与存量风险。
- 剩余：提升为 `timestamptz`（含 `scripts/generate_orm_models.py` 生成模型与数十张表）。
- 前置：需可达 PostgreSQL（迁移是发布流程步骤）。
- 验收：新迁移追加到 `migrations/versions/`（单 head，CI schema job 校验）；ORM 重新生成；`alembic upgrade head --sql` 离线渲染通过。

## 5. L5 文档 ✅

- L5-2 架构与请求链路（`65b0558`）：新增 [架构与请求链路](architecture.md)（组件拓扑、一次对话请求的完整链路、事件回传与断线重放、其它入口、凭证与信任边界、自查命令），README 已链接；文中命令均在本机实测。

## 6. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖（beautifulsoup4/aiohttp 类）、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/enterprise`/`monitor`/`storage`/`id`/`model` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | `gateway_router.go` 单文件承载，是否拆分待 L3-4 评估 |

## 7. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-2 待用户决策 |
| D. 可维护性 | L3-2 待决项、L3-4 | 无功能收益，放最后 |

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
