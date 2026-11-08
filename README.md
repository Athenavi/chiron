# Chiron

<p align="center"><img src="assets/chiron-logo-detail.svg" width="180" alt="Chiron"></p>


[![CI](https://github.com/Athenavi/chiron/actions/workflows/ci.yml/badge.svg)](https://github.com/Athenavi/chiron/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Go](https://img.shields.io/badge/Go-1.26-00ADD8?logo=go&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white)

**多租户 SaaS AI Agent 平台**：Go 网关 + Python AI 引擎 + Vue 3 前端 三层架构。
对话、Agent、技能、知识库、插件与多层级记忆一体化；轨迹可循、过程可见 ——
**而且真的能跑第二个副本。**

<!-- 首屏截图：把文件放进 docs/images/ 后取消下面两行的注释（规格见 docs/images/README.md）。
     现在用注释包住，是因为图片尚未入库 —— 直接引用会在 GitHub 上显示破图。
![Chiron 对话界面：思考过程与工具调用逐步可见](docs/images/screenshot-chat.png)
-->

### 一条命令看到效果

```bash
python scripts/demo_up.py
# 打开 http://localhost:3000 —— 注册的第一个账号自动成为系统管理员（owner）
```

自带 PostgreSQL / Redis 与自动迁移，密钥随机生成，**不需要先填任何配置**。
想真的跑通对话，配一个模型 key 即可（也可以在登录后的「系统设置 → 服务提供商」里填）：

```bash
ANTHROPIC_API_KEY=sk-... python scripts/demo_up.py
```

> 这套体验栈刻意放弃生产属性（单副本、进程内沙箱、本地存储），只为让人尽快看到产品。
> 生产部署见下方 [快速开始 → 容器（多副本编排）](#容器多副本编排)。

<!-- 在线 Demo 占位：等服务器就绪后替换掉这段注释。
     公开 Demo 必须做到三件事，否则会成为攻击面与成本黑洞（详见 docs/images/README.md 的录制纪律）：
       1) 只读或沙箱租户：不给 owner 权限，避免被当成免费 API 刷；
       2) 每日定时重置数据库：否则垃圾数据会永久累积；
       3) 模型 key 走独立额度并在后台设日上限：公开可用的 key 必然被刷。

> **在线试用**：<https://demo.example.com>（每天 UTC 00:00 重置数据，只读演示账号）

-->

### 能用它做什么

| 场景 | 你得到什么 |
|---|---|
| **对内交付 Agent 能力** | 一套实例服务全公司：成员各自建 Agent、挂知识与技能，管理员统一管配额与审计 |
| **对外做 Agent SaaS** | 租户隔离（连数据库层都有租户上下文）、计费幂等、邀请码与套餐、按租户限流 |
| **私有化部署** | 数据不出内网；工具执行下沉到独立受限容器，沙箱不可用时**拒绝启动**而非偷偷降级 |

> **它不是什么**：不是低代码可视化搭建平台，也不是模型评测/微调平台。
> 它假设你要的是"**多人、多租户、能自己部署**"的 Agent 底座。

## 架构

```
浏览器 ─► 前端(Nginx/Vue 3, 宿主机 :3000) ─► Go 网关(cmd/chiron, :8080)
                                                    │
                                                    ▼
                                    Python 引擎(python-engine, :8000)
                                                    │
              PostgreSQL · Redis ◄──────────────————┴───────► S3（云端）/ 自托管 RustFS
```

| 组件 | 目录 | 端口 | 职责 |
|---|---|---|---|
| Go 网关 | `cmd/chiron`、`internal/*` | 8080（容器内，不发布宿主机端口） | 认证、路由、限流、计费、管理后台、SSE 转发 |
| Python 引擎 | `python-engine/` | 8000（容器内） | 对话、Agent、知识库、技能、记忆、MCP |
| 前端 | `frontend-vue/` | 5173（dev）/ 80（容器） | Vue 3 + TypeScript + Vite；nginx 同源反代 `/v1`、`/events`、`/ws` |
| 数据库 | `migrations/` | 5432 | PostgreSQL（Alembic 迁移；摘要向量用 `float8[]`，**不需要** pgvector 扩展） |
| 缓存/队列/事件 | — | 6379 | Redis（会话、限流、后台任务、SSE 事件、run 归属） |

## 快速开始

> 只想先看看效果：回到上方[一条命令看到效果](#一条命令看到效果)，不需要读本节。
> 本节是**真正部署**的路径 —— 数据库自备、沙箱隔离生效、可横向扩副本。

### 本地开发（非容器）

```bash
python scripts/init.py           # 交互式初始化：生成 APP_SECRET、写 .env、自检 PostgreSQL/Redis
python run.py setup              # 安装 Python / 前端依赖
python -m alembic upgrade head   # 数据库迁移（需 PostgreSQL 可达）
python run.py start              # 网关 :8080 + 引擎 :8000 + 前端 :5173
# 打开前端注册第一个账号 —— 它自动成为系统管理员（owner）
```

> **本地开发需先退出沙箱隔离**：引擎默认 `SANDBOX_BACKEND=service`（把工具执行下沉到
> 独立受限容器，见 [多实例部署指南](docs/deployment-multi-instance.md)），而本地没有该容器 ——
> 启动探测失败会**拒绝启动**（fail-closed）。请在 `.env` 设 `SANDBOX_BACKEND=local`
> （或用 `DEGRADED_MODE=true` 允许回退进程内执行）。

> 非交互场景：`python scripts/init.py --check`（只自检）、
> `python scripts/init.py --set POSTGRES_DSN=... --migrate`（写 .env 并执行迁移）。

### 容器（多副本编排）

```bash
cp .env.example .env             # 填写 compose 必填项（见下）
python scripts/init.py --check   # 可选：自检必填项与 PostgreSQL/Redis 连通性
# 1) 先对目标 PostgreSQL 执行迁移（数据库由云厂商/DBA 维护，不在 compose 内）
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://user:pwd@your-pg:5432/dbname' \
  python -m alembic -c alembic.ini upgrade head
# 2) 启动应用层
docker compose up -d redis  # 对象存储走云端 S3（配好 .env）；自托管则加 --profile selfhosted-s3
docker compose up -d --scale gateway=2 --scale python-engine=2
# 对外入口是 frontend 容器的 nginx：http://localhost:3000
```
### 能不能上生产？
多副本部署、依赖门禁、就绪探针与伸缩边界的完整说明见
[多实例部署指南](docs/deployment-multi-instance.md)。

一次对话请求从浏览器到引擎、再回到浏览器的完整链路（每步都给出代码位置与自查命令）见
[架构与请求链路](docs/architecture.md)。

## 关键配置

| 变量 | 说明 |
|---|---|
| `APP_SECRET` | **必须**：部署级主密钥（≥32 字符）。派生 `JWT_SECRET`/`INTERNAL_TOKEN`，并加密后台敏感配置（丢失后已加密配置无法解密） |
| `POSTGRES_DSN`、`REDIS_ADDR`（引擎用 `REDIS_URL`） | 基础设施连接串 |
| `DEGRADED_MODE` | 默认 `false`：Redis 不可用时网关与引擎**拒绝启动**；仅单机开发设 `true` 才允许进程内降级 |
| `MCP_POOL_ENABLED` | 默认 `false`：每个开启的引擎副本会为活跃用户持有 MCP 连接（副本数 × 活跃用户 × server），需要时显式开启 |
| `MCP_MAX_USERS_PER_INSTANCE` / `MCP_MAX_CONNECTIONS_PER_INSTANCE` | 每实例 MCP 连接预算（默认 20 / 50，`0`=不限）；超限时只服务最近活跃的用户，跳过计数见 `mcp_pool_rejected_total` |
| `MCP_OWNER_LEASE_ENABLED` | 默认 `false`：开启后每个活跃用户只由一个引擎实例持有 MCP 连接，其它实例经 Redis 转发调用（连接数从 实例×用户×server 降为 用户×server），详见部署文档 |
| `ENGINE_ADVERTISE_URL` | 引擎自注册地址；设置后网关可把某 session 的 run 请求（审批/取消）路由到持有它的实例 |
| `TURN_RETENTION_DAYS` / `TASK_IDEMPOTENCY_RETENTION_DAYS` | 保留策略天数（默认 30），防长期运行表膨胀 |
| `ALLOW_SCHEMA_DRIFT` | 默认 `false`：启动时校验数据库 migration 版本，不一致**拒绝启动**（迁移超前/回滚场景可设 `true` 放行） |

## 常用命令

```bash
go build -mod=mod ./...                   # Go 编译（-mod=mod：排除 vendor/ 参考源码目录）
go test -mod=mod ./...                    # Go 测试
python -m pytest python-engine/tests -q   # 引擎测试（默认跳过 integration/live）
# 集成与真实依赖用例：需要本地 PostgreSQL + Redis（CI 由 job services 提供）
python -m pytest python-engine/tests -q -m "integration or live"
python -m pip install -r requirements-migrate.txt   # 迁移依赖（仅迁移需要）
python -m alembic upgrade head            # 迁移到最新（发布流程/DBA 执行）
python -m alembic heads                   # 迁移链检查（应只有一个 head）
docker compose config --quiet             # compose 配置校验
make build                                # 见 Makefile（fmt/lint/test/build） 
make check                                # 提交前快检：Go build/vet + 4 个文档守卫（几十秒）
make check-all                            # 全量：再加 Python(pytest/ruff/mypy) 与前端(build/test)
make dev-up                               # 容器一键起（单副本）；make dev-up-scale 起 2+2 验证跨实例语义
```

> **`go` 命令请保留 `-mod=mod`**：仓库的 `vendor/` 下放的是参考源码（`vendor/DeepSeek-Reasonix`，
> 不含 Go 依赖清单 `vendor/modules.txt`），而 Go 只要看到 `vendor/` 目录就会按 vendor 模式做
> 一致性检查并报 `inconsistent vendoring`。`run.py`、`Makefile`、`Dockerfile` 已在编译入口
> 显式排除该目录，手动执行 `go build` / `go test` / `go vet` 时同样需要 `-mod=mod`。

> 以上命令是本地速查；**提交前验收请以 CI 为准**，逐条对应关系见
> [贡献与验收流程](CONTRIBUTING.md)。

## 目录结构

```
cmd/               网关入口（chiron）
internal/          Go 网关实现（api、auth、billing、broadcast、db、engine、session、storage…）
python-engine/     引擎实现（app/agent、queue、api、tools、knowledge、memory…）
frontend-vue/      Vue 3 前端（nginx 反代 /v1、/events、/ws、/submit、/cancel、/media 到网关）
migrations/        Alembic 迁移（手写 SQL；schema 直接改数据库，无 ORM 生成链路）
market/skills/     内置技能资产（`{name}/SKILL.md` 目录型技能）。运行时经 `SkillStore` 的
                   内置源读取（scope=builtin，见 python-engine/app/skill/skillmd.py）：
                   本地开发从仓库根解析；**容器部署必须挂载**（compose 已只读挂载并设
                   `CHIRON_BUILTIN_SKILLS_PATH`）。源缺失会告警、技能列表不含内置技能
```

## 数据库迁移

**应用不自行迁移**（网关启动只做只读的 schema 版本校验）。迁移是发布流程的一次性步骤，
由 CI 流水线或 DBA 在受控窗口执行：

```bash
python -m pip install -r requirements-migrate.txt      # alembic/SQLAlchemy/psycopg2/cryptography/dotenv
DATABASE_DSN='postgresql://user:pwd@host:5432/db' \
  python -m alembic -c alembic.ini upgrade head        # 在线迁移
python -m alembic -c alembic.ini upgrade head --sql > upgrade.sql   # 离线：生成 DDL 交 DBA 审阅执行
```

- **单一权威基线 + 增量迁移**：`migrations/versions/` 只有一个**基线** `0001_authoritative_baseline`，其 DDL
  取自稳定后的数据库（`pg_dump --schema-only`），包含全部 79 张业务表与默认套餐种子数据；此后的 schema
  变更一律以**增量迁移**追加到同一目录，当前 head 以 `python -m alembic -c alembic.ini heads` 为准；
- **应用侧不写 DDL**：Go 的 `EnsureTables` / `InitTable` / `episodes` 建表与 Python 的建表逻辑全部改为
  **只读校验**（`VerifySchema` / `VerifyTable` / `ensure_tables`），缺失时明确报错并提示跑迁移；
- **升级既有库**：表已齐全的环境执行 `alembic stamp head`（只改版本号、不重放 DDL）；
  全新库直接 `alembic upgrade head`；
  > **每次向 `migrations/versions/` 新增迁移后，都必须复核本段。** 启动校验比对的是「代码期望的 head」与
  > 「数据库 `alembic_version`」（`internal/db/schema_version.go` 的 `CheckSchemaVersion`），
  > 把库 stamp 到**过期**的 revision 会让网关与引擎直接拒绝启动（仅 `ALLOW_SCHEMA_DRIFT=true` 放行）。
- 需要 `alembic.ini`、`migrations/` 在运行目录内（迁移为手写 SQL，`env.py` 不再加载 ORM 元数据）；
- 启动校验：比对 `migrations/versions` 的 head 与数据库 `alembic_version`，不一致时拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可放行）；
- 迁移链必须**单一 head**（分叉会导致启动校验失败）。

## 贡献

本地验收命令与 CI 五个 job 的逐条对应、提交信息约定见
[贡献与验收流程](CONTRIBUTING.md)；失败响应的 `code` 契约与三语言同步要求见
[错误码契约](docs/error-codes.md)。

## 许可

MIT（见 [LICENSE](LICENSE)）

## English summary

Chiron is a **multi-tenant AI agent platform** built for horizontal scale:
a Go gateway (auth, rate limiting, billing, SSE), a Python engine (agent runtime,
RAG, memory, MCP), and a Vue 3 frontend.

What makes it different from demo-grade agent frameworks is what happens with a **second replica**:
cross-instance session locks, SSE replay between replicas, run-affinity routing for approvals,
idempotent billing, crash-takeover of queued work, and tool execution isolated in a separate
sandbox container (fail-closed — the engine refuses to start if the sandbox is unreachable).

### Try it in one command

```bash
python scripts/demo_up.py   # then open http://localhost:3000
```

Brings up PostgreSQL, Redis, runs migrations, and starts the gateway, engine and frontend
with randomly generated secrets — no configuration needed. The first account you register
becomes the system owner. To actually chat, pass a model key:
`ANTHROPIC_API_KEY=sk-... python scripts/demo_up.py`.

This stack is for evaluation only (single replica, in-process sandbox, local storage).
For a real deployment see [docs/deployment-multi-instance.md](docs/deployment-multi-instance.md).

- Docs: [docs/architecture.md](docs/architecture.md) (Chinese; English summary above)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security reports: [SECURITY.md](SECURITY.md) — private channel only
- License: MIT

