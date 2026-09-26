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
mypy app/          # strict；全量，见下方「mypy strict 接线手册」
python -m pytest -q -m "not integration"
```

`mypy` 对 `app/` **全量**按 strict 检查（`pyproject.toml` 的 `strict = true` 与门禁已一致）。
这是 25 批分批接线的结果：起点是 1171 条 / 130 文件，过程见
[开发路线图](development-roadmap.md) 的 L2-1。

标记为 `integration` 的用例需要完整栈（网关 HTTP / 真实 PostgreSQL），**不在 CI 中执行**，
需要时单独跑 `pytest -m integration`。

#### mypy strict 接线手册

分批接线**已经收尾**（`mypy app/` 全量通过），下面这些约束与踩过的坑留作维护参考：
新增第三方依赖、或要动 `pyproject.toml` 的 overrides 时照办。

**两条始终生效的约束**

1. **历史：门禁曾带 `--follow-imports=silent`** —— 分批接线期间，未清零模块的依赖闭包不可控
   （`app/core` 的传递依赖达 76 个文件，`app/tools` / `app/workflow` / `app/skill` 各 74–76，
   单文件亦可拉到 72 个），只能让 mypy 只报告命令行里列出的模块。存量清零后该开关已移除。
   **若将来要把某块从全量门禁里摘出去，就得把它加回来**，并接受"依赖由它们各自的门禁覆盖"这个前提。
2. **第三方库缺口走 `pyproject.toml` 的 `[[tool.mypy.overrides]]`，不许用 `# type: ignore` 绕**：
   - 缺 stub / 缺包 → `ignore_missing_imports`（现为 `asyncpg` / `boto3.*` / `botocore.*` / `docx` /
     `fitz` / `langchain.*` / `markitdown` / `openpyxl` / `pdfplumber` / `psutil` / `pymilvus` /
     `qdrant_client` / `sentence_transformers` / `unstructured.*` / `uvicorn`）；
   - **有** stub 但标注不全 → 在**调用方**上单项豁免（现为 `module = ["app.rag.parser", "app.main"]`
     的 `disallow_untyped_calls = false`，因 `pymupdf` 自带 `py.typed` 却缺 `open` / `Document` 注解）。
     该判定由调用方作出，豁免只能落在调用它的文件上。

**已踩过的坑**

- ⚠ **给 FastAPI 路由补返回注解时，不要写含 `Response` 子类的联合类型** —— 路由函数的返回注解会
  被 FastAPI 当作 `response_model` 生成校验，而 `JSONResponse | dict[str, Any]` 不是合法 Pydantic
  字段类型，后果是**测试在 collection 阶段就报** `FastAPIError: Invalid args for response field!`。
  这些 handler 的出口分两类（错误时 `JSONResponse`、正常时 dict），要么统一写 `-> Any`
  （`Any` 是合法字段类型），要么在装饰器上显式 `response_model=None`。
- ⚠ **`[tool.mypy]` 的 `platform = "linux"`** 不可删：生产与 CI 都在 Linux，而本机可能是 Windows。
  不声明的话，Unix-only 模块的成员（`app/tools/_sandbox_worker.py` 的 `resource.setrlimit` /
  `RLIMIT_*`）在 Windows 上会误报 `attr-defined`。
- ⚠ **strict 的 `no_implicit_reexport`**：仅为「保留既有 import 路径」而做的 re-export
  （如 `app/subagent/budget.py` 转发 `app.agent.task_budget` 的符号）必须写 `__all__`，
  否则调用方会报 `does not explicitly export attribute`。
- ⚠ **CI 环境只装 `requirements.txt` + `requirements-dev.txt`，与本机不同** —— 每批接线时留意
  **顶层** import 是否被环境里的第三方包偶然带入：曾查出 `beautifulsoup4`（`app/tools/web.py`）
  与 `aiohttp`（`app/gateway/router.py`）从未被声明，此前只由 markdownify / aiobotocore 偶然带入。
- ⚠ **本机复核环境有已知差异**：隔离 `venv` 只能建在 Python 3.14（`requirements.txt` 的固定版本装不了：
  `grpcio==1.71.1` 无 wheel、`pydantic==2.11.5` 需 Rust 编译），依赖版本高于 CI 的 3.11 + 固定版本。
  若某个 step 首次在 CI 上报出本地没有的错误，根因大概率在此；**先按 CI 结果复核**，再决定是补
  overrides 还是改代码。
  曾出现过两类本地-only 误报，均已消除，可作同类问题的判例：
  - `opentelemetry.exporter.otlp.proto.grpc.*` 的 `import-not-found`（本机没装，
    `requirements.txt` 里有）→ 归入 `ignore_missing_imports`，两种环境都不再报错；
  - `markitdown` 的 `convert()` 参数签名（本机装了、CI 不装）→ 复查发现**代码本身也是错的**
    （给只接受 `str | Path | Response | BinaryIO` 的 API 传了裸 `bytes`），改成 `io.BytesIO`
    后两边都对。**看到"环境差异"先确认代码是否真的对，别急着加豁免。**

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
