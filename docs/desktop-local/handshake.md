# 启动与握手（Electron ↔ 本地 core）

本文件说明 **local 模式**下 Electron 主进程怎样把 `desktop-local` 拉起来、拿到地址与
设备 token、再交给渲染进程。契约基线仍是 [`contracts.md`](./contracts.md)（§3.1 环境变量、
§4 本地 API），这里只写"实际怎么跑"和"跑不起来怎么查"。

涉及文件（都不在 `desktop-local/app/**` 里，`app` 侧一行没改）：

```
frontend-vue/electron/components.cjs          startCore()：spawn core + 解析 listening + 返回 baseUrl/token/pid
frontend-vue/electron/main.cjs                startLocalBackend()：取 token；建窗时注入 baseUrl 与 deviceToken
frontend-vue/electron/preload.cjs             window.chironDesktop.deviceToken（只读）
desktop-local/scripts/verify_handshake.py     不起 Electron，直接验一遍同一条链路
```

## 1. 启动链路

```
app.whenReady()
  └─ resolveMode(config.json) ──> 'local'
      └─ startLocalBackend()                       main.cjs
          └─ components.startCore()                components.cjs
              ├─ resolveCoreLaunch()               已装二进制 > 开发态源码（`-m app.main` + cwd=desktop-local）
              ├─ buildCoreEnv()                    契约名 + config.py 真名，两套一起下发
              ├─ spawn(stdout/stderr = pipe)       只累计字节数，stderr 留末尾 2000B 给报错
              ├─ 等 stdout 的 {"event":"listening"} 拿真实端口（同时轮询 /healthz 兜底）
              └─ return { baseUrl, token, pid }    外加 port / dataDir / listening 供诊断
          └─ createWindow()
              ├─ currentApiUrl = localBackend.baseUrl
              └─ additionalArguments = [
                   --chiron-mode=local
                   --chiron-api-url=http://127.0.0.1:<port>   （有地址才传）
                   --chiron-device-token=<token>              （有 token 才传，cloud 模式不传）
                 ]
                  └─ preload.cjs ──> window.chironDesktop = { mode, apiUrl, deviceToken, ... }
```

要点：

- **地址与 token 都走 `additionalArguments`**，不走 `process.env`：渲染进程的环境变量是
  启动时继承的，用户改了地址之后旧 env 不会更新（和 `API_URL_ARG_PREFIX` 同一个理由）。
- 先建 session 监听再建窗口；本地后端未就绪时不注入地址，直接落到 `error.html` 并把
  `localBackend.reason/message` 写进详情页。
- 本地后端只在 `before-quit` / 切模式 / `restart` 时停一次（`localBackendStopped` 标志）。

## 2. 环境变量

`contracts.md` §3.1 定义的是 `CHIRON_*`；当前 `desktop-local/app/config.py` 是
pydantic-settings，`env_prefix = "CHIRON_LOCAL_"`，**真正生效的是 `CHIRON_LOCAL_*`**。
所以 `components.cjs::buildCoreEnv()` 两套一起写（`CORE_ENV_ALIASES`）。

实测（Windows，`--no-pg --no-redis`，只给一个变量）：

| 只下发 | core 报告的数据目录 | 结论 |
|---|---|---|
| `CHIRON_DATA_DIR=<tmp>` | `%LOCALAPPDATA%\Chiron\local` | **静默失效** |
| `CHIRON_LOCAL_DATA_DIR=<tmp>` | `<tmp>` | 生效 |

| 契约名（§3.1） | 实际生效名 | 含义 | 默认 |
|---|---|---|---|
| `CHIRON_DATA_DIR` | `CHIRON_LOCAL_DATA_DIR` | 数据目录 | Electron：`userData/local`；CLI 单独跑：平台默认目录 |
| `CHIRON_PORT` | `CHIRON_LOCAL_PORT` | 监听端口，`0` = 随机 | Electron 先借一个空闲端口；`0` 时只认 listening 事件 |
| `CHIRON_DEVICE_TOKEN` | —（暂无对应实现） | 设备 token | Electron 侧随机 32 字节 hex |
| `CHIRON_INFERENCE_PROXY` | `CHIRON_LOCAL_INFERENCE_PROXY` | loopback 推理代理地址 | 空 = 未配置 |
| `CHIRON_PYTHON` | —（Electron 自己读） | 指定 Python 解释器 | `python` |

`CHIRON_LOCAL_PG_ENABLED` / `CHIRON_LOCAL_REDIS_ENABLED` / `CHIRON_LOCAL_RUN_MIGRATIONS`
等**只有 core 侧的名字**，没有契约别名；通过 `startCore({ env })` 透传即可（例如
`{ env: { CHIRON_LOCAL_PG_ENABLED: 'false' } }`）。

## 3. core 的 CLI 与 stdout 契约

`startCore()` 在**开发态**额外下发 CLI 参数（`argparse` 里的显式参数优先于 env，
是"一定生效"的那条路）：

```
python -m app.main --data-dir <dir> --port <port> [--inference-proxy <url>]
```

`cwd` 必须是 `desktop-local/`。**不要**写成 `python desktop-local/app/main.py`：
`main.py` 是包内导入（`from app import ...`），以脚本方式启动时 `sys.path[0]` 是
`desktop-local/app`，`app` 包不在搜索路径上，必然 `ModuleNotFoundError`。
`-m` 会把 cwd 放进 `sys.path`，这也是 `verify_handshake.py` 的做法。

装好的二进制（`userData/components/core/chiron-local[.exe]`）**只走 env**，不追加 CLI ——
不知道那个包是哪个版本编出来的，不能假设它认识新参数。

core 绑成功后才打一行 JSON（`main.py::serve`），Electron 按行解析，端口以它为准：

```json
{"event":"listening","host":"127.0.0.1","port":48969,"pid":35368,
 "version":"0.1.0","data_dir":"...","startup_ms":0.0,"storage_errors":[]}
```

只认 `event === "listening"` 且 `port` 在 1..65535 的行；其它 stdout（uvicorn 日志、
未来加的调试输出）一律忽略。`host` 不是 `127.0.0.1`/`localhost` 时按 `127.0.0.1` 处理。

## 4. token 由谁产生

三处来源，**尚未合并成一个**：

| 来源 | 值 | 谁用 |
|---|---|---|
| `components.startCore()` 生成并注入的 `CHIRON_DEVICE_TOKEN` | 每次启动随机 32 字节 hex | 返回给 `main.cjs` → preload 的 `deviceToken` |
| core 落盘的 `<data-dir>/auth/device-token` | 首次启动生成，之后保持不变 | `POST /v1/auth/session` 返回的值 |
| 云端 token | 用户登录后由服务端签发 | `desktop:get-token` / `save-token`（safeStorage），与本地无关 |

当前行为：

- 注入的 token 进 preload，页面可以拿它做 `Authorization: Bearer`；
- core 的 `auth_enabled` 默认 `False`（`/readyz` 报 `auth: "disabled"`），**不校验**任何 token，
  所以两种 token 都能用，握手不会因此失败；
- 想拿 core 自己那个（重启不变）就调 `POST /v1/auth/session`，把 `data.token` 存下来。

待收口的方向是二选一：让 core 认 `CHIRON_DEVICE_TOKEN`，或让 Electron 把
`<data-dir>/auth/device-token` 读出来交给 preload。改之前别假设它们相等。

## 5. 验证

```bash
cd desktop-local

# 握手本身（临时数据目录 + 随机端口，跑完即删；~2s）
python scripts/verify_handshake.py

# 连 embedded PG / Redis 一起（等同 Electron 默认行为，会慢很多）
python scripts/verify_handshake.py --with-storage

# 指定目录 / 解释器 / 保留现场
python scripts/verify_handshake.py --data-dir D:\chiron-local --python C:\Python312\python.exe --keep
```

脚本做的事与 `components.startCore()` 完全同形：`python -m app.main --port 0 --no-migrate`
（cwd = `desktop-local`）+ `CHIRON_*` 与 `CHIRON_LOCAL_*` 双写 → 解析 listening →
`GET /healthz` → `POST /v1/auth/session` → 比对 `<data-dir>/auth/device-token`。
退出码 `0` = 全 PASS（SKIP 不算失败），`1` = 有 FAIL，失败时打印 core 的 stderr 末尾。

`SKIP` 的一项是"注入的 `CHIRON_DEVICE_TOKEN` 被 core 采用"：当前不成立（见 §4），
记 SKIP 而不是 FAIL，等 §4 收口后它会自动变 PASS。

## 6. 失败排查

按"最先出错的那一环"往下列，不要跳步。

| 症状 | 原因 | 处理 |
|---|---|---|
| `未找到本地 core：请安装 …` | 既没有 `components/core/chiron-local[.exe]`，也没有 `desktop-local/app/main.py` | 装组件，或在仓库内以开发态运行（需要 `desktop-local/` 完整） |
| stderr 出现 `ModuleNotFoundError: No module named 'app'` | 用 `python <path>/main.py` 直接启动，`app` 包不在 `sys.path` | 用 `-m app.main` 且 `cwd=desktop-local`（`startCore()` 已如此） |
| stderr 出现 `ModuleNotFoundError: No module named 'fastapi'` / `uvicorn` | 解释器里没装 `desktop-local` 的依赖 | `cd desktop-local && python -m pip install -e ".[dev]"`；或 `CHIRON_PYTHON` 指向装好的解释器 |
| spawn `ENOENT` / `spawn python` | `python` 不在 PATH，或该名字是 Windows 商店占位符 | 设 `CHIRON_PYTHON` 指向真实解释器 |
| `在 30000ms 内没有收到 stdout 的 listening 事件（端口未知…）` | core 卡在 lifespan（例如等 embedded PG 启动）或根本没起来 | 看同一行里的 `stderr 末尾`；`CHIRON_LOCAL_PG_ENABLED=false` 先只验 HTTP；`verify_handshake.py --with-storage` 单独复现存储问题 |
| `在 30000ms 内没有响应 http://127.0.0.1:<port>/healthz` | 进程活着但没绑到该端口（端口被占、绑到了别的 host） | 看 listening 事件的 `port`/`host`；端口占用用 `startCore({ port: 0 })` 让内核挑 |
| 端口对了但 `/readyz` 503 | 存储不健康（PG / Redis） | `/readyz` 里 `pg_error` / `redis_error` / `errors` 逐条给原因；`/healthz` 仍应是 200 |
| 数据目录不是预期的（跑到 `%LOCALAPPDATA%\Chiron\local`） | 只下发了 `CHIRON_DATA_DIR` | 改 `CHIRON_LOCAL_DATA_DIR`，或让 `startCore()`/`verify_handshake.py` 下发（它们已经双写） |
| 页面里 `deviceToken` 为空串 | 本地后端没起来（`localBackend.token` 为空），或当前是 cloud 模式 | 先解决后端启动；空串时页面应回退 `POST /v1/auth/session` |
| 报错消息里出现 `password=***` / `…:***@` | 正常：stderr 进对话框前会被 `redactSecrets()` 脱敏 | 需要完整 traceback 就手工跑 `verify_handshake.py`（它打印未脱敏的 stderr，仅本地终端） |
| 关掉应用后端口还被占 | core 没被停止（`before-quit` 前崩溃 / SIGKILL） | `stopCore()` 是 SIGTERM → 5s → SIGKILL；异常残留按 PID 手工清理 |

日志：主进程日志在 `<userData>/logs/main.log`，只有地址与"有没有 token"，**不含 token 本身**。
core 的 stdout/stderr 一律不落盘（只留字节数和末尾一小段用于报错）。

## 7. 已知差异

1. **环境变量双写**：`CHIRON_*` 与 `CHIRON_LOCAL_*` 并存（`CORE_ENV_ALIASES`）。core 支持契约名后
   删掉别名表即可，Electron 侧不用动。
2. **token 未统一**（§4）：注入值与 core 落盘值不是同一个。
3. **`CHIRON_DEVICE_TOKEN` 未被校验**：`auth_enabled=False` 期间不影响使用。
4. `startCore()` 不传存储开关：Electron 的默认行为是连 embedded PG / Redis 一起起；
   `verify_handshake.py` 默认关掉它们，只为把 HTTP 握手的失败与存储失败分开看。
5. `components.cjs` 里的 `--chiron-device-token=` 前缀在 `main.cjs` / `preload.cjs` 各写一份
   （都先读 `shared.cjs`，缺失才用同一字面量）。按契约 §3.1 应搬进 `shared.cjs`，
   **尚未搬**。
6. 渲染进程还没有消费 `deviceToken`（`frontend-vue/src/**` 不在本模块范围内），
   目前只是把值暴露出来。
