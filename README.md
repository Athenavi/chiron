# Chiron

多租户 SaaS AI Agent 平台：**Go 网关 + Python AI 引擎 + Vue 3 前端** 三层架构。
对话、Agent、工作流、技能、知识库、插件与多层级记忆一体化；轨迹可循、过程可见。

## 架构

```
浏览器 ─► 前端(Nginx/Vue 3, 宿主机 :3000) ─► Go 网关(cmd/chiron, :8080)
                                                    │
                                                    ▼
                                    Python 引擎(python-engine, :8000)
                                                    │
              PostgreSQL(pgvector) · Redis ◄───────┴───────► Milvus · MinIO/S3
```

| 组件 | 目录 | 端口 | 职责 |
|---|---|---|---|
| Go 网关 | `cmd/chiron`、`internal/*` | 8080（容器内，不发布宿主机端口） | 认证、路由、限流、计费、管理后台、SSE 转发 |
| Python 引擎 | `python-engine/` | 8000（容器内） | 对话、Agent、工作流、RAG、技能、记忆、MCP |
| 前端 | `frontend-vue/` | 5173（dev）/ 80（容器） | Vue 3 + TypeScript + Vite；nginx 同源反代 `/v1`、`/events`、`/ws` |
| 数据库 | `migrations/`、`shared/models/` | 5432 | PostgreSQL + pgvector（Alembic 迁移 + 生成式 ORM 模型） |
| 缓存/队列/事件 | — | 6379 | Redis（会话、限流、后台任务、SSE 事件、run 归属） |

## 快速开始

### 本地开发（非容器）

```bash
python scripts/init.py           # 交互式初始化：生成 APP_SECRET、写 .env、自检 PostgreSQL/Redis
python run.py setup              # 安装 Python / 前端依赖
python -m alembic upgrade head   # 数据库迁移（需 PostgreSQL 可达）
python run.py start              # 网关 :8080 + 引擎 :8000 + 前端 :5173
# 打开前端注册第一个账号 —— 它自动成为系统管理员（owner）
```

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
docker compose up -d redis minio etcd milvus
docker compose up -d --scale gateway=2 --scale python-engine=2
# 对外入口是 frontend 容器的 nginx：http://localhost:3000
```

多副本部署、依赖门禁、就绪探针与伸缩边界的完整说明见
[多实例部署指南](docs/deployment-multi-instance.md)。

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
python -m pytest python-engine/tests -q   # 引擎测试
python -m pip install -r requirements-migrate.txt   # 迁移依赖（仅迁移需要）
python -m alembic upgrade head            # 迁移到最新（发布流程/DBA 执行）
python -m alembic heads                   # 迁移链检查（应只有一个 head）
docker compose config --quiet             # compose 配置校验
make build                                # 见 Makefile（fmt/lint/test/build） 
```

> **`go` 命令请保留 `-mod=mod`**：仓库的 `vendor/` 下放的是参考源码（`vendor/DeepSeek-Reasonix`，
> 不含 Go 依赖清单 `vendor/modules.txt`），而 Go 只要看到 `vendor/` 目录就会按 vendor 模式做
> 一致性检查并报 `inconsistent vendoring`。`run.py`、`Makefile`、`Dockerfile` 已在编译入口
> 显式排除该目录，手动执行 `go build` / `go test` / `go vet` 时同样需要 `-mod=mod`。

## 目录结构

```
cmd/               网关与 CLI（chiron 网关；chiron-cli 管理本地启动的进程）
internal/          Go 网关实现（api、auth、billing、broadcast、db、engine、session、storage…）
python-engine/     引擎实现（app/agent、queue、api、tools、workflow、memory、rag…）
frontend-vue/      Vue 3 前端（nginx 反代 /v1、/events、/ws、/submit、/cancel、/media 到网关）
migrations/        Alembic 迁移；ORM 模型由 configs/orm/V1/models.yaml 经
                   scripts/generate_orm_models.py 生成到 shared/models/
market/skills/     内置技能市场内容（SKILL.md 等，运行时读取）
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
  取自稳定后的数据库（`pg_dump --schema-only`）；此后的 schema 变更一律以**增量迁移**追加到同一目录，
  当前 head 以 `python -m alembic -c alembic.ini heads` 为准。此前迁移链只覆盖 12 张表、库里却有 80 张
  （其余由 `init.sql`、Go 侧启动建表与一份已丢失的早期迁移共同建立），已整体收敛；
- **应用侧不写 DDL**：Go 的 `EnsureTables` / `InitTable` / `episodes` 建表与 Python 的建表逻辑全部改为
  **只读校验**（`VerifySchema` / `VerifyTable` / `ensure_tables`），缺失时明确报错并提示跑迁移；
- **升级既有库**：表已齐全的环境执行 `alembic stamp head`（只改版本号、不重放 DDL）；
  全新库直接 `alembic upgrade head`；
  > **每次向 `migrations/versions/` 新增迁移后，都必须复核本段。** 启动校验比对的是「代码期望的 head」与
  > 「数据库 `alembic_version`」（`internal/db/schema_version.go` 的 `CheckSchemaVersion`），
  > 把库 stamp 到**过期**的 revision 会让网关与引擎直接拒绝启动（仅 `ALLOW_SCHEMA_DRIFT=true` 放行）。
- 需要 `alembic.ini`、`migrations/`、`shared/models/` 三者在运行目录内（`migrations/env.py` 会加载 ORM 元数据）；
- 启动校验：比对 `migrations/versions` 的 head 与数据库 `alembic_version`，不一致时拒绝启动（`ALLOW_SCHEMA_DRIFT=true` 可放行）；
- 迁移链必须**单一 head**（分叉会导致启动校验失败）。

## 许可

MIT（见 [LICENSE](LICENSE)）
