# 开发路线图

**待办账本**：只收录**未完成**事项（每条标「依据/验收」），已完成项只留指针，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**判据**：一切"完成"以**对外可核实的行为**为准（可复现命令 + 期望输出 + 反例），而不是"有对应模块"。

## 0. 当前状态

**最近一次全量核验（2026-10-22）**：Go `build`/`vet`/`test` 全绿；引擎 `ruff`/`mypy` 0、
`pytest` 2839 passed；前端 `vue-tsc` 0 + `vitest` 620 passed；仓库根守卫全 exit 0；
alembic 单 head `0029_model_prices`。integration 已在真实 PG 上跑通（40 passed / 1 skipped）。
本机跑 pytest 请加 `--basetemp=.pytest_tmp`：默认 TEMP 里那条死符号链接会在会话收尾抛
`PermissionError`，把汇总行吃掉、看起来像整轮红了。

**本机做不到的**：容器相关（无 Docker daemon）⇒ 镜像与容器内自查须在能跑 Docker 的环境验证。
**逐轮历史数字**不内联：查 `git log` 对应提交。

## 1. 已完成（只留指针与仍生效的护栏）

| 项 | 指针 | 仍生效的护栏 / 注意 |
|---|---|---|
| L1 国际化 | `b7fea4f`…`283c84b` | `check-i18n-keys` 锁 `en-US`/`ar` 缺键 = 0；`check-i18n.mjs` 禁止翻译调用里写中文字面键；两者都在 `check:ui`，且用 esbuild 真编译（不对注释假阳性）。 |
| L2-1/L2-2 mypy strict + 真实栈集成门禁 | `21a96ae`…`bf4860f` · `578bc90` | `mypy app/ acp_adapter/` 全量 strict；`integration`（真 PG）与 `live`（真 Redis）分层标记；真实栈 job 见 §3。 |
| CI 首跑前必红的两处静态门禁（M16） | `b120026` 起的 mypy/ruff 修复 | 逐条按代码判定，零 `# type: ignore`；顺带修出 Anthropic `system` parts 拍平与 `ResponsesProvider` 继承两个真 bug。 |
| 死实例的在途预留清扫 + 优雅退出释放（M17） | `app/instance_liveness.py` · `app/subagent/line_ledger.py` | 新增无条件存活键 `{prefix}engine:alive:{instance_id}`（TTL 60s / 续期 20s，`stop()` 主动删）；不复用有条件写的 `engine:instance:*`，否则读不到键会误清扫全站预留。 |
| 在途预留的投影重述 + 带用量事件即冲刷（M18） | `app/subagent/line_ledger.py` · `app/agent/subagent_runner.py` | 带用量事件 = 一次模型调用结束：先 flush 步骤缓冲，再把预留覆盖式重述为「已花 + 观测速率×120s」；上限夹 per-run 上限、下限不低于已花、速率分母 `max(本段用时, 30s)`。 |
| 崩溃/接管段用量的按 run 对账（M19） | `internal/api/run_usage_reconciler.go` · `0026_run_usage_settlements.py` | 权威账 = `agent_runs.checkpoint.usage`，已计费取 max（不是相加）；三档 `off` / `shadow`（默认）/ `enforce`；原则「宁可少补，不能多收」。 |
| Agent 预算命名分离 + 后台配置接线（M20） | `config/agent_env_rename.go` · `internal/api/submit_handler.go` | 网关 `AGENT_MAX_OUTPUT_TOKENS` / `AGENT_DEFAULT_MAX_TURNS`，引擎 `AGENT_RUN_MAX_TOKENS` / `AGENT_RUN_MAX_TURNS`；旧名由 `ValidateRenamedAgentEnv` 拒绝启动；优先级 = 请求 > 后台 > 默认。 |
| 人工介入待办的跨会话可见性与提醒（M21） | `app/api/interactions.py` · `internal/api/interaction_events_relay.go` | 汇总端点 + 用户级 SSE（`/v1/agent/interactions/stream`，身份只来自 claims）；事件不带 `arguments`；超时 24h；不做浏览器通知 / 邮件 / Webhook。 |
| L3 摘要检索模式透出到前端（M22） | `app/memory/service.py` · `frontend-vue/src/views/MemoryView.vue` | `summary_mode` 是独立字段，不与 L2 的 `mode` 合并；`empty`/缺失不显示标签；存量摘要向量回填见 §3。 |
| 换 key 重试（M23） | `app/gateway/key_retry.py` | 只包住 `create()` 提交点，绝不包 chunk 循环；`KEY_RETRY_MAX` 默认 1（0 = 关）；401/403/408/409/429 + 5xx + 传输类可换 key。 |
| credits 配额池生效（M24） | `internal/api/ent_quota_enforce.go` | `TenantCreditPools` 按 `credit_transactions` 的扣减量求和（与用户账单同源）；`quotaPeriodKey` 加 `resource_type`；credits 池只读判定 + 到限硬拦，不预扣；`trueUp` 跳过。 |
| 后台「系统设置」的 python 段摘除（M25） | `frontend-vue/src/views/admin/SettingsView.vue` · `0027_drop_python_settings.py` | 8 个全库零读取点字段整段摘除；迁移删 `category='python'`，downgrade 有意留空。 |
| 主 run 纳入会话线闸门（M26） | `app/subagent/line_ledger.py` · `app/subagent/store.py` | 线 id 复用 `session_id`；`SESSION_LINE_GATE` 默认 `shadow`；会话线不做 Redis 镜像；到限硬拦 + 挂提额待办。 |
| KeyRing 每 key 权重（M27） | `0028_key_weights.py` · `app/gateway/key_ring.py` | smooth WRR（所有参与者 `cw += w`、选中者 `cw -= total`）；权重缺省 1、`w <= 0` 不参与；冷却与权重正交。 |
| 按模型的公开价格同步（M28） | `0029_model_prices.py` · `app/pricing/sync.py` | 两源交叉验证后 upsert 到 `model_prices`；`MODEL_PRICE_SYNC` 默认关、默认 30min；单位归一化到 USD/1000 tokens。 |
| 存量摘要向量回填模块（M29） | `app/memory/backfill.py` | 默认 dry-run、幂等（`embedding IS NULL`）、一条失败不中断、空向量不写；文本只认 `SummaryEntry.embed_text`。 |
| 知识库重构后的静态门禁红 | 2026-10-19 顺手清 | `ruff`/`mypy` 清零；异常类统一 `Error` 后缀；测试 import 改指 `app/queue/streams.py`。 |
| L3-2 ESLint `no-explicit-any` | `de7cb61`…`22ebf95`（272→0） | 不为清零加 `eslint-disable`；`catch` 取值分层契约在 `utils/apiError.ts`。 |
| L3-7/L3-8 组件测试 + `SSEProducer` 死代码 | `views/__tests__/WorkflowView.spec.ts` | 两个 VTU 坑记在测试注释里；架构文档 §6 已同步。 |
| L3-9 工具名单漂移 | `scripts/check_tool_policy_parity.py` | 锁定 Go ↔ Python ↔ 前端的对话模式、提供商目录（各 27 条）、故障注入作用面；与 `LEGACY_TOOL_NAMES` 一起守。 |
| DSH 切口 #3（思考 / 用量） | 内部依据 | 思考走独立 `thinking`；`usage` 按次增量 + `done` 累计；阶段已决定不做。 |
| L4-2 CLI 迁移入口 | [决策记录](db-migration-entry.md) | Alembic 唯一入口；Go `db migrate`、Python `migrate run`/`downgrade` 已删，只留只读诊断。 |
| 沙箱/出站/命令守卫（A 层） | 内部依据 切口 #6 | 子模块按逐级前缀判、出站 IP 语义归一（IPv4-mapped/6to4/Teredo）、插件命令只认裸 basename 或绝对路径、远程技能流式体积上限。 |
| 执行审计覆盖面 | `exec_audit` + `plugin_audit` | 每条执行路径都要进审计；`test_exec_audit_inventory.py` 未归类即失败；多副本集中摄取默认关。 |
| `internal/*` 零测试包 | 5 个包 39 条用例 | 覆盖雪花 ID 并发唯一性、`AtomicStore` 并发切换、RBAC 缓存 nil/空切片、`WorkingMemory` 拷贝、环形直方图、成本表淘汰、span 父子传播。 |
| 文档断链守卫 | `scripts/check_doc_links.py` + CI | 基线只降不升（已 19→0）；新增断链即失败；相对链接与围栏代码块口径已纳入。 |
| Markdown 表格结构守卫 | `scripts/check_md_tables.py` + CI | 同一表格块内列数必须一致；存量基线为空。 |
| `scripts/` lint 门禁 | 仓库根 `ruff.toml` + CI | 历史问题 8→0；规则集与引擎一致。 |
| L5-2 架构与请求链路 | `65b0558` | 文中命令均本机实测。 |
| 对外 SDK（第一方示例客户端） | `clients/python/` | 停在示例客户端：不发布、不给兼容性承诺；护栏 `test_chiron_client_sdk*.py`。 |
| L4-1 run 现场 checkpoint 续跑 | `0003_agent_runs.py` · `app/agent/{checkpoint,resume,runtime}.py` · [设计](run-checkpoint-design.md) | 回合末落盘（只写不读）、热 1h/冷 24h、续跑/接管 + 启动 reconciler；护栏含跨进程「硬杀→接管不重放」`test_resume_kill_drill.py`。 |
| R5(b) 子 agent 继续 / 分叉 | `0008_subagent_resume_link.py` · `app/subagent/{access,resume}.py` | `resume_subagent(run_id, instruction, at_step)`；只许终态 run、原 run 不改、写权限只能收紧；`SUBAGENT_RESUME_ENABLED` 默认关。 |
| 任务线账与 run 级预算的第二道闸门（M15） | `0021_subagent_lineage_root.py` · `0022_run_lineage_ledger.py` · `app/subagent/line_ledger.py` | 线账单位是血缘闭包（`root_run_id`），`charged = settled + 在途预留 + own`；越界暂停该线并挂提额待办；run 账读 `agent_runs.checkpoint.usage` 且不新增预留。 |
| office「发布工作区文件」通道 | `app/tools/media.py` 的 `from_workspace` | 不新增工具；路径过 `sandbox.safe_join`、体积上限前置到 `stat`；三种来源互斥。 |
| 钩子协议批次 1 / 2a / 2b / 3 / 4 | `app/hooks/*` · [设计](hook-protocol-design.md) | 声明面默认空 = 零行为变化；`command`/`webhook` 均为独立开关 + 空 allowlist = fail-closed；上下文注入只走结构化字段且默认关。 |
| 发布就绪收尾（M31） | `.github/workflows/*` · `go.mod`/`Dockerfile` · `requirements.txt` | 修掉 CI 首跑暴露的跨平台路径、`JWT_SECRET`、Py3.11 默认值、响应脱敏等 10 类真问题；Trivy 漏洞 24→0。 |

## 2. 待办

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（本机实测 2026-10-08，**2026-10-09 复核一致**）：**142 列** naive（72 表）· **12 列** `timestamptz`（5 表）· VARCHAR 侧名字即时间的有 3 列（`schema_migrations.applied_at` 已废弃，`meeting_notes.date` / `admin_tenant_usage.stat_date` 属另议）。
- **语义已抽样判定**：存量 naive 值存的是**本地墙钟（+08）**而非 UTC —— 同一轮 run 在 `agent_runs.*`（`timestamptz`）是 `2026-09-30T14:26:40Z`，而 `turns` / `messages`（naive）是 `22:26–22:27`。
- ⚠ **风险不是"哪一侧对"，而是"同一列可能混着两种语义"**：`NOW()`/`CURRENT_TIMESTAMP`（95 处）跟**数据库会话时区**，`time.Now()`（110 处，含 6 处 `.UTC()`）跟**宿主时区**；两侧不一致时（典型：容器 TZ=UTC + 数据库 `timezone=Asia/Shanghai`）任何单条 `USING … AT TIME ZONE 'X'` 都会把一半的行移错时刻。**已加启动只读诊断**：`internal/db/tz_diagnostic.go` + `warnOnTimezoneMismatch`（不一致即告警）。
- 剩余步骤：① 生产先跑同一条诊断（或把两侧时区对齐）；② **抽样确认生产存量语义，不要套用本机结论**；③ 用**影子列回填 → 校验 → 切换**三步走，而不是一次性 `ALTER … USING`；④ ORM 渲染 `DateTime(timezone=True)`。
- 前置：生产时区确认；需可达 PostgreSQL。验收：新迁移单 head + `alembic upgrade head --sql` 通过 + ORM 重新生成 + **迁移注释写清 `USING` 的时区依据**。

### S5-(e) 执行沙箱服务 —— **服务应用已落地**，剩部署形态

- 已落地：`sandbox-service/service.py`（`/v1/internal/exec/run` + health、令牌 fail-closed、**直接复用引擎的 `run_in_sandbox`** 基线与审计、身份随请求恢复）；引擎侧客户端 `app/backends/remote_exec.py` 已随请求带身份；接线（`SANDBOX_BACKEND=service` → 分流）抽成 `_apply_sandbox_service_branch()` 并有 4 条分支用例。测试：`tests/test_sandbox_service*.py`（含独立进程端到端）。
- 剩余（**都在代码之外**）：**独立容器**部署（无 docker socket、仅内网、不与引擎共容器）· `SANDBOX_ROOT` **与引擎共享同一工作区卷**（否则命令产物与文件工具分叉）· 白名单租户**真的走服务**的服务侧断言 · 灰度/回滚手册。**真实部署验证不能靠单测替代**。（审计多副本收集已落地：引擎与沙箱服务成批送 `POST /v1/internal/audit/exec`。）

### L3-4 巨型文件拆分 —— 批次 1、3、2(类型) 与 4(校验) 已完成

- 已完成（每批都验证**逐函数/定义逐字一致**）：`gateway_router.go` → 11 个 `routes_*.go`（1254→442）· `mail_handler.go` → 4 个文件（1488→695）· `session/manager.go` → 4 个文件（1198→128）· `runtime.py` 类型/决策 → `runtime_types.py`（3370→3198）· `main.py` 启动校验 → `app/deps.py`（2170→2061）· 压缩簇 → `app/agent/compaction.py`（4743→4369）· `main.py` 的 `lifespan` 分段（590→116，20 个 `_start_*`/`_bind_*`）· 前端 composable：`useWorkflowExecution` / `useWorkflowTemplates` / `useChatShare` / `useWorkflowCanvas` + `useWorkflowPersistence`。
- 剩余：**无功能收益，排最后**（§4 批次 D）。边界与验收见拆分评估。
- ⚠ 续抽 `lifespan` 时顺带修掉 `tenant_id → int16/int` 遗留（`app/knowledge/settings_store.py` 漏 import、测试断言按字符串）与前端 `tenant_id → number` 的 8 处类型遗留；现 `vue-tsc -b` 通过、vitest 全绿、eslint 0 error。

## 3. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| **多副本语义的对外保证** | 见 [多实例部署指南](deployment-multi-instance.md) §9；每个能力项立项前须给出**可复现命令、期望输出与反例** |
| **CI 实跑后仍未验证的项** | 静态门禁已在本机提前排掉（M16）；仍未验证：未声明的顶层依赖（本机 Python 3.14、CI 是 3.11 + 固定版本）、pgvector 镜像、网关在 runner 内的启动、容器内自查（本机无 Docker）。integration 已在真实 PG 跑通（40 passed / 3 skipped） |
| 新功能方向 / 前端 e2e（Playwright） | 新功能方向需产品路线图输入；前端当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：按业务域拆 `routes_*.go`，见拆分评估 |
| 引擎镜像自包含内置技能 | 现由 compose 只读挂载 + `CHIRON_BUILTIN_SKILLS_PATH` 提供（源缺失会告警）；镜像自包含需把构建上下文改为仓库根并 `COPY market/skills` —— 必须**真实 `docker build` + 容器内自查**，不能用单测替代 |

## 4. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-2 待用户决策 |
| D. 可维护性 | L3-4 | 无功能收益，放最后 |

> 批次 A（L2-2/L5-2）与批次 B（L1-4）已完成。

## 5. 通用验收口径

（与 CI 一致；逐 job 说明见 [贡献与验收流程](../CONTRIBUTING.md)，以 `.github/workflows/ci.yml` 为唯一事实来源）

```bash
# 仓库根
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1
python scripts/check_source_encoding.py
python scripts/check_tool_policy_parity.py   # Go ↔ Python 分级表必须同构（L3-9）
python scripts/check_doc_links.py             # 被引用的 docs/*.md 必须存在（新增断链即失败）
ruff check scripts/                           # 仓库根脚本（配置见 ruff.toml）
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# 真实栈用例（需可达 PostgreSQL / Redis；CI 由 real-stack job 跑）
go test -mod=mod ./... -count=1 -run 'Live|Diag'   # CHIRON_TEST_POSTGRES_DSN / CHIRON_TEST_REDIS_ADDR
python python-engine/tests/drills/multi_instance_drill.py <网关二进制>  # 跨实例**双进程**演练（CI real-stack job 也跑）

# 本机真实栈：把**网关也起起来**，`pytest -m integration` 才会全绿（配方见
# [贡献与验收流程](../CONTRIBUTING.md) 的「本机真实栈」）

# python-engine/
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
