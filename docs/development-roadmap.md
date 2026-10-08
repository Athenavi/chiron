# 开发路线图

**待办账本**：只收录**未完成**事项（每条标「依据/验收」），已完成项只留指针，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**北极星：超越 deepseek-harness** —— 可比性、逐层对照与 6 个切口见 [与 DSH 的差距分析](dsh-gap-analysis.md)；判据以**对外可核实的行为**为准。

## 0. 当前状态

**最近一次核验（2026-10-08）**：Go build/vet/test 通过；另以本机真实 Redis 跑通 `internal/broadcast` · `internal/api` · `internal/engine` 的 Live 用例（补发保序/排他、跨实例可见、缓冲上限、滑动 TTL、运行锁互斥/续期/释放-CAS、取消归属、**提交路径的会话忙拒绝（HTTP 级）**、归属路由与回退）· `ruff` 0 · `mypy app/ acp_adapter/` 0（242 文件）· `pytest -m "not integration"` **1855 passed** · `pytest -m integration`（真实 PG + Redis 8）**8 passed**（另 2 条依赖本机未启动的 Go 网关，CI 的 real-stack job 会拉）· `check_source_encoding` / `check_tool_policy_parity` 通过 · alembic 单 head `0007_subagent_error_code`。
**未核验**（沿用上次全量）：`npm run test` 466 passed · `check:ui` · `vue-tsc -b` · `npm run lint` 0 errors / 27 warnings。**本机无 compose 插件、Docker daemon 未运行** ⇒ 容器相关改动（内置技能挂载）须在能跑 Docker 的环境复核。**CI 尚未实跑**（分支未 push，见 §3）。

## 1. 已完成（只留指针与仍生效的护栏）

| 项 | 指针 | 仍生效的护栏 / 注意 |
|---|---|---|
| L1 国际化 | `b7fea4f`…`283c84b` | `check-i18n-keys` 锁 `en-US/ar` 缺键 = 0；`check-i18n.mjs` 禁止**翻译调用**里写中文字面键（`tr`/`translate`/`i18n.t` 别名；注释与 `.test.ts` 除外） |
| L2-1 mypy strict | `21a96ae`…`bf4860f`（1171→0 / 200 文件） | 接线手册见 [贡献与验收流程](contributing.md) |
| L2-2 真实栈集成门禁 | `578bc90` | ⚠ **未在 CI 实跑**（见 §3） |
| L3-2 ESLint `no-explicit-any` | `de7cb61`…`22ebf95`（272→0） | 不为清零加 `eslint-disable`；`catch` 取值**分层**契约写在 `utils/apiError.ts` 文件头；余 27 条风格警告不再动 |
| L3-7 `WorkflowView` 组件测试 | `views/__tests__/WorkflowView.spec.ts` | 两个 VTU 坑记在测试注释里 |
| L3-8 `SSEProducer` 死代码 | 已删除 | [架构与请求链路](architecture.md) §6 已同步 |
| L3-9 工具名单漂移 | 两侧策略表已修 | **不变量**：`scripts/check_tool_policy_parity.py`（已接入 CI 的 `Source encoding` job）+ `tests/test_tool_policy_coverage.py` 的 `LEGACY_TOOL_NAMES` |
| L5-2 架构与请求链路 | `65b0558` | 文中命令均本机实测 |

## 2. 待办

### L4-1 run 现场 checkpoint 续跑 —— 设计已产出，待评审

- 设计：[run 现场 checkpoint 续跑设计](run-checkpoint-design.md) —— 状态机（running / checkpointed / resuming / 终态 / abandoned）· **回合级**落点（依据 `app/agent/runtime.py` 的 `for turn in range(task.max_turns)`）· 存储分层（PG `agent_runs` 唯一事实源 + Redis 热镜像）· **幂等边界**（含 `checkpoint_ttl ≤ TASK_IDEMPOTENCY_RETENTION_DAYS`）· 恢复路径（用户重试 + reconciler）· 5 批落地；已盘点可复用机制（workflow 节点级 checkpoint、run 租约、会话运行锁、SSE 重放）。
- **待拍板**：设计 §8 的 5 问 —— 对话 run 是否改走 `engine:tasks`、messages 快照形状、非幂等工具的中断判定、reconciler 接管风暴、checkpoint TTL 取值。
- 拆批见设计 §7（批 2 只增表与写入、无行为变更；批 3 才改执行路径）。

### L4-2 CLI 迁移入口 —— 决策已落地，剩两处待定

- 决策（用户）：**Alembic 唯一入口**（→ [决策记录](db-migration-entry.md)）。理由：迁移是发布流程步骤而非运行时命令；两套入口必然版本漂移；应用镜像**刻意不装 Python**，CLI 迁移在生产最需要时恰恰不可用。
- 已落地：删 `chiron-cli db migrate` / `runDBMigrate`、失去调用者的 `internal/db/migrate.go`（含测试）与 `hasInternalMigrationFiles`；**保留**只读 `db status` 与 `internal/db/schema_version.go`。
- **未处理**（决策文档 §4）：`scripts/cli/commands/migrate.py` 的 `revision`（**autogenerate**）/`upgrade`/`downgrade` 与 `scripts/init.py` 第 6 步自动迁移 —— 定下前「唯一入口」只在 Go CLI 层成立。

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（实测）：**142 列** `timestamp without time zone`（72 表）、9 列 `with time zone`；唯一 VARCHAR 是已废弃 `schema_migrations.applied_at`。
- ⚠ **前置未解**：naive 值的**时区语义未确认**，而它决定 `USING` 怎么写 —— Go 侧 `time.Now().UTC()` 仅 **7** 处、`time.Now()`（**本地时间**）**102** 处；PG 的 `now()` 写进无时区列时按**会话时区**丢弃时区（本机实测 `+08`）。照抄 `USING col AT TIME ZONE 'UTC'` 会让存量**整体偏移 8 小时**；须先抽样（近期写入行 vs `timestamptz` 列/UTC 时刻）再定 `AT TIME ZONE '<会话时区>'` 或分级处理。
- 剩余：提升为 `timestamptz`（含 `scripts/generate_orm_models.py` 与数十张表）—— ORM 侧需渲染 `DateTime(timezone=True)`（模板 `orm-template.jinja2` 现无条件渲染 `DateTime`）。
- 前置：① 上述抽样确认；② 需可达 PostgreSQL。验收：新迁移追加到 `migrations/versions/`（单 head，CI schema job 校验）· ORM 重新生成 · `alembic upgrade head --sql` 通过 · **迁移注释写清 `USING` 的时区依据**。

### L3-4 巨型文件拆分 —— 批次 1、3 与批次 2(类型) 已完成

- 已完成（每批都验证**逐函数/定义逐字一致**）：`gateway_router.go` → 11 个 `routes_*.go`（**1254→442**）· `mail_handler.go` → 4 个文件（**1488→695**）· `session/manager.go` → `manager_{crud,messages,queries,branch}.go`（**1198→128**）· `runtime.py` 类型与决策取值 → `runtime_types.py`（**3370→3198**）。
- 剩余：`runtime.py` 压缩簇（**必须先按现状重新定界** —— 评估里的 1841/1557 行已过期）· `main.py` · 前端 composable。边界、批次与验收见 [拆分评估](split-assessment.md)；无功能收益，排最后（§4 批次 D）。

## 3. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| **超越 deepseek-harness 的切口** | 已产出 [与 DSH 的差距分析](dsh-gap-analysis.md)（逐层对照 + 6 个切口 + 未验证清单）与 [多副本语义的对外保证](deployment-multi-instance.md)；每个切口立项前须给出**可复现命令、期望输出与反例** |
| `docs/deepagents-gap-analysis.md` 悬空引用 | `vendor/规划.md` §2 把它列为判断依据，但该文件**在仓库中不存在**：需重做或删引用 |
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/{enterprise,monitor,storage,id,model}` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：按业务域拆 `routes_*.go`，见 [拆分评估](split-assessment.md) |
| 引擎镜像自包含内置技能 | 现由 compose 只读挂载 + `CHIRON_BUILTIN_SKILLS_PATH` 提供（源缺失会告警）；镜像自包含需把构建上下文改为仓库根并 `COPY market/skills` —— 必须**真实 `docker build` + 容器内自查**，不能用单测替代 |

## 4. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-2 待用户决策 |
| D. 可维护性 | L3-4 | 无功能收益，放最后 |

> 批次 A（L2-2/L5-2）与批次 B（L1-4）已完成。

## 5. 通用验收口径

（与 CI 一致；逐 job 说明见 [贡献与验收流程](contributing.md)，以 `.github/workflows/ci.yml` 为唯一事实来源）

```bash
# 仓库根
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1
python scripts/check_source_encoding.py
python scripts/check_tool_policy_parity.py   # Go ↔ Python 分级表必须同构（L3-9）
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# 真实栈用例（需可达 PostgreSQL / Redis；CI 由 real-stack job 跑）
go test -mod=mod ./... -count=1 -run 'Live|Diag'   # CHIRON_TEST_POSTGRES_DSN / CHIRON_TEST_REDIS_ADDR

# python-engine/
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
