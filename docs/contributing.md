# 贡献与验收流程

**唯一事实来源是 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)**：本文是它的
「人话版落地口径」，用于本地复现 CI。若两者出现分歧，**以 `ci.yml` 为准**，并顺手改本文。

本地跑全绿 = 可以提 PR。

## 工具链版本

| 组件 | 版本来源 |
|---|---|
| Go | `go.mod` 的 `go` 指令（CI 用 `go-version-file: go.mod`） |
| Node | `22` |
| pnpm | `10` |
| Python | `3.11`（**不要用 3.12+ 本地验收**：`python-engine` 有一处 PEP 701 嵌套引号 f-string，在 3.11 下是 `SyntaxError`，CI 用 3.11 正是为了挡住它） |

## CI 的五个 job

### `go` — Go 网关

```bash
go build -mod=mod ./...
go vet -mod=mod ./...
go test -mod=mod ./... -count=1
```

> **`-mod=mod` 不可省略**：仓库的 `vendor/` 下放的是**参考源码**（`vendor/DeepSeek-Reasonix`，
> 不含 `vendor/modules.txt`），而 Go 只要看到 `vendor/` 目录就会按 vendor 模式做一致性检查并报
> `inconsistent vendoring`。`run.py` / `Makefile` / `Dockerfile` 已在编译入口显式排除该目录，
> 手动执行 `go build` / `go test` / `go vet` 时同样需要 `-mod=mod`。

### `python` — Python 引擎（工作目录 `python-engine/`）

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt   # 与 pyproject 的 [dev] extra 对齐
ruff check .
# --follow-imports=silent：只报告这里列出的模块（依赖由它们各自的门禁覆盖）
mypy --follow-imports=silent
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
  app/queue/worker.py app/rag/builder.py app/rag/context_injector.py \
  app/rag/hybrid_search.py app/rag/parser.py app/rag/retriever.py \
  app/rag/stores/base.py app/rag/stores/milvus_store.py \
  app/rag/stores/pgvector_store.py app/run_registry.py app/session_store.py \
  app/skill/manager.py app/skill/store.py app/sse app/subagent/affinity.py \
  app/subagent/followup.py app/subagent/redact.py app/subagent/registry.py \
  app/subagent/reporting.py app/subagent/runtime_cache.py app/subagent/store.py \
  app/tools/_sandbox_worker.py app/tools/client.py app/tools/code_guard.py \
  app/tools/context.py app/tools/discovery.py app/tools/graph.py \
  app/tools/job_runner.py app/tools/jobs.py app/tools/kb.py app/tools/memory.py \
  app/tools/rag_query.py app/tools/registry.py app/tools/run_code.py \
  app/tools/skill.py app/tools/skill_catalog.py app/tools/ssrf.py \
  app/tools/subagent.py app/tools/terminal.py app/tools/web.py app/trace \
  app/workflow/dynamic_nodes.py app/workflow/engine.py app/workflow/executor.py \
  app/workflow/tools.py app/workflow/tracing_engine.py \
    # 分批接线，见开发路线图 L2-1
python -m pytest -q -m "not integration"
```

`mypy` 目前只覆盖**已清零的模块**：`pyproject.toml` 已声明 `strict = true`，但 `app/` 仍有存量
错误，所以门禁按域分批扩大。**`mypy` 会连带检查被 import 的模块并报它们的错误**，因此范围必须
包含传递依赖（第一批里的 `app/config.py` 就是这么来的）—— 清单与扩批进度见
[开发路线图](development-roadmap.md) 的 L2-1。

标记为 `integration` 的用例需要完整栈（网关 HTTP / 真实 PostgreSQL），**不在 CI 中执行**，
需要时单独跑 `pytest -m integration`。

### `frontend` — 前端（工作目录 `frontend-vue/`）

```bash
pnpm install --frozen-lockfile
pnpm run lint          # eslint src
pnpm run build         # check:ui（5 道棘轮）→ vue-tsc -b → vite build
pnpm run test          # vitest run
```

`check:ui` 是五道契约棘轮（z-index / 主题 token / 动效 token / i18n / a11y），任何一道不通过都会
**让构建失败**。其中 i18n 棘轮的存量账本是空账本 —— 它的作用是**阻止任何新增硬编码中文**
（`$t('…')` / `t('…')` 里的中文不算，那是迁移目标）。

### `schema` — 迁移链

```bash
python -m pip install -r requirements-migrate.txt
python -m alembic -c alembic.ini heads                       # 必须恰好 1 个 head
python -m alembic -c alembic.ini upgrade head --sql > /dev/null   # 离线渲染
```

**迁移链必须单一 head**：分叉会让启动校验（`internal/db/schema_version.go` 的
`ParseMigrationHead`）直接拒绝启动。新增迁移后请复核 README「数据库迁移」一节。

### `encoding` — 源码编码

```bash
python scripts/check_source_encoding.py
```

`U+FFFD` / 非法 UTF-8 是**字节已损坏**的信号，不是排版问题：它曾把依赖清单吞进注释行、把
`nginx.conf` 的 5 条配置指令吃掉（大括号失衡导致 frontend 容器起不来）。**看到该 job 红，
先确认磁盘上的字节，别靠编辑器"看起来正常"判断。**

## 提交前自查清单

- [ ] 上面五个 job 对应的命令在本地全绿；
- [ ] 改了 Go 网关 → 至少 `go build -mod=mod ./... && go vet -mod=mod ./...`；
- [ ] 改了 Python 引擎 → `ruff check .` + `mypy <已接线目录>`（见 L2-1）+ `pytest -q -m "not integration"`；
- [ ] 改了前端 → `pnpm run lint` **和** `pnpm run build`（只跑 `vue-tsc` 不够：i18n 批量替换
      里 `t` 未定义这类问题只有 ESLint 的 `no-undef` 报得出来）；
- [ ] 新增/修改了错误码 → 三语言 `locales/{zh-CN,en-US,ar}/errors.ts` 同步，
      见 [错误码契约](error-codes.md)；
- [ ] 新增了迁移 → `alembic heads` 仍是单 head；
- [ ] 没有留下调试脚本 / 临时产物。

## 提交信息约定

Conventional Commits，描述用中文，`scope` 取改动域：

```
feat(database): 添加企业 Webhook 订阅功能
fix(billing): 修复支付入账失败时订单状态未回退问题
refactor(frontend): 优化前端代码类型安全和依赖管理
```

## 相关文档

- [错误码契约](error-codes.md) —— 后端 `code` 与前端文案的同步要求；
- [多实例部署指南](deployment-multi-instance.md) —— 部署形态、依赖门禁与伸缩边界；
- [开发路线图](development-roadmap.md) —— 未完成事项的待办账本；
- [frontend i18n 约定](../frontend-vue/src/i18n/README.md) —— key 规范、codemod 与棘轮口径。
