# embedded PostgreSQL（PyPI `embedded-postgres` 后端）

`desktop-local/app/storage/pg.py` 的第一优先后端：**pip 装一个 wheel，自带整套 PostgreSQL
二进制**，不需要系统装 PostgreSQL、不需要 Docker、不需要管理员权限。

> 本文的数字全部来自本机实测（Windows 11 / CPython 3.14 / `embedded-postgres==18.6.3`），
> 不是估算。三端支持里只有 `win_amd64` 是这次真跑过的，其余来自 PyPI 元数据与上游 README，
> 见 §4 的“已验证 / 未验证”标注。

## 1. 实测结论

```
cd desktop-local
python scripts/verify_p0.py --keep --json            # 报告真实 backend
python scripts/verify_p0.py --require-backend embedded-pack   # 要求必须走这条后端
```

本机实测（`--require-backend embedded-pack` 通过；下表的耗时是多次运行的范围，不是最好一次）：

| 项目 | embedded-pack | 同机 binaries（系统 PG 18） |
|---|---|---|
| backend | `embedded-pack` | `binaries` |
| 进程启动（首次 initdb + start） | **3.9 – 4.6 s** | 4.1 – 6.1 s |
| 同目录重启（已有 pgdata） | **0.80 – 0.92 s** | 2.2 s |
| 停止 | 0.14 s | — |
| `alembic upgrade head`（3 个 revision） | 553 – 762 ms | 581 – 685 ms |
| 端到端冷启动（起 PG + 迁移 + Redis 探测） | **6.5 – 7.2 s** | 7.1 – 13.4 s |
| data-dir 体积（含迁移后的库） | 65.3 MiB | 66.4 MiB |

两者跑出来的 `alembic_version` 一致（`0003_system_settings_key_unique`），停机重启后
探针行与迁移版本都还在 —— 契约 §5 第 1、2 项。Redis/fakeredis 那一项与 PG 后端无关。

**回退路径也实测过**：把 `embedded-postgres` 卸掉再跑，输出的是
`backend=binaries（实测）` 加上

```
回退原因（更优先的后端为什么没用上）：
  - embedded-pack: RuntimeError: No module named 'embedded_postgres'
    （装包：pip install 'embedded-postgres>=18.6.3,<19'）
```

—— 也就是说“包不可用”时不会伪造 embedded-pack 成功，而是如实说走了 binaries 和原因。

## 2. 真实 API

`embedded-postgres` 18.6.x 的公开面很小（`dir()` 实测）：

```python
import embedded_postgres as ep

ep.get_server(pgdata, *, cleanup_mode="stop")   # 构造即 initdb + pg_ctl start，返回句柄
ep.PostgresServer(pgdata, *, cleanup_mode="stop")  # 同一个东西的类

srv = ep.get_server(data_dir / "pg")
srv.get_uri()                # postgresql://postgres:@127.0.0.1:<port>/postgres
srv.get_uri("chiron")        # 换成自己的库名
srv.psql("select 1")         # 便捷执行（走包内 psql）
srv.cleanup()                # 停服务器；pgdata 保留（cleanup_mode="stop"）
```

要点：

- **没有** `port` / `user` / `password` / `database` 参数。端口是包内部挑的空闲端口，
  超级用户固定 `postgres`，`initdb --auth=trust`（无口令），只在 loopback/socket 上监听。
- 构造即启动。`get_server()` 要求 pgdata 的**父目录已存在**。
- `cleanup_mode`：`'stop'`（默认，停服务器留数据）/ `'delete'`（连数据一起删）/ `None`（不管）。
  另外它自己 `atexit` 注册了 `_cleanup`，进程正常退出会兜底停服。
- `dir()` 里那一堆 `pg_ctl` / `initdb` / `psql` / `postgres` / `libpq.dll` … 是**包内二进制
  的包装函数**，可调用但**不是**服务器入口。`pg.py` 靠 `__module__` 以 `._commands` 结尾
  把它们排除掉，避免把 `psql(...)` 当成“启动入口”。

因此 `pg.py` 的做法是：先 `probe_embedded_api()` 记录 `dir()` + 候选入口的真实签名，
再 `resolve_embedded_entry()` 选入口，最后 `_call_entry()` **按签名**适配调用
（`pgdata=` 关键字 → 位置参数 → 历史 kwargs 兜底）。失败时把真实异常与这份探测结果
原样写进 `PostgresUnavailable.attempts` / `.probes`。

**参数必须按 `pgdata` 传，不要按关键字猜**：按 `data_dir=/port=/user=...` 猜参数的变体
在真实入口上全部 `TypeError`，会让 embedded-pack 永远"不可用"并静默回退到系统 `initdb/pg_ctl`。

## 3. 体积（实测，Windows）

| 项 | 大小 |
|---|---|
| wheel 下载（`win_amd64`） | 18.3 MB（18,321,678 bytes） |
| 安装后 `site-packages/embedded_postgres/` | 56 MiB |
| 其中 `pginstall/bin`（PostgreSQL 18.6 可执行文件） | 27.5 MB |
| 首次 initdb 后的 pgdata | ≈ 41 MB |
| 跑完迁移 + P0 探针表的整个 data-dir | 65.3 MiB |

对比：`binaries` 后端要求用户自己装 PostgreSQL（安装包 ~300 MB 起），且 data-dir 体积相同。
打包 Electron 时记得把 `embedded_postgres/pginstall` 从 asar 里排除（含 `.exe`/`.dll`，
必须保持可执行文件原样落盘）。

## 4. 三端 wheel 支持

上游每个平台发一个 `py3-none-<platform>` wheel（不分 Python 版本），版本号跟 PostgreSQL 走：

| 平台 | wheel tag | 状态 |
|---|---|---|
| Windows x86_64 | `win_amd64` | **已验证通过**（`18.6.3`） |
| macOS Apple Silicon | `macosx_11_0_arm64` | 上游 CI 报告通过；**本机未验证** |
| macOS Intel | `macosx_11_0_x86_64` | 上游 CI 报告通过；**本机未验证** |
| Linux x86_64 | `manylinux_2_28_x86_64` | 上游 CI 报告通过；**本机未验证** |
| Linux aarch64 | `manylinux_2_28_aarch64` | 上游 CI 报告通过；**本机未验证** |
| Linux armv7l（32 位树莓派） | `manylinux_2_31_armv7l` | 只跑上游的 smoke test；**本机未验证** |

没有：Windows 32 位、musl（Alpine）、FreeBSD、Python < 3.9。
Python ≥ 3.11（本仓库要求）没问题，包本身 `requires_python>=3.9`。

**POSIX 上的行为差异（尚未实测，代码已适配）**：非 Windows 平台它用 **unix socket**
（`pg_ctl -o '-h ""' -o '-k <socket_dir>'`），**不监听任何 TCP 端口**，`get_uri()` 返回
`postgresql://postgres:@/db?host=<socket_dir>`。所以 `pg.py` 只在 DSN 里解析出
`host:port` 时才等 TCP（`_tcp_endpoint()`），否则直接靠 SQL 轮询判断可用性；
`replace_database()` / `database_of()` 也改成先摘 `?query` 再切库名，否则 `?host=/tmp/x`
里的斜杠会把 DSN 切坏。

## 5. 与 `binaries` 后端的差异

| | `embedded-pack` | `binaries` |
|---|---|---|
| 前置条件 | `pip install 'embedded-postgres>=18.6.3,<19'` | PATH 或 `CHIRON_LOCAL_PG_BIN_DIR` 里有 `initdb`/`pg_ctl` |
| 版本 | 固定 18.6（跟 wheel 走） | 用户装什么就是什么（16/17/18…） |
| 连接用户 | **固定 `postgres`**（`--auth=trust`） | `CHIRON_LOCAL_PG_USER`（默认 `chiron`） |
| 口令 | 无（trust，仅 loopback/socket） | 有（本机也是 trust，DSN 里带占位口令） |
| 端口 | 包内部挑空闲端口，`CHIRON_LOCAL_PG_PORT` **不生效** | `CHIRON_LOCAL_PG_PORT`，0 = 随机 |
| 监听 | Windows：`127.0.0.1`；POSIX：unix socket | 始终 `127.0.0.1:<port>` |
| 失败面 | wheel 装不上 / 目标平台没 wheel / Windows 管理员权限的目录 ACL | PostgreSQL 没装 / 版本太老 |
| 日志 | `<pgdata>/log`（包自己写） | `CHIRON_LOCAL_DATA_DIR/logs/postgres.log` |

两处“配置不生效”不会被静默吞掉：`PostgresServer.notes` 会记下来，
`verify_p0.py` 会打印在“后端差异（配置在该后端下不生效的部分）”里，也进 JSON 的 `pg_notes`。

## 6. 失败时怎么读

```bash
python scripts/verify_p0.py --json | tail -1        # pg_backend / pg_notes / embedded_probe
```

`PostgresUnavailable` 里三样东西：

- `attempts`：逐条后端的真实错误。embedded-pack 那条会把 `dir()`、候选入口签名、
  最终实际调用形态、真实异常类型全带上，例如
  `embedded-pack: RuntimeError: PermissionError: invalid binary "...\initdb.exe": Permission denied（调用形态 get_server(pgdata=...)）`；
- `probes`：结构化探测结果（`module` / `version` / `dir` / `bundled_binaries` /
  `signatures` / `entry` / `call` / `error`），CI 里可直接断言；
- 末尾附 `PG_SUGGESTIONS` 的可执行替代方案。

`--require-backend embedded-pack|binaries|external`：默认不强制；指定后实际 backend 不符
就是 FAIL（退出码 1），不会把“回退到别的后端”当成功。注意：**这次没有通过 `external`
量任何东西**，`external` 只是 `POSTGRES_DSN` 的直连路径。

## 7. 已知限制

1. **Windows + 管理员账户**：上游 README 明确写了，`initdb` / `pg_ctl` / `postgres` 会以
   受限令牌重启自身，看不到只靠 Administrators 组可达的目录；而 CPython ≥3.12.4 的
   `tempfile.mkdtemp()` 恰好造出这种目录。症状是
   `invalid binary "...\initdb.exe": Permission denied`。本机是**非管理员**账户，未复现。
   以管理员身份运行时请用 `--data-dir` 指定一个普通目录（或 `icacls` 授权）。
2. **POSIX 的 unix socket 路径未实测**：代码按 `get_uri()` 返回的 DSN 走，逻辑上支持，
   但只在 Windows 上跑过（见 §4）。
3. **`CHIRON_LOCAL_PG_PORT` / `CHIRON_LOCAL_PG_USER` / `_PASSWORD` 在 embedded-pack 下不生效**，
   属于上游 API 限制（没有这些参数）。如果以后需要固定端口，只能在 `pg.py` 里自己写
   `postgresql.conf`（`port = N` + `listen_addresses`），目前未实现。
4. **包是 alpha**（Development Status :: 3 - Alpha，2026-09 才开始发版）。入口名换了也不要紧
   —— 探测会把它写进报错；但如果新版本改成异步 API，`pg.py` 需要跟着改。
5. **没有 SSL / ICU / readline**（上游 configure 参数），只适合本机回环，不要拿去当共享实例。
6. 上游 wheel 里**附带 pgvector 0.8.6**，如果之后要做向量检索，可以先看这里能不能直接用。
7. `verify_p0.py` 默认用 `tempfile.mkdtemp()` 建数据目录 —— 与风险 1 同一个坑，
   以管理员运行时请显式给 `--data-dir`。

## 8. 实现约束

1. 只把**真能执行 SQL** 的实例算启动成功（`_prepare()` 会连 `postgres` 库跑 `SELECT 1`
   再 `CREATE DATABASE`），起进程不算。
2. 每个后端都要能被单独关掉/换掉：`POSTGRES_DSN`（external）> embedded-pack > binaries。
3. 失败就报真实异常 + 探测结果，**不要把回退包装成成功**。
