# 打包与体积

> 目标：`app.asar` 控制在 100MB 以内，安装包增量主要来自 `dist/` 与随包资源，
> 而不是被重复打包的 `node_modules`。
>
> 相关文件：`frontend-vue/electron-builder.yml`、`frontend-vue/electron/components.cjs`、
> `frontend-vue/electron-builder.yml` 的 `extraResources`。

## 1. 体积构成

| 部分 | 位置 | 说明 |
|---|---|---|
| Electron 运行时 | `release/win-unpacked/*.dll`、`Chrome.exe` | 由 electron-builder 下载，约 250MB |
| 前端产物 | `resources/app.asar` 内的 `dist/` | Vite 构建，含 vendor chunk 与 wasm |
| 本地 core | `resources/desktop-local/` | Python 源码（`extraResources`） |
| 迁移 | `resources/migrations/`、`resources/alembic.ini` | alembic 需要真实文件路径 |
| 可选组件 | `resources/components/` | PageIndex / LangExtract，按需下载 |
| Python 运行时 | `resources/python/` | 可选；不存在时回退 `CHIRON_PYTHON` / PATH |

**改造前实测**（2026-10-27）：`app.asar` 863MB、`win-unpacked` 1.3GB。
原因是 `package.json` 的 production dependencies 被 electron-builder 自动打进 asar，
而 Vite 已经把同一批依赖 bundle 进 `dist/`。

## 2. 排除策略

`electron-builder.yml` 的 `files` 显式排除 `node_modules/**`：

```yaml
files:
  - electron/**/*
  - dist/**/*
  - package.json
  - "!node_modules/**"
```

- 前端运行时依赖全部来自 `dist/`，不需要 `node_modules`。
- `main.cjs` / `preload.cjs` 只用 Node 内置模块（`fs` / `path` / `net` / `crypto` /
  `child_process`）与 `electron` 本身。
- **唯一例外是 `electron-updater`**：`setupAutoUpdater()` 里 `require('electron-updater')`
  会失败，但已有 try/catch 捕获并只记一条 warn，启动不受影响。
  未签名构建本来就不能自动更新，所以当前形态下这是可接受的取舍。

签名接入后若要恢复自动更新，按下面两种方式之一：

1. 把 `electron-updater` 及其依赖单独放到 `extraResources`，在 `main.cjs` 里按
   `process.resourcesPath` 解析；或
2. 改用 electron-builder 的 `files` 白名单只保留 `electron-updater` 一棵依赖树。

## 3. Python 运行时（可选）

默认不随包，`components.cjs::bundledPython()` 会依次尝试：

1. `resources/python/python.exe`（或 `resources/python/bin/python3`）
2. `CHIRON_PYTHON` 环境变量
3. `PATH` 上的 `python`

要随包发布，把 `python-build-standalone` 解压到 `resources/python/` 即可，
无需改代码。

## 4. 构建命令

```bash
cd frontend-vue

# 解包构建（真机验证用，最快）
NODE_TLS_REJECT_UNAUTHORIZED=0 \
CSC_IDENTITY_AUTO_DISCOVERY=false \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
pnpm exec electron-builder --win --dir --publish never

# 正式安装包（NSIS）
NODE_TLS_REJECT_UNAUTHORIZED=0 \
CSC_IDENTITY_AUTO_DISCOVERY=false \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
pnpm exec electron-builder --win --publish never
```

### TLS / 镜像排障

本机网络对 electron-builder 的下载源存在证书校验与连接复用问题，实测报错：

- `unable to verify the first certificate` → 需要 `NODE_OPTIONS=--use-system-ca`
  或 `NODE_TLS_REJECT_UNAUTHORIZED=0`；
- `read ECONNRESET` → 换 `ELECTRON_BUILDER_BINARIES_MIRROR`（npmmirror）。

**这些开关只用于本机构建，不要写进 CI**：CI 的 runner 证书链是正常的，
关掉校验会让供应链风险变成默认值。

## 5. 验证清单

构建后至少确认：

```bash
ls -la release/win-unpacked/resources/app.asar           # 目标 < 100MB
ls release/win-unpacked/resources/desktop-local/app     # core 源码在位
ls release/win-unpacked/resources/migrations/versions   # 迁移在位
release/win-unpacked/Chiron.exe --remote-debugging-port=9223
```

真机启动后从 CDP 检查：

- `window.chironDesktop.mode === 'local'`
- `GET {apiUrl}/readyz` 返回 `pg=true`、`migration.ok=true`
- 退出后 `netstat` 无 core / PG 残留监听

## 6. 已知限制

- 未签名产物在 macOS 上需要用户手动放行；Windows SmartScreen 会拦。
- `asarUnpack` 只解包 wasm 与 `vendor/**`；新增二进制资源时记得补规则。
- 随包 Python 未接入构建流水线，目前靠手工解压到 `resources/python/`。
