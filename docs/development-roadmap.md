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
| `mypy`（已接线范围，见 L2-1） | 0 —— 95 个路径，见 `.github/workflows/ci.yml` 的 `Mypy (strict)` step |
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
- **已量化**（mypy 2.3.1）：接线前基线 **1171 errors / 130 个文件（共 200 个源文件）**，
  按域 `agent` 214、`api` 108、`rag` 101、`tools` 88、`core` 80、`memory` 74、`providers` 66、
  `main.py` 55、`queue` 48、`gateway` 47；按码以 `type-arg` 488、`no-untyped-def` 266、
  `no-untyped-call` 116 为主，环境相关（缺 stub / 缺包）仅 20 条。
  **截至第二十一批已清 973 条 → 198 / 7 文件**。结论：1171 条**不属于需要下调 `strict` 的量级**
  （备选阈值是「数万条」）。
- **实施顺序**：
  1. ~~装 mypy 跑 `mypy app/`，记录错误总数与按域分布~~ —— 已完成，数据见上；
  2. **分批接线**：✅ 已完成 **21 批、清 973 条**，全部零行为变更、引擎套件 `1259 passed` 未变。
     各批的范围、修复要点与踩坑细节见提交信息，本文不重复维护：

     | 批 | 提交 | 范围 |
     |---|---|---|
     | 1 | `21a96ae` | `app/sse` + `app/trace` + `app/config.py`（11 条） |
     | 2 | `478047a` | `app/interfaces`（7 条） |
     | 3 | `7b9204a` | `app/media` + `app/observability`（10 条；引入 `[[tool.mypy.overrides]]`） |
     | 4 | `c0af3bb` | `app/db.py` + `app/db_client.py`（36 条；两处签名与实现不符） |
     | 5 | `5200cdf` | `app/llm` + `app/chaos` + `app/middleware` + `gateway/ratelimit.py`（17 条） |
     | 6 | `d613c54` | `engine_registry` + `session_store` + `knowledge` + `context`（69 条） |
     | 7 | `07bf869` | `providers` + `memory` + `gateway/{provider,cache,router}`（142 条） |
     | 8 | `7cefafc` | `app/core`（80 条；门禁改用 `--follow-imports=silent`） |
     | 9 | `eb3e0f4` | `app/api/knowledge.py` + `app/api/unified_executor.py`（48 条） |
     | 10 | `293929b` | `app/api/{agents,context,skills,system}.py` + `app/tools/{code_guard,context,rag_query,skill,skill_catalog,ssrf,web}.py`（21 条；另补 `beautifulsoup4` / `aiohttp` 两个缺失依赖） |
     | 11 | `9618a62` | `app/agent/{event_sink,message_codec}.py` + `app/mcp/{client,registry}.py`（60 条） |
     | 12 | `57def13` | `app/agent/loop.py` + `app/queue/producer.py` + `app/rag/stores/base.py`（26 条；含 `LLMProvider.chat` 的异步生成器协议修正） |
     | 13 | `c093acd` | `app/agent/multi_agent.py` + `app/workflow/engine.py`（33 条；`chat_stream` 的 `messages` 按 `normalize_messages` 契约放宽为 `list[Any]`） |
     | 14 | `58741a9` | `app/gateway/coalescer.py` + `app/rag/parser.py`（27 条；overrides 补 `pdfplumber`，并对 `app.rag.parser` 精确豁免 `disallow_untyped_calls`） |
     | 15 | `ea982bf` | `app/api/media.py` + `app/api/workflows.py` + `app/agent/collaboration.py`（41 条） |
     | 16 | `2718755` | `app/api/memory.py` + `app/subagent/affinity.py` + `app/plugins/pool.py`（34 条） |
     | 17 | `301d1f0` | `tools/_sandbox_worker.py` + `rag/stores/milvus_store.py` + `agent/subagent_runner.py` + `plugins/owner_lease.py` + `workflow/tracing_engine.py`（48 条） |
     | 18 | `bb23a49` | 小文件清扫 12 个：`agent/{guards,modes,prompt_engine}.py` + `gateway/key_ring.py` + `rag/{hybrid_search,stores/pgvector_store}.py` + `subagent/{redact,registry,store}.py` + `tools/{jobs,run_code,subagent}.py`（72 条） |
     | 19 | `1682ba6` | 小文件清扫 23 个（`tools/{registry,client,discovery,graph,job_runner,kb,memory,terminal}.py`、`queue/{dlq,idempotency}.py`、`subagent/{followup,reporting,runtime_cache}.py`、`agent/{profile,side_effect_ledger}.py`、`rag/context_injector.py`、`skill/store.py`、`run_registry.py`、`plugins/broker_proxy.py`、`api/capabilities.py`、`workflow/{dynamic_nodes,executor,tools}.py`）（81 条；含 `AsyncClient.close()` 应为 `aclose()` 等三处真实缺陷） |
     | 20 | `eda243a` | `app/skill/manager.py`（31 条）+ `[[tool.mypy.overrides]]` 增补可选后端（`langchain.*` / `markitdown` / `openpyxl` / `qdrant_client` / `unstructured.*`） |
     | 21 | `884a7a4` | `app/rag/builder.py`（40 条；`_vector_store` 收窄 + `docx.Document` 按 `Any` 处理） |

     第八批修的 3 个**真实缺陷**值得留个索引（都在其提交信息里）：
     `core/agent_skill_selector.py` 的 `cap.usage_count`（`Capability` 无此字段）、
     `core/task_router.py` 的 `_group_by_dependencies`（拿 `SubTask` 对象与 `subtask_id` 比较，
     恒为真）、`core/prompt_library.py` 的 `_executor: callable | None`（内置函数当类型用）。

     - 继续方式：每清零一块就往 `.github/workflows/ci.yml` 的 `Mypy (strict)` step 列表里追加
       （现为 **95 个路径**）；`mypy app/` 全绿后删掉 `--follow-imports=silent`；
     - 下一步候选（按文件切；`python -m mypy app/` 的存量，共 198 条 / 7 个文件）：
       大文件 3 个 —— `agent/runtime.py` 99、`main.py` 50、`queue/worker.py` 35（共 184 条）；
       另有 4 个文件**刻意留出**（缺陷修法需要设计决策，不属"零行为变更"范围）：
       - `app/tools/browser.py`（4 条）：`BrowserHub` Protocol 声明**同步**的
         `connected_client_ids` / `exec_command`，而 `GatewayBrowserHub` 实现是 **async**，
         `_resolve_client` / `_exec` 却同步调用 —— `ids` 会是 coroutine，`return ids[0]` 必
         `TypeError`。要么把 Protocol 与调用点改成 async，要么给实现加同步包装；
       - `app/api/plugins.py`（5 条）：引用不存在的 `app.main.get_plugin_pool` 与
         `Settings.log_dir`（`app/main.py` 里只有模块级 `_plugin_pool`）；
       - `app/batch_processor.py`（2 条）：引用不存在的 `app.rag.builder.build_knowledge`
         （`RAGBuilder` 只有 `build_document`，参数签名也不同）；
       - `app/tools/media.py`（4 条）：对 `pymupdf.Document` 直接迭代（stub 无 `__iter__`），
         且 `openpyxl` 缺 stub（后者照例走 `ignore_missing_imports` 即可）；
     - **两条仍生效的约束**（新增依赖或新批次时照办）：
       1. **门禁用 `mypy --follow-imports=silent`** —— 剩余模块的依赖闭包不可控（`app/core` 的传递
          依赖达 76 个文件，`app/tools` / `app/workflow` / `app/skill` 各 74–76，单文件亦可拉到 72 个）。
          `silent` 让门禁只报告**列出的**模块，依赖由它们各自的门禁覆盖 —— 列出的模块仍按 strict 检查；
       2. **无 `py.typed` 的第三方库走 `[[tool.mypy.overrides]]`**（`pyproject.toml`），当前为
          `asyncpg` / `boto3.*` / `botocore.*` / `docx` / `fitz` / `pdfplumber` / `psutil` /
          `pymilvus` / `sentence_transformers` —— 只放宽这些库的 import 解析，本仓库代码仍按
          strict 检查；**不要用 `# type: ignore` 绕**。
          另有两条**非 import 类**的精确豁免，同样写在 `pyproject.toml`：
          - `module = ["app.rag.parser"]` + `disallow_untyped_calls = false` —— `pymupdf` 自带
            `py.typed` 但 `open` / `Document` 缺注解，该判定由**调用方**作出，豁免只能落在调用
            它的文件上；
          - 新增同类第三方缺口时照此办理：先判断是「缺 stub」（走 `ignore_missing_imports`）
            还是「有 stub 但标注不全」（走调用方的单项豁免），两者都不许用 `# type: ignore`；
     - ⚠ **给 FastAPI 路由补返回注解时不要写含 Response 子类的联合类型** —— 路由函数的返回
       注解会被 FastAPI 当作 `response_model` 生成校验，而 `JSONResponse | dict[str, Any]`
       不是合法 Pydantic 字段类型，后果是**测试在 collection 阶段就报**
       `FastAPIError: Invalid args for response field!`（第十六批 `api/memory.py` 实测踩到）。
       这些 handler 的出口本来就分两类（错误时 `JSONResponse`、正常时 dict），要么统一写
       `-> Any`（`Any` 是合法字段类型，`api/media.py` 即如此），要么在装饰器上显式
       `response_model=None`。
     - ⚠ **`[tool.mypy]` 的 `platform = "linux"`**：生产与 CI 都在 Linux，而本机是 Windows。
       不声明的话，Unix-only 模块的成员（`app/tools/_sandbox_worker.py` 里的
       `resource.setrlimit` / `RLIMIT_*`）在 Windows 上会误报 `attr-defined`。
     - ⚠ **strict 的 `no_implicit_reexport`**：仅为"保留既有 import 路径"而做的 re-export
       （如 `app/subagent/budget.py` 转发 `app.agent.task_budget` 的符号）必须写 `__all__`，
       否则调用方会报 `does not explicitly export attribute`。
     - ⚠ **本分支从未 push，CI 一次都没跑过** —— 迄今所有 mypy 结果都来自本机隔离 venv。
       首次真正跑 CI 时，除已接线的 mypy 之外还要留意「只装 `requirements.txt` +
       `requirements-dev.txt` 的环境」缺哪些**顶层** import：第十批就是这样查出
       `beautifulsoup4`（`app/tools/web.py`）与 `aiohttp`（`app/gateway/router.py`）从未被声明，
       它们此前只由环境里的 markdownify / aiobotocore 偶然带入。
     - ⚠ **本机复核环境有已知差异**：隔离 `venv` 只能建在 Python 3.14（`requirements.txt` 的固定
       版本装不了：`grpcio==1.71.1` 无 wheel、`pydantic==2.11.5` 需 Rust 编译），依赖版本高于 CI。
       CI 是 3.11 + 固定版本 —— 若该 step 首次运行报出本地没有的错误，根因大概率在此；
       先按 CI 结果复核，再决定是补 overrides 还是改代码。

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
# mypy 只覆盖**已接线的模块**（分批扩大，清单见 L2-1）；
# --follow-imports=silent 让门禁只报告列出的模块（依赖由它们各自的门禁覆盖）
ruff check . && mypy --follow-imports=silent
  app/agent/collaboration.py app/agent/event_sink.py app/agent/guards.py \
  app/agent/loop.py app/agent/message_codec.py app/agent/modes.py \
  app/agent/multi_agent.py app/agent/profile.py app/agent/prompt_engine.py \
  app/agent/side_effect_ledger.py app/agent/subagent_runner.py app/api/agents.py \
  app/api/capabilities.py app/api/context.py app/api/knowledge.py \
  app/api/media.py app/api/memory.py app/api/skills.py app/api/system.py \
  app/api/unified_executor.py app/api/workflows.py app/chaos app/config.py \
  app/context app/core app/db.py app/db_client.py app/engine_registry.py \
  app/gateway/cache.py app/gateway/coalescer.py app/gateway/key_ring.py \
  app/gateway/provider.py app/gateway/ratelimit.py app/gateway/router.py \
  app/interfaces app/knowledge app/llm app/mcp/client.py app/mcp/registry.py \
  app/media app/memory app/middleware app/observability \
  app/plugins/broker_proxy.py app/plugins/owner_lease.py app/plugins/pool.py \
  app/providers app/queue/dlq.py app/queue/idempotency.py app/queue/producer.py \
  app/rag/builder.py app/rag/context_injector.py app/rag/hybrid_search.py \
  app/rag/parser.py app/rag/retriever.py app/rag/stores/base.py \
  app/rag/stores/milvus_store.py app/rag/stores/pgvector_store.py \
  app/run_registry.py app/session_store.py app/skill/manager.py \
  app/skill/store.py app/sse app/subagent/affinity.py app/subagent/followup.py \
  app/subagent/redact.py app/subagent/registry.py app/subagent/reporting.py \
  app/subagent/runtime_cache.py app/subagent/store.py \
  app/tools/_sandbox_worker.py app/tools/client.py app/tools/code_guard.py \
  app/tools/context.py app/tools/discovery.py app/tools/graph.py \
  app/tools/job_runner.py app/tools/jobs.py app/tools/kb.py app/tools/memory.py \
  app/tools/rag_query.py app/tools/registry.py app/tools/run_code.py \
  app/tools/skill.py app/tools/skill_catalog.py app/tools/ssrf.py \
  app/tools/subagent.py app/tools/terminal.py app/tools/web.py app/trace \
  app/workflow/dynamic_nodes.py app/workflow/engine.py app/workflow/executor.py \
  app/workflow/tools.py app/workflow/tracing_engine.py \
  && python -m pytest -q -m "not integration"

# frontend-vue/
pnpm install --frozen-lockfile && pnpm run lint && pnpm run build && pnpm run test
```

> `-mod=mod` 不可省略：仓库 `vendor/` 下放的是参考源码（无 `vendor/modules.txt`），
> Go 见到 `vendor/` 目录即按 vendor 模式做一致性检查并报 `inconsistent vendoring`
> （详见 README「常用命令」）。
