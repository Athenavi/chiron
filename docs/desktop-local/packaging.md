# 打包与体积

> 目标：`app.asar` 控制在 100MB 以内，安装包增量主要来自 `dist/` 与随包资源，
> 而不是被重复打包的 `node_modules`。
>
> 相关文件：`frontend-vue/electron-builder.yml`（`files` 与 `extraResources`）、
> `frontend-vue/electron/components.cjs`、`frontend-vue/scripts/build-gateway.mjs`。

## 1. 体积构成

| 部分 | 位置 | 说明 |
|---|---|---|
| Electron 运行时 | `release/win-unpacked/*.dll`、`Chrome.exe` | 由 electron-builder 下载，约 250MB |
| 前端产物 | `resources/app.asar` 内的 `dist/` | Vite 构建，含 vendor chunk 与 wasm |
| Go 网关 | `resources/chiron[.exe]` | **随包**：本地模式的唯一业务入口，由 `npm run build:gateway` 就位 |
| 本地 core | `resources/desktop-local/` | Python 源码（`extraResources`） |
| 迁移 | `resources/migrations/`、`resources/alembic.ini` | alembic 需要真实文件路径 |
| 可选组件 | `resources/components/` | PageIndex / LangExtract，按需下载 |
| Python 运行时 | 解释器 `resources/python-runtime/<platform>/cpython-*/`；依赖 `<userData>/local/runtime/venv/` | **解释器随包**（`prepare_python.py` 就位，约 66MB）；依赖仍在首启按 `pyproject.toml` 现装 |
| uv 二进制 | `resources/uv/<platform>/uv[.exe]` | **随包**：只用来建 venv 并装依赖，不再下载解释器 |
| Redis 二进制 | `resources/desktop-local/bin/redis/` | **随包**：`redis-server` + MSYS2 运行时 DLL |

**不排除时的体积**：`app.asar` 863MB、`win-unpacked` 1.3GB —— `package.json` 的 production
dependencies 会被 electron-builder 自动打进 asar，而 Vite 已经把同一批依赖 bundle 进 `dist/`，
两者叠加即为重复打包。§2 的排除策略就是为消除这份重复。

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

安装包**携带一份受管 CPython 与 uv**：

- `resources/python-runtime/<platform>/cpython-*/` —— 裸解释器，由
  `desktop-local/scripts/prepare_python.py` 在打包前就位（约 66MB/平台）；
- `resources/uv/<platform>/uv[.exe]` —— 只用来建 venv 并装依赖。

首次启动时由壳层做剩下的两步：

1. 建 venv：`uv venv --python <随包解释器的绝对路径>` 在
   `<userData>/local/runtime/venv` 建空环境。**传绝对路径而不是版本号**：
   传版本号时 uv 会去 `github.com` 取 python-build-standalone；
2. `uv pip install` 按 `<resources>/desktop-local/pyproject.toml` 装依赖
   （`embedded_postgres` 自带的整套 PG 二进制随依赖一起进来）；
3. 真实 `import` 一次本地核心做验收，通过才写安装清单。

用户机上不需要预装 Python，也不需要 pip。这条路径的全部实现在
`frontend-vue/electron/runtime-install.cjs`。

**为什么解释器要随包**：实测 `github.com` 在不少网络下连 TCP 都不通
（`invalid peer certificate: UnknownIssuer`）。只随包 uv、让 uv 自己去下载解释器，
结果就是"包里带了工具、原料取不到"，用户看到首次启动永远卡在创建环境、
所有页面报服务繁忙。把解释器随包后，首启两步都不碰 CPython 下载，
弱网下只剩装依赖这一步需要联网。这与已废弃的 `resources/python` 不同：
那份是"解释器 + 已装依赖"的整包，会冻结构建机状态、漏一个包就整体失效。

**为什么依赖仍然首装而不是随包**：随包一份装好依赖的运行时意味着安装包与构建机的
Python 版本、架构强绑定，且升级任何一个依赖都要重新出包。依赖清单只留
`desktop-local/pyproject.toml` 一份，装出来的环境天然与本机匹配。

**唯一的联网步骤**是 `uv pip install`；网络受限时用 `CHIRON_PIP_INDEX_URL`
指向可用镜像。缺少随包解释器或 uv 时**不做联网补救**：那是安装包残缺，
报"请重新安装"才是可执行的指引。

### 两套环境：本地核心与 AI 引擎

同一份安装代码（`runtime-install.cjs`）按 `RUNTIME_TARGETS` 装**两套互不重叠**的 venv：

| target | 随包源码 | 依赖清单（唯一来源） | 落点 | 何时装 |
| --- | --- | --- | --- | --- |
| `core` | `resources/desktop-local` | `pyproject.toml` | `<userData>/local/runtime/venv` | 起 Go 栈前（`prepareCoreLaunch`） |
| `engine` | `resources/python-engine` | `requirements.txt` | `<userData>/local/engine/venv` | 真要起 Go 栈时（按需，`ensureEngineRuntime`） |

**为什么要两套**：引导器（desktop-local）只要 fastapi / uvicorn / asyncpg / redis /
embedded-postgres，而引擎还要 pydantic-ai / anthropic / openai / pymupdf / grpcio，
差一个数量级。合成一份等于让本地模式用户先下载几百 MB 引导器根本用不到的东西；
更糟的是**失败不各归其位** —— 重建环境第一步是 `removeVenv()`，共用一份 venv 时
装引擎失败会顺手毁掉本来就好的引导器环境，本地模式从"聊天不可用"变成"全废"。

**为什么引擎按需装**：引擎只在本地模式用得上，云端模式的用户不该为它付一次下载。
触发点因此是 `main.cjs::tryStartGoStack()` 起栈之前，进度沿用首启那条
`desktop:runtime-stage` 通道 —— 安装窗按事件里的 `target` / `targetLabel`
显示"正在准备：本地核心 / AI 引擎"，两套环境是先后两轮，进度条会复位一次。

**为什么不按"装过没装过"判就绪**：判断只看"能否 `import` 整包"（见 §3 开头的验收），
而且**两套各判各的**：core 装好了不代表引擎装好了。用标记位或共用结论的后果是
引擎永不安装、进度窗永不出现。

**依赖指纹的公式里刻意不含 target id**：老用户已落盘的 `install.json` 记的是
`sha256(RUNTIME_PYTHON_VERSION + 清单正文)`，把 target 掺进去会让所有存量 core 环境
在升级后判"依赖变了"，触发一次本可避免的联网重建。

### venv 的解释器绑定要跟着当前的包走

`userData/local/{runtime,engine}/venv` 是跨版本、跨包共用的，但 venv 里的解释器**不是**
一个独立的东西：Windows 的 `Scripts\python.exe` 只是跳转器，标准库与 DLL 由
`pyvenv.cfg` 的 `home` 决定；POSIX 的 `bin/python3` 是指向 home 里解释器的符号链接。
而 `home` 写的是**当初创建它那一次出包的绝对路径**。真机取到的两份：

| venv | `home` 实际指向 |
| --- | --- |
| core | `D:\Program Files\Chiron\resources\python-runtime\win32-x64\cpython-3.12.16-…`（安装位置） |
| engine | `X:\…\frontend-vue\release-m47\win-unpacked\resources\python-runtime\…`（某次临时验证出包） |

那个目录被删（清理验证用的出包目录）或安装位置变了，解释器就再也起不来。实测把
`home` 改指一个不存在的目录：

```
exit=103  stderr=No Python at '"X:\…\release-does-not-exist\…\python.exe"'  stdout=""
```

而 venv 的文件**一个都没少**，`fs.existsSync(Scripts\python.exe)` 仍然为真。旧代码只有
两条出口：Windows 上报一句谁都看不懂的探测失败、POSIX 上（悬空链接）直接判
`venv-missing` —— 两种都落到**全量重建**：实测引擎那份 340MB、两百多个包、必须联网，
离线时本地模式从"能用"变成"不可用"，而日志里看不出真因只是"换了个包"。

现在 `runtime-install.cjs::rebindVenvBinding()` 在 `inspectRuntime()` 的**开头**做一次性
能修复，规则四条：

- **判坏要看 home 里的解释器在不在**，不看目录在不在（目录还在、`python.exe` 被安全
  软件单独隔离走，是同一个死法）；
- **改绑排在任何"解释器在不在"的判定之前**（`inspectRuntime` 与 `ensureRuntime` 两处都
  是），否则 POSIX 的悬空链接永远修不到；
- **只替换 `home` 那一行**（临时文件 + rename），`uv` / `version_info` /
  `include-system-site-packages` 这些 uv 自己写的键保持原样；替换用函数返回而不是模板
  串，路径里出现 `$&` 时模板写法会被当成特殊序列；
- **minor 版本相同才改绑**（`3.12.x`）。扩展按 minor 的 ABI 编译（`.cp312-*.pyd`），
  补丁号不同改绑安全；跨 minor 就是换一种崩法，此时返回 `version-mismatch` 并带上
  人话，`ensureRuntime` 直接跳到重建、**不再浪费一次注定失败的增量同步**（解释器本身
  跑不起来，`uv pip install` 也用它）。

返回值 `reason ∈ {rebound, binding-ok, no-binding-file, no-bundled-runtime,
version-mismatch, write-failed, no-user-data}` 一路带到上层，日志分两条路落地：
`ensureRuntime` 通过 `onLine` 落到 `[runtime]`；只读询问那条由
`main.cjs` 记 `runtime python binding rebound (<target>): <from> -> <to>`。改绑是
"零成本修好但用户永远看不见"的那类自愈，不留这一行，下次就没法区分"环境从没装过"
与"绑定还跟着上一个包跑"。

**只读询问那条读的是 `rebinds`，不是 `binding`。** `components.inspectLocalRuntime()`
先判 core、core 好了再判引擎，返回值是**最终那一套**的（引擎），它的 `binding` 里
根本没有 core 那次改绑 —— 第一轮真机验证（`release-m51`）实测日志里只有
`(engine)` 一行，core 修好了却查不到痕迹。现在两套的改绑在
`components.inspectLocalRuntime()` 里汇成一个 `rebinds` 数组（每条带 `target`，
所以 `rebindVenvBinding` 的返回里必须有 `target: target.id`），主进程逐条记日志。

真机自愈验证分三层：

1. **模块级**（拿本机两份**真实** venv，随包资源用 `release-m50/win-unpacked`）：破坏
   `home` → 解释器 `exit=103` → `inspectRuntime` 返回 `rebound`、`ready=true`
   （探测器是真 `pkgutil.walk_packages` + 逐模块 import，不是替身）→ 解释器 `exit=0`，
   两条 uv 命令都没跑。POSIX 的悬空链接分支在 Windows 上跑不到，由
   `relinkPosixInterpreter` 的直接用例覆盖（只重指"确实是符号链接且目标已不存在"的那两个）。
2. **打包壳 · 第一轮**（`release-m51/win-unpacked/Chiron.exe`，本地模式真启动）：两份
   cfg 的 `home` 先改指不存在的目录（`exit=103` 确认过），起壳后 `pyvenv.cfg` 落到
   该包内的解释器，**两份 venv 的 `Lib/site-packages` 最新 mtime 与 `install.json`
   一字未动**（零重建、零联网）—— 这两项才是"没重建"的判据，启动耗时不是：同一条
   链路的冷启动本来就随 PG 恢复情况在几十秒到一分半之间飘。引擎侧链路：`GET /ready` 回
   `postgres=up redis=up`，`/v1/skills`、`/v1/capabilities`、`/v1/graphs`（三条
   100% 依赖 python-engine 的路由）带本地模式铸的 owner JWT 全 200。
   **这一轮只记到 `(engine)` 一条** —— 汇总口径就是补这一处，见上。
3. **打包壳 · 第二轮**（`release-m52`，带汇总口径）：同样破坏两份 `home` 再起壳，
   日志出现 `binding rebound (core)` 与 `(engine)` 两行（间隔 1ms，都在建栈之前），
   零重建、`/v1/skills` 等三条路由仍 200；退出后 `local backend stopped
   go-stack（gateway=true engine=true launcher=true）`。

用例与门禁：`electron/__tests__/runtime-install.test.cjs` 新增 19 条（该文件 42 条），
可见性再补 4 条（`engine-runtime.test.cjs`：两套都改绑 / 只有 core 改绑 / core 改绑但
整栈仍未就绪的早退分支 / 按源码钉 `main.cjs` 只遍历 `rebinds`），electron 全套
**261 条全绿**。变异验证：绑定本体 12 条 + 可见性 6 条（`rebinds` 不收集 / 早退丢汇总 /
引擎分支丢汇总 / 不看 `rebound` 就收 / 记录不带 `target` / 主进程回到只看单份
`binding`）全部被捕获，且每条都由预期的用例变红、不误伤别的用例。

**这条修复只对未来出的包生效**：已经装到机器上的旧壳里没有 `rebindVenvBinding`，
它照样会全量重建。所以清理验证用的出包目录前，先想清楚那台机器上的 venv 是不是
旧壳建的（`pyvenv.cfg` 的 `home` 指哪儿）。

`desktop-local/scripts/prepare_uv.py` / `prepare_python.py` 都是打包前的就位步骤，
产物落到 `frontend-vue/assets/uv|python-runtime/<platform>/`，
再经 `electron-builder.yml` 的 `extraResources` 映射进 `resources/`：

- uv 的版本号与各平台 SHA256 固化在 `desktop-local/scripts/uv_manifest.json`；
- 下载后**强校验** SHA256，不符即非零退出（`--from` 可指定已下载的资产离线就位）；
- `--check` 只做自检：真实执行 `uv --version` / `python -V` 并比对版本号。

```bash
npm run prepare:redis     # 就位 desktop-local/bin/redis/
npm run prepare:uv        # 就位 assets/uv/<platform>/
npm run prepare:python    # 就位 assets/python-runtime/<platform>/（需先有 uv）
```

`prepare_uv.py` / `prepare_python.py` 的下载步骤需要联网；要离线就位请先用 `--from` 指定。

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

`components.cjs::prepareCoreLaunch()`（打包态）先试 `resolveCoreLaunch()`，拿不到启动
信息就**就地按需装一次**（`installRuntimeIfPossible`）再试一次。候选链只有两条，
每条都**真实跑一遍探测**（`runtime-probe.cjs`：`pkgutil.walk_packages` + 逐模块
`import`，让 Python 自己算依赖闭包，不再有手维护的模块清单）：

1. `<userData>/local/runtime/venv` —— 首次启动按需装的运行时（**唯一正常来源**）
2. `CHIRON_PYTHON` 环境变量 —— 用户/开发者显式指定

**为什么要有"就地装一次"这一步**：全新安装包恰好是"venv 还不存在"，此时
`resolveCoreLaunch()` 必然抛错。老写法把它当成"这条链路不适用"直接回落 desktop-local
core —— 第一次启动因此拿到的是只有旧 core 的半套界面（网关那侧的 skills / graphs /
kb / workflows 全缺），而且引擎依赖在那条路径上压根轮不到装，要等第二次启动。

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

引擎那一份的解析（`components.cjs::resolveEngineLaunch()`）更短，因为只有两条：

1. `<userData>/local/engine/venv` —— 按需装出来的引擎环境（打包态**唯一**正常来源）
2. `CHIRON_PYTHON` —— 显式指定

两条都没有时返回 `null`（= 起不了引擎），**不再**回退到引导器 venv 或 `python`：
引导器那份从来没有引擎依赖，选中它唯一的结局是 `ModuleNotFoundError`；PATH 上那份
则是上一段说不许用的东西。开发态（没有随包 `python-engine`）由本机环境负责，
壳不去装，也不弹进度窗。


## 4. 随包 Go 网关

安装包必须带 `resources/chiron[.exe]`：它是本地模式的**唯一业务入口**。壳的
`main.cjs::resolveGatewayExe()` 找不到它时**不报错**，只记一条
「本地网关不可用，退回 desktop-local core」的 info 就继续启动 —— 而 core 只实现
`/v1/**` 的一小部分（见 `docs/desktop-local/endpoint-coverage.md`），
用户看到的就是"装好了，但几乎什么都用不了"。这条降级路径之所以能一路绿灯，是因为
electron-builder **对缺失的打包输入只 warn 不报错**（`app-builder-lib` 的
`out/fileMatcher.js::copyFiles()`：`statOrNull(matcher.from)` 为 null 就
`log.warn({from}, "file source doesn't exist")` 后直接 `return`；目录在但 glob 匹配不到
同样只是什么都不拷），于是"CI 全绿、残包照发"。

就位方式与 uv / CPython 同一套约定 —— 构建产物落 `frontend-vue/assets/<name>/`，
整目录不入库，由 `extraResources` 映射进 `resources/`：

```bash
npm run build:gateway   # → frontend-vue/assets/gateway/chiron[.exe]
```

`electron-builder.yml` 里对应 `- from: assets/gateway` + `to: .`（落 resources 根）。

**为什么不再从仓库根 glob**（改之前的形态）：根下的 `chiron(.exe)` 是 `run.py`
在源码比产物新时自动重建的**开发态**二进制，`resolveGatewayExe()` 的开发态候选也在那儿。
把它同时当打包输入有两个坑：出包会覆盖（或因网关正在运行而写不进）开发者正在用的二进制；
更糟的是**陈旧** —— 跳过构建步骤时上个提交建的产物照样被 glob 命中，
于是安装包里是个"能跑但与前端不同源"的网关，静默失效只是换了个形态。

三道拦截，从内到外：

1. `scripts/build-gateway.mjs`：找不到 `go`、`go build` 非零退出、产物不存在/零字节/
   不是普通文件 —— 全部**抛错并非零退出**，脚本存在的全部意义就是"不许跳过"；
2. `electron/__tests__/build-gateway.test.cjs`：把"脚本写的目录"与
   `electron-builder.yml` 的 `from:`、把"产物名"与 `resolveGatewayExe()` 的候选名锁成同源，
   任一侧改名另一侧忘跟就红；
3. `.github/workflows/desktop.yml`：`Build bundled Go gateway` 排在打包之前
   （`GOOS`/`GOARCH` 取 matrix 的 **target** 架构而非 runner —— `macos-latest` 是 arm64
   runner 而 mac target 是 x64），出包后 `Verify packaged runtime assets` 再按
   **安装包内真实路径**正向断言 `$resources/chiron[.exe]` 非空。

编译口径：`-mod=mod`（仓库里的 `vendor/` 是参考源码、不含 `modules.txt`，缺这个参数
Go 直接报 inconsistent vendoring）、`-ldflags "-s -w"`、`CGO_ENABLED=0`（交叉编译带 CGO 会链接失败）。

## 5. 构建命令

```bash
cd frontend-vue

# 一条命令建网关、就位 redis，再出解包目录（真机验证用，最快）
npm run desktop:pack
```

`desktop:pack` / `desktop:dist` 已把 `build:gateway` 与 `prepare:runtime`（uv → python → redis）
串在 electron-builder 前面，不需要手工拷贝。等价的手工步骤：

```bash
npm run build:gateway   # 建 assets/gateway/chiron[.exe]
python desktop-local/scripts/prepare_redis.py  # 就位 desktop-local/bin/redis/
```

`prepare:uv` / `prepare:python` 的产物**同样进安装包**（§3），只是下载需要联网；
离线时用 `--from <压缩包|目录>` 就位：

```bash
npm run prepare:uv      # 落到 frontend-vue/assets/uv/<platform>/
npm run prepare:python  # 落到 frontend-vue/assets/python-runtime/<platform>/
```

只跑 electron-builder（复用已就位的网关 / uv / redis，本机快速迭代用）：

```bash
NODE_TLS_REJECT_UNAUTHORIZED=0 \
CSC_IDENTITY_AUTO_DISCOVERY=false \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
pnpm exec electron-builder --win --dir --publish never
```

正式安装包（NSIS）把 `--dir` 去掉即可。

**这条捷径只适合本机验证**：它不会重建网关，用的就是 `assets/gateway/` 里躺着的那份。
`desktop:pack` / `desktop:dist` 与 CI 都先把 `build:gateway` 串在前面，正式发布不走捷径。

**更坑的是它也不重建渲染端**：`electron-builder` 只把 `dist/` 当前存在的文件打进包，
`dist/` 陈旧时出包**一声不响**。真机上的形状是"改了源码、重出包、界面照旧"——
一次因为 `dist/assets/index-*.js` 还是 10-28 那版（早于去掉 `/admin` 上 `cloudOnly`
的改动），本地模式的 owner 账号进管理后台被弹回首页，且**没有任何网络请求**，
看起来像权限坏了，实际包里没有那份 JS。排查口径：比对 `dist/assets/index-*.js` 的
mtime 与目标提交的日期，别只看 `release/win-unpacked` 的时间戳（后者每次出包都会变新）。
改了 `src/**` 要验证，就走 `npm run desktop:pack`（含 `build:desktop`）；
只改 `electron/**` 才可以走上面的捷径。

### TLS / 镜像排障

本机网络对 electron-builder 的下载源存在证书校验与连接复用问题，实测报错：

- `unable to verify the first certificate` → 需要 `NODE_OPTIONS=--use-system-ca`
  或 `NODE_TLS_REJECT_UNAUTHORIZED=0`；
- `read ECONNRESET` → 换 `ELECTRON_BUILDER_BINARIES_MIRROR`（npmmirror）。

**这些开关只用于本机构建，不要写进 CI**：CI 的 runner 证书链是正常的，
关掉校验会让供应链风险变成默认值。

## 6. 验证清单

构建后至少确认：

```bash
ls -la release/win-unpacked/resources/app.asar           # 目标 < 100MB
ls release/win-unpacked/resources/chiron.exe            # 随包网关（本地模式的业务入口）
ls release/win-unpacked/resources/desktop-local/app     # core 源码在位
ls release/win-unpacked/resources/desktop-local/pyproject.toml  # core 依赖清单在位（uv 靠它装）
ls release/win-unpacked/resources/python-engine/app/main.py     # 引擎源码在位（有 main.py 才算真目录）
ls release/win-unpacked/resources/python-engine/requirements.txt # 引擎依赖清单在位
ls release/win-unpacked/resources/migrations/versions   # 迁移在位
ls release/win-unpacked/resources/desktop-local/bin/redis/redis-server.exe
ls release/win-unpacked/resources/uv/win32-x64/uv.exe    # 随包 uv
ls release/win-unpacked/resources/python-runtime/win32-x64/cpython-*/python.exe
release/win-unpacked/Chiron.exe --remote-debugging-port=9223
```

**`resources/chiron[.exe]` 必须存在且非空**：缺了它壳会静默退回 core（§4），
装上之后不报错、只是处处不可用，所以 CI 对它做正向断言。
`main.log` 里出现「本地网关不可用（未找到 chiron.exe），退回 desktop-local core」
就是这一条没做到。

**`resources/python-engine/requirements.txt` 必须存在**：引擎那份 venv 唯一的依赖来源
就是它（`uv pip install -r`）。缺了它，聊天 / 知识库 / 记忆 / 图谱整条链在首启时装不起来，
表现是「本地模式未就绪」对话框与错误页写着 `AI 引擎的依赖没能装好：…` —— 不是网关的毛病，
别去查网关。判目录是否算"带着源码来了"用的是 `app/main.py`，光有空目录不算。
CI 的 `Verify packaged runtime assets` 对这两份都做**正向断言**。

**`resources/uv` 与 `resources/python-runtime` 必须存在**：解释器与 uv 都随包，
缺任何一份都说明安装包没按布局打好（首启会直接失败并提示重新安装）。
CI 里有对应的**正向断言**，按安装包内真实路径检查可执行文件本身，
而不只是目录在不在。

**`resources/python` 仍然不应存在**：那是"解释器 + 已装依赖"的旧整包，
会冻结构建机状态、漏一个包就整体失效。CI 对它是反向断言。

一旦首次启动装不起来，先看两套环境各自的清单 —— core 在
`<userData>/local/runtime/install.json`，引擎在 `<userData>/local/engine/install.json` ——
再看 `logs/main.log`：建 venv、装依赖各有独立阶段，失败会指名是**哪一套环境**（进度窗
的标题就是这一条）、哪一步、缺的是哪个组件，以及用哪个环境变量换源
（装依赖那步用 `CHIRON_PIP_INDEX_URL`）。
本地模式首启会把两份都装出来；只有 core 那份、没有 engine 那份说明 Go 栈那条链路
压根没走到（最常见是包里缺网关，见 §4）。

真机启动后从 CDP 检查：

- `window.chironDesktop.mode === 'local'`
- `logs/main.log` 里**没有**「本地网关不可用（未找到 chiron.exe），退回 desktop-local core」
  —— 出现这一条就是包里缺网关，症状是"能开、但处处空转"
- 首次启动时先出现安装进度窗，装完自动关闭并进主界面；窗里「正在准备：本地核心」那一行
  会切成「正在准备：AI 引擎」，两套环境的进度不会混在同一行里
- `<userData>/local/runtime/install.json` 存在，`dependencyFingerprint` 非空
- `<userData>/local/engine/install.json` **同样存在**（本地模式起栈时两轮一起装完），
  且它的 `dependencyFingerprint` 与 core 那份**不同**（两份清单内容不同）
- `GET {apiUrl}/readyz` 返回 `pg=true`、`migration.ok=true`
- 退出后 `netstat` 无 core / PG 残留监听

**换过包之后再验一次解释器绑定**：`<userData>/local/{runtime,engine}/venv/pyvenv.cfg`
的 `home` 应指向**当前这个包**的 `resources/python-runtime/<platform>/cpython-*`
（POSIX 多一层 `bin`）。两个判据：

```bash
head -1 "$APPDATA/frontend-vue/local/runtime/venv/pyvenv.cfg"   # home 落在当前包内
# 两套环境各一行：只有 (engine) 没有 (core) 说明汇总口径又漂回单份 binding
grep "binding rebound\|绑定已失效" "$APPDATA/frontend-vue/logs/main.log"
```

正常开机不该出现这些行（绑定本来就对）；换包后应当**core 与 engine 各一行**，
且随后 `install.json` 没被改写、两份 venv 的 `Lib/site-packages` 最新 mtime 不变、
`logs` 里没有 `创建 Python 环境` —— 三条一起看才是"零成本自愈"，只看日志会被
"其实偷偷重建了一遍"骗过去。
**反过来**，如果日志里出现 `创建 Python 环境` 而 venv 又明明存在，八成是绑定修不上
（版本对不上，或包里没有可用解释器）—— 按 §3 的 `version-mismatch` /
`no-bundled-runtime` 分流去查，不要先怀疑依赖清单。

## 7. 已知限制

- 未签名产物在 macOS 上需要用户手动放行；Windows SmartScreen 会拦。
- `asarUnpack` 只解包 wasm 与 `vendor/**`；新增二进制资源时记得补规则。
- **首次启动仍需联网（只装依赖这一步）**：解释器与 uv 都随包，建 venv 不碰网络；
  只有 `uv pip install` 要访问 PyPI。受限时用 `CHIRON_PIP_INDEX_URL` 换源，
  换好源重启应用会自动重试；也可以事后把整套环境放到 `<userData>/local/runtime`。
- **引擎环境只在本地模式装**：它的触发点是 `tryStartGoStack()` 起栈之前，
  云端模式用户永远不会为它付这次下载。所以本地模式首启是**两轮**安装 ——
  先 core、再引擎，同一个进度窗、标题跟着换。引擎的 `requirements.txt`
  比 core 的 `pyproject.toml` 重一个数量级（pydantic-ai / anthropic / openai /
  pymupdf / grpcio），首启因此明显更久；中途失败只影响引擎那一份，
  core 已装好的环境不会被回滚。
- **装成功之前，`resolveEngineLaunch()` 只返回 `null`**：壳不会拿 core 的 venv 去顶替引擎，
  也不会拿 PATH 上的 `python`（见 §3 的解析顺序）。所以引擎环境没就绪时，症状是
  "引擎功能全不可用"，而不是"引擎用了另一份依赖然后到处 ModuleNotFound"。
- 首次安装较重：`embedded-postgres` 自带整套 PG 二进制，首次要拉几十到上百 MB。
- 构建期**需要**先就位 uv 与解释器（`npm run prepare:runtime` 会串联 uv / python / redis
  三步）；已就位时可重复执行，脚本自检通过就跳过下载。离线时用 `--from <压缩包|目录>`。
- 构建期**需要 Go 工具链**（`npm run build:gateway`）。`go` 不在 PATH 且没设 `GO_BIN`
  时脚本直接非零退出，不给"跳过网关照样出包"留口子；CI 由 `actions/setup-go@v5` 按
  `go.mod` 提供版本。
- redis 二进制来自构建机的发行版（含 MSYS2 运行时 DLL）；脚本会解析 scoop 的
  shim 取真身，但不会跨平台转换 —— Linux/macOS 包需在对应平台生成。
- **Windows 的 redis 只能走预编译发行版**：MSYS2 仓库里只有客户端库
  （`hiredis`、`python-redis`、`redis-plus-plus`），没有 redis 服务端，
  `pacman -S mingw-w64-x86_64-redis` 必然 `target not found`。CI 改为按 tag
  下载 `redis-windows` 的 `Redis-<ver>-Windows-x64-msys2.zip` 并校验 SHA256
  （版本与哈希钉在 workflow 里，不用 `latest`）。该构建自带 `msys-*.dll`，
  且实测接受 Windows 原生 `--dir` 路径，无需 cygdrive 形式。

