# 开发路线图

本文是**待办账本**，只收录**尚未完成**的事项，每条标注「依据」与「验收」；未经验证的推测显式标注「待确认」。已完成项仅留提交号指针，明细见提交信息。已明确不做的项见 [多实例部署指南](deployment-multi-instance.md) 第 10 节；mypy 接线约束见 [贡献与验收流程](contributing.md)。

## 0. 当前状态

主干全绿（最近一次本机核验）：Go build/vet/test 通过；`pytest -m "not integration"` 1259 passed；`npm run test` 460 passed；`npm run check:ui` 通过（i18n 存量 0、a11y 0）；`npm run lint` 0 errors / 300 warnings（`no-explicit-any` 剩 272，全在 `views/`，见 L3-2）；`vue-tsc -b` build 通过；`mypy app/` 0；alembic 单 head。

i18n 基线已是空账本，护栏转为「阻止新增硬编码中文」。

## 1. L1 国际化

### L1-3 译文补齐（ar / en-US）✅

- 提交 `b7fea4f`（en-US legacy 2150/2150）、`84c6991`（前后台语言切换器）、`2dc7d81`（ar legacy 2150/2150）。
- 验收：`check-i18n-keys` 与 `check:ui` 通过，baseline 锁定 `en-US:0 / ar:0`；ar 语义域此前已对齐。

### L1-4 语义化 key 改造

- 依据：`src/i18n/README.md` 末段——生成的 key 是**原文**（gettext 风格），语义化 key 改造是独立任务（只改 key，不改文案）；key 规范 `<域>.<语义>`。
- 依赖：L1-3 已完成，可启动。
- 验收：`legacy.ts` 原文 key 收敛为语义化 key；只改 key 不改文案；测试与 `check:ui` 保持绿。
- **状态（auth 域试点已完成）**：14 个键（`login`/`register`/`logout`/`username`/`password`/`confirmPassword`/`email`(合并既有)/`phone`/`verificationCode`(+sent/+sendFailed)/`loginFailed`/`registerFailed`/`resetPassword`）已从 `legacy.ts` 迁入 `auth.ts` 作嵌套键，调用点同步改写为 `t('auth.*')`；`check-i18n-keys`/`check:ui`/lint/`vue-tsc`/vitest(464) 全绿。
- **方案（已验证）**：rename **不能**写成 `legacy.ts` 里的带点字符串键——vue-i18n 把 `t('auth.login')` 的 `.` 当路径分隔符，扁平点分键永远命不中。正确做法是把中文键**迁移进对应域文件（`auth.ts`/`common.ts`/`chat.ts`/`errors.ts`/`admin.ts`）作嵌套键**并从 `legacy.ts` 删除。剩余 ~2136 个键按同法按域推进即可。

## 2. L2 质量门禁接线

### L2-1 `mypy --strict` 接入 CI ✅

- 提交 `21a96ae` … `bf4860f`（25 批，1171→0 条 / 130→200 文件）；接线手册见 [贡献与验收流程](contributing.md)。
- ⚠ 待确认（见 §6）：本分支从未 push、CI 未实际运行。

### L2-2 真实栈集成测试门禁（两侧当前都被永久跳过）

- 依据：Go 侧 5 文件 6 处 `t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")`，覆盖支付入账/回退/支付宝验签/设置持久化等高风险路径（`internal/api/diag_alipay_key_test.go`、`payment_live_test.go`、`internal/billing/pgstore_live_test.go`、`revert_payment_test.go`、`internal/settings/settings_live_test.go`）；Python 侧 CI 仅 `pytest -m "not integration"`，`mark.integration` 仅 4 处，真实栈极薄。
- 实施：新增 CI job，`services:` 起 pgvector PostgreSQL（compose 无 PG，须自建）→ `pip install -r requirements-migrate.txt` + `alembic upgrade head` → 导出 `CHIRON_TEST_POSTGRES_DSN` 跑 `go test ./internal/api/... ./internal/billing/... ./internal/settings/...` + `pytest -m integration`。
- 验收：上述 Go live 测试在 CI 实际执行（非 skip）；Python integration 有明确最小集合并通过。
- 风险：首次接入可能暴露真实缺陷，勿与其它批次混做。

## 3. L3 技术债

### L3-2 收敛 ESLint warning（剩 272 条 `no-explicit-any`，全在 `views/`）

- 依据：`npm run lint` 0 errors / 300 warnings；`components/` 已清零（提交 `de7cb61`…`4fb2e61`），契约层 47 条已类型化；剩 `views` 272 条（ChatView 38、DatabaseManagementView 19、MediaView 16、WorkflowView 15、KnowledgeDetailView 14 为前五大）。
- 已知做法（已验证可沿用）：明确结构→定义契约接口；本质动态→`unknown` 并窄化调用侧；DOM 非标准成员→交叉类型 + 存在性判断；多形状解析→小助手（`asArray`/`asObject`）；axios 错误体→`utils/apiError.ts` 的 `serverErrorMessage`/`errorStatus`（与 `describeApiError` 分工不同：前者取后端原文，后者转 i18n 文案）。
- 待决：全仓 136 处 `catch (e: any)` 是否统一改用 `describeApiError`（可见行为变更，需单独定）。
- 硬约束：不为清零加 `eslint-disable`；每批单独跑 `vue-tsc -b` 与组件测试。
- 坑：① 纯文本删「未使用导入」会误删标识符（默认导入名/catch 参数名与具名导入文本层不可分），结构性删除须 AST 或人工逐行确认；② `no-undef` 走 browser globals，与 tsc `lib.dom` 覆盖不一致（如 `SpeechRecognitionEvent`），第三方/较新 DOM API 一律自声明形状；③ `vue/no-template-shadow`：`<router-view v-slot="{ Component }">` 的 slot prop 名与 vue 导入 `Component` 冲突，需别名（如 `VueComponent`）；④ 宽泛 `Record<string, any>` 会吞掉字段访问错误（`SkillMarketCard` 改 `MarketManifest` 后 5 处显形）。

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
| mypy 接线结果未经真实 CI 验证（L2-1） | push 并跑一次 CI，留意未声明顶层依赖（beautifulsoup4/aiohttp 类） |
| 新功能方向 | 产品路线图输入 |
| 前端 e2e（Playwright） | 当前无 e2e 配置，需确认是否引入浏览器依赖 |
| `internal/enterprise`/`monitor`/`storage`/`id`/`model` 补测试 | 当前 0 测试文件但较小，需确认回归风险 |
| `internal/api` 路由聚合方式 | `gateway_router.go` 单文件承载，是否拆分待 L3-4 评估 |

## 7. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| A. 门禁与文档（性价比最高） | L2-2、L5-2 | L2-1 已清零，L2-2 可能暴露存量缺陷（即价值）；L5-2 提升后续改动可信度 |
| B. i18n 收尾 | L1-4（语义化 key） | L1-3 已完成，可启动 |
| C. 跨层设计 | L4-1、L4-2、L4-3 | 需设计评审或可达 PG；L4-3 是唯一库结构变更 |
| D. 可维护性 | L3-2、L3-4、L3-7 | 无功能收益，放最后 |

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
