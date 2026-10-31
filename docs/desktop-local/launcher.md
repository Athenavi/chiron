# 本地数据栈引导器（`chiron-launcher`）

`desktop-local/app/launcher.py` 的架构决策记录。它回答一个问题：
**为什么本地模式要多一个进程，而不是让 Go 网关自己把 PG/Redis 拉起来？**

契约基线仍是 [`contracts.md`](./contracts.md)（§1 模式语义、§3.1 环境变量、§4 本地 API）。
本文只写"为什么这么切分"和"怎么排查"。

## 1. 结论：职责二分

```
Electron main
  │
  ├─ spawn ──> chiron-launcher（Python，本文件）   ← 只碰数据栈
  │               ├─ embedded PostgreSQL（initdb / start / migrate / stop）
  │               └─ bundled Redis
  │               └─ stdout 一行 JSON：{event:"launcher-ready", pg_dsn, redis_addr, ...}
  │
  └─ 拿到 DSN/端口后 spawn ──> chiron.exe（Go 网关） ← 唯一业务入口
                                  └─ 全部 /v1/** 路由
```

| | 引导器 | Go 网关 |
|---|---|---|
| 职责 | PG / Redis 的生命周期与迁移 | 全部业务路由 |
| 对外端口 | **没有**（不起 HTTP） | `PORT`（默认随机挑空闲） |
| 生命周期 | 先起，后停 | 后起，先停 |
| 失败语义 | 非零退出 | 非零退出 |

## 2. 为什么不让 Go 网关自己起 PG/Redis

网关对 PG/Redis 是**硬依赖**，起不来直接 `FATAL` 退出：

```
FATAL PostgreSQL is required but unavailable
FATAL redis is required but unavailable
```

它没有"我来 initdb"的能力，也刻意不该有。而 embedded PG 的脏活（挑空闲端口、
`initdb`、`pg_ctl start`、等 socket 就绪、跑 alembic、停机时干净 `pg_ctl stop`）
在 Python 侧已经做实了，见 [`embedded-pg.md`](./embedded-pg.md) 的实测数据。

在 Go 里重写一遍等于两套 initdb 逻辑并存 —— 一旦某天两边对"数据目录布局"的理解
分叉，用户看到的是"升级后本地数据打不开"。让引导器独占这块，是刻意的单点。

## 3. 三个刻意的设计

### 3.1 不继承 FastAPI：没有端口，就没有"实现了一半"的空间

引导器跑 asyncio + 两个存储类，**不起 HTTP 服务**。

理由：一旦它自己实现 `/v1/**` 端点当"本地后端"，就与 Go 网关成了两套并存的实现，
覆盖面天然对不齐 —— 前端每加一个页面，本地就多一处 404 或 501，
用户感受到的就是"本地模式几乎什么都用不了"。

收窄成"只起数据栈"之后，它**没有路由可写**，也就没有再次分叉的余地。

### 3.2 连接信息走 stdout，不落盘

`pg_dsn` 是**真实** DSN（含密码），网关没有它起不来，所以不能打码。

安全边界是：该值只出现在 stdout 管道与网关进程内存里，**不落盘、不写日志**。
embedded PG 固定用 `postgres` 超级用户 + trust 认证，且只绑 `127.0.0.1`，
所以这个密码的价值仅限本机回环。

`--state-file` 是给人工排查留的显式出口（权限收到 `0600`），默认关闭。

### 3.3 就绪即宣告，不等业务

PG 起来 + 迁移跑完 + Redis 起来，就打印 `launcher-ready`。

网关能否起来是**下一步**的事。混在一起会让故障归因变难：用户看到"启动失败"时，
无法区分是数据栈起不来，还是网关自己崩了。分开之后，Electron 侧的日志分别带
`[launcher]` 与 `[gateway]` 前缀。

## 4. 输出契约

stdout **只认 JSON 行**，且必须 `flush`（父进程按行阻塞读取）。

```json
{
  "event": "launcher-ready",
  "pid": 12345,
  "version": "0.1.0",
  "data_dir": "C:\\Users\\<user>\\AppData\\Roaming\\Chiron\\local",
  "startup_ms": 549.9,
  "pg_dsn": "postgresql://postgres:@127.0.0.1:50165/chiron",
  "redis_addr": "127.0.0.1:50404",
  "pg_backend": "embedded-pack",
  "pg_port": 50165,
  "pg_safe_dsn": "postgresql://postgres:***@127.0.0.1:50165/chiron",
  "redis_backend": "bundled",
  "redis_port": 50404,
  "redis_lua": null,
  "migration": { "ok": true, "revision": "0003_system_settings_key_unique" }
}
```

失败时打一行 `{"event":"launcher-error","error":"..."}` 并**非零退出**。

⚠ **stdout 是混合流**：alembic 会往同一处打 `[Alembic] ...` 日志。父进程解析时
必须"逐行 `JSON.parse`，失败即跳过"，**不能**用"包含 `launcher-ready` 字样"之类的
模糊判断 —— 否则日志里出现该字符串就会被误判为就绪，于是拿着空 DSN 去起网关，
真正的错误被埋掉。这条约束在 `electron/local-stack.cjs::parseLauncherLine` 里实现，
并由 `electron/__tests__/local-stack.test.cjs` 钉住。

## 5. 生命周期

### 5.1 常驻与退出

就绪后引导器**常驻**，直到二者之一发生：

1. **stdin EOF** —— 父进程退出时管道关闭。这条路径跨平台都成立，是主路径。
2. 收到 `SIGINT` / `SIGTERM` —— Windows 上没有可靠的 `SIGTERM`，只作补充。

为什么必须盯 stdin：否则父进程被强杀后会留下"父进程没了但 PG/Redis 还在"的孤儿，
下次启动 `initdb` 就会撞上残留的 postmaster，表现为"上次还好，这次起不来了"。
所以 Electron 侧 spawn 时 **stdin 保持 `pipe`**（不能 `ignore`）。

### 5.2 停的顺序

**与启动相反**：先 Redis，再 PG。

```
启动： PG ──> migrate ──> Redis ──> 网关
停止： 网关 ──> Redis ──> PG ──> 引导器
```

网关先停是因为它持有到 PG/Redis 的连接池；先停 PG 会让网关刷出一串连接错误，
把真正的停机原因盖掉。

## 6. 排查

自检（不起 Electron，打印就绪信息后立刻停机）：

```bash
cd desktop-local
python -m app.launcher --data-dir "$LOCALAPPDATA/Chiron/local" --print-and-exit
```

常用开关：`--no-pg` / `--no-redis` / `--no-migrate`（隔离单个环节）、
`--state-file <path>`（把就绪信息另存一份，含密码，仅排障用）。

| 现象 | 常见原因 |
|---|---|
| `initdb` 失败 / 端口被占 | 上次的 postmaster 未退净：确认没有残留 `postgres.exe` |
| `launcher-error` 里含 `alembic` | 迁移失败，看 stderr 里的 alembic 输出 |
| 网关报 `PostgreSQL is required but unavailable` | 引导器没就绪就起了网关，或 DSN 解析失败 |
| 网关报 `APP_SECRET` 相关 | `APP_SECRET` 少于 32 字符（见 [`contracts.md`](./contracts.md) §3.1） |

## 7. 相关文件

```
desktop-local/app/launcher.py              引导器入口（`chiron-launcher` 脚本）
desktop-local/app/storage/pg.py            embedded PG：起停、迁移、DSN
desktop-local/app/storage/redis.py         Redis：起停、端口
desktop-local/tests/test_launcher.py       入口契约测试（不依赖真实 PG）
frontend-vue/electron/local-stack.cjs      引导器 → 网关的交接与生命周期
docs/desktop-local/handshake.md            启动与握手的实际链路
docs/desktop-local/embedded-pg.md          PG 后端的实测数据
```
