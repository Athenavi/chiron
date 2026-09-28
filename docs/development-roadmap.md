# 开发路线图

本文是**待办账本**，只收录**尚未完成**的事项，每条标注「依据」与「验收」；未经验证的推测显式标注「待确认」。已完成项仅留提交号指针，明细见提交信息。已明确不做的项见 [多实例部署指南](deployment-multi-instance.md) 第 10 节；mypy 接线约束见 [贡献与验收流程](contributing.md)。

## 0. 当前状态

主干全绿（最近一次本机核验）：Go build/vet/test 通过；`pytest -m "not integration"` 1259 passed + `pytest -m integration` 4 passed（真实 PG 18 + Redis 7）；`npm run test` 464 passed；`npm run check:ui` 通过（i18n 存量 0、a11y 0）；`npm run lint` 0 errors / 30 warnings（`no-explicit-any` 剩 3，见 L3-2）；`vue-tsc -b` build 通过；`mypy app/` 0；alembic 单 head。

i18n 基线已是空账本（`legacy.ts` 三语种清空），L1-4 已完成；护栏转为 `check-i18n.mjs` 的**「翻译调用禁用中文字面键」**——legacy 清空后再写 `t('中文')` 必然缺键（回显 key、不插值、en-US/ar 永不命中）。

## 1. L1 国际化

### L1-3 译文补齐（ar / en-US）✅

- 提交 `b7fea4f`（en-US legacy 2150/2150）、`84c6991`（前后台语言切换器）、`2dc7d81`（ar legacy 2150/2150）。
- 验收：`check-i18n-keys` 与 `check:ui` 通过，baseline 锁定 `en-US:0 / ar:0`；ar 语义域此前已对齐。

### L1-4 语义化 key 改造 ✅

- 提交 `283c84b`（2136 个 legacy 键全量迁入 13 个域文件，附 `_analyze.mjs`/`_migrate.mjs`/`_migrate_report.json`）、本次提交（收尾）。`legacy.ts` 三语种清空为 0。
- **方案（已验证）**：rename **不能**写成 `legacy.ts` 里的带点字符串键——vue-i18n 把 `t('auth.login')` 的 `.` 当路径分隔符；正确做法是把中文键**迁入对应域文件作嵌套键**并从 `legacy.ts` 删除。auth 域 14 键为试点（`78c5d70`）。
- **收尾修掉四类迁移缺口**（脚本的正则/换行假设造成，验收时逐类核出）：
  1. `index.ts` 的新域 import/export 因 CRLF 漏加（脚本按 `\n` 匹配 `\r\n` 行）→ `agent.*` 等 8 域键全部命不中，`t()` 只回显键名；
  2. 带参调用未改写（正则要求 `t('key')` 紧跟右括号，`t('读取 {n}', { n })` 全漏）+ `tr` 别名整类漏掉（`const { t: tr } = useI18n()` 不匹配 `\bt`）→ 脚本补改写 417 处；另 5 处人工改：3 处键内含 `"`（扫描器字符类也漏了它们）、2 处 `退出登录`（auth 试点键已从 legacy 删除，挂 `auth.logout`）；
  3. 迁移键与既有 `common` 键撞名 14×3：同值删 11、异值改名 3（`editModify`/`clear_2`/`copiedExcl`，调用点按「迁移前整行内容」判定归属后同步改写）；
  4. 2 个 legacy 里根本没有的中文键：`已选择 {sel} / {total}` 改挂既有 `common.selected_sel_total_items`，市场空态提示新增 `common.market_empty_hint`（三语种补齐）。
- 护栏：`check-i18n.mjs` 新增**「翻译调用禁用中文字面键」**（三种引号 + `tr`/`translate`/`i18n.t` 别名，注释与 `.test.ts` 除外）。实测该缺口曾一次漏 400+ 处，靠人眼守不住；守护脚本自身用注入探针验证过能红。
- 测试口径同步：`SessionStatsPanel.spec` 的「`$t` 回显 key」桩已改走 test-setup 注册的真实 i18n（语义化 key 后回显的是 `chat.stats.turns` 这类键名，桩断言中文的前提不再成立）。
- 验收：`check-i18n-keys`/`check:ui` 0 缺键、中文字面键扫描 0 处、lint 0 errors / 299 warnings、`vue-tsc -b` 通过、vitest **464 passed**。

## 2. L2 质量门禁接线

### L2-1 `mypy --strict` 接入 CI ✅

- 提交 `21a96ae` … `bf4860f`（25 批，1171→0 条 / 130→200 文件）；接线手册见 [贡献与验收流程](contributing.md)。
- ⚠ 待确认（见 §6）：本分支从未 push、CI 未实际运行。

### L2-2 真实栈集成测试门禁 ✅（CI 首跑待确认）

- 本次提交：新增 CI job `integration` —— `services:` 起 pgvector PostgreSQL + Redis（compose 无 PG，须自建）→ `alembic upgrade head` 建 schema → `go test … -run 'Live|Diag'` → `pytest -m integration`。Python 的 `UnifiedDBClient`/`UnifiedRedisClient` 走的是网关 `/v1/internal/{db,redis}/*`，故 job 里一并 build 网关并后台启动（轮询 `/health` 就绪）；`APP_SECRET` 必须 ≥32 字符，否则网关 fail fast 拒绝启动。
- 本机以真实 PostgreSQL 18 + Redis 7 全量验证（DSN 指向开发库；本机 `chiron_app` 无建库权限，**空库路径只能靠 CI 覆盖**）：Go live 6 条全过，Python integration 从"全红"到 4 条全过。
- 首次接入暴露并修复的真实缺陷（正是本项的价值所在）：
  1. `internal/api/system_handler.go` 的 `RedisGet` 把 go-redis 的 `redis.Nil`（键不存在）当 500 —— Python 侧 `UnifiedRedisClient.get` 的"不存在返回 None"契约永远拿不到结果，调用方还会把"没缓存"误判成"Redis 故障"。现按 Redis 语义返回 200 + `value: null`。
  2. `python-engine/app/api/workflows.py` 的实例 INSERT 传 aware datetime 撞 `timestamp without time zone` 列（asyncpg：can't subtract offset-naive and offset-aware datetimes），异常被 `except` 静默吞掉 → 实例永远写不进库、`/status` 恒 404（"看起来成功、实际丢失"）。改按 UTC naive 落库。
  3. Python integration 用例此前**不可能在 pytest 里通过**：`app.db.get_pool()` 要求显式 `init_pool`（平时只有 app lifespan 调），`app.redis_client.get_redis()` 的模块级单例又绑定在创建它的事件循环上。新增 `tests/conftest.py` 的 integration fixture（建池 + 用例结束复位 Redis 单例），未配 `POSTGRES_DSN` 时显式 skip 并写清理由。
  4. `test_workflow_status_returns_instance` 断言的仍是旧的**同步执行**契约（completed/error），而 execute 早已是异步提交（立即 `running` + `instance_id`）；改为按现契约断言，并在用例结束清理落库实例。
  5. 跨端契约测试 `test_workstation_contract` 自 L1-4 起一直红：TS 的 `WORKSTATION_LABELS` 已改为 i18n 键，而 `shared/workstations.json` 还是中文原文。统一为「label/description 存文案键」，文案本体由 `frontend-vue/src/locales/zh-CN` 承担（键存在性由 `check-i18n-keys.mjs` 守）。
- 待确认/待决：
  - ⚠ 本分支从未 push，CI 未实际运行（与 L2-1 同一前提）；空库（alembic 刚建表）这条路本地无法复现，首跑需留意。
  - workflow 实例 INSERT 失败仍只 `logger.warning`：是否升级为可观测的失败（指标 / 接口报错）需单独定。

## 3. L3 技术债

### L3-2 收敛 ESLint warning（`no-explicit-any` 272 → 3，余下 3 条为待决项）

- 依据：`npm run lint` 0 errors / 30 warnings；`components/` 已清零（提交 `de7cb61`…`4fb2e61`），契约层 47 条已类型化；`views/` 272 条本批清零（提交 `f0c6327` … 本次提交）。总 warning 数曾随 L1-4 迁移 300→318（18 条 `vue/html-indent`），已 `eslint --fix` 回落。
- 本批新增的收敛手段集中在 `utils/apiError.ts`（都**只取值、不改文案**，把散落的 `catch (e: any)` 取值动作收成一处）：
  - `errorDetail(error, fallback)`：后端 `error` 原文 → JS `message` → 兜底（等价于 `e?.response?.data?.error || e?.message || fallback`）；
  - `errorMessageText(error, fallback)`：只看 JS `message`（等价于 `err?.message || fallback`）；
  - `serverErrorDetail(error, fallback)`：`detail`（Python 引擎）→ `error`（网关）→ `message` → 兜底（同一响应不会给出互相冲突的 detail/error，故统一顺序等价于各处手写的链）；
  - 既有 `serverErrorMessage`/`errorStatus`/`apiErrorMessage` 继续沿用；`describeApiError` 保持不变（它会本地化文案，不可用于"要原文"的分支）。
- 视图侧类型化的四类做法（已验证）：① `ref<any[]>` → 契约接口（`DomainEntry`/`TenantEntry`/`BackupEntry`/`ApiKey`/`WaitingTask`/`RedisStatus`/`KnowledgeDocument` 等）；② a-table `#bodyCell` 的 `record` 是 `Record<string, any>`，要具体类型时在模板侧断言（`restoreBackup(record as BackupEntry)`）；③ antd 组件参数从 props 推导（`Parameters<NonNullable<UploadProps['customRequest']>>[0]`），不手抄内部类型名；④ 模板里**不要写 `as A | B`**——`|` 会被 `vue/no-deprecated-filter` 判成 Vue 2 filter，改用 script 侧窄化小助手（如 `SkillsView.paramText`）。
- 待决（需单独定，勿顺手改）：
  - `ChatView` 的 3 处 `updateConversation(..., { llm_config } as any)`：`PUT /v1/conversations/{id}` 只接受 `title/pinned/tag/alias`（`internal/api/conversation.go` 的 `Update`；`DecodeJSON` 不拒绝未知字段），其中 `persistRuntime` 那处**只带 `llm_config`** → 必被 400 拒绝并被 `.catch(() => {})` 静默吞掉，rename/pin 两处的 `llm_config` 被后端忽略。删除这层无效负载（连同随之失去调用者的 `buildPersistLlmConfig`）是行为变更。
  - 全仓 136 处 `catch (e: any)` 是否统一改用 `describeApiError`（会本地化文案，属可见行为变更）。
- 硬约束：不为清零加 `eslint-disable`；每批单独跑 `vue-tsc -b` 与组件测试。
- 坑：① 纯文本删「未使用导入」会误删标识符（默认导入名/catch 参数名与具名导入文本层不可分），结构性删除须 AST 或人工逐行确认；② `no-undef` 走 browser globals，与 tsc `lib.dom` 覆盖不一致（如 `SpeechRecognitionEvent`），第三方/较新 DOM API 一律自声明形状；③ `vue/no-template-shadow`：`<router-view v-slot="{ Component }">` 的 slot prop 名与 vue 导入 `Component` 冲突，需别名（如 `VueComponent`）；④ 宽泛 `Record<string, any>` 会吞掉字段访问错误（`SkillMarketCard` 改 `MarketManifest` 后 5 处显形）；⑤ 把 `any` 换成具体类型后，模板里"以前靠 any 蒙过"的调用会成片显形——`new Date(x)`、`x.length`、`formatSize(x)`、`kb.type.toUpperCase()` 都需在调用侧收口（`?? 0` / `as string | number` / `|| ''`）；⑥ interface 赋给 `Record<string, unknown>` 形参会报缺索引签名，改用 `type`（`PaymentView` 的 `PaymentConfigForm`）；⑦ `node -e` 里别写 `||`/`&&`：本机 shell 是 PowerShell 5.1，会先解析再执行（here-string 也救不了），脚本改用正则 `\|{2}` 或临时 `.js` 文件。

### L3-4 巨型文件拆分（评估项）

- 依据：`internal/api/gateway_router.go` 62 KB；`python-engine/app/agent/runtime.py` 101 KB、`app/main.py` 85 KB、`app/memory/service.py` 54 KB、`app/queue/worker.py` 51 KB。
- 验收（若启动）：先出拆分边界与契约清单，再按「纯移动 + 零行为变更」分步提交，每步测试绿。

### L3-7 `WorkflowView` 缺组件测试

- 依据：`WorkflowView`「套用模板」UI 已补齐，但无组件测试（视图 1700 行，密集依赖 VueFlow 画布）。
- 验收：先做 `@vue-flow/*` 测试替身，再补最小 spec（模板按钮存在、点击弹窗打开、列表按 `templateNodeCount`/`templateEdgeCount` 渲染、点「使用」时 `templateUsingId` 进入 loading）。可与 L3-4 同做。

## 4. L4 结构性后续（来源：多实例部署指南 §10）

### L4-1 run 现场 checkpoint 续跑

- 现状：实例故障该 run 中断；SSE 断连靠客户端 `Last-Event-ID` 从 Redis Stream 重放。
- 依赖：需跨 Go 网关与 Python 引擎的统一 run 状态模型设计（先设计后编码）。
- 验收：产出设计文档（状态机、checkpoint 落点、幂等边界、与 `TASK_IDEMPOTENCY_RETENTION_DAYS` 关系），评审通过再拆实现。

### L4-2 `chiron-cli db` 迁移入口交互设计

- 现状：`chiron-cli db migrate` 实际 shell 出 `alembic upgrade head`，要求目标机有 python+alembic；`db status` 已读 `alembic_version`。
- 待决：CLI 是否完全不接触迁移（alembic 唯一入口）或保留「受控便捷封装」。
- 验收：产出决策记录，并统一 `chiron-cli db` 帮助文本/退出码/错误提示。

### L4-3 时间列 `timestamp` → `timestamptz`

- 依据：实测 145 个时间列中 136 个 `timestamp without time zone`、9 个 `with time zone`，唯一 VARCHAR 是已废弃 `schema_migrations.applied_at`，不涉及 `USING` 转换与存量风险。
- 剩余：提升为 `timestamptz`（含 `scripts/generate_orm_models.py` 生成模型与数十张表）。
- 前置：需可达 PostgreSQL（迁移是发布流程步骤）。
- 验收：新迁移追加到 `migrations/versions/`（单 head，CI schema job 校验）；ORM 重新生成；`alembic upgrade head --sql` 离线渲染通过。

## 5. L5 文档

### L5-2 架构与请求链路总览

- 缺口：缺**一次对话请求的完整链路**（前端 → nginx 反代 `/v1`、`/events`、`/ws` → 网关认证/限流/计费 → 引擎 → Redis Stream 事件回传）；`internal/api` 85 文件 / 26 133 行，python-engine 200 源文件。
- 验收：补一篇「架构与请求链路」，每步由代码/命令支撑，放进 `docs/` 并从 `README.md` 链接。

## 6. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| CI 首跑（L2-1 的 mypy / L2-2 的 integration） | push 并跑一次 CI：未声明顶层依赖（beautifulsoup4/aiohttp 类）、空库建表路径、pgvector 镜像、网关在 runner 内的启动，都只能在 CI 里验证 |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/enterprise`/`monitor`/`storage`/`id`/`model` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | `gateway_router.go` 单文件承载，是否拆分待 L3-4 评估 |

## 7. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| A. 门禁与文档（性价比最高） | L2-2 ✅、L5-2 | L2-2 已完成，并当场暴露/修掉 2 个真实缺陷（见 §2）；L5-2 提升后续改动可信度 |
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-3 是唯一库结构变更 |
| D. 可维护性 | L3-2、L3-4、L3-7 | 无功能收益，放最后 |

> 批次 B（i18n 收尾 L1-4）已完成，见 §1。

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
