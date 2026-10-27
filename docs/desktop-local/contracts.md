# 桌面端双模式 / 本地化开发契约（第一批）

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

`window.chironDesktop` 必须暴露以下字段（`preload.cjs`）。第一批先实现前 6 项；组件相关项
由 `components.cjs` 提供、`preload.cjs` 接线。

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
}

interface ChironDesktop {
  isDesktop: true
  platform: 'win32' | 'darwin' | 'linux'
  apiUrl: string
  mode: ChironMode

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

  // 本地抽取（P3 接线；第一批只需类型占位）
  pickDocument(): Promise<{ path: string; name: string } | null>
}
```

IPC channel 命名统一 `desktop:<verb>`：

```
desktop:get-device-settings
desktop:set-mode
desktop:restart
desktop:list-components
desktop:install-component
desktop:remove-component
desktop:start-component
desktop:stop-component
desktop:pick-document
```

所有 `ipcMain.handle` 必须先 `assertTrustedSender(event)`。

## 3. 进程与模块边界

```
frontend-vue/electron/
  main.cjs               # 壳：窗口、生命周期、接线（W1 独占）
  preload.cjs            # IPC 暴露（W1 独占）
  config.cjs             # 设备设置读写（W1 独占）
  shared.cjs             # 常量（W1 独占）
  mode-switch.cjs        # 切换 + 清除（W1 新增）
  components.cjs         # 组件管理（W4 新增，W1 接线）
  inference-proxy.cjs    # loopback 推理代理（W4 新增，W1 接线）
  secrets.cjs            # key 加密存储（W4 新增，W1 接线）
  __tests__/             # 纯 Node 单测（W4 可写；W1 不写）
```

- W1 不写 `components.cjs` / `inference-proxy.cjs` / `secrets.cjs` 的实现；只 require + 接线。
- W4 不写 `main.cjs` / `preload.cjs` / `config.cjs` / `shared.cjs`。
- 两边如需新增共享常量，先加到本文件 §3，再各自实现。

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

第一批只建骨架与 P0 验证，不迁移业务代码。

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

本地 API 契约（第一批）：

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` |
| GET | `/readyz` | `{"status":"ok","pg":true,"redis":true}` |
| POST | `/v1/auth/session` | `{"data":{"token":"...","user":{"id":"local","name":"Local","role":"owner","tenant_id":1}}}` |
| GET | `/v1/models` | `{"data":[]}`（占位） |
| GET | `/events?session_id=` | `text/event-stream`（第一批只发心跳） |

统一响应包装：与 Go 网关一致，业务响应放 `{"data": ...}`。

鉴权：`Authorization: Bearer <device-token>` 或同源 httpOnly cookie；第一批允许无鉴权启动，
但 `/readyz` 必须报告 `auth: "disabled"`。

## 5. P0 验证的硬指标

`desktop-local/scripts/verify_p0.py` 必须输出：

1. `embedded-postgres` 能启动、`alembic upgrade head` 能跑通（复用仓库 `migrations/`）。
2. 数据目录可指定；停止后重启数据仍在。
3. `fakeredis` 对 `XADD / XREADGROUP / XACK / XCLAIM / XAUTOCLAIM / PUBLISH / SUBSCRIBE`
   的实际支持情况（逐项 PASS/FAIL，不许假设）。
4. 冷启动耗时（毫秒）与数据目录体积。
5. 失败时给出可执行的替代方案（Valkey 二进制 / 缩小 core 范围）。

## 6. 验收命令

各 worktree 在提交前至少运行：

```bash
# W1 / W4（Electron 主进程）
node --check frontend-vue/electron/main.cjs
node --check frontend-vue/electron/preload.cjs
node --check frontend-vue/electron/config.cjs
node --check frontend-vue/electron/components.cjs
node --check frontend-vue/electron/inference-proxy.cjs
node --check frontend-vue/electron/secrets.cjs

# W2（前端）
cd frontend-vue && pnpm exec vue-tsc -b
cd frontend-vue && pnpm test
cd frontend-vue && pnpm check:ui

# W3（本地后端）
cd desktop-local && python -m ruff check .
cd desktop-local && python -m pytest -q
python desktop-local/scripts/verify_p0.py
```

CI/本机无法运行的项（如 Electron 真机启动）在提交说明里写“未验证 + 原因”，不要假装通过。

## 7. 分支与合并

- 集成分支：`alpha`（从 `master` 切出）。
- 开发分支：
  - `feat/desktop-mode-shell`（W1）
  - `feat/desktop-local-frontend`（W2）
  - `feat/local-core`（W3）
  - `feat/local-components`（W4）
- 每个 worktree 独立目录：`X:/project/Chiron-worktrees/<name>`。
- 只提交到自己的分支；**不要** push、不要合并、不要改 `alpha`/`master`。
- 合并顺序：W3 → W4 → W1 → W2（先底层后接线）。
- 合并后在 `alpha` 跑完整门禁；`alpha` 验证通过后再合并 `master`。

## 8. 禁止事项

- 不要在 `python-engine/` 主依赖里加入 `langextract` / `pageindex`。
- 不要在渲染进程暴露任意 `fs` / `require` / `ipcRenderer`。
- 不要把用户 provider key 写入日志、SQLite、localStorage 或备份。
- 不要默认删除 `userData/local/**`。
- 云端片段推理端点见 §9（第二批已实现）。
- 不要改 `internal/**`（Go）与 `frontend-vue/src/**` 之外的共享文件而不在本文件登记。

## 9. 桌面片段推理端点（第二批）

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

- 成功的请求把用量累加进 Redis Hash `desktop:infer:usage:{tenant}:{date}`（UTC 日期，TTL 40 天），
  字段：`requests` / `input_tokens` / `output_tokens` / `cached_tokens`。
- **该键不含 credits**：credits 的商业口径（provider 定价 × 计费 token）与扣费都在 Go billing
  模块（`internal/api/submit_handler.go` 的 biller 链路）。引擎再写一份 credits 会导致重复扣费。
- 因此当前 `/v1/desktop/infer` **不在网关扣 credits**：网关不读响应体（读了就等于解析用户内容），
  计价所需的 `provider` / `model` / `usage` 只在引擎侧可见。本批只落"用量账本"，
  自动计费（按 `desktop:infer:usage:*` 对账扣费）是后续工作。
- 计量口径的一个已知后果：命中**幂等重放**的请求不再计 token（这是正确的 —— 上游确实没被调用一次）；
  命中**缓存**的请求仍会上报 provider 返回的 usage（其中 `cached_tokens` 表示提示词缓存命中的输入）。

### 9.4 缓存 key

网关 `SemanticCache` 的精确键与语义键都纳入 `response_format`（`python-engine/app/gateway/cache.py`）：

- `response_format is None` 的请求哈希与改动前**逐字节相同**，存量 L2/L3 条目继续命中，无需迁移；
- 非空时形状参与哈希：同一 prompt 先文本、后 `json_object` 请求**不会**命中前者的纯文本结果；
- 该约束有单测守着：`python-engine/tests/test_desktop_infer.py`。

