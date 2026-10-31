# 桌面 smoke（不需要人工点 GUI）

一条命令回答一个问题：**这台机器上，桌面端从本地 core 到 Electron 壳还能不能跑起来。**

`desktop-local/scripts/smoke_desktop.py` 真起一个 core 子进程（真实 PostgreSQL + 本地 mock
推理），按桌面端的默认姿势开设备 token 鉴权，跑完
auth → chat → media → memory → kb；加 `--electron` 再用
`frontend-vue/node_modules/.bin/electron` 起壳，观察 10s 后回收。

与另外两个脚本的分工：

| 脚本 | 回答什么 | 起什么 |
|---|---|---|
| `desktop-local/scripts/verify_local_e2e.py` | 每个端点的契约是否符合（含已知缺陷判定） | core（真 PG，可关） |
| `desktop-local/scripts/verify_handshake.py` | Electron ↔ core 的握手细节 | 只起 HTTP 骨架 |
| `desktop-local/scripts/smoke_desktop.py` | **能不能用** —— 一条确定性的 PR 门禁 | core + 可选 Electron 壳 |

## 1. 前置条件

| 需要 | 说明 | 缺了会怎样 |
|---|---|---|
| Python ≥ 3.11 + `desktop-local` 依赖 | `cd desktop-local && python -m pip install -e ".[dev]"` | core 起不来，直接 FAIL 并打印 traceback |
| PostgreSQL 工具或 `POSTGRES_DSN` | `initdb` / `pg_ctl` 在 `PATH`（或 `CHIRON_LOCAL_PG_BIN_DIR`），也可以给一个现成实例 | `/readyz` 报 `pg=false` + `pg_error`，FAIL |
| `fakeredis`（可选） | 没有就用 `--no-redis`，或用 docker 起 valkey 并设 `CHIRON_LOCAL_REDIS_URL` | 只记 SKIP，不影响其余链路 |
| `httpx`（可选） | 装了就走 httpx，没装回退标准库 `urllib` | 无影响（只是错误信息朴素些） |
| Electron（仅 `--electron`） | `cd frontend-vue && npm install`（`node_modules/.bin/electron`），以及**图形会话** | SKIP，并打印缺什么 |

## 2. 运行方式

```bash
# 只验 core（CI / 无 GUI / 最快；本目录以外也能跑，路径按脚本 __file__ 推导）
python desktop-local/scripts/smoke_desktop.py --no-electron

# 顺带起 Electron 壳，观察 10s
python desktop-local/scripts/smoke_desktop.py --electron

# 常见组合
python desktop-local/scripts/smoke_desktop.py --electron --electron-wait 30   # 首启 initdb 慢
python desktop-local/scripts/smoke_desktop.py --no-electron --keep            # 留下数据目录排查
python desktop-local/scripts/smoke_desktop.py --no-electron --no-redis        # 不碰 Redis 适配层
python desktop-local/scripts/smoke_desktop.py --no-electron --data-dir D:\chiron-smoke
```

| 开关 | 默认 | 作用 |
|---|---|---|
| `--electron` / `--no-electron` | 关（`--no-electron`） | 是否拉起壳。两者互斥，不写等于 `--no-electron` |
| `--electron-wait <秒>` | `10` | 壳起来后观察多久再判存活 |
| `--timeout <秒>` | `240` | 等 core 的 `listening` 事件上限（首启 initdb 慢的机器要放宽） |
| `--data-dir <目录>` | 临时目录 | 显式数据目录（不会自动删） |
| `--keep` | 关 | 保留数据目录（含 PG 集群） |
| `--no-auth` | 关（即**默认开鉴权**） | 不下发 `CHIRON_LOCAL_AUTH_ENABLED=1` |
| `--no-redis` | 关 | 下发 `CHIRON_LOCAL_REDIS_ENABLED=0` |

脚本自己做的事（都不需要人工）：

1. 起 mock 推理服务（标准库 `http.server`，`POST /infer` 固定回 `{"content": "smoke reply", …}`），
   用 `--inference-proxy` 指给 core；
2. `python -m app.main --port 0 --data-dir … --inference-proxy …`（与 `components.cjs` 的开发态
   **同一条命令**），从 stdout 解析 `{"event": "listening", …}` 拿真实端口；
3. 下发 `CHIRON_DEVICE_TOKEN`（固定 64 位 hex）并要求 `/v1/auth/session` 返回**同一个** token ——
   这条断言就是"壳层注入的 token 会不会失效"的机械检查；
4. 逐项跑 auth → chat → media → memory → kb，失败即 FAIL 并打印 core 的 stderr 尾部；
5. `--electron` 时起壳、观察、terminate/kill，最后连进程树一起回收（Windows 用 `taskkill /T /F`，
   否则 core 会变成孤儿进程）。

## 3. 预期输出

全绿时（本机实测，`--no-electron`，约 20s；首启 initdb 会再久一点）：

```text
==============================================================================
Chiron 桌面 smoke — core 全链路（不含 Electron）
仓库根: X:\project\Chiron\.worktrees\desktop-smoke
数据目录: C:\Users\…\Temp\chiron-smoke-_67et0qw\data
Python: 3.14.4 (D:\scoop\apps\python\current\python.exe)
==============================================================================
core 子进程 pid=8452，等待 listening（上限 240s）…
HTTP 客户端：httpx 0.28.1
[1/9] core 进程拉起并上报 listening ... PASS
      pid=8452 port=3365 version=0.1.0 startup_ms=6106.0 waited=8.0s
[2/9] 启动期无存储错误（PG 必须真通） ... PASS
[3/9] GET /healthz ... PASS
[4/9] GET /readyz（真实 PG） ... PASS
      /readyz → 200 pg=true backend=binaries version=18.1 auth=device_token
[5/9] auth：POST /v1/auth/session（设备 token） ... PASS
      token=注入值 user=local role=owner；无 token 请求 401 → 带 token 200
[6/9] chat：POST /v1/chat/submit（mock 推理）+ 回读 ... PASS
[7/9] media：上传 / 列表 / 删除 ... PASS
[8/9] memory：写入 / 检索 / 删除 ... PASS
[9/9] kb：建库 / 上传 / 检索 / 删除 ... PASS
------------------------------------------------------------------------------
合计 9 项：PASS 9 / SKIP 0 / FAIL 0
全部 PASS
```

**没装 `fakeredis` 的机器**（例如开发机）会把第 2 项降级成 SKIP，其余不变 ——
这是实测输出，不是理想化样例：

```text
[2/9] 启动期无存储错误（PG 必须真通） ... SKIP
      PG 正常；可选的轻量后端未就绪（不影响 core 其余链路）：redis: fakeredis 初始化失败（…）
      。想让它也 PASS：python -m pip install fakeredis，或加 --no-redis
[4/9] GET /readyz（真实 PG） ... PASS
      /readyz → 503 pg=true backend=binaries version=18.1 auth=device_token，Redis 未就绪（…）
------------------------------------------------------------------------------
合计 9 项：PASS 8 / SKIP 1 / FAIL 0
全部 PASS（含 SKIP，见上）
```

每一项的状态只有三种：
- `PASS` —— 真验过了；
- `FAIL` —— 真挂了，同时打印 `core stderr 尾部（40 行）`，脚本**从不把失败写成 PASS**；
- `SKIP` —— 未判定，且 `detail` 里写清原因与补救命令。典型两种：
  - `启动期无存储错误（PG 必须真通）`：PG 通了，但 Redis 适配层起不来（没装 `fakeredis`），
    这一项记 SKIP 而不是 FAIL —— 本 smoke 的硬要求是 **PG 真通**；
  - `Electron 启动器`：`node_modules` 没装 / `npx --no-install electron` 找不到包。

退出码：`0` = 没有 FAIL（含只有 SKIP 的情况）；`1` = 至少一项 FAIL（含"没等到 listening"）。
所以门禁里可以直接用退出码，SKIP 不会让 PR 变红，但会把原因打在日志里。

## 4. Electron 的限制（先说清楚，免得误判）

`--electron` 只验"**壳起得来、活着、日志写出来了**"，不验界面：

1. **10s 观察窗不够等 core ready**。壳在 local 模式要先跑 `components.cjs::startCore()`，
   首启要 initdb（本机 ~6-10s）+ 迁移，`--electron-wait 10` 通常只够看到
   `starting in local mode` 与 `local backend ready` 之间的一行。想看完整启动就
   `--electron-wait 60`；想看"页面真的渲染出来"不在本脚本范围内（见 §5）。
2. **没有 `dist/` 时窗口会落到 `error.html`**。`main.cjs` 在非 `--dev` 下加载
   `frontend-vue/dist/index.html`；仓库里没构建过就没有这个文件。壳仍然正常存活、
   `main.log` 正常写 —— 脚本判 PASS，但窗口里是错误页。要真看界面先
   `cd frontend-vue && npm run build:desktop`。
3. **单实例锁**：`main.cjs` 启动就 `requestSingleInstanceLock()`。如果已经有一个 Chiron 在跑，
   新起的这个会**立刻退出（exit 0）**，脚本据此判 FAIL 并在 detail 里点明原因。先关掉正在运行的实例。
4. **本地模式由环境变量钉住**：脚本下发 `CHIRON_DESKTOP_MODE=local`（`config.cjs` 的显式覆盖），
   避免全新配置触发"老配置迁移"原生对话框卡住流程。
5. **userData 用临时目录**：同时给 `--user-data-dir=<tmp>/userdata` 和
   `CHIRON_DESKTOP_USER_DATA`，`config.json` 与 `logs/main.log` 都落在临时目录里，跑完一起删。
   若某个 Electron 版本忽略 `--user-data-dir`，脚本会去默认位置（`%APPDATA%\Chiron` 等）找
   `logs/main.log`，并在 detail 里说明"日志实际在别处"。
6. **无图形会话就 SKIP**：Linux 上没有 `DISPLAY` / `WAYLAND_DISPLAY` 直接 SKIP（不起进程）；
   起了才发现缺 `libgtk` / `libnss3` / X server 的，也归成 SKIP（stderr 命中特征串），
   因为那不是产品缺陷而是机器没装 GUI 依赖。
7. **回收方式**：`terminate` → 等 10s → `kill`；Windows 上 `TerminateProcess` 不级联，
   所以最后再 `taskkill /PID <pid> /T /F` 把 core（python）一起收掉，否则会留下孤儿 PG 进程。

## 5. 怎么自己看窗口 / 日志

```bash
# 1) 想真的看界面：先构建渲染层，再跑壳（这两个动作脚本不做）
cd frontend-vue
npm install              # 只有 --electron 需要
npm run build:desktop    # 产出 dist/index.html

# 2) 想让窗口一开就 hook 到 Vite dev server（热更新）
npm run dev &            # 起 Vite
npm run desktop:dev      # electron . --dev（main.cjs 会 openDevTools）

# 3) 只想复现"壳起得来"这一步，保留现场
python desktop-local/scripts/smoke_desktop.py --electron --keep --electron-wait 40
```

日志位置与内容：

| 位置 | 谁写的 | 里面有什么 |
|---|---|---|
| `<userData>/logs/main.log` | `main.cjs::logMain` | `starting in local mode` / `local backend ready at http://127.0.0.1:<port> (device token: injected)` / 加载失败、渲染进程崩溃、IPC 拦截 |
| `<userData>/config.json` | `config.cjs` | 模式（`local`）、`apiUrl`、`local.lastStartedAt`、`local.dataDir` |
| `<userData>/local/` | core | embedded PG 数据目录 + `auth/device-token` |
| `<userData>/components/` | `components.cjs` | 装好的 core 二进制（开发态没有这个目录） |

`main.log` 里**不含 token 本身**，只有 `injected` / `absent`；core 的 stdout/stderr 也不落盘
（`components.cjs` 只保留字节数与末尾一小段用于报错）。要 core 的原始输出，直接手工跑：

```bash
cd desktop-local && python -m app.main --port 0 --data-dir D:\chiron-debug
```

`--electron --keep` 时脚本会把 `userData` 目录的绝对路径打出来（数据目录与 `<tmp>/userdata` 同级）。

## 6. 壳层单测（不需要 Electron）

```bash
node --test frontend-vue/electron/__tests__/smoke.test.cjs
```

12 个用例，mock `spawn` / `fetch` / `app.getPath`，但走的是**真实的**
`components.cjs::startCore/stopCore` 代码路径：

- **启动参数**：dev 态必须是 `-m app.main` + `cwd=desktop-local`（写成
  `python desktop-local/app/main.py` 必然 `ImportError`）；二进制态不带 CLI；`--data-dir` /
  `--port` / `--inference-proxy` 同时进 CLI 与 `CHIRON_*` / `CHIRON_LOCAL_*` 两套 env；
  token 只进 env 不进 argv；`windowsHide: true`。
- **退出回收**：`stopCore` 发 `SIGTERM`，进程不退才 `SIGKILL`（等满 5s 宽限期），之后状态清零、
  可重新拉起；core 自己挂掉时抛出的错误里带真实 stderr（不伪装成功）。
- **local 模式 baseUrl 注入**：`--port 0` 时以 stdout 的 `listening` 事件为准；非 loopback 的
  `host` 压回 `127.0.0.1`；没有 `listening` 事件时回退到"借来的端口 + `/healthz`"；
  `startComponent('core')` 拿到的是同一个 `baseUrl`；`shared.cjs` 的三个参数前缀与
  `partitionFor('local')` 就是 `main.cjs` 拼 `additionalArguments` 用的那几个常量。

## 7. 失败时按这个顺序查

| 症状 | 原因 | 处理 |
|---|---|---|
| `[1/9] … FAIL`，`在 240s 内没等到 listening` | core 卡在 lifespan（等 PG / 迁移）或直接崩了 | 看紧跟着打印的 `core stderr 尾部`；先用 `--no-redis`、再 `--no-auth` 缩小范围 |
| `/readyz` 报 `pg=false` | 没有可用 PG（`initdb`/`pg_ctl` 不在 PATH，也没有 `POSTGRES_DSN`） | 装 PostgreSQL 16+ 并设 `CHIRON_LOCAL_PG_BIN_DIR`，或给一个现成实例 |
| `auth … FAIL`，`core 没有使用 CHIRON_DEVICE_TOKEN 注入的 token` | `auth.py::load_or_create_token` 的优先级被改过 | 这是壳层会静默失效的那条路：注入的 token 与 core 落盘值不一致，页面全部 401 |
| `chat … FAIL`，`mock 推理服务没收到请求` | `--inference-proxy` 没生效 | 检查 core 启动参数；`CHIRON_LOCAL_INFERENCE_PROXY` 也可用 |
| `media` / `memory` / `kb` 单点 FAIL | 真实 PG 上的 SQL 与 DDL 不一致 | 端点细节与已知缺陷判定用 `verify_local_e2e.py` 复现 |
| `Electron 进程 … FAIL`，`exit=0` 且很快退出 | 另一个 Chiron 实例占着单实例锁 | 关掉正在运行的实例再跑 |
| `Electron 进程 … SKIP`，`没有图形会话` | 无 GUI（CI / 远程无 X） | 本来就不该跑壳；`--no-electron` 就是给这种情况的 |
| `userData 日志 … FAIL` | 10s 内还没进 `whenReady`，或 `--user-data-dir` 被忽略且默认目录无权限 | 调大 `--electron-wait`；用 `--keep` 后按打印的路径手工看 |
| `taskkill: 没有找到进程` | 壳已经自己退了，收尾命令扑空 | 正常现象（`check=False`），只看 `Electron 退出回收` 的结论 |

## 8. 已知未覆盖

1. **界面不验**：不截图、不点按钮、不检查 DOM。要验渲染层请走
   `frontend-vue` 的 `npm run build:desktop` + 手工窗口，或后续加 Playwright/wdio 层。
2. **不验 SSE / `/events`**：需要长期连接，放进 `verify_p0.py` 更合适。
3. **不验模式切换 / 云端模式**：切换会 relaunch，脚本里起两次壳的收益低于噪音。
4. **Windows 上不保证 core 一定被回收**：`taskkill /T` 依赖进程树未被中断；若手工 Ctrl-C 过
   一次，请用 `Get-Process python` 自查残留（这也是脚本把 `--keep` 打出来的原因）。
