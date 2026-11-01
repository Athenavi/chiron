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

## 3. 几个刻意的设计

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

### 3.4 身份由网关铸造，不在壳里伪造

网关就绪后，前端第一件事是 `POST /v1/auth/session` 换一份**真 JWT**
（`internal/api/auth_local_session.go`）：本地库里没有那个 owner 账号就顺手铸一个，
已有就原样返回 —— 用户改了邮箱或降了角色，重启不会被覆盖回来。

铸出来的 owner 带 `password_set = FALSE` 与一段随机 bcrypt，所以它**不能**用口令登录，
只能从本机免口令换会话。局域网里的其它设备因此只能等本机用户先设过口令
（`POST /v1/auth/password` 在 `password_set=FALSE` 时跳过"校验当前口令"，这正是无需新增 UI 的设口令入口），
再用口令走 `POST /v1/auth/login`。

开关是 `LOCAL_BOOTSTRAP`，由 `electron/local-stack.cjs::LOCAL_BOOTSTRAP_ENV` 显式置 `1`；
网关侧默认关，云端部署压根不会注册这条路由（`internal/api/routes_auth.go`）。
**安全边界不在环境变量，而在处理链上**：只有 loopback 来源能免口令拿到 owner，
其它来源 403。这条分界就是"本机主人自动有个能进管理后台的账号"与
"局域网里任何人都能拿到 owner"的区别，也是本地模式得以与云端共用同一套
`requiresAdmin` / 网关 RBAC 判定的前提 —— 前端对本地模式没有任何身份特判。

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

第 1 条在 Windows 上有个坑，值得写下来免得再踩：**不要**用
`loop.connect_read_pipe(sys.stdin)`。Proactor 事件循环给匿名管道注册 IOCP 时直接
`OSError: [WinError 6] 句柄无效`，而且死在传输层的读回调里，`await` 那一步 catch 不到 ——
EOF 于是永远等不到（真机现象正是"Electron 正常退出后 postmaster 仍在监听"）。现在用的是
一个 daemon 线程做阻塞 `sys.stdin.buffer.read()`：父进程关掉写端后实测 1.4s 内返回，三个平台
共用同一条代码路径。线程必须是 daemon —— 走信号路径先结束时，还阻塞在 `read()` 里的线程会把
解释器拖住不退，那等于换个方向卡死。

还要记住 stdin EOF **不是**唯一的收尾手段，也不是万能的：壳侧的兜底是 `killProcessTree`，
它只能收走引导器的**子进程**（Redis 是），而 PG 由 `embedded_postgres` 经 `pg_ctl` 拉起、
不在进程树里，树杀碰不到 —— 只有引导器自己走到 `finally` 才会 `pg.stop()`。所以
`launcher=false` 这条日志的意思不是"引导器没退"那么简单，而是"**它没机会收尾，PG 可能还活着**"，
必须去 `netstat` 复核（见 §5.4 的验收口径）。

### 5.2 停的顺序

**与启动相反**：先 Redis，再 PG。

```
启动： PG ──> migrate ──> Redis ──> 引擎 ──> 网关
停止： 网关 ──> 引擎 ──> Redis ──> PG ──> 引导器
```

网关先停是因为它持有到 PG/Redis 的连接池；先停 PG 会让网关刷出一串连接错误，
把真正的停机原因盖掉。

引擎排在网关**之前**：网关起来时就要读 `PYTHON_ENGINE_ADDRESS` 建客户端，
地址必须已经定好。引擎与 PG、Redis 一样是**强依赖** —— 本地模式的聊天、知识库、
记忆、图谱全部实现在引擎侧，它起不来整条链就起不来，`startLocalStack()` 因此直接抛
（带 `reason` 与 stderr 尾部），不再"少了引擎也照样把网关拉起来"。

### 5.3 运行中引擎退出

引擎退出后**网关不会主动宣告**：它对引擎地址走的是被动收敛 —— 引擎崩掉时注册表 key
要等 TTL 60s 过期、网关每 15s 扫一次才把它从地址表里摘掉（`internal/engine/discovery.go`、
`python-engine/app/engine_registry.py:23-24`），既没有主动探活也没有熔断。这段窗口里
界面照常活着，只有聊天在 SSE 上回一句"服务暂时不可用"并把这一轮标成 failed ——
用户看不出任何原因。

所以壳侧另开一条通知通道：`local-stack.cjs::watchEngineExit()` 在引擎进程上挂
一次性 `exit` 监听，把 `{ code, signal, address, stderrTail, at }` 交给
`main.cjs::notifyEngineExit()`，后者写一条**带退出码**的 error 日志并弹一个
原生对话框（按钮「重启应用」真的能按）。两条刻意的约束：

- **正常退出应用时不提示**：`stopEngine()` 先立 `record.stopping = true` 再动手 kill，
  监听器看到这个标志就哑掉。顺序反了会先收到 `exit` 事件，用户每次关应用都看到一条
  "引擎崩溃"告警；
- **每轮运行只提示一次**：崩了重启再崩才是真问题，但一串窗口只会让人忽略全部。
  重新起栈成功时（`tryStartGoStack` 里）该标志复位。

### 5.4 退出回收：谁起的进程就找谁停

打包态一条链上有三套进程，**归属分两家**：

| 进程 | 归属 | 停止函数 |
| --- | --- | --- |
| Go 网关、python-engine | Electron 主进程的直接子进程 | `local-stack.cjs::stopLocalStack()` |
| PG、Redis、引导器本体 | **引导器 python 进程**（`embedded_postgres` / `pg_ctl` 起的） | 同左：stdin EOF → `_watch_stdin_eof`，兜底 `killProcessTree` |
| desktop-local 老 core | `components.cjs` | `components.stopCore()` || desktop-local 老 core | `components.cjs` | `components.stopCore()` |

前两套**不能互相顶替**。真机上的事故形状是：

```
23:32:17 [info] local backend stopped      ← 日志说停了
（此后 postgres.exe / redis-server.exe 仍在监听，data dir 仍被占着）
```

`stopLocalBackend()` 过去无条件调 `components.stopCore`。打包态没起 core，`state.core`
为空 ⇒ `stopCore()` 返回 false ⇒ 老实现把"没停成"和"停了"混成同一条成功日志。于是每次
退出都留下一套活着的 PG，下一次启动再拉起一套，两个实例抢同一个 data dir（`postmaster.pid`
冲突、崩溃恢复期 `pg_ctl -w` 超时，表现为"上次还好，这次起不来了"）。

现在的规则（`main.cjs::stopLocalBackend`，回归见 `__tests__/backend-stop.test.cjs`）：

- 启动成功时把归属写进 `localBackend.via`（`'go-stack'` / `'core'`），停止按它分派；
- 归属**不兜默认值**：`via` 为空就是"从没成功起过"，直说、不去叫任何一条链的停止函数；
- 链路未知、停止函数取不到、返回 `null` —— 一律 `return false` 并 warn「进程可能未被回收」，
  绝不打 `local backend stopped`；成功日志全函数只允许出现在真停掉之后那一处；
- 成功日志带逐条结果 `（gateway=… engine=… launcher=…）`，一行就知道谁没被回收；
- 停完把 `localBackend` 整份复位：死 `baseUrl` 与废 token 会在建窗时被注入 preload；
  「只停一次」的标记也只由本函数置位，调用点不再各写一份。

归属分派只解决"该叫谁停"。叫了之后**是不是真停了**，引导器那一侧还要自己复核一次：
`embedded_postgres` 的 `cleanup()` 判据是"登记表 `<pgdata>/.handle_pids.json` 里只剩我自己"，
而那份表**从不清理死 pid** —— 真机量到 14 条陈旧记录，判据永远不成立，`cleanup()` 一进门就
return，对外却什么都没报。现在 `pg.py` 在停之前裁掉"已退出"与"pid 被别的程序复用"两类句柄
（判不准一律算活着），停之后用"端口不再监听"验一次，还不行就 `pg_ctl -m fast` 兜底，
每一步结论进 `notes` 与 stderr。机制与实测数字见
[embedded-pg.md §9.1](embedded-pg.md)；同一个包"只等 10s"导致首启必失败一次的另一处兜底在 §9.2。
也因为这个，壳侧等引导器自己退的宽限是 `LAUNCHER_EXIT_GRACE_MS = 20_000`（fast shutdown +
端口释放要在几秒内完成，留足余量），宽限到点才落到 `killProcessTree`。

验证口径（Windows）：退出应用后 `netstat -ano | findstr LISTENING` 里不该再有本轮的
PG / Redis 端口，`tasklist` 里不该有 `postgres.exe` / `redis-server.exe` 残留。
手工清残留用随包的 `pg_ctl stop -m fast -D %APPDATA%\frontend-vue\local\pg`；
Redis 没有随包 `redis-cli`，用 core 那个 venv 的 `redis` 包发 `shutdown(save_now=False)`。

### 5.5 两个身份密钥由壳显式下发

本地模式有三把钥匙，只有第一把落盘：`APP_SECRET`（`<userData>/local/auth/app-secret`，64 位十六进制），
`JWT_SECRET` 与 `INTERNAL_TOKEN` 是它的两个派生子密钥
（`HMAC-SHA256(APP_SECRET, "chiron-jwt" / "chiron-internal")` → base64url 无 padding）。
Go、Python、Node 三边各写了一遍这个表达式，口径由同一组**跨语言黄金向量**钉住：
`frontend-vue/electron/__tests__/local-stack.test.cjs` 与 `config/derive_subsecret_test.go`
用同一个 `APP_SECRET = 'x'*32` 和同一对期望值，改任何一侧而没同步另两处，两边同时红。

关键在于**壳把派生结果也显式写进两个子进程的环境**（`local-stack.cjs::applyLocalModeSecrets`，
网关和引擎各调一次），而不是只传 `APP_SECRET` 让各自派生。因为三边读配置的口径不一样：

| 侧 | `.env` 搜索 | 打包态结果 |
|---|---|---|
| Go 网关 | `config/config.go::findFileUpward` 从 cwd **一路往上到文件系统根** | 安装目录树上任何一层有 `.env`（开发机上就是仓库那份）它的 `JWT_SECRET`/`INTERNAL_TOKEN` 就当**显式配置**生效，压过派生 |
| python-engine | `app/config.py::_find_env_file` 只从包目录往上找 **3 层** | 打包态必然找不到 ⇒ 老实按 `APP_SECRET` 派生 |

真机事故形状（包装在仓库树里的开发机）：网关认开发 `.env` 里的 token、拒绝引擎用的派生 token，
于是**每一跳双向 401** —— 网关→引擎被顶回（界面表现为「python engine error」「获取技能列表失败」
「internal error」），引擎→网关的回调同理；而 `LOCAL_BOOTSTRAP` 是好的，会话引导照样铸得出 JWT，
只是下一跳就作废，所以看起来像"登录成功但全线失灵"。显式下发之后，环境文件、`config.json`、
继承自 shell 的同名变量都没法再让两侧不一致：赋值发生在 `{...process.env}` 展开**之后**，
本地模式的身份口径只由那把持久化密钥决定。

⚠ 这只关闭了**身份**这一条通道。同一份祖先 `.env` 仍能把 `CHIRON_DATA_DIR`、`S3_*`、
`REDIS_PASSWORD` 之类的开发配置漏进打包网关（它按自己的优先级覆盖壳传的值）。把打包态配置源
整体隔离（只认壳传的环境，或给网关一个受控 cwd）是另一条独立的决定，尚未做。

### 5.6 引擎回调网关的地址在 spawn 时就定死

启动顺序是**引导器 → 引擎 → 网关**：网关起来时要读 `PYTHON_ENGINE_ADDRESS` 建客户端，而引擎需要
网关的地址，所以网关端口必须在**起引擎之前**就定下来。引擎侧 `GATEWAY_INTERNAL_URL` 默认
`http://127.0.0.1:8080`，只在 spawn 那一刻读一次（`_load_gateway_config()`，1s 超时，失败静默沿用默认）。

- 壳挑完引擎端口后先把网关端口定下来（`options.port` 合法就用它，否则 `pickFreePort`），
  再以 `GATEWAY_INTERNAL_URL = http://127.0.0.1:<该端口>` 传给引擎；
- 网关随后必须落在**同一个端口**。落不上就抛 `gateway-port-drift` 终止起栈 —— 引擎已在跑、
  环境改不了，"将就用"的后果是工具授权、媒体、跟进任务、webhook、系统设置这一批回调
  静默打到不存在的地址，比一次明确的启动失败难查得多。

反方向没有这个约束：网关在引擎就绪之后才起，地址能现算现传（`buildGatewayEnv` 里给
`PYTHON_ENGINE_ADDRESS`）。

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
| 本地模式全场 401、进不了管理后台 | 网关没拿到 `LOCAL_BOOTSTRAP=1` ⇒ 会话引导路由没注册，`POST /v1/auth/session` 回 405；或请求不是从 loopback 进来 ⇒ 被处理链 403（§3.4） |
| 抛 `python-engine 未能启动（…）` 并带一段 stderr | 引擎是强依赖，起不来整条栈就起不来（§5.2）。看 stderr 尾部分流：`ModuleNotFoundError` ⇒ 引擎那份 venv 残缺，按 [`packaging.md`](./packaging.md) §3 换源重装（`CHIRON_PIP_INDEX_URL`）；端口/配置类 ⇒ 改环境变量 |
| 用着用着聊天全挂、日志有 `python-engine 运行中退出` | 引擎崩了（§5.3）。日志那行带退出码与最后 8 行 stderr，是对话框之外唯一的证据；`<userData>/local/engine` 删掉重装机能重建环境 |
| 本地模式"能开、但几乎什么都用不了"，日志有「退回 desktop-local core」 | 包里缺网关（[`packaging.md`](./packaging.md) §4）。日志里另有「引导器运行时不可用：…」则是引导器那套 Python 环境没装好，走旧 core 路径去触发安装。两者都不再吞异常：**试过**起网关而**失败**现在是抛错 + 恢复对话框（重试 / 切云端 / 退出），不再静默回落 |
| 登录成功、之后几乎每页都「python engine error」/「internal error」/「获取技能列表失败」 | 网关与引擎的 `INTERNAL_TOKEN`/`JWT_SECRET` 不是同一把（§5.5）。壳显式下发之后这条只剩一种触发方式：**绕过壳**手工起网关或引擎（带上了开发 `.env`）。用 `python -m app.launcher` 自检不算，那条链没有网关 |
| 抛 `网关没能落在预定端口 N（实际 M）`（`gateway-port-drift`） | 预定端口被别的进程占了、或网关自己换了号。引擎已经按 N 配好回调地址且改不了，所以整条栈判死而不是将就用：腾出 N 或显式换一个 `--port` 再起 |

## 7. 相关文件

```
desktop-local/app/launcher.py              引导器入口（`chiron-launcher` 脚本）
desktop-local/app/storage/pg.py            embedded PG：起停、迁移、DSN
desktop-local/app/storage/redis.py         Redis：起停、端口
desktop-local/tests/test_launcher.py       入口契约测试（不依赖真实 PG）
frontend-vue/electron/local-stack.cjs      引导器 → 引擎 → 网关的交接与生命周期
frontend-vue/electron/runtime-install.cjs  两套 Python 环境（core / engine）的按需安装
frontend-vue/electron/__tests__/local-stack.test.cjs 引擎强依赖与运行中退出的用例
frontend-vue/electron/__tests__/backend-stop.test.cjs 退出回收按归属分派、不许谎报 stopped（§5.4）
config/derive_subsecret_test.go                  子密钥派生的黄金向量（与上面那份 JS 用例同值，§5.5）
internal/api/auth_local_session.go        本地会话引导：铸 owner 与 loopback 闸门（§3.4）
docs/desktop-local/handshake.md            启动与握手的实际链路
docs/desktop-local/embedded-pg.md          PG 后端的实测数据
```
