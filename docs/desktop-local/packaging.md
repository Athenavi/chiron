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
| Python 运行时 | `<userData>/local/runtime/`（解释器、venv 与依赖） | **都不随包**：首次启动按需下载 `uv`（多源测速 + SHA256 强校验），再由 uv 取受管 CPython |
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

## 3. Python 运行时（全部按需下载 + 依赖首装）

安装包**不携带任何 Python 运行时**：既没有 `resources/python-runtime/`，也没有
`resources/uv/`。首次启动时由壳层现取现装：

1. 取 `uv`：`runtime-fetch.cjs` 对候选源**并发测速**，按快慢排序后逐个尝试下载
   wheel 并解出二进制，落到 `<userData>/local/runtime/bin/uv[.exe]`。每个候选都做
   **SHA256 强校验**（清单见 `desktop-local/scripts/uv_manifest.json`），不符即弃用该源；
2. 建 venv：`uv venv --python 3.12 --python-preference only-managed` 在
   `<userData>/local/runtime/venv` 建空环境。解释器**由 uv 自己下载**到
   `UV_PYTHON_INSTALL_DIR`（同一份 userData 下），外壳不自写 pbs 下载器；
3. `uv pip install -r <resources>/desktop-local/pyproject.toml` 装依赖
   （`embedded_postgres` 自带的整套 PG 二进制随依赖一起进来）；
4. 真实 `import` 一次本地核心做验收，通过才写安装清单。

用户机上不需要预装 Python，也不需要 pip。这条路径的全部实现在
`frontend-vue/electron/runtime-install.cjs`（下载层在 `runtime-fetch.cjs`）。

**为什么解释器也不随包**：随包解释器会把安装包撑大 15–20MB/平台，且与构建机的
架构强绑定；而"解释器已随包"还会掩盖另一件事——真正卡住用户的是 `github.com`
不可达导致 uv 取不到 python-build-standalone。所以镜像必须可选可换，见下。

**镜像为什么不能写死**：uv 默认从 GitHub Releases 取 python-build-standalone，在
`github.com` 不可达的网络下会以 `invalid peer certificate: UnknownIssuer` 失败，
且**发生在创建窗口之前**，表现为"首次启动卡住、所有页面报服务繁忙"。因此候选源是
一个列表、运行时测速择优；也可显式覆盖：

- `CHIRON_UV_MIRROR` —— uv 二进制的 PyPI 镜像前缀；
- `CHIRON_PYTHON_MIRROR` / `UV_PYTHON_INSTALL_MIRROR` —— 受管 CPython 的来源。

**为什么依赖仍然首装而不是随包**：随包一份装好依赖的运行时意味着安装包与构建机的
Python 版本、架构强绑定，且升级任何一个依赖都要重新出包。依赖清单只留
`desktop-local/pyproject.toml` 一份，装出来的环境天然与本机匹配。

**代价**：首次启动需要联网（取 uv、取解释器、装依赖三者都要）。网络受限时按上面的
环境变量换源；`CHIRON_PIP_INDEX_URL` 另管 `uv pip install` 的 index。

`desktop-local/scripts/prepare_uv.py` 仍然保留，但**不再参与出包**：它是构建/开发期
的离线取件工具，产物在以前的布局里会落到 `frontend-vue/assets/uv/<platform>/`。
现在 `electron-builder.yml` 已不再映射这个目录，安装包里不会有它：

- 版本号与各平台 SHA256 固化在 `desktop-local/scripts/uv_manifest.json`；
- 下载后**强校验** SHA256，不符即非零退出（`--from` 可指定已下载的压缩包离线就位）；
- `--check` 只做自检：真实执行 `uv --version` 并比对清单里的版本号。

```bash
npm run prepare:redis     # 就位 desktop-local/bin/redis/（唯一仍随包的二进制）
npm run prepare:uv        # 仅开发期离线取 uv，不进安装包
npm run prepare:python    # 同上，仅离线取解释器供本地调试
```

`prepare_uv.py` / `prepare_python.py` 的下载步骤同样需要联网；要离线就位请先用 `--from` 指定压缩包。

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

1. `<userData>/local/runtime/venv` —— 首次启动按需装的运行时（**唯一正常来源**）
2. `CHIRON_PYTHON` 环境变量 —— 用户/开发者显式指定

**刻意不再兜底到 `PATH` 上的 `python`**。这一条是踩出来的：兜底存在时，只要用户机上
碰巧有个装了 `fastapi`/`asyncpg` 的 `python`，候选链就会用上它、`resolveCoreLaunch`
不抛错，于是「按需安装运行时」这条路径**永远不会被触发**——表现就是四个安装阶段
（创建 Python 环境 / 安装依赖 / 同步依赖 / 验证运行时）一直不出现。运行时到底装没装，
必须由它自己的记录决定，不能被环境里凑巧存在的解释器掩盖。

谁真的能导入依赖就用谁；**不会盲用**下一个候选。全都不可用时抛出的错误会逐个列出
候选与具体原因（例如"缺少模块 fastapi"），并给出修复路径，而不是静默回退、
让用户只看到"服务繁忙"。

之所以要逐个探测而不是简单回退：用户机上 `PATH` 里的 `python` 可能是 Microsoft Store
的占位符（spawn 成功但立刻退出），也可能装了 Python 却没有 `fastapi`/`asyncpg`，
这两种情况都会让 core 起不来，而错误信息对用户毫无指导性。

## 4. 构建命令

```bash
cd frontend-vue

# 一条命令就位 redis，再出解包目录（真机验证用，最快）
npm run desktop:pack
```

`desktop:pack` / `desktop:dist` 已把 `prepare:redis` 串在前面，
不需要手工拷贝 redis。等价的手工步骤：

```bash
python desktop-local/scripts/prepare_redis.py # 就位 desktop-local/bin/redis/（唯一随包的二进制）
```

`prepare:uv` / `prepare:python` 仍可用，但**只是开发期离线取件**，产物不进安装包：

```bash
npm run prepare:uv      # 落到 frontend-vue/assets/uv/，仅本地调试
npm run prepare:python  # 落到 frontend-vue/assets/python-runtime/，仅本地调试
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
ls release/win-unpacked/resources/desktop-local/bin/redis/redis-server.exe
release/win-unpacked/Chiron.exe --remote-debugging-port=9223
```

**`resources/uv`、`resources/python`、`resources/python-runtime` 都不应存在**：
安装包不携带任何 Python 运行时（uv 与 CPython 都不带），首次启动按需下载。
CI 里有三条反向断言，防止其中任何一个被悄悄加回来——加回来会让包体积涨回
100MB+，同时让"按需下载"这条路径再没被真正执行过。

一旦首次启动装不起来，先看 `<userData>/local/runtime/install.json` 与
`logs/main.log`：下载、建 venv、装依赖各有独立阶段，失败会指名是哪一步、
缺的是哪个组件，以及用哪个环境变量换源。

真机启动后从 CDP 检查：

- `window.chironDesktop.mode === 'local'`
- 首次启动时先出现安装进度窗，装完自动关闭并进主界面
- `<userData>/local/runtime/install.json` 存在，`dependencyFingerprint` 非空
- `GET {apiUrl}/readyz` 返回 `pg=true`、`migration.ok=true`
- 退出后 `netstat` 无 core / PG 残留监听

## 6. 已知限制

- 未签名产物在 macOS 上需要用户手动放行；Windows SmartScreen 会拦。
- `asarUnpack` 只解包 wasm 与 `vendor/**`；新增二进制资源时记得补规则。
- **首次启动必须联网**：安装包不带任何运行时，取 uv、取 `python-build-standalone`、
  `uv pip install` 三步都要访问网络。网络受限时用 `CHIRON_UV_MIRROR` /
  `CHIRON_PYTHON_MIRROR` / `CHIRON_PIP_INDEX_URL` 换源，换好源重启应用会自动重试；
  也可以事后把整套环境放到 `<userData>/local/runtime`。
- 首次安装较重：`embedded-postgres` 自带整套 PG 二进制，首次要拉几十到上百 MB。
- 构建期**不再**需要联网下载 uv（不再随包）；只有 `prepare:uv` / `prepare:python`
  这两个开发期离线取件工具才会联网，可按需用 `--from <压缩包>` 走本地缓存。
- redis 二进制来自构建机的发行版（含 MSYS2 运行时 DLL）；脚本会解析 scoop 的
  shim 取真身，但不会跨平台转换 —— Linux/macOS 包需在对应平台生成。
- **Windows 的 redis 只能走预编译发行版**：MSYS2 仓库里只有客户端库
  （`hiredis`、`python-redis`、`redis-plus-plus`），没有 redis 服务端，
  `pacman -S mingw-w64-x86_64-redis` 必然 `target not found`。CI 改为按 tag
  下载 `redis-windows` 的 `Redis-<ver>-Windows-x64-msys2.zip` 并校验 SHA256
  （版本与哈希钉在 workflow 里，不用 `latest`）。该构建自带 `msys-*.dll`，
  且实测接受 Windows 原生 `--dir` 路径，无需 cygdrive 形式。

