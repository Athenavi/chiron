# 开发路线图

**待办账本**：只收录**未完成**事项（每条标「依据/验收」），已完成项只留指针，未经验证的推测标「待确认」。已明确不做的见 [多实例部署指南](deployment-multi-instance.md) §10。
**北极星：超越 deepseek-harness** —— 可比性、逐层对照与 6 个切口见 [与 DSH 的差距分析](dsh-gap-analysis.md)；判据以**对外可核实的行为**为准。

## 0. 当前状态

**最近一次全量核验（2026-10-09 · round 68，全部本机实测）**：Go `build`/`vet` 通过 · `go test ./...`（**真实 Redis 8 + 真实 PG** 在场）全绿 · **Go live 子集**（`CHIRON_TEST_POSTGRES_DSN` + `CHIRON_TEST_REDIS_ADDR`）`internal/{api,billing,settings,broadcast,engine}` 全绿 · `ruff check .` **0** · `ruff check scripts/` 0 · `mypy app/ acp_adapter/` 0（**244** 文件）· `pytest -m "not integration"` **1936 passed** · `pytest -m integration` **13 passed（全绿）**（需 Redis + 网关；含 L4-1 kill→重启演练、**双进程跨实例演练**、**SDK 端到端**）· 前端 `npx vitest run` **480 passed**（56 文件）+ `vue-tsc -b` 通过 · 守卫：`check_source_encoding` / `check_tool_policy_parity` / `check_doc_links`（存量基线 8 条）通过 · alembic 单 head `0007_subagent_error_code`。

**本机做不到的**：**容器相关**（无 `docker compose` 插件、Docker daemon 未运行）⇒ 镜像/容器内自查、以及**沙箱服务的容器化部署**须在能跑 Docker 的环境验证。**CI 尚未实跑**（分支未 push，见 §3）。

## 1. 已完成（只留指针与仍生效的护栏）

| 项 | 指针 | 仍生效的护栏 / 注意 |
|---|---|---|
| L1 国际化 | `b7fea4f`…`283c84b` | `check-i18n-keys` 锁 `en-US/ar` 缺键 = 0；`check-i18n.mjs` 禁止**翻译调用**里写中文字面键（`tr`/`translate`/`i18n.t` 别名；注释与 `.test.ts` 除外） |
| L2-1/L2-2 mypy strict + 真实栈集成门禁 | `21a96ae`…`bf4860f`（1171→0 / 200 文件）· `578bc90` | 接线手册见 [贡献与验收流程](contributing.md)；⚠ 真实栈 job **未在 CI 实跑过**（见 §3） |
| L3-2 ESLint `no-explicit-any` | `de7cb61`…`22ebf95`（272→0） | 不为清零加 `eslint-disable`；`catch` 取值**分层**契约写在 `utils/apiError.ts` 文件头；余 27 条风格警告不再动 |
| L3-7/L3-8 组件测试 + `SSEProducer` 死代码 | `views/__tests__/WorkflowView.spec.ts`；死代码已删 | 两个 VTU 坑记在测试注释里；[架构与请求链路](architecture.md) §6 已同步 |
| L3-9 工具名单漂移 | 两侧策略表已修 | **不变量**：`scripts/check_tool_policy_parity.py`（已接入 CI 的 `Source encoding` job）+ `tests/test_tool_policy_coverage.py` 的 `LEGACY_TOOL_NAMES` |
| DSH 切口 #3（思考 / 用量） | [差距分析](dsh-gap-analysis.md) 切口 #3 | 思考走独立 `thinking`；`usage` 按**次**增量 + `done` 累计（网关按**事件类型**分流以防重复计费，前端 `mergeTurnStats` 合并成本轮一行）；**阶段已决定不做**（主对话无真实状态；子 agent 侧本就有 `subagent.status`） |
| L4-2 CLI 迁移入口 | [决策记录](db-migration-entry.md) | **Alembic 唯一入口**：Go `db migrate`、Python `migrate run`/`downgrade` 均已删，只留**只读**诊断（`db status` / `migrate history`，失败非 0 退出）；`scripts/init.py` 第 6 步保留（跑的是同一份 `alembic.ini`，属部署向导） |
| 沙箱/出站/命令守卫（A 层） | [差距分析](dsh-gap-analysis.md) 切口 #6 | 三处实证逃逸已修：① 白名单模块的**子模块**按**逐级前缀**判（`asyncio.subprocess` 曾可起进程）；② 出站 IP 按**语义归一**（`::ffff:169.254.169.254` 等 IPv4-mapped/6to4/Teredo 曾绕过 SSRF 名单）；③ 插件命令从"只比 basename"改为**裸 basename 或绝对路径**（`../`、UNC 曾可执行攻击者放置/远程的同名二进制，Go 与 Python 两侧同步）。护栏：`test_code_guard_submodule_escape.py` + `test_ssrf.py` + `plugin_command_allowlist_test.go`；④ 远程技能取回（`skill.py::_fetch_skill_json`，install 与 discover 的唯一入口）的体积上限改为**流式**：越界**立即中止并关闭响应**，而不是读完再判（后者只拦解析、拦不住下行流量与内存）——`tests/test_skill_remote_fetch.py` 用"块计数"证明提前停止，并做过变异验证 |
| 执行审计覆盖面（`exec_audit` + `plugin_audit`） | [差距分析](dsh-gap-analysis.md) 切口 #4 | **每条执行路径都要进审计**：补了后台命令 `tool_job`（每个终态，含取消）与 git 工具；plugin audit 抽成可复用的 `app/plugins/audit.py`（`ts` 对齐 UTC+毫秒），运行时拉起插件进程的两处（`mcp/client.py`、`skill/manager.py`）已接入。**覆盖清单机械化**：`tests/test_exec_audit_inventory.py`（起进程位置未归类即失败）；护栏：`tests/test_job_exec_audit.py`、`tests/test_plugin_audit.py`；**多副本集中摄取（N4）**：`POST /v1/internal/audit/exec`（内部令牌、逐条校验部分接受、单批上限 500）+ 引擎侧**有界队列**发货（`EXEC_AUDIT_SHIP_URL`，**默认关**、不阻断执行、满了丢最旧且计数），引擎与 S5e 沙箱服务共用同一 `exec_audit` 路径 ⇒ 多副本审计回到一处可查 |
| `internal/*` 零测试包 | **5 个包 39 条**用例（`id` 8 · `storage` 4 · `enterprise` 6 · `model` 4 · `monitor` 17）—— 此前这 5 个包**零测试文件** | 雪花 ID **并发唯一性** + worker 位布局 + 时钟回退不重号 · `AtomicStore` **并发切换不撕裂** · RBAC 缓存的 **nil ↔ 空切片防越权语义** · `WorkingMemory` 返回拷贝（外部改不动历史）· **环形直方图只保留最后 N 个样本** · **会话成本表容量淘汰 + TTL 过期剔除** · span **父子传播**（同 trace、ParentID 正确）与 `End()` 默认 OK + 真导出 |
| 文档断链守卫 | `scripts/check_doc_links.py` + **CI「Source encoding」job** | 被引用的 `docs/*.md` 必须存在；存量走基线（`scripts/doc_link_baseline.txt`，**只应缩小**：**已 19 → 8**），**新增断链即失败**（变异验证过）。口径已排除测试/评测 fixture、合成占位路径、**URL 里出现的 docs/ 路径**（如厂商文档链接），并接受 vendor 下对标项目的同名文档。`--show-known` 可按文档列出引用方，便于逐条还债 |
| `scripts/` lint 门禁 | 仓库根 `ruff.toml` + CI「Source encoding」job | 历史问题 **8 → 0**（未用变量/未用导入/裸 `except`/该有的 `noqa` 说明），规则集与引擎一致；护栏：CI 跑 `ruff check scripts/` |
| L5-2 架构与请求链路 | `65b0558` | 文中命令均本机实测 |
| 对外 SDK（第一方示例客户端） | `clients/python/chiron_client.py` + 同目录 `README.md` | **决定（2026-10-09）：停在示例客户端** —— **不发布、不给兼容性承诺**（README 已写明）。零第三方依赖：登录/注册 · 提交 · SSE 订阅含 `Last-Event-ID` 重连 · 取消。护栏：`tests/test_chiron_client_sdk.py`（8 条契约单测，假 `HTTPConnection` 跑真代码）+ `tests/test_chiron_client_sdk_live.py`（**真实网关端到端**，已实测通过）。⚠ 若将来要发布或承诺兼容，**需重开产品决定**（并补 OpenAPI/JSON-RPC 面） |
| L4-1 run 现场 checkpoint 续跑 | `migrations/versions/0003_agent_runs.py` · `app/agent/{runtime.py,checkpoint.py,resume.py}` · 设计见 [run 现场 checkpoint 续跑设计](run-checkpoint-design.md) | 表 `agent_runs`（含**防脑裂唯一索引**）· 回合末落盘（只写不读，失败只降级恢复粒度）· schema v1 + 热 1h/冷 24h · 续跑/接管 + 启动 reconciler + 指标。护栏：`tests/test_checkpoint.py` · `test_checkpoint_schema_version.py` · `test_run_resume.py`（38 条）**+ 跨进程"硬杀→接管不重放"** `tests/test_resume_kill_drill.py`（integration：真 `AgentRuntime`、真硬杀、断言历史里第 1 回合工具结果恰好一份）。设计里"新增表 `agent_runs`"是**原文过期**（该表在 `0003` 就存在）；原"待拍板 5 问"已在实现中定案。批 5（长逻辑 checkpoint / 跨实例现场迁移）**明确不做** |

## 2. 待办

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据（本机实测 2026-10-08）：**142 列** naive（72 表）· **12 列** `timestamptz`（5 表）· 唯一 VARCHAR 时间列是已废弃的 `schema_migrations.applied_at`。
- **语义已抽样判定**：存量 naive 值存的是**本地墙钟（+08）**而非 UTC —— 同一轮 run 在 `agent_runs.*`（`timestamptz`）是 `2026-09-30T14:26:40Z`，而 `turns` / `messages`（naive）是 `22:26–22:27`。
- ⚠ **风险不是"哪一侧对"，而是"同一列可能混着两种语义"**：`NOW()`/`CURRENT_TIMESTAMP`（**95** 处）跟**数据库会话时区**，`time.Now()`（**110** 处，含 6 处 `.UTC()`）跟**宿主时区**；两侧不一致时（典型：容器 TZ=UTC + 数据库 `timezone=Asia/Shanghai`）任何单条 `USING … AT TIME ZONE 'X'` 都会把一半的行移错时刻。**已加启动只读诊断**：`internal/db/tz_diagnostic.go` + `warnOnTimezoneMismatch`（不一致即告警；本机实测两侧同为 `+08` ⇒ aligned）。
- 剩余步骤：① 生产先跑同一条诊断（或把两侧时区对齐）；② **抽样确认生产存量语义，不要套用本机结论**；③ 用**影子列回填 → 校验 → 切换**三步走，而不是一次性 `ALTER … USING`；④ ORM 渲染 `DateTime(timezone=True)`（模板现无条件渲染 `DateTime`）。
- 前置：生产时区确认；需可达 PostgreSQL。验收：新迁移单 head + `alembic upgrade head --sql` 通过 + ORM 重新生成 + **迁移注释写清 `USING` 的时区依据**。

### S5-(e) 执行沙箱服务 —— **服务应用已落地**，剩部署形态

- 已落地：`sandbox-service/service.py`（`/v1/internal/exec/run` + health、令牌 fail-closed、**直接复用引擎的 `run_in_sandbox`** 基线与审计、身份随请求恢复）；引擎侧客户端 `app/backends/remote_exec.py` 已随请求带身份；**接线**（`SANDBOX_BACKEND=service` → 分流）抽成 `_apply_sandbox_service_branch()` 并有 4 条分支用例。测试：`tests/test_sandbox_service.py`、`tests/test_sandbox_service_wiring.py`、**`tests/test_sandbox_service_multiprocess.py`（独立进程端到端）**。
- 剩余（**都在代码之外**）：**独立容器**部署（无 docker socket、仅内网、不与引擎共容器）· `SANDBOX_ROOT` **与引擎共享同一工作区卷**（否则命令产物与文件工具分叉）· 白名单租户**真的走服务**的服务侧断言 · 灰度/回滚手册。**真实部署验证不能靠单测替代**。（**审计多副本收集已落地**：引擎与沙箱服务成批送 `POST /v1/internal/audit/exec`。）

### L3-4 巨型文件拆分 —— 批次 1、3、2(类型) 与 4(校验) 已完成

- 已完成（每批都验证**逐函数/定义逐字一致**）：`gateway_router.go` → 11 个 `routes_*.go`（**1254→442**）· `mail_handler.go` → 4 个文件（**1488→695**）· `session/manager.go` → `manager_{crud,messages,queries,branch}.go`（**1198→128**）· `runtime.py` 类型与决策取值 → `runtime_types.py`（**3370→3198**）· `main.py` 的三个启动前置校验 → `app/deps.py`（**2170→2061**）。
- 剩余：`runtime.py` 压缩簇（**必须先按现状重新定界**）· **`main.py` 的 `lifespan` 分段**（关闭段 + **6 个无耦合启动段** ✅ 已抽，`lifespan` 740 → 561 行；剩 5 段有真实耦合，需显式传参/返回句柄）· **前端 composable**（**批次 1 ✅** 执行/轮询簇 → `useWorkflowExecution.ts`，1746 → 1637 行；**批次 2 ✅** 模板市场簇 → `useWorkflowTemplates.ts`，1636 → 1597 行；持久化簇因**注入面 14 个**暂缓，须先抽画布状态；另 **`ChatView`（3429 行）已补冒烟网** 3 条，可开始拆簇）。边界与验收见 [拆分评估](split-assessment.md)；无功能收益，排最后（§4 批次 D）。

## 3. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| **超越 deepseek-harness 的切口** | 已产出 [与 DSH 的差距分析](dsh-gap-analysis.md)（逐层对照 + 6 个切口 + 未验证清单）与 [多副本语义的对外保证](deployment-multi-instance.md)；每个切口立项前须给出**可复现命令、期望输出与反例** |
| **文档断链：8 份被引用的 `docs/*.md` 不存在（已基线化 + 门禁；**当前暂停还债**，优先功能）** | 守卫 `scripts/check_doc_links.py`（**已接 CI**）实测：**原 19 份**（`docs/subagent-design.md` 被 16 个文件引用 · `mail.md` 4 个 …），git 历史里**从未存在过**（不是被删）。**已还 10 条 + 剔除 1 条误报**：① 会话地图的引用**改指**到恢复出来的 [会话地图与分支设计](session-map-branch-design.md)；② 两处（只读副本一致性 / Redis 键前缀）**删引用**（规则本就在代码注释里）；③ 两处"我们引用过某份没入库的文档"的自述**去掉路径**；④ **按代码重建五份**：[会话运行时 spec](session-runtime-spec.md)（7 处引用）· [邮件通道](mail.md)（`mail.go` 里 8 处**带小节号**的引用，重建时**沿用原小节号**）· [模型服务提供商](service-providers.md)（含 OpenCode 的 `x-opencode-session` 一节）· [Agent 安全与可靠性](agent-safety-and-reliability.md)（**§1.4 MCP 收窄** 与 **§4.2 撤销栈诚实优先**两个锚点）· [LLM 密钥管理 DR](llm-provider-key-management-dr.md)（集中派：权威层/keyset/状态机/KeyRing 与 V1→V2 预留）；⑤ **剔除 1 条守卫误报**：有一条"缺失文档"其实是 **Ollama 文档 URL 的一部分**（守卫已加"URL 里的路径不算引用"，故这里不写出那个路径以免又造一条断链）。**剩 8 条仍待决策**：逐条改引用还是补写文档（`--show-known` 给出每条的引用方） |
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖、空库建表路径、pgvector 镜像、网关在 runner 内的启动 |
| 新功能方向 / 前端 e2e（Playwright） | 新功能方向需产品路线图输入；前端当前无 e2e 配置，需确认是否引入浏览器依赖 |
| 多实例**重复投递**（双进程演练发现并已修复，2026-10-08） | `internal/broadcast/hub.go`（新增 `RelayEvent` + `logicalEventID`，`ReplayAfter` 去重）· `internal/api/subagent_events_relay.go` · 引擎侧发布带 `event_id` | 修复前：中继 `hub.Publish` 既追加共享重放流**又跨实例再广播** ⇒ 实时各收 **2 份**、补发 `[2,2,3,3]`（N 实例 = N 份）。修复：中继改走 `RelayEvent`（只追加本实例流 + 本地 fanout，**不再广播**），**写入端不做互斥**（否则 N-1 个实例的客户端拿不到 `id`），改为**读取端按逻辑身份去重**（`event_id`，退化 payload 哈希）。护栏：`hub_live_test.go::TestLiveReplayDedupesSameLogicalEventAcrossInstances` · `logical_id_test.go` · 双进程演练 `multi_instance_drill.py`（**已接 CI real-stack job**，4 条断言全绿） |
| `internal/api` 路由聚合方式 | 已评估（L3-4）：按业务域拆 `routes_*.go`，见 [拆分评估](split-assessment.md) |
| **遗留的第二套 agent 循环 `app/agent/loop.py`** | 实测：它有自己的 `run_agent`（316 行，也自发 `usage`），经引擎路由 `/v1/agent/run` 暴露；而**产品链路是 `/v1/agent/submit` → `AgentRuntime`（`runtime.py`）**，且 Go 侧 `PythonClient.Run`（唯一会打 `/v1/agent/run` 的调用方）**零调用者** ⇒ 该文件＋该端点是**遗留**。**已做**：① `loop.py` 标为遗留并写明"新能力一律加在 `runtime.py`"；② `/v1/agent/run` 加**一次性告警**，用来观测是否真有外部调用方（有证据再决定删，不靠猜）；③ 把该模块**独有覆盖**的安全性质用例搬到产品链路（`tests/test_runtime_tool_truncation.py`）—— 搬的过程中当场查出 **runtime 的截断 `tool_call` 在守卫之前就已下发**（前端多一张卡、网关把半截 JSON 落库），已修。**待做**：确认无调用方后，连同 `/v1/agent/run`、`PythonClient.Run`（Go 侧零调用者）与 `tests/test_agent.py` 一起删除。
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
python scripts/check_doc_links.py             # 被引用的 docs/*.md 必须存在（新增断链即失败）
ruff check scripts/                           # 仓库根脚本（配置见 ruff.toml）
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# 真实栈用例（需可达 PostgreSQL / Redis；CI 由 real-stack job 跑）
go test -mod=mod ./... -count=1 -run 'Live|Diag'   # CHIRON_TEST_POSTGRES_DSN / CHIRON_TEST_REDIS_ADDR
python python-engine/tests/drills/multi_instance_drill.py <网关二进制>  # 跨实例**双进程**演练（CI real-stack job 也跑）

# 本机真实栈：把**网关也起起来**，`pytest -m integration` 才会全绿（配方见
# [贡献与验收流程](contributing.md) 的「本机真实栈」）

# python-engine/
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 为参考源码（无 `modules.txt`），Go 见 `vendor/` 即报 `inconsistent vendoring`。
