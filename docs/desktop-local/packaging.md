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
| Python 运行时 | `resources/python/` | **随包**：解释器 + `desktop-local` 全部依赖 + PG 二进制，约 150MB |
| Redis 二进制 | `resources/desktop-local/bin/redis/` | **随包**：`redis-server` + MSYS2 运行时 DLL |

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

## 3. Python 运行时（随包）

安装包**自带**一份精简 Python 运行时，用户机上不需要预装 Python，也不需要 pip。

`desktop-local/scripts/prepare_python_runtime.py` 负责生成 `frontend-vue/python-runtime/`，
`electron-builder.yml` 再把它映射成 `resources/python/`：

- 解释器与标准库：剔除 `Lib/test`、`idlelib`、`ensurepip` 等，约 40MB；
- `desktop-local` 的全部依赖装在 `Lib/site-packages/`（含 `embedded_postgres` 自带的
  整套 PG 二进制，约 56MB），装完约 150MB；
- 依赖由**宿主机**的 `pip install --target` 装入 —— 精简运行时没有 pip，在目标解释器里
  执行 `-m pip` 必然失败。

构建脚本会在 `electron-builder` 之前自动调用它：

```bash
npm run prepare:runtime   # 生成 frontend-vue/python-runtime/
npm run prepare:redis     # 就位 desktop-local/bin/redis/
```

两者都带自检：`prepare_python_runtime.py --check` 会在干净环境下真实 import 关键模块，
`prepare_redis.py` 会真起一个实例跑 8 条命令（含 `EVAL`）。任一步失败即非零退出。

### 启动时的解析顺序

`components.cjs::resolveCoreLaunch()` 在打包态按下列顺序**逐个真实探测**
（`probeBundledRuntime` 会实际 `import fastapi, uvicorn, asyncpg, embedded_postgres`）：

1. `resources/python/python.exe`（或 `resources/python/bin/python3`）
2. `CHIRON_PYTHON` 环境变量
3. `PATH` 上的 `python`

谁真的能导入依赖就用谁；**不会盲用**下一个候选。三者都不可用时抛出的错误会逐个列出
候选与具体原因（例如"缺少模块 fastapi"），并给出修复路径（重装 / 重新打包），
而不是静默回退、让用户只看到"服务繁忙"。

之所以要逐个探测而不是简单回退：用户机上 `PATH` 里的 `python` 可能是 Microsoft Store
的占位符（spawn 成功但立刻退出），也可能装了 Python 却没有 `fastapi`/`asyncpg`，
这两种情况都会让 core 起不来，而错误信息对用户毫无指导性。

## 4. 构建命令

```bash
cd frontend-vue

# 一条命令就位运行时与 redis，再出解包目录（真机验证用，最快）
npm run desktop:pack
```

`desktop:pack` / `desktop:dist` 已把 `prepare:runtime` 与 `prepare:redis` 串在前面，
不需要手工解压 Python 或拷贝 redis。等价的手工步骤：

```bash
python desktop-local/scripts/prepare_python_runtime.py   # 生成 frontend-vue/python-runtime/
python desktop-local/scripts/prepare_redis.py            # 就位 desktop-local/bin/redis/
```

只跑 electron-builder（复用已生成的运行时）：

```bash
NODE_TLS_REJECT_UNAUTHORIZED=0 \
CSC_IDENTITY_AUTO_DISCOVERY=false \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
pnpm exec electron-builder --win --dir --publish never
```

正式安装包（NSIS）把 `--dir` 去掉即可。

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
ls release/win-unpacked/resources/python/python.exe     # 随包运行时在位
ls release/win-unpacked/resources/python/Lib/site-packages/embedded_postgres/pginstall/bin/initdb.exe
ls release/win-unpacked/resources/desktop-local/bin/redis/redis-server.exe
release/win-unpacked/Chiron.exe --remote-debugging-port=9223
```

**`resources/python` 必须存在**：缺了它用户机上 core 起不来，前端所有页面会报
"服务繁忙"。壳层现在会在启动时探测并报出具体缺什么，但打包阶段就该拦住 ——
`prepare_python_runtime.py --check` 会真实 import 关键模块，失败即非零退出。

真机启动后从 CDP 检查：

- `window.chironDesktop.mode === 'local'`
- `GET {apiUrl}/readyz` 返回 `pg=true`、`migration.ok=true`
- 退出后 `netstat` 无 core / PG 残留监听

## 6. 已知限制

- 未签名产物在 macOS 上需要用户手动放行；Windows SmartScreen 会拦。
- `asarUnpack` 只解包 wasm 与 `vendor/**`；新增二进制资源时记得补规则。
- 随包 Python 目前从**构建机**的解释器精简而来（`prepare_python_runtime.py` 的
  `--from` 默认取本机 Python），因此产物与构建机的 Python 版本绑定；
  跨平台/跨版本发布时需在对应平台各构建一次。
- 随包运行时约 150MB，安装包体积会显著增加。这是"用户机开箱即用"的代价：
  不随包时用户机上大概率没有 `fastapi`/`asyncpg`，core 必然起不来。
- redis 二进制来自构建机的发行版（含 MSYS2 运行时 DLL）；脚本会解析 scoop 的
  shim 取真身，但不会跨平台转换 —— Linux/macOS 包需在对应平台生成。
