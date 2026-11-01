# 桌面端双模式 / 本地化开发契约

> 本文件是并行开发的**唯一接口基线**。任何 worktree 需要改动这里的约定时，先改本文件并提交，
> 再改实现；不要各自发明字段名。

## 1. 模式与设备设置

- 模式取值：`local` | `cloud`。
- 新装默认：`local`。
- 模式是**设备级设置**，与登录无关；登录页与设置页都可读写。
- 真相在主进程 `userData/config.json`；渲染进程 `localStorage` 只做镜像。

`config.json` 结构（新增字段，保留既有 `apiUrl`）：

```json
{
  "mode": "local",
  "apiUrl": "",
  "local": {
    "dataDir": "",
    "lastStartedAt": ""
  },
  "ui": {
    "locale": "zh-CN",
    "theme": "system"
  },
  "migration": {
    "legacyCloudDetected": false,
    "modeChosenAt": ""
  },
  "setup": {
    "pendingApiUrl": false
  }
}
```

约束：

- 切换模式只清**离开模式**的 cookie / localStorage / IndexedDB / CacheStorage / Service Worker /
  HTTP 缓存 / token；**默认不删** `userData/local/**` 用户数据。
- 切换后必须重启应用（`app.relaunch()` + `app.exit(0)`），避免两个后端并存。
- 老用户已有 `apiUrl` 且无 `mode` 时，首次启动弹迁移提示，默认 `local`，但保留 `apiUrl`。
- `setup.pendingApiUrl` 是**跨重启、只用一次**的标记：切进云端（含首启选云端）时置位，
  建窗时消费并清零。它存在的原因是上一条"`apiUrl` 保留"——保留下来的地址可能是上个时代
  留下的、已经连不上的地址，而"地址非空"不等于"用户确认过"：后端不可达 ⇒ 登不了录 ⇒
  进不了设置页 ⇒ 没有任何改地址的入口。切回本地时顺手清掉，盘上不留来路不明的待确认。
- 启动分流集中在 `shared.cjs::resolveStartupWindow`（纯函数，判据只有模式 / 地址 /
  本地后端就绪 / 待确认标记 / `forceSetup` 五个输入）：云端有待确认标记或没地址 →
  `setup.html`；本地后端就绪 → 主界面（注入本机地址）；本地没就绪 → `error.html`。
  **标记不得影响本地模式**，本地也永远不该被送去云端地址配置页。
- `setup.html` 保存只卡地址格式（`http(s)`），**不卡"必须先测通"**：服务还没起来、端口写错、
  探测被防火墙挡住的时刻恰恰最需要把地址存下来；但保存的永远是输入框当前值，测 A 存 B 仍不可能。
  页面打开时预填旧地址，并提供「改用本地模式」出口（它常常是当时唯一的窗口）。

## 2. preload 契约

`window.chironDesktop` 必须暴露以下字段（`preload.cjs`）。组件相关项由 `components.cjs` 提供、
`preload.cjs` 接线。

```ts
type ChironMode = 'local' | 'cloud'

interface DeviceSettings {
  mode: ChironMode
  apiUrl: string
  local: { dataDir: string; lastStartedAt: string }
  ui: { locale: string; theme: string }
  migration: { legacyCloudDetected: boolean; modeChosenAt: string }
  // §1 的跨重启一次性标记：主进程建窗时消费，渲染进程读到也不该再动它
  setup: { pendingApiUrl: boolean }
}

interface ComponentInfo {
  name: 'core' | 'pageindex' | 'langextract'
  version: string
  installed: boolean
  running: boolean
  sizeBytes: number
  // 进程监督：连续崩溃重启次数；非空串 = 已放弃自动拉起（原因在这里）
  restarts: number
  supervisionGaveUp: string
}

// 读回值**永不含明文 key**（`secrets.cjs::readPublicSecrets`）：只有 hasKey 布尔。
interface LocalModelConfig {
  active: string
  providers: Record<string, { baseUrl: string; model: string; hasKey: boolean }>
  safeStorageAvailable: boolean
}

interface ChironDesktop {
  isDesktop: true
  platform: 'win32' | 'darwin' | 'linux'
  versions: { electron: string; chrome: string }
  apiUrl: string
  mode: ChironMode
  /** 本地设备 token（只读）：local 模式由主进程注入，cloud 模式恒为空串 */
  deviceToken: string

  getDeviceSettings(): Promise<DeviceSettings>
  setMode(mode: ChironMode, opts?: { deleteLocalData?: boolean }): Promise<void>
  restart(): Promise<void>

  testApiUrl(url: string): Promise<boolean>
  setApiUrl(url: string): Promise<boolean>
  /**
   * 打开主进程的连接配置页（`setup.html`），重建窗口。
   * 改服务地址在应用内**只有这一个实现**：设置页与登录页都调它，界面里不内嵌第二个编辑器，
   * 否则校验与探测就有两套口径。老外壳没有这个成员，调用方据此禁用入口。
   */
  openSetup(): Promise<boolean>

  getToken(): Promise<string>
  saveToken(token: string): Promise<boolean>
  clearToken(): Promise<boolean>

  listComponents(): Promise<ComponentInfo[]>
  installComponent(name: string): Promise<void>
  removeComponent(name: string): Promise<void>
  startComponent(name: string): Promise<void>
  stopComponent(name: string): Promise<void>

  // 本地模型配置：读回值永不含明文 key，只有 hasKey 布尔
  getModelConfig(): Promise<LocalModelConfig>
  saveModelConfig(config: LocalModelConfig): Promise<{ ok: boolean; message?: string }>
  restartLocalBackend(): Promise<{ ok: boolean; message?: string }>

  // 首次启动的运行时安装进度；返回取消订阅函数
  onRuntimeInstallStage(callback: (info: unknown) => void): () => void

  // 本地文档选择：只回路径与文件名，不读内容；用户取消时 resolve null
  pickDocument(): Promise<{ path: string; name: string } | null>
}
```

IPC channel 命名统一 `desktop:<verb>`。`ipcMain.handle` 实际注册的 18 个：

```
desktop:get-device-settings
desktop:set-mode
desktop:restart
desktop:test-api-url
desktop:set-api-url
desktop:open-setup
desktop:get-token
desktop:save-token
desktop:clear-token
desktop:list-components
desktop:install-component
desktop:remove-component
desktop:start-component
desktop:stop-component
desktop:pick-document
desktop:get-model-config
desktop:save-model-config
desktop:restart-local-backend
```

单向事件（主进程 → 渲染进程，`webContents.send`，不走 `handle`）：
`desktop:runtime-stage` —— 运行时安装进度，仅 `install.html` 订阅。
载荷是 `{ stage, message, target, targetLabel }`：本地模式有**两套**Python 环境
（`core`＝引导器、`engine`＝AI 引擎，见 [`packaging.md`](./packaging.md) §3「两套环境」），
同一条通道按先后两轮推送，渲染层靠 `target` 复位进度条、靠 `targetLabel` 换标题。
`target` 缺省当作 `core`，所以只装一套环境的老载荷仍然可用。

`desktop:pick-document` 的边界：`showOpenDialog` 只取第一个路径，**不读文件内容**
（正文属于用户数据，由本地链路按需处理）；对话框以 `BrowserWindow.fromWebContents(event.sender)`
为模态父窗口，因此设置页开在独立窗口时不会挂到主窗口上；不加扩展名过滤器，本地抽取接受任意文件。

所有 `ipcMain.handle` 必须先 `assertTrustedSender(event)`。

## 3. 进程与模块边界

```
frontend-vue/electron/
  main.cjs               # 壳：窗口、生命周期、接线
  preload.cjs            # IPC 暴露
  config.cjs             # 设备设置读写
  shared.cjs             # 常量
  mode-switch.cjs        # 切换 + 清除
  components.cjs         # 组件管理
  inference-proxy.cjs    # loopback 推理代理
  secrets.cjs            # key 加密存储
  __tests__/             # 纯 Node 单测
```

- `main.cjs` / `preload.cjs` / `config.cjs` / `shared.cjs` 不承载组件管理、推理代理、密钥存储的实现，
  只 require + 接线；后三者的实现全在 `components.cjs` / `inference-proxy.cjs` / `secrets.cjs`。
- 需要新增共享常量时，先加到本文件 §3.1，再各自实现。

### 3.1 已冻结的共享常量

`frontend-vue/electron/shared.cjs`：

- `API_URL_ARG_PREFIX = '--chiron-api-url='`
- `MODE_ARG_PREFIX = '--chiron-mode='`
- `PARTITION_PREFIX = 'persist:chiron-'`；`partitionFor(mode)` 返回
  `persist:chiron-local` / `persist:chiron-cloud`

`config.cjs::readDeviceSettings()` 额外返回两个**派生、不落盘**字段：
`needsMigration`、`modeChosen`。

本地后端环境变量（`components.cjs` → `desktop-local`）：

- `CHIRON_DATA_DIR`：数据目录（默认 `userData/local`）
- `CHIRON_PORT`：监听端口（0 = 随机）
- `CHIRON_DEVICE_TOKEN`：本地设备 token
- `CHIRON_INFERENCE_PROXY`：主进程推理代理地址
- `CHIRON_PYTHON`：可选，指定 Python 解释器

## 4. 本地后端（desktop-local）

只建进程骨架与存储验证，不迁移业务代码。

```
desktop-local/
  pyproject.toml
  README.md
  app/
    __init__.py
    main.py              # FastAPI；127.0.0.1；随机端口
    config.py            # data-dir / port / inference-proxy
    storage/
      __init__.py
      pg.py              # embedded-postgres 生命周期
      redis.py           # fakeredis 或 Valkey 适配
    api/
      __init__.py
      health.py          # /healthz /readyz
      auth.py            # /v1/auth/session（本地用户）
      models.py          # /v1/models（占位）
  scripts/
    verify_p0.py         # P0 验证：PG 迁移 / Redis Streams / 启动耗时
  tests/
```

本地 API 契约：

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` |
| GET | `/readyz` | `{"status":"ok"\|"degraded","pg":true,"redis":true}`，详见下方语义说明 |
| POST | `/v1/auth/session` | `{"data":{"token":"...","user":{"id":"local","name":"Local","role":"owner","tenant_id":1}}}` |
| GET | `/v1/models` | `{"data":[]}`（占位） |
| GET | `/events?session_id=` | `text/event-stream`（只发心跳） |

统一响应包装：与 Go 网关一致，业务响应放 `{"data": ...}`。

鉴权：`Authorization: Bearer <device-token>` 或同源 httpOnly cookie；允许无鉴权启动，
但 `/readyz` 必须报告 `auth: "disabled"`。

### 本地模式的两个后端与身份来源

本地模式的后端有两种形态，凭据不同，但**前端走同一条判定路径**
（`src/router/guard.ts` 对本地模式没有身份特判，`requiresAdmin` 一律看后端会话）：

| 后端 | 身份来源 | 请求带的 Bearer |
|---|---|---|
| Go 网关（引导器交接，见 [`launcher.md`](./launcher.md) §3.4） | `POST /v1/auth/session` 免口令换到的真 JWT：owner，仅 loopback | 本地会话 token |
| `desktop-local` core（本节上面那套 API） | `POST /v1/auth/session` 返回固定 `LOCAL_USER` | preload 注入的设备 token |

两条路都不串用云端凭据：`src/api/index.ts::setAuthToken(token, scope)` 按归属分桶 ——
本地 token 不进 safeStorage，云端登录 token 不参与本地请求。模式切换时
`getDeviceMode()` 立刻变新值、进程却稍后才重启，那个窗口期就是分桶存在的理由。

本地模式的身份密钥是**跨语言冻结常量**：`APP_SECRET`（落盘）→
`JWT_SECRET = base64url_nopad(HMAC_SHA256(APP_SECRET, "chiron-jwt"))`、
`INTERNAL_TOKEN = …("chiron-internal")`，Go（`config/config.go::deriveSubsecret`）、
Python（`app/config.py::_validate_security_defaults`）、Node（`local-stack.cjs::deriveSubsecret`）
各实现一份，域标签必须逐字一致。两个口径由黄金向量锁住（`config/derive_subsecret_test.go` 与
`electron/__tests__/local-stack.test.cjs` 用同一组期望值）；打包态这两个值由壳**显式写进**网关与
引擎的环境，不让任何一侧自行派生 —— 理由与事故形状见 [`launcher.md`](./launcher.md) §5.5。

### `/readyz` 的判定语义

`ready` 只看**业务依赖**（PG 与迁移），不把 redis 算进就绪条件：

- PG 可用且迁移完成 → `ready: true`，HTTP 200；
- redis 不可用以 **`status: "degraded"` + HTTP 200** 报告，原因写在 `redis_error`，
  **不阻断**请求。

依据：`desktop-local` 的业务路由（chat / conversations / components / media / memory）
对 redis 零引用，全仓只有 `health.py` 读 `app.state.redis`。把 redis 算进就绪条件会让
"redis 没装"直接变成"所有页面 503 服务繁忙"，而实际上业务完全可用。

这与 `python-engine`（主引擎）不同：主引擎有 10+ 模块硬依赖 redis 的 Streams、限流、
预算与缓存，那里 redis 不可用就是不健康。

## 5. 存储验证的硬指标

`desktop-local/scripts/verify_p0.py` 必须输出：

1. `embedded-postgres` 能启动、`alembic upgrade head` 能跑通（复用仓库 `migrations/`）。
2. 数据目录可指定；停止后重启数据仍在。
3. Redis 对 `XADD / XREADGROUP / XACK / XCLAIM / XAUTOCLAIM / PUBLISH / SUBSCRIBE` **以及
   `EVAL`** 的实际支持情况（逐项 PASS/FAIL，不许假设）。

   为什么必须带上 `EVAL`：前七条只能证明"命令存在"，`EVAL` 才能证明"语义正确"。
   实测 `fakeredis` 前七条全 PASS，唯独 `EVAL` 报 `unknown command 'eval'`；而引擎的
   KeyRing 仲裁（D19）、限流、预算预留、队列闸门共 15 处依赖 Redis 单键 Lua 的原子性。
   只看七条会得出"fakeredis 够用"的相反结论。
4. 冷启动耗时（毫秒）与数据目录体积。
5. 失败时给出可执行的替代方案（Valkey 二进制 / 缩小 core 范围）。

## 6. 验收命令

提交前至少运行：

```bash
# Electron 主进程
node --check frontend-vue/electron/main.cjs
node --check frontend-vue/electron/preload.cjs
node --check frontend-vue/electron/config.cjs
node --check frontend-vue/electron/components.cjs
node --check frontend-vue/electron/inference-proxy.cjs
node --check frontend-vue/electron/secrets.cjs

# 前端
cd frontend-vue && pnpm exec vue-tsc -b
cd frontend-vue && pnpm test
cd frontend-vue && pnpm check:ui

# 本地后端
cd desktop-local && python -m ruff check .
cd desktop-local && python -m pytest -q
python desktop-local/scripts/verify_p0.py

# 引擎：Redis 必须真连得上（两个变量都要给）—— key-ring 的 live 用例看 CHIRON_TEST_REDIS_ADDR，
# runtime_cache 只看 settings.redis_url（REDIS_URL）。漏掉后者时它仍连默认 6379，每个用例
# 各等一次连接超时：本机实测全量 65.02 秒 → 1412.75 秒，并凭空多出 27 个失败，看着像大面积回归。
# 并发跑两份 pytest 请各给 --basetemp：共用 pytest-of-athenavi 会在收尾时互相踩符号链接。
cd python-engine && CHIRON_TEST_REDIS_ADDR=127.0.0.1:6399 \
  REDIS_URL=redis://127.0.0.1:6399/0 python -m pytest -q tests/
```

CI/本机无法运行的项（如 Electron 真机启动）在提交说明里写“未验证 + 原因”，不要假装通过。

## 7. 变更流程

- **先改本文件，再改实现**：字段名、端点形状、环境变量名一律以本文件为准，不要各自发明。
- 共享文件（`internal/**`、`frontend-vue/src/**`、`desktop-local/**` 之外的交叉面）的改动
  必须在本文件登记后再落地。
- 提交前跑完 §6 的验收命令；CI/本机无法运行的项在提交说明里写"未验证 + 原因"，不要假装通过。

## 8. 禁止事项

- 不要在 `python-engine/` 主依赖里加入 `langextract` / `pageindex`。
- 不要在渲染进程暴露任意 `fs` / `require` / `ipcRenderer`。
- 不要把用户 provider key 写入日志、SQLite、localStorage 或备份。
- 不要默认删除 `userData/local/**`。
- 云端片段推理端点见 §9。
- 不要改 `internal/**`（Go）与 `frontend-vue/src/**` 之外的共享文件而不在本文件登记。
- 不要用「`apiUrl` 非空」判定云端已经配好可进主界面（§1）：保留旧地址是切模式的契约，
  而旧地址未必还连得上——不可达的后端会让设置页（登录后才存在）够不着，用户被锁在门外。
  判据是 `setup.pendingApiUrl`，且只有 `resolveStartupWindow` 一处。

- 不要用 `EventSource` 承载需要 Authorization 的流（§11）：它**设不了请求头**，只能把凭据塞进
  URL，而打包桌面态的身份（`POST /v1/auth/session` 换到的本地 JWT）恰恰只在头里被认。
  结果是 401 被浏览器的自动重连藏起来 —— 界面停在「思考中」，既不报错也不出字。
- 不要假设引擎 lifespan 结束时 Redis keyset 已经有 key（§10）：打包桌面态的 Redis 是空库起来
  的，且引擎比网关**先**启动。要让"配完 key 不用重启"成立，靠的是回填 + 订阅失效这两条，
  少一条就退化成「必须重启」或「永远空表」。
- 不要在运行期只往 `GatewayRouter._providers` 里塞 provider（§10.2）：`_breakers` /
  `_embed_breakers` / `_latencies` / `_catalog` 在构造时建成并按键直取，漏一张是 KeyError，
  不是优雅降级。运行期注册只能走 `router.register_provider`。

## 9. 桌面片段推理端点

`POST /v1/desktop/infer`（云端网关 → Python 引擎）：

```
渲染进程 ──JWT──> Go 网关 POST /v1/desktop/infer  ──X-Internal-Token + ?user_id&tenant_id──> 引擎 /v1/desktop/infer
```

请求体：

```json
{
  "model": "gpt-4o-mini",
  "messages": [{"role": "user", "content": "..."}],
  "max_tokens": 2048,
  "temperature": 0,
  "response_format": {"type": "json_object"},
  "request_id": "uuid"
}
```

响应（引擎形状，网关原样透传，不包 `{"data": ...}`）：

```json
{
  "content": "...",
  "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
  "provider": "openai",
  "model": "gpt-4o-mini"
}
```

约束：只允许 `user` 角色与纯文本；`max_tokens <= 4096`；messages 总字符 `<= 2_000_000`；
服务端不记录 messages/content。

### 9.1 Go 网关路由

| 项 | 值 |
|---|---|
| 路由注册 | `internal/api/routes_desktop.go::registerDesktopRoutes`（由 `gateway_router.go` 调用） |
| handler | `internal/api/desktop_infer.go::DesktopInferHandler` |
| 中间件 | `authMW`（用户 JWT 必填）→ `rlMW`（限流）—— 与 `/v1/chat/submit` 同级 |
| 上游 | `pythonClient.ForwardRequest`，路径 `/v1/desktop/infer?user_id=&tenant_id=` |
| 身份 | 由网关从**已验证的 claims** 重建 query 与 `X-User-Id` / `X-Tenant-Id`；客户端伪造的同名 query/header 不作数 |
| 请求体 | **不解析、不落日志、不落库**；流式透传，网关侧上限 16 MiB（业务上限仍在引擎侧） |
| 错误 | 401（无 claims/租户）、413（body 超限）、500（引擎未接线）、引擎返回的 422/502 原样透传 |

频控不作为独立配额：`rlMW` 的租户级 QPS 与 `/v1/chat` 共用同一套桶。

### 9.2 幂等（`request_id`）

- 引擎侧用 Redis `SET key <pending> NX EX 60` 抢占位：`key = desktop:infer:idem:{tenant}:{request_id}`。
- 60s 内同 `request_id` 的第二次请求**重放第一次的完整响应**（逐字段相同），不再打上游、不再计 token。
- 占位仍在（首个请求还在跑）时返回 **409 `duplicate request in progress`** —— 返回空 `content` 会被调用方当成真结果。
- 失败（502/上游异常）会**删除占位**，所以失败后可以立即重试。
- `request_id` 为空 = 不去重（不同用户/设备的空串不能被当成同一个键）。

### 9.3 用量与计费

计费口径只有三条，先于实现写死（口径变更是产品决定，不是实现细节）：

| 场景 | 扣 credits | 落账 |
|---|---|---|
| 本地模式（`desktop-local` 本地后端 + 本地 provider） | **否** | 什么都不写 |
| 云端片段推理（本文档的 `POST /v1/desktop/infer`） | **否**（只记 usage） | 只写 Redis `desktop:infer:usage:{tenant}:{date}` |
| `dtk_` 结算（按设备/令牌维度出账） | **未启用** | 仓库内不存在任何 `dtk_` 键的读写 |

#### 9.3.1 云端片段推理：只记 usage，不扣 credits

- 成功请求把用量累加进 Redis Hash `desktop:infer:usage:{tenant}:{date}`（UTC 日期，TTL 40 天），
  字段**只有四个**：`requests` / `input_tokens` / `output_tokens` / `cached_tokens`。
- 这条链路上**没有** `credit_transactions`（用户余额流水，`turn_id` 唯一索引是扣费幂等键）、
  **没有** `billing_records`（企业成本中心明细）、**没有** `DeductTokens` / `RecordTokenUsage` /
  `TrueUpTenantQuota`。网关的 `DesktopInferHandler` 只接受 `*engine.PythonClient`，结构上没有计费入口，
  也不读响应体（读了就等于解析用户本地文档内容）；引擎侧只写上面那一个 Redis 键。
- 不扣 credits 的三条理由：
  1. 计价所需的 `provider` / `model` / `usage` 只在引擎侧可见，网关不解析响应体就看不见它们；
  2. credits 的商业口径（provider 定价 × 计费 token）只存在于 Go billing 模块
     （`internal/billing`，由 `internal/api/submit_handler.go` 的 biller 链路调用）。引擎再写一份 =
     同一段用量双写 ⇒ 重复扣费；
  3. 扣费是有余额后果、需要退款路径的动作。没有对账/退款闭环之前，宁可只落账本。
- 只落"用量账本"；自动计费（按 `desktop:infer:usage:*` 对账扣费）未实现，且扣费键前缀
  `dtk_` 尚未启用（见 9.3.3）。
- 计量口径的两个已知后果：命中**幂等重放**的请求不再计用量（正确 —— 上游确实没被调用第二次）；
  命中**缓存**的请求仍上报 provider 返回的 usage（`cached_tokens` 表示提示词缓存命中的输入）。

#### 9.3.2 本地模式不计费

- 本地模式的推理发生在用户自己的机器上（本地后端 + 用户自己的 provider key），云端**没有成本发生**，
  因此不存在任何计费动作：不调 billing、不写 `credit_transactions` / `billing_records`，
  也**不写** `desktop:infer:usage:*`。
- 不写 usage 是刻意的：`desktop:infer:usage:*` 是**云端**对账账本，把本地用量混进去会让后续自动计费
  把本地推理当云端消费来收钱（多收）。
- 硬约束：`desktop-local/**` 内不出现 credits/billing 相关代码（当前 `grep -i "credit\|billing"` 无命中），
  本地库也没有计费表。加计费表 / 计费调用必须先改本节。

#### 9.3.3 `dtk_` 结算：未启用

- 含义：将来"按设备/令牌维度结算片段推理"的扣费幂等键前缀，与 run 用量对账用的 `rsm_` 同型 ——
  `dtk_` + 摘要（键里带用量总量），长度 36 字符，可落 `credit_transactions.turn_id` /
  `billing_records.turn_id`（两列都是 `varchar(36)` 且各有唯一索引，同一段用量重复对账只会命中同一行）。
- 现状：**未实现**。仓库里没有任何代码写入或读取 `dtk_` 前缀的键（`grep -rn "dtk_"` 全仓无命中），
  谁也不能假设它存在。
- 为什么延后到产品确认：
  1. **计费主体是租户/用户，不是设备**：桌面设备 token 由本地后端自己生成
     （`desktop-local/app/api/auth.py`，`CHIRON_DEVICE_TOKEN` 可被主进程覆盖），**从不经过网关**，
     不能当作可信的计费身份；把它当扣费主体等于让客户端自证身份。
  2. 是否要按设备出账（例如免费额度按设备发放、换机迁移时历史用量归属哪个账号、重装后设备是否重置额度）
     是商业口径问题，不是实现细节。
  3. 先扣后改口径要补退款流水，代价远大于晚一周上线。
- 启用条件：本节先写清扣费主体、额度发放、迁移归属与退款路径，并补上对应用例；在那之前
  `/v1/desktop/infer` 保持"只记 usage"。

#### 9.3.4 谁在守着这些口径

- Go：`internal/api/routes_desktop_test.go` —— 真实代理路径的响应**逐字节**透传（网关不解析 ⇒ 无从计费）；
  并对 `desktop_infer.go` 做源码扫描，出现 `DeductTokens` / `RecordTokenUsage` / `TrueUpTenantQuota` /
  `credit_transactions` / `billing_records` 即失败。
- Python：`python-engine/tests/test_desktop_infer.py` —— 成功请求只写 `desktop:infer:idem:*` 与
  `desktop:infer:usage:*` 两类键，usage Hash 的字段集合严格等于上表四个，模块不 import 任何 db/billing。

### 9.4 缓存 key

网关 `SemanticCache` 的精确键与语义键都纳入 `response_format`（`python-engine/app/gateway/cache.py`）：

- `response_format is None` 的请求哈希与改动前**逐字节相同**，存量 L2/L3 条目继续命中，无需迁移；
- 非空时形状参与哈希：同一 prompt 先文本、后 `json_object` 请求**不会**命中前者的纯文本结果；
- 该约束有单测守着：`python-engine/tests/test_desktop_infer.py`。

## 10. LLM provider key：三层一致性与「配完不用重启」

一个 key 同时活在三层，改任何一层都必须让另外两层知道：

| 层 | 位置 | 形状 |
|---|---|---|
| 权威 | PG 表 `llm_provider_keys` | AES-256-GCM 密文，只有网关能解（`settings.Store`） |
| 运行时镜像 | Redis HASH `llm:keys:{provider}` | field = `key_hash[:12]`，value = JSON `{"k": 明文, "s": 权威状态, "w": 权重}`；引擎在同一条记录里另记 LRU/冷却/失败计数（`u`/`c`/`f`/`ft`）。版本计数 `llm:keys:ver` |
| 失效通知 | Redis PUBLISH `llm:keys:changed` | payload = provider 名 |

引擎的 `KeyRing`（`python-engine/app/gateway/key_ring.py`）**不维护进程内缓存**：`get_key` /
`report_failure` 各跑一段只碰这一个 HASH 的 Lua，读写在服务端原子完成；
但**哪些 provider 进路由表**是引擎 lifespan 里一次成型的（`python-engine/app/main.py:502 _start_gateway`）。
闸门三条，满足任一才注册：env 种子、keyset 里有该 provider 的 key、免-key 的本地端点被显式配置。

### 10.1 为什么必须"回填 + 惰性注册"两条都做

打包桌面态的启动顺序是 引擎(:893) → 网关(:918)，且 Redis 每次以空库起来
（`--appendonly no --save ""`）。于是：

- **只靠管理端写库后那一次镜像**：云端看不出问题（Redis 长驻、keyset 无 TTL，配一次活过所有重启），
  但桌面态「本机早先配好的 key」在引擎眼里等于从没配过 ⇒ 每条回合立刻
  `no LLM provider available for model=…: 引擎当前没有注册任何提供商`。解法是网关启动时按权威层整体重建：
  `cmd/chiron/main.go:152` → `api.HydrateProviderKeysets`（`internal/api/llm_keys.go:154`），
  逐 provider 调 `syncProviderKeyset`，用**一次原子 Lua** 先 DEL 再逐对 HSET
  （`llmReplaceKeyset`，`internal/api/llm_keys.go:202`）—— 增量 HMSET 会让删掉的 key 复活。
  Redis 不可用时如实返回 `(0, nil)`：引擎退化为 env 种子，不谎报成功也不阻断启动。
- **只回填还不够**：回填发生在网关起来之后，**晚于**引擎的注册表成型。所以引擎必须能在运行期
  把新 keyset 变成新 provider —— 订阅 `llm:keys:changed`
  （`app/main.py:965 _start_llm_keys_watch` / `:981 _listen_llm_keys_changes`）并调
  `router.refresh_providers(force=True)`。订阅断线按 5s 退避重连，不影响主流程。

### 10.2 运行期注册的约束

- 只能走 `GatewayRouter.register_provider`（`app/gateway/router.py:149`）：它同时补
  `_providers` / `_breakers` / `_embed_breakers` / `_latencies` / `_catalog`。后四张表在构造时
  按下标直取，少一张就是 KeyError；`_catalog` 是目录的**副本**，不补就路由不到 model 前缀。
  已有的熔断状态**不覆盖**（新 key 不该让正在退避的 provider 立刻满血）。
- `refresh_providers`（`app/gateway/router.py:168`）带 2s 去抖：时间戳在 `await` **之前**占位，
  否则并发刷新会一起越过阈值；`force=True` 绕过去抖（广播路径就是要立刻生效）；
  `_refresh_lock` 保证同进程内扫描不重叠。回调异常吞掉并返回 0 —— 刷新失败不能让路由表更糟。
- 刷新触发点有三处：路由未命中（`reason="route-miss"`）、全部熔断（`reason="all-circuit-open"`）、
  密钥变更广播（`reason="keyset-changed:{provider}"`）。

### 10.3 失败可见性

空表与非空表两条文案（`app/gateway/router.py:606 _no_provider_message`）：

- 空表只指**管理后台 → API 密钥**，并明说「保存后无需重启」；**不出现 `.env` 字样** ——
  打包桌面态用户够不着 `.env`，指过去等于给一条死路。
- 非空表点名已注册的 provider，并把怀疑方向指向 BASE_URL 与熔断退避。

### 10.4 谁在守

- Go：`internal/api/llm_keys_hydrate_test.go`（回填的选取与跳过口径）、
  `internal/api/llm_keys_hydrate_live_test.go`（真 Redis 上重建后**必须**有 `llm:keys:changed`）。
- Python：`python-engine/tests/test_gateway_runtime_registration.py`（12 例：五张表补齐、运行期
  provider 真能出字/被前缀路由/能服务 embed、保留熔断状态、route-miss 与全熔断两条补注册路径、
  去抖与 force、并发不重叠、回调异常不致命、两条文案）、
  `python-engine/tests/test_llm_keys_watch.py`（7 例：前缀化频道、bytes/str 两种 payload 都强制刷新、
  网关未就绪只丢不炸、断线重连并关掉旧 pubsub、关闭段取消订阅任务、两条 AST 护栏）。

## 11. 会话事件流 `/events`：身份、续传与静默

链路：`GET /events` / `GET /v1/events` 在 `authMW(rlMW(sseHandler))` 之后
（`internal/api/routes_agent.go:348`）⇒ **必须带 Authorization**。前端实现是
`fetch` + `ReadableStream`（`frontend-vue/src/api/index.ts:625 openEventStream`，会话流是它的
薄壳 `:757 createSSEConnection`），不是 `EventSource`（原因见 §8）。

**同一条实现服务两条长连接**：`GET /v1/agent/interactions/stream`（跨会话待办角标，
`frontend-vue/src/api/interactions.ts:70 createUserEventsConnection`，传 `autoReconnect: true`）
也在 `authMW` 之后（`internal/api/routes_agent.go:106`）。它此前也是 `EventSource` —— 桌面态静默
401，角标只剩 30s 轮询兜底（"实时提醒"退化成"半分钟提醒"）；两条连接分开实现就只会修好一条，
所以身份/帧解析/重连/关闭只有一份。

| 项 | 口径 |
|---|---|
| 身份 | `requestToken()`：本地模式 = 设备 token / 本地 JWT，云端 = 登录 token；**只进 header**，URL 里不再有 `token=` |
| cookie | 桌面端 `credentials: 'same-origin'`（`file://` 是跨站，cookie 带不过去），Web 端 `'include'` |
| 帧切分 | 空行分帧（连续两个换行）；CRLF 归一必须做在**累计缓冲**上 —— 跨块的「行尾 CR + 行首 LF」也要拼回边界；冒号后的第一个空格属于协议 |
| 派发 | 空 `data` 与命名事件**不**进 `onMessage` —— 本地 core 兜底通道发的是 `event: heartbeat`（`desktop-local/app/api/events.py`），派发出去就是对话里的幽灵消息 |
| 非 2xx | 立即 `onError` 且不重连：401/403 不会自己好，重试只会把失败拖成「思考中」不消失 |
| 续传 | 收到带 `id:` 的帧就记录（并回调 `onLastEventId`），重连时放进 `Last-Event-ID` 头（网关 header 优先于 query，`internal/api/events.go:49`）；空值不发这个头 |
| 重连 | 只在 `autoReconnect` 下发生（默认关 = 旧语义「断线立即报错」），连续失败上限默认 3；计数**只在真的收到过字节**时清零 —— 只看响应头会被「接受连接就收尾、什么都不发」的端点每次清零 ⇒ 无限重连 |
| 关闭 | `close()` = `AbortController.abort()` + 幂等静默；自己关的不算故障，不回调 `onError`（否则切会话就弹「连接已断开」） |

遗留：`session_id` 仍在 URL query（服务端有所有权校验），换 WebSocket 才能从日志/历史里去掉，本批次不动。
子 Agent 那条 `GET /v1/subagent/runs/{run_id}/events` 走的是轮询读 Redis Stream，不是这条长连接。

守这条契约的用例：`frontend-vue/src/api/__tests__/sseConnection.spec.ts`（11 例，含一条
"不许退回 `new EventSource`" 的源码护栏），以及 `ChatView.interaction.spec.ts` 里把
`createSSEConnection` 打桩成真实句柄形状（只有 `close()`）。
