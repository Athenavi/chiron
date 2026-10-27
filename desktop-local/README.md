# desktop-local — Chiron 桌面端本地 core 后端（骨架 / P0）

本目录是**本地模式**后端的进程骨架：FastAPI 起在 `127.0.0.1`，按配置拉起本机
PostgreSQL 与 Redis 适配层，并跑通仓库根 `migrations/` 的迁移。
接口基线是 `docs/desktop-local/contracts.md` §4，**本目录不实现任何对话 / Agent / 知识库业务**
（第二批再做）。

```
desktop-local/
  pyproject.toml
  app/
    main.py            # create_app() + CLI（--data-dir / --port / --no-pg / --no-redis）
    config.py          # data-dir / port / inference-proxy / db、redis 开关（CHIRON_LOCAL_*）
    api/
      health.py        # GET /healthz   GET /readyz
      auth.py          # POST /v1/auth/session
      models.py        # GET /v1/models（占位 {"data": []}）
      events.py        # GET /events?session_id=  → text/event-stream（只发心跳）
    storage/
      pg.py            # embedded PostgreSQL 启停 + alembic upgrade head + health()
      redis.py         # fakeredis / 外部 Redis 适配 + 7 条命令逐项实测
  scripts/verify_p0.py # P0 五项验证（契约 §5）
  tests/               # 纯骨架单测（不需要真实 PG/Redis）
```

## 安装

```bash
cd desktop-local
python -m pip install -e ".[dev]"      # 需要 Python >= 3.11
```

> 依赖里额外带了 `SQLAlchemy` / `psycopg2-binary` / `python-dotenv`：仓库根
> `migrations/env.py` 自身 `import sqlalchemy` 与 `from dotenv import load_dotenv`，
> 且 alembic 走 psycopg2 方言。少了这三项，P0 第 1 项（`alembic upgrade head`）必然失败。
> `dev` 里额外带 `httpx`：`fastapi.testclient` 的传输层就是 httpx。

## 运行

```bash
# 默认：随机端口 + 默认数据目录（Windows: %LOCALAPPDATA%\Chiron\local）
python -m app.main

# 指定数据目录与端口（端口 0 = 随机）
python -m app.main --data-dir D:\chiron\local --port 0

# 只起 HTTP 骨架，不碰存储（调前端接线时最省事）
python -m app.main --no-pg --no-redis
```

启动成功后 stdout 会打印**一行 JSON**（Electron 主进程按行解析，别去猜端口）：

```json
{"event": "listening", "host": "127.0.0.1", "port": 51234, "pid": 1234,
 "version": "0.1.0", "data_dir": "...", "startup_ms": 4210.5, "storage_errors": []}
```

也可以交给 uvicorn factory 模式：`uvicorn app.main:create_app --factory --port 0`。

## 端点（契约 §4）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}`，不碰存储 |
| GET | `/readyz` | `{"status":"ok","pg":true,"redis":true,"auth":"disabled", ...}`；任一**已启用**存储不健康 → 503 + `status:"degraded"` |
| POST | `/v1/auth/session` | `{"data":{"token":"...","user":{"id":"local","name":"Local","role":"owner","tenant_id":1}}}` |
| GET | `/v1/models` | `{"data":[]}`（占位） |
| GET | `/events?session_id=` | `text/event-stream`，第一批只发 `event: heartbeat` |

`/readyz` 额外字段（都是实测值，不是常量）：`pg_backend`、`pg_version`、`pg_error`、
`redis_backend`、`redis_error`、`migration`（`alembic_version` / 耗时）、`startup_ms`、
`errors`（lifespan 里记下的逐条失败原因）。

## 配置

全部可用环境变量覆盖，前缀 `CHIRON_LOCAL_`：

| 变量 | 默认 | 含义 |
|---|---|---|
| `CHIRON_LOCAL_DATA_DIR` | 平台默认 | 数据目录（契约 §1 的 `userData/local`） |
| `CHIRON_LOCAL_HOST` | `127.0.0.1` | 监听地址（只绑 loopback） |
| `CHIRON_LOCAL_PORT` | `0` | 端口；`0` = 内核随机分配 |
| `CHIRON_LOCAL_INFERENCE_PROXY` | 空 | loopback 推理代理地址（W4 提供，本批只占位） |
| `CHIRON_LOCAL_PG_ENABLED` | `true` | 是否拉起 embedded PostgreSQL |
| `CHIRON_LOCAL_REDIS_ENABLED` | `true` | 是否启用 Redis 适配层 |
| `CHIRON_LOCAL_REDIS_URL` | 空 | 给了就连真实 Redis/Valkey，否则用 fakeredis |
| `POSTGRES_DSN` | 空 | 给了就**复用外部 PG**（不自己起进程），优先级最高 |
| `CHIRON_LOCAL_RUN_MIGRATIONS` | `true` | 启动时是否 `alembic upgrade head` |
| `CHIRON_LOCAL_PG_BIN_DIR` | 空 | `initdb`/`pg_ctl` 所在目录（找不到二进制时用） |
| `CHIRON_LOCAL_PG_DATABASE` / `_PG_USER` | `chiron` | 实例内的库名 / 超级用户名 |
| `CHIRON_LOCAL_SSE_HEARTBEAT_SECONDS` | `15` | SSE 心跳间隔 |
| `CHIRON_LOCAL_AUTH_ENABLED` | `false` | 第一批不校验 token；`/readyz` 报 `auth:"disabled"` |

数据目录布局：

```
<data-dir>/
  pg/            # embedded PostgreSQL 的 initdb 目录（契约要求）
  logs/postgres.log
  auth/device-token
```

## 验证

```bash
cd desktop-local
python -m ruff check .
python -m pytest -q
python scripts/verify_p0.py          # 契约 §5 五项；有 FAIL 时退出码 1
```

`verify_p0.py` 常用开关：`--data-dir <路径>`（复用/指定目录）、`--keep`（保留临时目录）、
`--skip-pg` / `--skip-redis`、`--json`（末尾多打一行机器可读摘要）。

### P0 五项怎么读

1. **PG 启动 + 迁移**：打印 `backend`（`external` / `embedded-pack` / `binaries`）、进程启动耗时、
   迁移耗时、`alembic_version`。迁移**复用仓库根 `migrations/`**，不复制、不改写。
2. **重启后数据仍在**：写入 `<data-dir>` 内一个探针表 → `pg_ctl stop` → 再起 → 比对行数与
   `alembic_version`。这一项只有在数据目录固定时才成立（`--data-dir`）。
3. **Redis 逐项**：`XADD / XREADGROUP / XACK / XCLAIM / XAUTOCLAIM / PUBLISH / SUBSCRIBE`
   各跑一次真实调用，PASS/FAIL 后面跟的是**真实返回或真实异常原文**。
4. **冷启动耗时 / 数据目录体积**：端到端毫秒数 + 递归体积（字节数与人类可读）。
5. **失败建议**：PG 失败给 `POSTGRES_DSN` / 官方二进制 / Docker 三条路；Redis 失败给 Valkey
   与"缩小 core 范围"两条路。

## 已知限制（不是待办清单，是当前事实）

- **Windows 上 embedded PostgreSQL 可能起不来**。`initdb` 需要可写的普通目录、不能以管理员
  身份运行；某些 `embedded_postgres` 包版本只提供 Linux/macOS 二进制。`pg.py` 因此按
  `external → embedded-pack → binaries` 三条路依次尝试，**只有真能执行 SQL 的实例才算成功**，
  三条都失败会把每一条的真实错误打出来。可行兜底：装 PostgreSQL 16 并设置
  `CHIRON_LOCAL_PG_BIN_DIR`，或直接给 `POSTGRES_DSN`。
- **fakeredis 的 Streams 能力是有缺口的**（历史版本上 `XAUTOCLAIM` / `XCLAIM` 的 pending
  语义、`XREADGROUP` 的阻塞读都不完整）。本仓库**不假设**它支持——`verify_p0.py` 逐项实测。
  不支持的项有两条兜底：外部 Valkey（`CHIRON_LOCAL_REDIS_URL`）或缩小 core 范围（不用消费组）。
- **PG 端口每次启动随机**（`CHIRON_LOCAL_PG_PORT=0`）。这是故意的：本机可能已跑着别的 PG。
  需要固定端口就显式给 `CHIRON_LOCAL_PG_PORT`。
- **迁移会屏蔽仓库 `.env` 的 `DATABASE_DSN`**。`migrations/env.py` 用
  `load_dotenv(override=True)` 读根目录 `.env`，若那份 `.env` 里有开发库 DSN，它会覆盖本地
  实例 DSN 造成"迁移打到开发库"。`pg.py` 在调用 alembic 期间把 `load_dotenv` 打成空操作，
  迁移后用 `alembic_version` 复核，复核不到就报错而不是假装成功。
- **没有鉴权**：第一批允许无鉴权启动（契约 §4），`POST /v1/auth/session` 只是发一个设备
  token 并落盘，没有任何接口校验它。只绑 `127.0.0.1` 是当前唯一的边界。
- **`/events` 只有心跳**，没有业务事件；断线重连由客户端 `EventSource` 负责。
- 本目录**故意不实现**对话 / Agent / 知识库，也不含 provider key 读写（W4 的
  `secrets.cjs` / `inference-proxy.cjs` 负责）。

## 与契约 §4 的偏差（已登记）

- 契约的目录树里没有 `app/api/events.py`，但 §4 的端点表要求 `GET /events`；本批把它单独成
  文件（`health.py` 只管 healthz/readyz），路由路径与契约完全一致。
- 依赖比任务清单多了 `SQLAlchemy` / `psycopg2-binary` / `python-dotenv`（迁移必需，见上）与
  dev 的 `httpx`（TestClient 必需）；没有删任何指定依赖。
