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
| Python 运行时 | `<userData>/local/runtime/`（venv 与依赖） | **解释器随包**（`resources/python-runtime/`），venv 与依赖首次启动由随包 `uv` 现装 |
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

## 3. Python 运行时（解释器随包 + 依赖首装）

安装包**带一份纯解释器**（`resources/python-runtime/<platform>-<arch>/<版本目录>/`）
和 `uv` 二进制，但**不带 venv、不带已装依赖**。首次启动时由壳层调用 uv：

1. `uv venv --python <随包解释器的绝对路径> --python-preference only-managed` 在
   `<userData>/local/runtime/venv` 建空环境。传绝对路径是为了让 uv **完全不联网找
   解释器** —— 这一点很关键，见下面的「为什么解释器必须随包」；
2. `uv pip install -r <resources>/desktop-local/pyproject.toml` 装依赖
   （`embedded_postgres` 自带的整套 PG 二进制随依赖一起进来）；
3. 真实 `import` 一次本地核心做验收，通过才写安装清单。

用户机上不需要预装 Python，也不需要 pip。这条路径的全部实现在
`frontend-vue/electron/runtime-install.cjs`。

**为什么解释器必须随包**：只带 uv 时，`uv venv --python 3.12 --python-preference
only-managed` 在本地没有受管 CPython 时会去 **GitHub Releases 下载
python-build-standalone**。在 `github.com` 不可达的网络下（企业内网、部分地区）这一步
会以 `invalid peer certificate: UnknownIssuer` 失败，而且**发生在创建窗口之前**，表现
出来就是"首次启动卡住、所有页面报服务繁忙"——它不是靠重试能自愈的。实测确认：随包
解释器 + 绝对路径后，建 venv 全程离线且 1 秒内完成（`Using CPython 3.12.16
interpreter at: <resources>/...`）。

**为什么依赖仍然首装而不是随包**：随包一份装好依赖的运行时意味着安装包与构建机的
Python 版本、架构强绑定，且升级任何一个依赖都要重新出包。依赖清单只留
`desktop-local/pyproject.toml` 一份，装出来的环境天然与本机匹配。

**代价**：首次启动需要联网装依赖（建 venv 已完全离线）。网络受限时可设
`CHIRON_PIP_INDEX_URL` 指向可达的镜像源。

`desktop-local/scripts/prepare_uv.py` 负责把 uv 就位到 `frontend-vue/assets/uv/<platform>/`，
`electron-builder.yml` 再把它映射成 `resources/uv/`：

- 版本号与各平台 SHA256 固化在 `desktop-local/scripts/uv_manifest.json`；
- 下载后**强校验** SHA256，不符即非零退出（`--from` 可指定已下载的压缩包离线就位）；
- `--check` 只做自检：真实执行 `uv --version` 并比对清单里的版本号。

构建脚本会在 `electron-builder` 之前自动调用它：

```bash
npm run prepare:uv        # 就位 frontend-vue/assets/uv/<platform>-<arch>/
npm run prepare:python    # 就位 frontend-vue/assets/python-runtime/<platform>-<arch>/
npm run prepare:redis     # 就位 desktop-local/bin/redis/
```

`prepare:python` 需要联网下载解释器。若 `github.com` 不可达（uv 默认从那里取
python-build-standalone），用 `--mirror <地址>` 换源，或用 `--from <已就位目录>` 走离线。
**这一步不能省**：只带 `uv` 而没带解释器时，首次启动会卡在创建环境（详见第 3 节）。

`prepare_uv.py` 的下载步骤同样需要联网；离线构建请先用 `--from` 指定压缩包。

**构建机上的 Python 依赖**：`prepare_uv.py` / `prepare_python.py` 只用标准库，但
`prepare_redis.py` 不是——它的自检要真起一次 redis 并跑契约命令，因此需要
`pydantic`、`pydantic-settings`、`redis` 三个包（自检构造 `Settings(pg_enabled=False)`，
不会走到 PG / alembic 分支，所以 `asyncpg`、`alembic`、`psycopg2` 可以不装）。
CI 里由 `Install build-script dependencies` 那一步负责；
`desktop-local/tests/test_prepare_redis_deps.py` 会把脚本的 import 与那条 pip 命令对齐，
新增导入却忘了改 CI 时会先红掉。

**三个构建脚本都会把 stdout/stderr 固定为 UTF-8**：`windows-latest` 是 en-US 镜像，
stdout 默认 cp1252，而这些脚本的输出含中文，`print` 会抛 `UnicodeEncodeError`
（表现是「下载 uv」那一行整个 job 崩掉，与构建逻辑无关）。

### 启动时的解析顺序

`components.cjs::resolveCoreLaunch()` 在打包态按下列顺序**逐个真实探测**
（`probeBundledRuntime` 会实际 `import fastapi, uvicorn, asyncpg, embedded_postgres`）：

1. `<userData>/local/runtime/venv` —— 首次启动自装的运行时（正常情况）
2. `resources/python` —— 旧形态安装包里的随包运行时（只为兼容，新包不带）
3. `CHIRON_PYTHON` 环境变量
4. `PATH` 上的 `python`

谁真的能导入依赖就用谁；**不会盲用**下一个候选。全都不可用时抛出的错误会逐个列出
候选与具体原因（例如"缺少模块 fastapi"），并给出修复路径，而不是静默回退、
让用户只看到"服务繁忙"。

之所以要逐个探测而不是简单回退：用户机上 `PATH` 里的 `python` 可能是 Microsoft Store
的占位符（spawn 成功但立刻退出），也可能装了 Python 却没有 `fastapi`/`asyncpg`，
这两种情况都会让 core 起不来，而错误信息对用户毫无指导性。

## 4. 构建命令

```bash
cd frontend-vue

# 一条命令就位 uv 与 redis，再出解包目录（真机验证用，最快）
npm run desktop:pack
```

`desktop:pack` / `desktop:dist` 已把 `prepare:uv` 与 `prepare:redis` 串在前面，
不需要手工解压 uv 或拷贝 redis。等价的手工步骤：

```bash
python desktop-local/scripts/prepare_uv.py    # 就位 frontend-vue/assets/uv/<platform>-<arch>/
python desktop-local/scripts/prepare_redis.py # 就位 desktop-local/bin/redis/
```

只跑 electron-builder（复用已就位的 uv / redis）：

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
ls release/win-unpacked/resources/desktop-local/pyproject.toml  # 依赖清单在位（uv 靠它装）
ls release/win-unpacked/resources/migrations/versions   # 迁移在位
ls release/win-unpacked/resources/uv/win32-x64/uv.exe   # 随包 uv 在位（首次启动自装的入口）
ls release/win-unpacked/resources/desktop-local/bin/redis/redis-server.exe
release/win-unpacked/Chiron.exe --remote-debugging-port=9223
```

**`resources/uv` 必须存在**：缺了它首次启动装不出运行时，用户机上 core 起不来，
前端所有页面会报"服务繁忙"。`prepare_uv.py --check` 会真实执行 `uv --version`
并比对清单版本号，失败即非零退出。

**`resources/python` 不应存在**：那是「解释器 + 已装依赖」的旧形态，已被
`resources/python-runtime/`（纯解释器）+ 首次启动装依赖取代（见第 3 节）。
CI 里有反向断言，防止它被悄悄加回来。

**`resources/python-runtime/<platform>-<arch>/` 必须存在**：缺了它首次启动会去 GitHub
下载解释器，在受限网络下必然失败。CI 会找到其中的 `python[.exe]` 并执行一次验证。

真机启动后从 CDP 检查：

- `window.chironDesktop.mode === 'local'`
- 首次启动时先出现安装进度窗，装完自动关闭并进主界面
- `<userData>/local/runtime/install.json` 存在，`dependencyFingerprint` 非空
- `GET {apiUrl}/readyz` 返回 `pg=true`、`migration.ok=true`
- 退出后 `netstat` 无 core / PG 残留监听

## 6. 已知限制

- 未签名产物在 macOS 上需要用户手动放行；Windows SmartScreen 会拦。
- `asarUnpack` 只解包 wasm 与 `vendor/**`；新增二进制资源时记得补规则。
- **首次启动需要联网装依赖**：解释器已随包，建 venv 完全离线；但 `uv pip install`
  要访问 index。网络受限时可设 `CHIRON_PIP_INDEX_URL` 指向可达镜像，或事后把环境
  放到 `<userData>/local/runtime`。
- 首次安装较重：`embedded-postgres` 自带整套 PG 二进制，首次要拉几十到上百 MB。
- 构建期也需要联网下载 uv（约 15–20MB/平台）；离线构建用
  `prepare_uv.py --from <压缩包>`。
- redis 二进制来自构建机的发行版（含 MSYS2 运行时 DLL）；脚本会解析 scoop 的
  shim 取真身，但不会跨平台转换 —— Linux/macOS 包需在对应平台生成。
- **Windows 的 redis 只能走预编译发行版**：MSYS2 仓库里只有客户端库
  （`hiredis`、`python-redis`、`redis-plus-plus`），没有 redis 服务端，
  `pacman -S mingw-w64-x86_64-redis` 必然 `target not found`。CI 改为按 tag
  下载 `redis-windows` 的 `Redis-<ver>-Windows-x64-msys2.zip` 并校验 SHA256
  （版本与哈希钉在 workflow 里，不用 `latest`）。该构建自带 `msys-*.dll`，
  且实测接受 Windows 原生 `--dir` 路径，无需 cygdrive 形式。

