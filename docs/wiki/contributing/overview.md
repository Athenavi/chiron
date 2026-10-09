# 贡献与验收

> **权威来源**：`docs/contributing.md`（贡献与验收流程）。本页只给「改完代码要过什么、怎么自查」。

## CI 的五个 job

| job | 工作目录 | 主要命令 |
|---|---|---|
| `go` | 仓库根 | `go build -mod=mod ./...` · `go vet -mod=mod ./...` · `go test -mod=mod ./... -count=1` |
| `python` | `python-engine/` | `ruff check .` · `mypy app/`（strict）· `python -m pytest -q -m "not integration"` |
| `frontend` | `frontend-vue/` | `pnpm run lint` · `pnpm run build`（`check:ui` 棘轮 → `vue-tsc -b` → `vite build`）· `pnpm run test` |
| `schema` | 仓库根 | `alembic heads` **必须恰好 1 个 head** · 离线渲染 DDL · ORM 生成物与迁移 DDL 一致 |
| `encoding` | 仓库根 | **八个根守卫**（见下） |

**本机真实栈**：`integration` 标记的用例需要真 PostgreSQL + Redis，还要**把网关也起起来**
（引擎侧统一客户端会走网关的 `/v1/internal/*`）—— 步骤见 `docs/contributing.md` §本机真实栈。

## 八个根守卫（提交前自己先跑一遍）

```bash
python scripts/check_source_encoding.py      # U+FFFD / 非法 UTF-8 / UTF-8 BOM
python scripts/check_tool_policy_parity.py   # Go ↔ Python（↔ 前端）必须同构
ruff check scripts/                          # 仓库根脚本
python scripts/check_doc_links.py            # 被引用的 docs/*.md 与相对 .md 链接都必须存在
python scripts/check_md_tables.py            # Markdown 表格列数必须一致
python scripts/check_error_code_keys.py      # Go Code* ↔ 三语言 errors.ts 键集一致
python scripts/check_orm_models.py           # shared/models 生成物与 models.yaml 一致
python scripts/check_compose_env.py          # compose 必填变量必须在 .env.example 出现
python scripts/check_orm_schema.py           # shared/models 列集合与迁移 DDL 一致（跑在 schema job）
```

**门禁的共同形态是「基线化 + 拦增量」**：存量债务走基线文件，**新增即失败**。
所以遇到红，先判断是「新增」还是「基线该缩小」—— **不要把基线调大**。

## 提交前自查清单

1. 改的是**契约**吗？先读对应文档（见 [架构](../core/architecture.md) 的契约索引），
   并确认门禁还在守它。
2. 三个语言面都改了吗？Go / Python / 前端之间**有成对的守卫**（工具策略、错误码、提供商目录）。
3. 迁移链**还是一个 head** 吗？
4. 文档里的**数值/行号**是否已经过期？（`scripts/doc_value_sweep.py`、
   `scripts/check_doc_line_refs.py` 是两个复核工具，不进 CI，但值得跑）
5. 提交信息按约定：`feat(database): 添加企业 Webhook 订阅功能` 这类**约定式**前缀。

## 两个"别踩"的坑

- **`goimports` 在整包编译不过时不会补 import**（它需要包可加载）⇒ 大拆分时会**静默无输出**，
  要「按限定符静态推导 + `go build` 兜底」。
- **本仓没开 eslint 的 `argsIgnorePattern`** ⇒ 形参写出来就要显式 `void`，否则 lint 会多出未使用参数警告。

## 延伸阅读

- [设计理念](../getting-started/philosophy.md) —— 为什么纪律要变成门禁
- `docs/contributing.md` —— 完整流程（含 mypy strict 接线手册）
