# 开发路线图

本文是**待办账本**，只收录**尚未完成**的事项，每条标注「依据」（命令 / 文件 / 行）与「验收」；
未经验证的推测显式标注「待确认」，不作为计划依据。

已完成项已从本文移除 —— 它们的依据与验收口径见各自的提交信息（如 `806a45d` 的 i18n 收尾），
不在两处重复维护。

已明确不做（技术结论已论证）的项见 [多实例部署指南](deployment-multi-instance.md) 第 10 节。

---

## 0. 当前状态

主干全绿，无待修复红灯：

| 检查 | 结果 |
|---|---|
| `go build/vet/test -mod=mod ./...` | 通过 |
| `pytest -q -m "not integration"`（`python-engine/`） | 1259 passed |
| `npm run test`（`frontend-vue/`） | 460 passed / 53 files |
| `npm run check:ui`（5 道棘轮） | 通过（i18n 存量 **0**、a11y 0） |
| `npm run lint` | 0 errors / **397 warnings** |
| `npm run build`（vue-tsc -b + vite） | 通过 |
| `python scripts/check_source_encoding.py` | 通过 |
| `mypy`（已接线范围，见 L2-1） | 0 —— 12 个路径，见 `.github/workflows/ci.yml` 的 `Mypy (strict)` step |
| `alembic -c alembic.ini heads` | 单 head：`0002_ent_chaos_experiments` |

**i18n 基线已是空账本** —— 该护栏的作用从此变为「阻止任何新增硬编码中文」。

---

## 1. L1 国际化

### L1-3 译文补齐（`ar` / `en-US`）

- **依据**：`frontend-vue/src/locales/ar/legacy.ts` 与 `en-US/legacy.ts` 均为空壳
  （`export default {}`）；`ar/` 缺 `admin.ts`、`auth.ts`（`en-US/` 有）。阿拉伯语界面当前
  **静默回退中文**，`ar/legacy.ts` 的注释把这称为「显式的翻译待办状态」。
- **前置**：`legacy` 域已在 i18n 收尾中**展平到顶层**（裸键现在能命中），译文补上即生效
  —— 在这之前补了也不生效。
- **验收**：`ar` / `en-US` 的键集与 `zh-CN` 一一对应（写个比对 key 集合的脚本并挂进 `check:ui`，
  防再次漂移）；译文由人工或翻译流程产出，不做机翻直出。

### L1-4 语义化 key 改造

- **依据**：`frontend-vue/src/i18n/README.md` 末段写明「生成的 key 是**原文**（gettext 风格），
  语义化 key 改造是独立任务（只改 key，不动文案）」；key 规范已定为 `<域>.<语义>`。
- **依赖**：L1-3 先做（否则 key 集合仍在变动，译文要跟着返工）。
- **验收**：`legacy.ts` 中的原文 key 收敛为语义化 key；改造**只改 key、不改文案**；
  全部测试与 `check:ui` 保持绿。

---

## 2. L2 质量门禁接线

### L2-1 `mypy --strict` 接入 CI

- **依据**：`python-engine/pyproject.toml` 已声明 `[tool.mypy] strict = true`，
  `[project.optional-dependencies].dev` 已列 `mypy>=1.15.0`，但 `.github/workflows/ci.yml` 的
  python job **只跑 ruff + pytest，从不跑 mypy** —— 严格类型门禁完全未接线。
- **已量化**（mypy 2.3.1，`python -m mypy app/`）：接线前基线 **1171 errors / 130 个文件
  （共 200 个源文件）**；截至第五批已清 81 条 → **1090 errors / 114 个文件**。
  - 按错误码：`type-arg` 488、`no-untyped-def` 266、`no-untyped-call` 116、`arg-type` 59、
    `union-attr` 45、`no-any-return` 41、`assignment` 40、`attr-defined` 38，其余各 ≤18；
    与环境相关的（缺 stub / 缺包）合计仅 20 条（`import-not-found` 12 + `import-untyped` 8）。
  - 按域（前 10）：`agent` 214、`api` 108、`rag` 101、`tools` 88、`core` 80、`memory` 74、
    `providers` 66、`main.py` 55、`queue` 48、`gateway` 47。
  - ⚠ 以上在**本机环境**测得，依赖未必与 CI 一致（缺 stub 的 `boto3`/`psutil`/`fitz` 会多报
    `import-untyped`，缺装的 `opentelemetry` 会报 `import-not-found`）；接线前应在 CI 环境复核基线。
  - 结论：1171 条**不属于需要下调 `strict` 的量级**（备选阈值是「数万条」），按下面的顺序分批接线。
- **实施顺序**（不可颠倒）：
  1. ~~装 mypy 跑 `mypy app/`，记录错误总数与按域分布~~ —— 已完成，数据见上；
  2. **按域分批接线**：
     - ✅ **第一批**：`app/sse` + `app/trace` + `app/config.py`，共 **11 条** —— `dict`/`list`
       补类型参数、两个 `model_validator` 补 `-> Self`、返回注解补 `-> dict[str, Any]`、
       `ConfigDict` → `SettingsConfigDict`；
     - ✅ **第二批**：`app/interfaces`（`llm.py` 5 + `vectorstore.py` 2，全是 `type-arg`），
       **闭包干净**（只检查 4 个文件）；
     - ✅ **第三批**：`app/media` + `app/observability`，共 **10 条** —— `prompt: str = None`
       → `str | None`、`record_process_metrics` 补 `-> None`、structlog processor 的签名
       （`logging.Logger` / `dict` → `Any` / `MutableMapping[str, Any]`）。
       另在 `pyproject.toml` 新增 `[[tool.mypy.overrides]]`：`boto3.*` / `botocore.*` / `fitz` /
       `docx` / `psutil` 这五个库既无 `py.typed` 也无可用 stub，只对其**放宽 import 解析**
       （这些库内部不参与检查，本仓库代码仍按 strict 检查）；
     - ✅ **第四批**：`app/db.py` + `app/db_client.py`，共 **36 条** —— 补函数注解
       （`fetch_one`/`fetch_all`/`execute`/`close`/`close_clients`/`ensure_tables` 等）、
       `dict`/`list` 补类型参数、`_RowDict(dict[str, Any])`、以及给内部中间变量显式注解以
       消除 `no-any-return`。两处**签名与实现不符**在此修正：`init_pool` 声明
       `-> asyncpg.Pool` 却在 unified 模式 `return None`（改为 `| None`）；`get_pool` 的
       unified 分支返回的是鸭子类型适配器而非真 `asyncpg.Pool`（用 `cast` 收敛，避免改动
       全部调用点）。另因 asyncpg 也无 `py.typed`，把它并入 overrides；
     - ✅ **第五批**：`app/llm` + `app/chaos` + `app/middleware` + `app/gateway/ratelimit.py`，
       共 **17 条** —— `LLMClient.__init__`/`bind_gateway` 补注解（`_gateway`/`_local_encoder`
       显式 `Any`：后者是动态加载的 SentenceTransformer）；`ChaosInjectionMiddleware` 的
       ASGI 三元组 `scope`/`receive`/`send` 补 `Any`（刻意不依赖 starlette 类型）；
       `AuthMiddleware`/`RateLimitMiddleware` 的 `app` 补 `Any`；两处 `get_remaining` 补
       `dict[str, Any]`、`_prune` 的 bucket 补 `list[float]`。JSON / `jwt.decode` 的返回值
       本质是 `Any`，一律用显式注解的中间变量消除 `no-any-return`。
       另把 `sentence_transformers` 并入 overrides（本地嵌入的可选 extra，
       `requirements.txt` 刻意不装它）；
     - 五批均零行为变更，引擎套件 `1259 passed` 未变。CI 的 `Mypy (strict)` step 现覆盖
       12 个路径（见 `.github/workflows/ci.yml`），全量存量降至 **1090 / 114 文件**；
     - 后续每清零一个域，就往那个列表里追加，全部通过后改成 `mypy app/`；
     - **下一批的依赖闭包（实测，第五批之后）**：

       | 候选 | 自身存量 | 闭包额外带出的文件 |
       |---|---|---|
       | `app/engine_registry.py` | 2 | 无 |
       | `app/session_store.py` | 13 | 无 |
       | `app/knowledge` | 23 | 无 |
       | `app/context` | 31 | 4 个文件（含自身） |
       | `app/batch_processor.py` | 94 | 8 个文件 |
       | `app/mcp` / `app/plugins` | 23 / 23 | ❌ **会拉进几乎整个 app**（1018 / 1016 条）—— 不可按目录接，除非先清掉它们 import 的大域 |

       ✅ 结构上「按目录」这条路已经走到头：剩下的大域（`agent` 214、`api` 108、`rag` 101、
       `tools` 88、`core` 80、`memory` 74、`providers` 66…）彼此高度耦合，单接一个目录会被
       依赖闭包放大到接近全量。下一批若要继续，**应按文件/子模块切**（如上表前几行），
       而不是按目录。

       ⚠ **`app/db.py` 与 `app/db_client.py` 是依赖瓶颈**（各 18 条）——`chaos` / `middleware` /
       `llm` 三个域都挂在它们后面，先清这两个能一次解锁三个域，比按目录存量排序更划算。
     > **门禁集合是「目标目录 + 传递依赖」，不是单看目录自身条数**（第一批的 `app/config.py`
     > 与上表就是这么来的）—— 每批都要先跑一遍确定闭包。
     > ⚠ **闭包结果依赖第三方 stub 版本**：复核用的隔离 `venv` 只能建在**本机唯一的 Python 3.14**
     > 上，且 `requirements.txt` 的固定版本在 3.14 装不了（`grpcio==1.71.1` 无 wheel、
     > `pydantic==2.11.5` 需 Rust 编译），故用的是最新版 pydantic/pydantic-settings。
     > CI 是 3.11 + 固定版本，若该 job 首次运行报出本地没有的错误，根因大概率在此。
  3. 全量通过后再把 CI 改成 `mypy app/`。
- **验收**：CI 中 mypy 对已接线目录返回 0；`pyproject.toml` 的 `strict = true` 与实际门禁一致。
- **备选**：若量化结果不可接受（如数万条），则**下调 `pyproject.toml` 的 strict 声明**并写明降级理由
  —— 不允许「声明 strict 却不跑」继续存在。

### L2-2 真实栈集成测试门禁（两侧当前都被永久跳过）

- **Go 侧依据**：5 个测试文件、共 6 处 `t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")`，CI 从不设该变量
  → **永久跳过**：`internal/api/diag_alipay_key_test.go:25,43`、
  `internal/api/payment_live_test.go:24,243`、`internal/billing/pgstore_live_test.go:19`、
  `internal/billing/revert_payment_test.go:18`、`internal/settings/settings_live_test.go:17`。
  覆盖的正是**支付入账 / 入账回退 / 支付宝密钥与验签 / 设置持久化**等高风险路径，
  而近期修复（`ed88f74` 入账失败未回退、`eab83be` 支付宝中文参数验签失败）恰恰都落在这里。
- **Python 侧依据**：CI 唯一口径是 `pytest -m "not integration"`，全仓 `mark.integration` 仅 4 处
  （`tests/test_unified_db.py` 3 处、`tests/test_tools_service_api.py` 1 处），真实栈覆盖极薄。
- **实施建议**：
  1. 新增 CI job，用 `services:` 起 `pgvector/pgvector` PostgreSQL（**compose 里没有 PG**，
     因为生产 PG 外置，所以集成门禁必须自建 service 容器，不能复用 compose）；
  2. 该 job 里 `pip install -r requirements-migrate.txt` + `alembic upgrade head` 建库；
  3. 导出 `CHIRON_TEST_POSTGRES_DSN` 后跑
     `go test -mod=mod ./internal/api/... ./internal/billing/... ./internal/settings/...`；
  4. 同 job 或独立 job 跑 `pytest -m integration`。
- **验收**：上述 5 个 Go live 测试在 CI 中**实际执行**（非 skip）；Python integration 用例有明确的最小集合并通过。
- **风险**：这些用例从未在 CI 跑过，首次接入很可能暴露真实缺陷 —— 这正是它的价值，
  但**不要与其它批次混做**，以免被红灯阻塞。

---

## 3. L3 技术债

### L3-2 收敛 ESLint warning（剩 **368 条** `no-explicit-any`）

- **依据**：`npm run lint` 现报 `0 errors / 397 warnings`。已完成两批共 250 条：
  `--fix` 清格式类与未使用项 193 条；**契约层 47 条 `any` 全部类型化**
  （`api` 26 / `composables` 10 / `utils` 7 / `types` 2 / `router` 2）。
- **剩余**：`views` 272 + `components` 96 = **368 条**，散落在组件内部逻辑里，
  每处都要读懂上下文才能给准类型。
- **已知做法**（契约层验证过的四类，可直接沿用）：
  - 有明确结构的 → 定义契约接口（如 `AuthUser`、`TemplateUseResult`、`RawProvider`）；
  - 本质动态的 → `unknown`，**并同时改调用侧窄化**，绝不把报错推给调用方；
  - DOM 非标准成员 → 用 `Navigator & { … }` / `Performance & { … }` 交叉类型，并补上存在性判断；
  - 多形状的宽松解析 → 小助手收敛（`asArray` / `asObject`），替掉链式 `any` 访问。
- **硬约束**：**不为清零而加 `eslint-disable`**；每批单独跑 `vue-tsc -b` 与组件测试。
- **⚠ 已踩过的坑**：批量删「未使用导入」的脚本一次弄坏 6 个文件 —— 默认导入名、catch 参数名、
  函数名与「具名导入成员」在**纯文本层面无法区分**，正则一宽就误删标识符
  （`import  from './X.vue'`、`const  = defineEmits(...)`）。
  **结构性删除只应在 AST 层面做，或逐个人工确认。**

### L3-4 巨型文件拆分（评估项，非缺陷）

- **依据**：`internal/api/gateway_router.go` 62 KB（`internal/api` 85 文件 / 26 133 行）；
  `python-engine/app/agent/runtime.py` 101 KB、`app/main.py` 85 KB、
  `app/memory/service.py` 54 KB、`app/queue/worker.py` 51 KB。
- **说明**：大文件本身不是缺陷，本项**仅为可维护性评估**，与功能交付无直接关系。
- **验收**（若启动）：先产出拆分边界与契约清单，再按「纯移动 + 零行为变更」分步提交，每步测试保持绿。

### L3-7 `WorkflowView` 缺组件测试

- **依据**：`WorkflowView` 的「套用模板」UI 已补齐（工具栏入口 → 弹窗 → `onUseTemplate` →
  `useWorkflowTemplate` 全链路），但**没有组件测试**：该视图 1700 行，setup 里密集依赖 VueFlow 画布
  （`useVueFlow` / `fitView` / `fromBackendFormat`），在 jsdom 里 mount 需要一整套 stub。
- **验收**：先做 `@vue-flow/*` 的测试替身，再补一个最小 spec：断言模板按钮存在、点击后弹窗打开、
  列表按 `templateNodeCount` / `templateEdgeCount` 渲染、点「使用」时 `templateUsingId` 进入 loading。
  （可与 L3-4 的拆分评估一起做。）

---

## 4. L4 结构性后续（项目已在部署文档中自述）

来源：[多实例部署指南](deployment-multi-instance.md) 第 10 节「结构性后续」，本文只补验收口径。

### L4-1 run 现场 checkpoint 续跑

- **现状**：实例故障时该 run 中断（现场状态不迁移）；SSE 断连窗口的实时事件依赖客户端携带
  `Last-Event-ID` 从 Redis Stream 重放。
- **价值**：把「故障后重跑」降级为「从 checkpoint 续跑」。
- **依赖**：需要跨 Go 网关与 Python 引擎的**统一 run 状态模型**设计 —— 先设计后编码。
- **验收**：产出设计文档（状态机、checkpoint 落点、幂等边界、与 `TASK_IDEMPOTENCY_RETENTION_DAYS`
  保留策略的关系），评审通过后再拆实现任务。

### L4-2 `chiron-cli db` 迁移入口的交互设计

- **现状**：`chiron-cli db migrate` 走 `internal/db/migrate.go` 的 `RunMigrations`，实际是 shell 出
  `alembic upgrade head`（**不是**应用内 DDL），要求目标机装有 python + alembic；
  `db status` 已改读 `alembic_version`。
- **待决问题**：CLI 是否应完全不接触迁移流程（让 `alembic` 成为唯一入口），还是保留为「受控便捷封装」。
- **验收**：产出决策记录（保留/移除 + 理由），并按决策统一 `chiron-cli db` 的帮助文本、退出码与错误提示。

### L4-3 时间列 `timestamp` → `timestamptz`

- **依据**：部署文档已更正原先的错误前提 —— 实测该库 145 个时间列中 136 个已是
  `timestamp without time zone`、9 个已是 `timestamp with time zone`，唯一 VARCHAR 时间列是已废弃的
  `schema_migrations.applied_at`，**不涉及** `USING` 转换与存量数据风险。
- **剩余工作**：把 `timestamp` 提升为 `timestamptz`（含 `scripts/generate_orm_models.py`
  生成的 ORM 模型与数十张表）。
- **前置**：需要可达的 PostgreSQL（迁移是发布流程步骤，应用不写 DDL，见 README「数据库迁移」）。
- **验收**：新迁移追加到 `migrations/versions/`（保持**单一 head**，CI 的 `schema` job 会校验）；
  ORM 模型重新生成；`alembic upgrade head --sql` 离线渲染通过。

---

## 5. L5 文档

### L5-2 架构与请求链路总览

- **依据**：`docs/` 现有 4 份（`deployment-multi-instance.md`、`development-roadmap.md`、
  `contributing.md`、`error-codes.md`），讲的是「部署与边界」「流程与契约」，
  **没有一次对话请求的完整链路**；而 `internal/api` 已达 85 文件 / 26 133 行、
  `python-engine/app` 有 200 个源文件。
- **缺口**：缺**一次对话请求的完整链路**
  （前端 → nginx 反代 `/v1`、`/events`、`/ws` → 网关认证/限流/计费 → 引擎 → Redis Stream 事件回传）。
- **验收**：补一篇「架构与请求链路」，每一步都能由代码或命令支撑，放进 `docs/` 并从 `README.md` 链接。

---

## 6. 开放项（待确认，不作为计划依据）

| 项 | 需要什么才能立项 |
|---|---|
| 新功能方向（各业务域下一步做什么） | 产品路线图输入 |
| 前端端到端测试（Playwright） | 当前 `frontend-vue/` 无任何 e2e 配置（仅 vitest）；需先确认是否值得引入浏览器依赖 |
| `internal/enterprise` / `monitor` / `storage` / `id` / `model` 补测试 | 这些包当前 0 个测试文件，但都较小（157–515 行）；需先确认是否有真实回归风险，避免为覆盖率而测试 |
| `internal/api` 的路由聚合方式 | `gateway_router.go` 单文件承载路由；是否拆分需 L3-4 评估后决定 |

---

## 7. 建议顺序

| 批次 | 内容 | 理由 |
|---|---|---|
| **A. 门禁与文档**（性价比最高） | L2-1（mypy 分批接线）、L2-2（真实栈集成门禁）、L5-2（架构与请求链路） | 提升后续所有改动的可信度；L2-2 可能暴露存量缺陷（这正是价值） |
| **B. i18n 收尾** | L1-3（译文）、L1-4（语义化 key） | 必须在 `legacy` 展平之后做，否则译文不生效；L1-4 依赖 L1-3 |
| **C. 跨层设计** | L4-1（run checkpoint 设计）、L4-2（CLI 决策）、L4-3（timestamptz） | 需设计评审或可达 PG；L4-3 是唯一的库结构变更 |
| **D. 可维护性** | L3-2（剩 368 条 `any`）、L3-4（巨型文件拆分，若评估通过）、L3-7（补 `WorkflowView` 测试） | 无功能收益，放最后 |

---

## 8. 通用验收口径

任何一批改动完成时，以下命令必须全绿（与 CI 一致）。**展开说明（逐 job 对应、注意事项）见
[贡献与验收流程](contributing.md)**，那里也以 `.github/workflows/ci.yml` 为唯一事实来源。

```bash
# 仓库根
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1
python scripts/check_source_encoding.py
python -m alembic -c alembic.ini heads        # 必须只有 1 个 head

# python-engine/
# mypy 只覆盖**已接线的目录**（分批扩大，清单见 L2-1）
ruff check . && mypy app/chaos app/config.py app/db.py app/db_client.py app/gateway/ratelimit.py app/interfaces app/llm app/media app/middleware app/observability app/sse app/trace && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 下放的是参考源码（无 `vendor/modules.txt`），
> Go 见到 `vendor/` 目录即按 vendor 模式做一致性检查并报 `inconsistent vendoring`
> （详见 README「常用命令」）。
