# 开发路线图

**待办账本**：只收录**未完成**事项（每条标「依据/验收」），已完成项只留指针，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**北极星：超越 deepseek-harness** —— 可比性、逐层对照与 6 个切口见 [与 DSH 的差距分析](dsh-gap-analysis.md)；判据以**对外可核实的行为**为准。

## 0. 当前状态

**最近一次核验（2026-10-08）**：Go build/vet/test 通过；另以本机真实 Redis 跑通 `internal/broadcast` · `internal/api` · `internal/engine` 的 Live 用例（补发保序/排他、跨实例可见、缓冲上限、滑动 TTL、运行锁互斥/续期/释放-CAS、取消归属、**提交路径的会话忙拒绝（HTTP 级）**、归属路由与回退）· `ruff` 0 · `mypy app/ acp_adapter/` 0（242 文件）· `pytest -m "not integration"` **1855 passed** · `pytest -m integration`（真实 PG + Redis 8）**8 passed**（另 2 条依赖本机未启动的 Go 网关，CI 的 real-stack job 会拉）· `check_source_encoding` / `check_tool_policy_parity` 通过 · alembic 单 head `0007_subagent_error_code`。
**前端（本机已核验）**：`npx vitest run` **477 passed**（56 文件）· `vue-tsc -b` 通过 · 本次改动文件 `eslint` 0 problems。**本机无 compose 插件、Docker daemon 未运行** ⇒ 容器相关改动（内置技能挂载）须在能跑 Docker 的环境复核。**CI 尚未实跑**（分支未 push，见 §3）。

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
| L4-2 CLI 迁移入口 | [决策记录](db-migration-entry.md) | **Alembic 唯一入口**：Go `db migrate`、Python `migrate run`/`downgrade` 均已删，只留**只读**诊断（`db status` / `migrate history`，失败非 0 退出）；`scripts/init.py` 第 6 步保留（跑的是同一份 `alembic.ini`，属部署向导） |
| L5-2 架构与请求链路 | `65b0558` | 文中命令均本机实测 |

## 2. 待办

### L4-1 run 现场 checkpoint 续跑 —— 设计已产出，待评审

- 设计：[run 现场 checkpoint 续跑设计](run-checkpoint-design.md) —— 状态机（running / checkpointed / resuming / 终态 / abandoned）· **回合级**落点（依据 `app/agent/runtime.py` 的 `for turn in range(task.max_turns)`）· 存储分层（PG `agent_runs` 唯一事实源 + Redis 热镜像）· **幂等边界**（含 `checkpoint_ttl ≤ TASK_IDEMPOTENCY_RETENTION_DAYS`）· 恢复路径（用户重试 + reconciler）· 5 批落地；已盘点可复用机制（workflow 节点级 checkpoint、run 租约、会话运行锁、SSE 重放）。
- **待拍板**：设计 §8 的 5 问 —— 对话 run 是否改走 `engine:tasks`、messages 快照形状、非幂等工具的中断判定、reconciler 接管风暴、checkpoint TTL 取值。
- 拆批见设计 §7（批 2 只增表与写入、无行为变更；批 3 才改执行路径）。

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（本机实测 2026-10-08）：**142 列** naive（72 表）· **12 列** `timestamptz`（5 表）· 唯一 VARCHAR 时间列是已废弃的 `schema_migrations.applied_at`。
- **语义已抽样判定**：存量 naive 值存的是**本地墙钟（+08）**而非 UTC —— 同一轮 run 在 `agent_runs.*`（`timestamptz`）是 `2026-09-30T14:26:40Z`，而 `turns` / `messages`（naive）是 `22:26–22:27`。
- ⚠ **风险不是"哪一侧对"，而是"同一列可能混着两种语义"**：`NOW()`/`CURRENT_TIMESTAMP`（**95** 处）跟**数据库会话时区**，`time.Now()`（**110** 处，含 6 处 `.UTC()`）跟**宿主时区**；两侧不一致时（典型：容器 TZ=UTC + 数据库 `timezone=Asia/Shanghai`）任何单条 `USING … AT TIME ZONE 'X'` 都会把一半的行移错时刻。**已加启动只读诊断**：`internal/db/tz_diagnostic.go` + `warnOnTimezoneMismatch`（不一致即告警；本机实测两侧同为 `+08` ⇒ aligned）。
- 剩余步骤：① 生产先跑同一条诊断（或把两侧时区对齐）；② **抽样确认生产存量语义，不要套用本机结论**；③ 用**影子列回填 → 校验 → 切换**三步走，而不是一次性 `ALTER … USING`；④ ORM 渲染 `DateTime(timezone=True)`（模板现无条件渲染 `DateTime`）。
- 前置：生产时区确认；需可达 PostgreSQL。验收：新迁移单 head + `alembic upgrade head --sql` 通过 + ORM 重新生成 + **迁移注释写清 `USING` 的时区依据**。

### DSH 切口 #3 剩余的一半：阶段（Phase）事件

- 思考（A1 的 `thinking`）与用量（C3 的 `usage`，**每次 LLM 调用**一条增量）**两条通道已落地且前端已消费**：`usage` 经 `mergeTurnStats` **累加成本轮一行** `turn_stats`（多步 ReAct 每个工具步一条事件，逐条 push 会堆出 N 行）；跨层计费守卫见 `internal/api/usage_accounting.go`。见 [差距分析](dsh-gap-analysis.md) §1 与切口 #3。
- 剩余**阶段**：需先定两件事 —— ① "新增事件类型"是否落入 `vendor/规划.md` §6「不改 SSE 协议」的边界；② 阶段的语义（当前运行时是 ReAct / Plan-and-Execute 循环，**对外没有阶段概念**，不应为了像对标物而造一个）。

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
| `scripts/` 无 lint 门禁 | CI 只跑 `scripts/check_*.py` 两个守卫；`ruff check scripts/` 现有 **8** 处历史问题（含 `scripts/cli/__init__.py` 一处刻意的 E402）——需决定是否纳入门禁并清历史 |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：按业务域拆 `routes_*.go`，见 [拆分评估](split-assessment.md) |
| **遗留的第二套 agent 循环 `app/agent/loop.py`** | 实测：它有自己的 `run_agent`（316 行，也自发 `usage`），经引擎路由 `/v1/agent/run` 暴露；而**产品链路是 `/v1/agent/submit` → `AgentRuntime`（`runtime.py`）**，且 Go 侧 `PythonClient.Run`（唯一会打 `/v1/agent/run` 的调用方）**零调用者** ⇒ 该文件＋该端点是**遗留**。待定：删除（含 `tests/test_agent.py` 的覆盖）还是显式标注 legacy；删前要确认没有外部调用方。
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
