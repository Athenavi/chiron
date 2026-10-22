# 贡献指南

> 中文为主，英文摘要见文末。提 PR 前请先读本页 —— 它写的都是**这个仓库特有的**约定，
> 通用开源礼仪（fork、分支、PR 描述）这里不赘述。

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

需要 Go 1.26+、Python 3.12+、Node 20+、PostgreSQL、Redis。**一个容易踩的坑**：

> 仓库根的 `vendor/` 放的是**本地参考源码**（不随仓库分发、不含 `vendor/modules.txt`），
> 而 Go 只要看到 `vendor/` 目录就会按 vendor 模式做一致性检查并报 `inconsistent vendoring`。
> **本机有该目录时**，`go build` / `go test` 必须加 `-mod=mod`：
> `go build -mod=mod ./...`。干净检出的仓库里没有这个目录，不需要该参数。
> 想一劳永逸：`go env -w GOFLAGS=-mod=mod`（只影响你的机器，不进仓库）。

## 3. 提交前必须本地跑过

CI 会在 PR 上跑这些；本地先跑能省一轮往返。**失败即阻断合入**：

```bash
# Go
go build -mod=mod ./... && go vet -mod=mod ./...
go test -mod=mod ./internal/...

# Python
cd python-engine && python -m pytest -q && ruff check . && mypy app/

# 前端
cd frontend-vue && npm run build      # 内含 8 个契约守卫（见下）与 vue-tsc
npm test

# 文档/仓库守卫（改过 .md 就跑）
python scripts/check_doc_links.py
python scripts/check_doc_line_refs.py
python scripts/check_md_tables.py
python scripts/check_source_encoding.py
```

> 嫌麻烦？`make check` 是上面 Go + 文档部分的快检（几十秒）；`make check-all` 再加
> Python（pytest / ruff / mypy）与前端（build / test），耗时数分钟。

### 这些守卫在拦什么（都属于「静默错」类，人眼看不出来）

| 守卫 | 拦的是 |
|---|---|
| `check_doc_links.py` | 文档里引用不存在的路径（**新增断链即失败**，存量走基线且只许缩小） |
| `check_doc_line_refs.py` | `文件:行号` 引用漂移（代码改动后行号会变） |
| `check_md_tables.py` | Markdown 表格列数不一致（渲染不报错、只会静默错位） |
| `check_source_encoding.py` | U+FFFD、非法 UTF-8、**UTF-8 BOM**（BOM 会让 Dockerfile/Makefile 首行失效） |
| `check-i18n.mjs` / `check-i18n-keys.mjs` | 前端新增硬编码中文、三语言键集不对齐 |
| `check-a11y.mjs` | 非交互元素绑 `@click` 却不带 role（键盘不可达） |
| `check-theme-token-*.mjs` | 硬编码颜色、主题 token 集合漂移 |

> 守卫是**棘轮**：基线只降不升。确实要接受新债务时才用各脚本的 `--write-baseline`，
> 并在 PR 里说明理由。

## 4. 代码约定（这个仓库特有的几条）

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

## 5. 提交与 PR

- 一个 PR 做一件事；重构与行为变更分开提。
- 提交信息用 `type(scope): 摘要`（`feat` / `fix` / `refactor` / `docs` / `test` / `chore`）。
  正文写**为什么**与**影响面**（多副本？迁移？配置变更？）。
- **行为变更**（默认值、fail-open/closed、对外契约）必须在 PR 描述里单列一节，
  并同步文档。仅改代码不改文档的 PR 通常会被要求补文档。
- 新增依赖需要说明理由；能不加就不加（依赖越少越可审计）。

## 6. 报告安全问题

**不要**开公开 issue。请按 [SECURITY.md](SECURITY.md) 的私有渠道报告。

## 7. 第一次贡献？

从 [入门任务清单](docs/good-first-issues.md) 挑一条 —— 它们都有**机械可判定的验收方式**，
不需要先理解整个架构。不确定就开 draft PR 问，不用憋到最后。

## 8. English summary

Contributions are welcome. This repository is documented primarily in Chinese; the README
has an English summary. Before opening a PR: read `docs/architecture.md`, run the checks in
§3 locally, and keep one PR to one change. Security issues must go through the private
channel in [SECURITY.md](SECURITY.md), never a public issue.
