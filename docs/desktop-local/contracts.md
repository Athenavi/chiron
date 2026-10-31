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
  }
}
```

约束：

- 切换模式只清**离开模式**的 cookie / localStorage / IndexedDB / CacheStorage / Service Worker /
  HTTP 缓存 / token；**默认不删** `userData/local/**` 用户数据。
- 切换后必须重启应用（`app.relaunch()` + `app.exit(0)`），避免两个后端并存。
- 老用户已有 `apiUrl` 且无 `mode` 时，首次启动弹迁移提示，默认 `local`，但保留 `apiUrl`。

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

IPC channel 命名统一 `desktop:<verb>`。`ipcMain.handle` 实际注册的 17 个：

```
desktop:get-device-settings
desktop:set-mode
desktop:restart
desktop:test-api-url
desktop:set-api-url
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
`desktop:runtime-stage` —— 首次启动的运行时安装进度，仅 `install.html` 订阅。

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
