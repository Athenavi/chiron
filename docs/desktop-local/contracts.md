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
- 不要实现“云端片段推理”端点（第二批再做）。
- 不要改 `internal/**`（Go）与 `frontend-vue/src/**` 之外的共享文件而不在本文件登记。
