# 开发路线图

本文是**待办账本**：只收录**尚未完成**的事项（每条标「依据」「验收」），已完成项只留提交号指针、明细见提交信息，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**北极星：超越 deepseek-harness** —— 可比性、逐层对照与 6 个切口见 [与 DSH 的差距分析](dsh-gap-analysis.md)；判据以**对外可核实的行为**为准，不以"有对应模块"为准。

## 0. 当前状态

**最近一次本机核验（2026-10-08）**：Go build/vet/test 通过 · `ruff check .` 0 · `mypy app/ acp_adapter/` 0（240 文件）· `pytest -m "not integration"` **1837 passed** · `pytest -m integration`（本机真实 PG + Redis 8）**8 passed**（另 2 条 `test_unified_db` 依赖本机未启动的 Go 网关，属环境限制，CI 的 real-stack job 会拉网关）· `scripts/check_source_encoding.py` 通过 · `scripts/check_tool_policy_parity.py` 通过 · alembic 单 head `0007_subagent_error_code`。
**本机核验不了**（沿用最近一次全量）：`npm run test` 466 passed · `npm run check:ui` · `vue-tsc -b` · `npm run lint` 0 errors / 27 warnings。另：本机**无 compose 插件、Docker daemon 未运行** ⇒ `docker compose config` 与"镜像/容器内"自查做不了，容器相关改动（本轮的内置技能挂载）需在能跑 Docker 的环境复核。
**CI 尚未实跑**（分支未 push，见 §6）。

## 1. L1 国际化 ✅

`legacy.ts` 三语种已清空（`b7fea4f`/`84c6991`/`2dc7d81`/`283c84b`）。**护栏仍生效**：`check-i18n-keys` 锁 `en-US/ar` 缺键 = 0；`check-i18n.mjs` 禁止**翻译调用**里写中文字面键（含 `tr`/`translate`/`i18n.t` 别名；注释与 `.test.ts` 除外）—— legacy 清空后 `t('中文')` 必然缺键且回显 key，靠人眼守不住。

## 2. L2 质量门禁 ✅（CI 首跑待确认）

- L2-1 `mypy --strict` 全量接入（`21a96ae`…`bf4860f`，1171→0 条 / 200 文件）；接线手册见 [贡献与验收流程](contributing.md)。
- L2-2 真实栈集成门禁（`578bc90`）：CI `integration` job = pgvector PostgreSQL + Redis `services:` → `alembic upgrade head` → Go live 测试 → 启动网关（Python 的 unified DB/Redis 客户端走 `/v1/internal/*`）→ `pytest -m integration`；`APP_SECRET` 需 ≥32 字符。本机真实栈全绿，并当场修掉两个缺陷（`RedisGet` 把 `redis.Nil` 当 500；workflow 写库传 aware datetime 撞 naive 列且异常被静默吞掉）。
- ⚠ 两者都**没在 CI 实跑过**：空库建表路径、pgvector 镜像、网关在 runner 内启动只能靠首跑确认（§6）。

## 3. L3 技术债

### L3-2 ESLint warning ✅

`no-explicit-any` 272 → 0（`de7cb61`…`4fb2e61`、`f0c6327`、`22ebf95`）。**硬约束**：不为清零加 `eslint-disable`；每批单跑 `vue-tsc -b` 与组件测试。`catch` 取值口径已定为**分层**并写入 `utils/apiError.ts` 文件头（终端用户即时反馈用 `describeApiError`；管理/配置页等需按原文分支的场景保留原文类 helper）。剩余 27 条风格警告（`no-unused-vars` 21 + `vue/require-default-prop` 5 + `vue/no-template-shadow` 1）无功能收益，不再动。

### L3-4 巨型文件拆分 —— 评估已产出，**批次 1 已完成**

- 结论：**建议拆**，只按「纯移动 + 零行为变更」分 5 批；批次 1（`gateway_router.go` → `routes_*.go`：同包纯移动）风险最低，已先做；三个大类的**内部**拆解（`AgentRuntime`/`MemoryService`/`QueueWorker` 各自是状态机/领域服务整体）属行为变更，**明确排除**。
- **批次 1 已完成**：`gateway_router.go` → 11 个 `routes_<domain>.go`（**1254 → 442 行**）；**逐函数文本与拆分前逐字一致**（16/16），Go build/vet/test 全绿。
- 实测行数、边界、契约、批次与验收口径全部见 [拆分评估](split-assessment.md)（避免两处漂移）。
- 剩余批次 2–5 未启动：本项无功能收益，排在功能项之后（§7 批次 D）。

### L3-7 `WorkflowView` 组件测试 ✅

`views/__tests__/WorkflowView.spec.ts`：`@vue-flow/*` 替身 + `useVueFlow` 最小桩，覆盖「工具栏入口 → 弹窗 → 按模板 payload 渲染 → 使用 → 关窗重布局」。验收：vitest 466 passed、`eslint` 0、`vue-tsc -b` 通过（两个 VTU 坑记在测试注释里）。

### L3-8 引擎侧 `SSEProducer` 未接线（死代码）✅

已删除 `python-engine/app/sse/` 与 `runtime.py` 里恒空的 `sse_producer` 参数、`self._sse` 赋值；[架构与请求链路](architecture.md) §6 同步。验收：`ruff` 0、`mypy app/` 0、`pytest -m "not integration"` 全绿。

### L3-9 工具名单漂移 —— 已修，「两侧同构」已变成机械断言 ✅

- **问题（本轮取证）**：`40dfc99` 删除 `app/tools/agent.py` 的 5 个工具后，`app/agent/tool_policy.py`、`internal/api/tool_policy.go`、`guards.py`、`subagent_runner.py` 的 `DELEGATE_TOOL_NAMES` 仍引用它们；`tool_search.py` 的「委派」别名指向不存在的工具。
- **本轮修法**：① 两张**活表**（Go 权威 + Python 保守镜像）删除 5 个死名；② 顺带修掉三处**真实漂移** —— `read_tool_result`（只在 Go）、`list_subagent_runs` / `rerun_subagent`（只在 Python），即权威判定与镜像对这三个真工具给出不同级别；③ 保留的兜底名（`knowledge`/`stderr_drain`/`tool`/`delete_file`/`kb_delete`/`execute_command`）在两侧注释与 `tests/test_tool_policy_coverage.py` 的 `LEGACY_TOOL_NAMES` 里**逐个点名**；④ `guards.py` 的 `DANGEROUS_TOOLS`/`WRITE_TOOLS` 是已被测试钉住的兼容名单（判定不依赖它们），不动。
- **新不变量**：`scripts/check_tool_policy_parity.py` 逐级别比对两张表（已接入 CI `Source encoding` job），漂移即失败；`test_tool_policy_coverage.py` 要求表里每个名字 ∈ 注册表 ∪ 点名兜底名。
- 验收：parity 脚本 OK（54 条分级 + 5 个命令类）；新增 3 条用例通过。

## 4. L4 结构性后续（来源：多实例部署指南 §10）

### L4-1 run 现场 checkpoint 续跑 —— 设计已产出，待评审

- 设计：[run 现场 checkpoint 续跑设计](run-checkpoint-design.md) —— 状态机（running / checkpointed / resuming / 终态 / abandoned）、checkpoint 内容与**回合级**落点（依据 `app/agent/runtime.py` 的 `for turn in range(task.max_turns)`）、存储分层（PG `agent_runs` 为唯一事实源 + Redis 热镜像）、**幂等边界**（含 `checkpoint_ttl ≤ TASK_IDEMPOTENCY_RETENTION_DAYS` 的不等式）、恢复路径（用户重试 + reconciler）与 5 批分步落地；已盘点可复用机制（workflow 节点级 checkpoint、run 租约、会话运行锁、SSE 重放）。
- 待评审拍板：设计 §8 的 5 个未决问题（对话 run 是否改走 `engine:tasks`、messages 快照形状、非幂等工具的中断判定、reconciler 接管风暴、checkpoint TTL 取值）。
- 实现拆批见设计 §7（批 2 只增表与写入、无行为变更；批 3 才改执行路径）。

### L4-2 CLI 迁移入口：完全不接触迁移 —— 决策已落地，剩两处待定

- 决策（用户）：**Alembic 唯一入口**（→ [决策记录](db-migration-entry.md)）。理由：迁移是发布流程步骤而非运行时命令；两套入口必然版本漂移；应用镜像**刻意不装 Python**（`requirements-migrate.txt`），CLI 迁移在生产最需要时恰恰不可用，只会造成"代码已升级、迁移未跑"的假象。
- 已落地：删除 `chiron-cli db migrate` / `runDBMigrate`、失去全部调用者的 `internal/db/migrate.go`（含测试）与死代码 `hasInternalMigrationFiles`；**保留**只读 `db status` 与 `internal/db/schema_version.go`（网关启动的只读 schema 校验）。
- **仍未处理**（需单独决定，见决策文档 §4）：`scripts/cli/commands/migrate.py` 的 `revision`（**autogenerate**）/`upgrade`/`downgrade`，以及 `scripts/init.py` 第 6 步的自动迁移 —— 定下前「唯一入口」只在 Go CLI 层面成立。

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（实测）：**142 列** `timestamp without time zone`（72 张表）、9 列 `with time zone`；唯一 VARCHAR 是已废弃 `schema_migrations.applied_at`，不涉及 VARCHAR 转换。
- ⚠ **前置未解（取证发现）**：naive 值的**时区语义未确认**，而它决定 `USING` 怎么写 —— Go 侧 `time.Now().UTC()` 仅 **7** 处、`time.Now()`（**本地时间**）**102** 处，Python 侧 `datetime.now(UTC)` 19 处；PG 的 `now()` 写进 `timestamp without time zone` 列时按**会话时区**丢弃时区（本机实测 `+08`，与 `now() at time zone 'UTC'` 差 8 小时）。照抄 `USING col AT TIME ZONE 'UTC'` 会让存量时间**整体偏移 8 小时**；须先抽样确认（近期写入行 vs `timestamptz` 列/UTC 时刻），再定 `AT TIME ZONE '<会话时区>'` 或分级处理。
- 剩余：提升为 `timestamptz`（含 `scripts/generate_orm_models.py` 生成模型与数十张表）—— ORM 侧需让 datetime 列渲染 `DateTime(timezone=True)`（模板 `orm-template.jinja2` 目前无条件渲染 `DateTime`）。
- 前置：① 上述时区语义抽样确认；② 需可达 PostgreSQL（迁移是发布流程步骤）。
- 验收：新迁移追加到 `migrations/versions/`（单 head，CI schema job 校验）；ORM 重新生成；`alembic upgrade head --sql` 离线渲染通过；**迁移注释里写清 `USING` 的时区依据**。

## 5. L5 文档 ✅

L5-2 [架构与请求链路](architecture.md)（`65b0558`）：组件拓扑、一次对话请求的完整链路、事件回传与断线重放、其它入口、凭证与信任边界、自查命令；README 已链接，文中命令均本机实测。

## 6. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| **超越 deepseek-harness 的切口** | 已产出 [与 DSH 的差距分析](dsh-gap-analysis.md)（逐层对照 + 6 个切口 + 未验证清单）；每个切口立项前须给出**可复现命令、期望输出与反例** |
| `docs/deepagents-gap-analysis.md` 悬空引用 | `vendor/规划.md` §2 把它列为"判断依据"，但该文件**在仓库中不存在**（未被 git 跟踪、工作区也没有）：需重做或删掉引用 |
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖（beautifulsoup4/aiohttp 类）、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/enterprise`/`monitor`/`storage`/`id`/`model` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：建议按业务域拆 `routes_*.go`（同包纯移动、零 import 变更），见 [拆分评估](split-assessment.md) |
| 引擎镜像自包含内置技能 | 现由 compose 只读挂载 `./market/skills` + `CHIRON_BUILTIN_SKILLS_PATH` 提供（源缺失会告警）。要**镜像自包含**（K8s 等不带仓库检出的部署）需把引擎构建上下文改为仓库根并 `COPY market/skills`：改动镜像内容，必须**真实 `docker build` + 容器内自查**，不能用单测替代（§3.4 同类） |

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
python scripts/check_tool_policy_parity.py   # Go ↔ Python 分级表必须同构（L3-9）
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# python-engine/
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
