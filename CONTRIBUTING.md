# 贡献指南

> 中文为主，英文摘要见文末。这里只写**这个仓库特有**的约定，通用开源礼仪不赘述。
>
> **唯一事实来源是 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)**：本文是它的本地复现口径。
> 两者分歧时以 `ci.yml` 为准，并顺手改本文。

## 先了解它是什么

| 想知道 | 看 |
|---|---|
| 这是什么、能做什么 | [README](README.md) |
| 组件构成与信任边界 | [架构与请求链路](docs/architecture.md) |
| 多副本会出什么问题、怎么扩 | [多实例部署](docs/deployment-multi-instance.md) |
| 错误码契约与三语言同步 | [错误码契约](docs/error-codes.md) |
| 数据库迁移怎么改 | [迁移入口](docs/db-migration-entry.md) |

## 开发环境

### 路线 A：容器（推荐，最少依赖）

```bash
cp .env.example .env                            # 按注释填必填项
python -m alembic -c alembic.ini upgrade head   # 迁移由发布流程执行
docker compose up -d --scale gateway=2 --scale python-engine=2
# 前端入口 http://localhost:3000
```

### 路线 B：本地非容器

需要 Go 1.26+、Python 3.14、Node 22、pnpm 10、PostgreSQL、Redis。

```bash
python scripts/init.py && python run.py setup
python run.py start
```

> **一个容易踩的坑**：本机若有 `vendor/` 参考源码目录，Go 会按 vendor 模式做一致性检查并报
> `inconsistent vendoring`。此时 `go` 命令需加 `-mod=mod`（干净检出不需要）。一劳永逸：
> `go env -w GOFLAGS=-mod=mod`。

## 提交前必须跑过

CI 会在 PR 上跑下面这些 job，**失败即阻断合入**。本地先跑能省一轮往返。

```bash
# Go 网关
go build -mod=mod ./... && go vet -mod=mod ./... && go test -mod=mod ./... -count=1

# Python 引擎（工作目录 python-engine/）
ruff check . && mypy app/ acp_adapter/ && python -m pytest -q -m "not integration"

# 前端（工作目录 frontend-vue/）
pnpm run lint && pnpm run build && pnpm run test

# 迁移链：必须恰好 1 个 head
python -m alembic -c alembic.ini heads

# 仓库根守卫（CI 的 encoding job）
python scripts/check_source_encoding.py
python scripts/check_tool_policy_parity.py
ruff check scripts/
python scripts/check_doc_links.py
python scripts/check_md_tables.py
python scripts/check_error_code_keys.py
python scripts/check_compose_env.py
```

懒得逐条敲：`make check` 是 Go + 文档快检（几十秒）；`make check-all` 是全量（数分钟）。

### 测试标记怎么选

按"这条用例需要什么"选，不是按"算不算集成测试"选：

* `integration` — 需要真实 PostgreSQL（`POSTGRES_DSN` 未设时整类 skip）。
* `live` — 只需要真实 Redis（Lua 原子性、EVAL 语义等桩判不了的东西）。连不上时自己 skip。

### 这些守卫在拦什么

它们都属于「静默错」类，人眼看不出来：

| 守卫 | 拦的是 |
|---|---|
| `check_doc_links.py` | `.md` 里引用了不存在的文档（**新增断链即失败**，存量基线只许缩小） |
| `check_md_tables.py` | Markdown 表格列数不一致（渲染不报错，只会静默错位） |
| `check_source_encoding.py` | U+FFFD、非法 UTF-8、UTF-8 BOM（BOM 会让 Dockerfile/Makefile 首行失效） |
| `check_error_code_keys.py` | Go `Code*` 与三语言 `errors.ts` 键集漂移 |
| `check_compose_env.py` | compose 的必填变量没写进 `.env.example` |
| `check-i18n*.mjs` | 前端新增硬编码中文、三语言键集不对齐 |
| `check-a11y.mjs` | 非交互元素绑 `@click` 却不带 role（键盘不可达） |
| `check-transcript-scroll.mjs` | transcript 滚动单写者被破坏（契约见 [transcript-contract](docs/transcript-contract.md)） |

## 代码约定

1. **失败方向要显式选择**：鉴权 / 授权 / 隔离处 **fail-closed**；业务限制类可以 fail-open，
   但必须在注释里写明为什么。
2. **单一事实源**：持久事实只在 PostgreSQL，Redis 只做协调与有界缓冲。新增缓存时必须回答
   "失效后能否从事实重建"。
3. **多副本假设**：任何进程内状态都要问"另一个副本看不到它会发生什么"。新增后台循环必须
   说明多副本下如何避免重复执行。
4. **数据库变更只走 Alembic**：应用启动只做只读 schema 校验。迁移必须向后兼容，
   破坏性变更用"扩展-迁移-收缩"两步发布。
5. **注释只写"为什么"，且要短**。该写：非显然的取舍、踩过的坑、跨模块不变量。
   不该写：复述代码、逐行翻译、提交历史标记。**过时注释比没有注释更糟** ——
   与代码矛盾时以代码为准，并在同一个 PR 里改掉。

## 提交与 PR

- 一个 PR 做一件事；重构与行为变更分开提。
- 提交信息用 `type(scope): 摘要`（`feat` / `fix` / `refactor` / `docs` / `test` / `chore`）：

  ```
  feat(billing): 添加按租户的用量预警
  fix(agent): 修复会话锁在实例重启后未释放
  ```

- 正文写**为什么**与**影响面**（多副本？迁移？配置变更？）。
- **行为变更**（默认值、fail-open/closed、对外契约）必须在 PR 描述里单列一节并同步文档。
- 新增依赖需要说明理由；能不加就不加。

## 第一次贡献？

从标有 `good first issue` 的 issue 里挑一条 —— 它们范围明确，且有机械可判定的验收方式，
不需要先理解整个架构。不确定就开 draft PR 问。

## 报告安全问题

**不要**开公开 issue。请按 [SECURITY.md](SECURITY.md) 的私有渠道报告。

## English summary

Contributions are welcome. This repository is documented primarily in Chinese; the README
has an English summary. Before opening a PR: read `docs/architecture.md`, run the checks in
"提交前必须跑过" locally, and keep one PR to one change. Security issues must go through the
private channel in [SECURITY.md](SECURITY.md), never a public issue.
