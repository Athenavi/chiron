# 贡献指南

> 中文为主，英文摘要见文末。提 PR 前请先读本页 —— 它写的都是**这个仓库特有的**约定，
> 通用开源礼仪（fork、分支、PR 描述）这里不赘述。
>
> **唯一事实来源是 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)**：本文是它的
> 「人话版落地口径」，用于本地复现 CI。若两者出现分歧，**以 `ci.yml` 为准**，并顺手改本文。

## 1. 先花 20 分钟了解它是什么

| 想知道 | 看 |
|---|---|
| 这是什么、能做什么 | [README](README.md)（含 60 秒快速开始） |
| 面向使用者的分主题文档 | [docs/wiki/](docs/wiki/) |
| 系统由哪些组件构成、信任边界在哪 | [docs/architecture.md](docs/architecture.md) |
| 多副本部署后会出什么问题、怎么扩 | [docs/deployment-multi-instance.md](docs/deployment-multi-instance.md) |
| 当前路线与优先级 | [docs/development-roadmap.md](docs/development-roadmap.md) |

## 2. 开发环境

### 路线 A：容器（推荐，最少依赖）

```bash
cp .env.example .env          # 按注释填必填项（REDIS_PASSWORD / POSTGRES_DSN / APP_SECRET …）
python -m alembic -c alembic.ini upgrade head   # 迁移由发布流程/DBA 执行，应用启动只做只读校验
docker compose up -d --scale gateway=2 --scale python-engine=2
# 前端入口 http://localhost:3000
```

### 路线 B：本地非容器

需要 Go 1.26+、Python 3.11、Node 22、pnpm 10、PostgreSQL、Redis。**一个容易踩的坑**：

> 仓库根的 `vendor/` 放的是**本地参考源码**（不随仓库分发、不含 `vendor/modules.txt`），
> 而 Go 只要看到 `vendor/` 目录就会按 vendor 模式做一致性检查并报 `inconsistent vendoring`。
> **本机有该目录时**，`go build` / `go test` 必须加 `-mod=mod`：
> `go build -mod=mod ./...`。干净检出的仓库里没有这个目录，不需要该参数。
> 想一劳永逸：`go env -w GOFLAGS=-mod=mod`（只影响你的机器，不进仓库）。

### 工具链版本

| 组件 | 版本来源 |
|---|---|
| Go | `go.mod` 的 `go` 指令（CI 用 `go-version-file: go.mod`） |
| Node | `22` |
| pnpm | `10` |
| Python | `3.11`（**不要用 3.12+ 本地验收**：`python-engine` 有一处 PEP 701 嵌套引号 f-string，在 3.11 下是 `SyntaxError`，CI 用 3.11 正是为了挡住它） |

## 3. 提交前必须本地跑过

CI 会在 PR 上跑下面这些 job；本地先跑能省一轮往返。**失败即阻断合入**。

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
mypy app/ acp_adapter/   # strict；全量，与 CI 同一条命令
python -m pytest -q -m "not integration"
```

`mypy` 对 `app/` **全量**按 strict 检查（`pyproject.toml` 的 `strict = true` 与门禁已一致）。

标记分两层，**按"这条用例到底需要什么"选，不是按"它算不算集成测试"选**：

* `integration`：需要**真实 PostgreSQL**（或完整栈：网关 HTTP + 库）。`tests/conftest.py` 里那个
  autouse fixture 在 `POSTGRES_DSN` 没设时**整类 skip** —— 于是"本机没接库"与"代码坏了"能分开。
* `live`：只需要**真实 Redis**（Lua 的算术、EVAL 的原子性……桩判不了的那些，如
  `tests/test_key_ring_live.py`、`tests/test_line_ledger.py::TestRealRedisSemantics`）。
  **别把它们标成 `integration`**：那会被上面那个"必须有 PG"的 fixture 整类拦下，本机明明有 Redis
  却永远验不到脚本；连不上时它们自己 skip。

CI 的 `Real-stack integration` job 跑 `pytest -q -m "integration or live"`（两类都覆盖，前置是起
PostgreSQL/Redis 服务、建 schema、构建并启动网关），本地按下面「本机真实栈」跑同一条命令即可。

#### mypy strict 维护手册

全量接线已经收尾，下面这些约束与踩过的坑留作维护参考。新增第三方依赖、或要动
`pyproject.toml` 的 overrides 时照办。

**两条始终生效的约束**

1. **门禁曾带 `--follow-imports=silent`** —— 分批接线期间，未清零模块的依赖闭包不可控
   （`app/core` 的传递依赖达 76 个文件，`app/tools` / `app/workflow` / `app/skill` 各 74–76，
   单文件亦可拉到 72 个），只能让 mypy 只报告命令行里列出的模块。存量清零后该开关已移除。
   **若将来要把某块从全量门禁里摘出去，就得把它加回来**，并接受"依赖由它们各自的门禁覆盖"这个前提。
2. **第三方库缺口走 `pyproject.toml` 的 `[[tool.mypy.overrides]]`，不许用 `# type: ignore` 绕**：
   - 缺 stub / 缺包 → `ignore_missing_imports`（现为 `asyncpg` / `boto3.*` / `botocore.*` / `docx` /
     `fitz` / `langchain.*` / `markitdown` / `openpyxl` / `pdfplumber` / `psutil` / `pymilvus` /
     `sentence_transformers` / `unstructured.*` / `uvicorn`）；
   - **有** stub 但标注不全 → 在**调用方**上单项豁免（现为
     `module = ["app.rag.parser", "app.main", "app.tools.media", "app.media.attachments"]`
     的 `disallow_untyped_calls = false`，因 `pymupdf` 自带 `py.typed` 却缺 `open` / `Document` 注解）。
     该判定由调用方作出，豁免只能落在调用它的文件上。**新增一个 pymupdf 调用方 = 往这个列表里加一个模块名**，
     不动全局 `strict`；反过来，**不是 pymupdf 造成的错，不许用这条豁免兜**。

**已踩过的坑**

- ⚠ **给 FastAPI 路由补返回注解时，不要写含 `Response` 子类的联合类型** —— 路由函数的返回注解会
  被 FastAPI 当作 `response_model` 生成校验，而 `JSONResponse | dict[str, Any]` 不是合法 Pydantic
  字段类型，后果是**测试在 collection 阶段就报** `FastAPIError: Invalid args for response field!`。
  这些 handler 的出口分两类（错误时 `JSONResponse`、正常时 dict），要么统一写 `-> Any`
  （`Any` 是合法字段类型），要么在装饰器上显式 `response_model=None`。
- ⚠ **`pymupdf` 的 `Document` 不要写 `for page in doc`**：它没有 `__iter__`，靠的是
  `__getitem__` 的旧式迭代协议（越界靠 `IndexError` 收尾）—— 跑得通，但既不是官方口径、类型上也只能报
  "not iterable"。写 `doc.pages()`，`doc.close()` 放 `try/finally`（判例：`app/media/attachments.py`）。
- ⚠ **`[tool.mypy]` 的 `platform = "linux"`** 不可删：生产与 CI 都在 Linux，而本机可能是 Windows。
  不声明的话，Unix-only 模块的成员（`app/tools/_sandbox_worker.py` 的 `resource.setrlimit` /
  `RLIMIT_*`）在 Windows 上会误报 `attr-defined`。
- ⚠ **strict 的 `no_implicit_reexport`**：仅为「保留既有 import 路径」而做的 re-export
  （如 `app/subagent/budget.py` 转发 `app.agent.task_budget` 的符号）必须写 `__all__`，
  否则调用方会报 `does not explicitly export attribute`。
- ⚠ **CI 环境只装 `requirements.txt` + `requirements-dev.txt`，与本机不同** —— 每批接线时留意
  **顶层** import 是否被环境里的第三方包偶然带入（曾查出 `beautifulsoup4` 与 `aiohttp` 从未被声明，
  此前只由 markdownify / aiobotocore 偶然带入）。
- ⚠ **本机复核环境有已知差异**：隔离 `venv` 只能建在 Python 3.14（`requirements.txt` 的固定版本装不了：
  `grpcio==1.71.1` 无 wheel、`pydantic==2.11.5` 需 Rust 编译），依赖版本高于 CI 的 3.11 + 固定版本。
  若某个 step 首次在 CI 上报出本地没有的错误，根因大概率在此；**先按 CI 结果复核**，再决定是补
  overrides 还是改代码。看到"环境差异"先确认代码是否真的对，别急着加豁免。

### `frontend` — 前端（工作目录 `frontend-vue/`）

```bash
pnpm install --frozen-lockfile
pnpm run lint          # eslint src
pnpm run build         # check:ui（9 个契约守卫）→ vue-tsc -b → vite build
pnpm run test          # vitest run
```

`check:ui` 是**九个**契约守卫脚本（z-index / 主题 token 契约 / 主题 token 对齐 / 动效 token /
i18n / i18n 键集 / a11y / **transcript 滚动单写者** / RTL 逻辑属性），任何一道不通过都会
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

### `encoding` — 仓库根守卫

```bash
python scripts/check_source_encoding.py      # U+FFFD / 非法 UTF-8 / **UTF-8 BOM**
python scripts/check_tool_policy_parity.py   # Go ↔ Python（↔ 前端）必须同构：分级表 / 对话模式 / 提供商目录 / 故障注入 / 共享键前缀 / 共享常量 / **工具授权模式**（L3-9）/ **子 Agent 终态集合**
ruff check scripts/                          # 仓库根脚本（配置见仓库根 ruff.toml）
python scripts/check_doc_links.py            # 被引用的 docs/*.md **与相对 .md 链接**都必须存在（存量走基线，新增即失败）
python scripts/check_md_tables.py            # Markdown 表格列数必须一致（存量走基线，新增即失败）
python scripts/check_error_code_keys.py      # Go `Code*` ↔ 三语言 `errors.ts` 契约键必须一致，且三语言键集相同
python scripts/check_compose_env.py          # compose 里 `${VAR:?…}` 声明的**必填**变量必须在 `.env.example` 出现（注释形态也算）
```

`U+FFFD` / 非法 UTF-8 是**字节已损坏**的信号，不是排版问题：它曾把依赖清单吞进注释行、把
`nginx.conf` 的 5 条配置指令吃掉（大括号失衡导致 frontend 容器起不来）。**看到该 job 红，
先确认磁盘上的字节，别靠编辑器"看起来正常"判断。**

### 本机真实栈 —— 把网关也起起来，`integration` 才会全绿

CI 的 real-stack job 会拉一个真网关；**本机也能这么干**（2026-10-08 实测跑通 11/11）。此前
`tests/test_unified_db.py` 的两条只能靠 CI，就是因为本机没起网关（引擎侧统一客户端会经
`/v1/internal/*` 反向走网关）。

```bash
# 1) 库需已 alembic upgrade head；起本地 Redis
redis-server --port 6390 --appendonly no --save ""

# 2) 起网关（它读 REDIS_ADDR，不是 REDIS_URL；密钥用仓库根的 .env）
REDIS_ADDR=127.0.0.1:6390 go run ./cmd/chiron     # 监听 8080
curl -sS localhost:8080/health && curl -sS localhost:8080/ready   # 都应 200

# 3) 再跑集成（引擎侧统一客户端会走网关的 /v1/internal/*）
POSTGRES_DSN=<来自 .env> REDIS_URL=redis://127.0.0.1:6390/0 \
  python -m pytest -q -m integration
```

> 起不来的常见原因：库的 schema 与代码 head 不一致（网关默认拒绝启动，`ALLOW_SCHEMA_DRIFT=true`
> 可临时放行）、`APP_SECRET` 短于 32 字符、Redis 地址写成 `REDIS_URL`（网关不认这个变量）。
>
> **跨实例双进程演练**另有一条命令（CI 的 real-stack job 里也跑，用同一份已构建的网关二进制）：
> `python tests/drills/multi_instance_drill.py <网关二进制>` —— 起**两个**真实网关进程，验跨实例扇出、
> 实时不重复、断线重连补发不重复。它读环境变量（无需 `.env`），Redis 地址默认取 `REDIS_URL`。

### 提交前自查清单

- [ ] 上面五个 job 对应的命令在本地全绿；
- [ ] 改了 Go 网关 → 至少 `go build -mod=mod ./... && go vet -mod=mod ./...`；
- [ ] 改了 Python 引擎 → `ruff check .` + `mypy app/ acp_adapter/` + `pytest -q -m "not integration"`；
- [ ] 改了前端 → `pnpm run lint` **和** `pnpm run build`（只跑 `vue-tsc` 不够：i18n 批量替换
      里 `t` 未定义这类问题只有 ESLint 的 `no-undef` 报得出来）；
- [ ] 新增/修改了错误码 → 三语言 `locales/{zh-CN,en-US,ar}/errors.ts` 同步，
      见 [错误码契约](docs/error-codes.md)；
- [ ] 新增了迁移 → `alembic heads` 仍是单 head；
- [ ] 没有留下调试脚本 / 临时产物。

> 嫌麻烦？`make check` 是 Go + 文档部分的快检（几十秒）；`make check-all` 再加 Python
> （pytest / ruff / mypy）与前端（build / test），耗时数分钟。

## 4. 这些守卫在拦什么

它们都属于「静默错」类，人眼看不出来：

| 守卫 | 拦的是 |
|---|---|
| `check_doc_links.py` | 文档里引用不存在的路径（**新增断链即失败**，存量走基线且只许缩小） |
| `check_doc_line_refs.py` | `文件:行号` 引用漂移（代码改动后行号会变） |
| `check_md_tables.py` | Markdown 表格列数不一致（渲染不报错、只会静默错位） |
| `check_source_encoding.py` | U+FFFD、非法 UTF-8、**UTF-8 BOM**（BOM 会让 Dockerfile/Makefile 首行失效） |
| `check_error_code_keys.py` | Go `Code*` 与三语言 `errors.ts` 键集不一致（只有该语言的用户会看到未本地化原文） |
| `check_compose_env.py` | compose 的必填变量没写进 `.env.example` |
| `check-i18n.mjs` / `check-i18n-keys.mjs` | 前端新增硬编码中文、三语言键集不对齐 |
| `check-a11y.mjs` | 非交互元素绑 `@click` 却不带 role（键盘不可达） |
| `check-theme-token-*.mjs` / `check-motion-tokens.mjs` / `check-z-index-tokens.mjs` | 硬编码颜色、主题/动效/z-index token 漂移 |
| `check-rtl-logical.mjs` / `check-transcript-scroll.mjs` | RTL 逻辑属性、transcript 滚动单写者被破坏 |

> 守卫是**棘轮**：基线只降不升。确实要接受新债务时才用各脚本的 `--write-baseline`，
> 并在 PR 里说明理由。

## 5. 代码约定（这个仓库特有的几条）

1. **失败方向要显式选择**：需要鉴权/授权/隔离的地方 **fail-closed**（依赖不可用即拒绝），
   业务限制类**可以** fail-open，但必须在注释里写明为什么。不要"看情况"。
2. **单一事实源**：持久事实只在 PostgreSQL；Redis 只做协调与有界缓冲。
   新增缓存时必须回答"失效后能否从事实重建"。
3. **不要在拆分/重构时扩大可注入面**：新增模块只承接已存在的依赖，不引入新的全局单例。
4. **多副本假设**：任何进程内状态都要问一句"另一个副本看不到它会发生什么"。
   新增后台循环/定时任务时，必须说明多副本下如何避免重复执行。
5. **数据库变更只走 Alembic**：应用启动只做**只读** schema 校验，不执行 DDL。
   迁移必须向后兼容（滚动升级），破坏性变更用"扩展-迁移-收缩"两步发布。
6. **注释写"为什么"**：本仓库的注释密度较高是有意的 —— 解释取舍与踩过的坑，
   而不是复述代码。提交信息同理。

## 6. 提交与 PR

- 一个 PR 做一件事；重构与行为变更分开提。
- 提交信息用 `type(scope): 摘要`（`feat` / `fix` / `refactor` / `docs` / `test` / `chore`），
  描述用中文，`scope` 取改动域：

  ```
  feat(database): 添加企业 Webhook 订阅功能
  fix(billing): 修复支付入账失败时订单状态未回退问题
  refactor(frontend): 优化前端代码类型安全和依赖管理
  ```

- 正文写**为什么**与**影响面**（多副本？迁移？配置变更？）。
- **行为变更**（默认值、fail-open/closed、对外契约）必须在 PR 描述里单列一节，
  并同步文档。仅改代码不改文档的 PR 通常会被要求补文档。
- 新增依赖需要说明理由；能不加就不加（依赖越少越可审计）。

## 7. 报告安全问题

**不要**开公开 issue。请按 [SECURITY.md](SECURITY.md) 的私有渠道报告。

## 8. 第一次贡献？

从 [入门任务清单](docs/good-first-issues.md) 挑一条 —— 它们都有**机械可判定的验收方式**，
不需要先理解整个架构。不确定就开 draft PR 问，不用憋到最后。

## 9. English summary

Contributions are welcome. This repository is documented primarily in Chinese; the README
has an English summary. Before opening a PR: read `docs/architecture.md`, run the checks in
§3 locally, and keep one PR to one change. Security issues must go through the private
channel in [SECURITY.md](SECURITY.md), never a public issue.
