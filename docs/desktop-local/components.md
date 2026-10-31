# 本地组件（pageindex / langextract）安装与启停

> 本文件是组件层说明，属于 `contracts.md` §2 `ComponentInfo` / §4 本地 API 的扩展。
> 新增端点不改 `contracts.md` 已冻结的字段，只在其后追加；实现见
> `desktop-local/app/components/`、`desktop-local/app/api/components.py`、
> `frontend-vue/electron/components.cjs`。

## 0. 一句话

组件（`pageindex` / `langextract`）从**用户指定的组件源**装进 `<data-dir>/components/<name>/<version>`，
由本地 core 校验 sha256、拒绝路径穿越、按需 spawn，并通过 `/v1/components*` 暴露给 Electron 壳层。
**不内置任何下载地址**：没配置源时安装直接 409，不会静默成功，也不会退回"假装已安装"。

## 1. 职责边界

| 组件 | 谁安装 | 谁启动 | 进程归谁 |
|---|---|---|---|
| `core` | 随桌面端发布（`userData/components/core/chiron-local[.exe]`，或开发态的 `desktop-local/app/main.py`） | `components.cjs::startCore` | Electron 主进程 |
| `pageindex` | 本地 core（`/v1/components/pageindex/install`） | 本地 core | 本地 core |
| `langextract` | 本地 core（`/v1/components/langextract/install`） | 本地 core | 本地 core |

- `components.cjs` 的 `startCore` / `stopCore` **行为不变**（仍是 stdout `listening` 事件 + `/healthz` 轮询）。
- 其余组件的 list / install / start / stop / remove 全部转成对本地 core 的 HTTP 调用；
  壳层不再自己下载、解压或 spawn 组件，也不再持有它们的子进程。
- core 不在运行时：`listComponents()` 回退到磁盘探测（`userData/components/<name>/manifest.json`）并给每项标
  `error: 'core-not-running'`；`install/start/stop/remove` 直接抛
  `本地 core 未运行：组件管理需要本地后端在线…`。

## 2. 组件源

环境变量（两者等价，契约名优先）：

```
CHIRON_COMPONENT_BASE_URL        # 契约名（推荐）
CHIRON_LOCAL_COMPONENT_BASE_URL  # config.py 的 env_prefix 形态
```

取值：

| 值 | 含义 |
|---|---|
| 本地目录，如 `/opt/chiron-components` 或 `X:/chiron-components` | 组件源目录：读 `<目录>/<name>/manifest.json` 与 `<目录>/<name>/<files[]>` |
| `file:///opt/chiron-components` | 同上（去掉 `file://` 前缀） |
| `http://…` / `https://…` | 组件源基址：读 `<基址>/<name>/manifest.json` 与 `<基址>/<name>/<files[]>` |

未配置时：`POST /v1/components/{name}/install` → `409 component_source_unconfigured`。
配置了但目录不存在 / HTTP 访问失败 → `502 component_source_unavailable`。

### 2.1 构建真实组件包

组件源里的包由 `desktop-local/scripts/build_component.py` 产出（只用标准库，不 `import app`，
可在没有 core 依赖的机器或 CI 上单独跑）：

```bash
cd desktop-local
python scripts/build_component.py --name pageindex  --out dist/components
python scripts/build_component.py --name langextract --out dist/components

# 只校验已产出的包，不构建、不联网
python scripts/build_component.py --name pageindex --out dist/components --verify

# 把 out 当组件源装进本地 core
CHIRON_COMPONENT_BASE_URL=dist/components python -m app.main
```

产物是 `<out>/<name>/manifest.json` + `<out>/<name>/bundle.zip`，解压后即组件版本目录：

```
run.py                          # entry：启动器，起 /healthz + /info
runtime/site-packages/**        # 隔离 venv 装出来的整棵依赖树
runtime/requirements.lock.txt   # pip freeze 快照（复现用）
runtime/build-info.json         # 解释器 / 平台 / 依赖 / 体积诊断
```

**为什么必须独立 venv**：两个组件的依赖与引擎/核心冲突，不能装进 core 的环境。

| 组件 | 关键约束 |
|---|---|
| PageIndex `0.4.4` | → `litellm` → **`openai<3`**，与引擎的 `openai 3.x` 冲突 |
| LangExtract `1.7.1` | → `pandas` / `numpy` / `google-genai`，体积大 |

**为什么依赖走一个 zip 而不是逐文件列进 `files`**：manager 限制 `files` 最多 256 项，
而真实依赖树有几千个文件；`files` 里出现 `.zip` 时 manager 会先按文件级 sha256 校验、
再解压、最后删掉归档（见 §4），所以"一个归档 + 归档 sha256"是唯一可行形态。

**为什么还要 `run.py` 这一层**：manager 用 `CHIRON_PYTHON` 或 core 自己的 `sys.executable`
启动 `.py` entry，**不会**用 venv 里的解释器，也不会把 venv 的 site-packages 加进
`sys.path`；而 `<venv>/Scripts/python.exe` 的 `pyvenv.cfg` 指向构建机的 base 解释器，
搬台机器就废。所以只带纯 site-packages，由 `run.py` 在启动时 `sys.path.insert` 进去。

## 3. `manifest.json`

放在组件源里的 `<源>/<name>/manifest.json`：

```json
{
  "name": "pageindex",
  "version": "1.4.0",
  "files": [
    {"path": "bundle.zip", "sha256": "9f2c…"}
  ],
  "sha256": "9f2c…",
  "entry": "pkg/app.py",
  "args": ["--mode", "index"],
  "health": {"path": "/healthz", "timeoutMs": 10000}
}
```

字段规则：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 必须等于请求的组件名（`pageindex` / `langextract`），否则 `component_manifest_invalid` |
| `version` | 是 | `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`；**直接当目录名用**，含 `/`、`..` 一律拒绝 |
| `files` | 是 | 非空数组，最多 256 项。元素是字符串（相对路径）或 `{path, sha256}` |
| `sha256` | 视情况 | 字符串 = **单个归档**的摘要（此时 `files` 只能有一项）；对象 = `{"<rel>": "<hex>"}` 逐文件覆盖 |
| `entry` | 是 | 启动入口的相对路径（相对版本目录）；必须在安装包里真实存在 |
| `args` | 否 | 启动参数（字符串/数字，最多 64 项，单个 ≤ 500 字符） |
| `health` | 否 | `"/healthz"` 或 `{"path": "/healthz", "timeoutMs": 10000}`；端口由 core 借好后通过 `CHIRON_COMPONENT_PORT` 告知组件 |

**每个待安装文件都必须有 sha256**（`files[].sha256` 或顶层 `sha256` 覆盖），缺失即
`400 component_manifest_invalid`。不做"本地源就免校验"的例外。

`files` 里出现归档（`.zip` / `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tbz2` / `.tar.xz` / `.txz`）时：
先按文件本身的 sha256 校验，再解压到版本目录根下，最后删掉归档本身。
所以 `entry` 要写**解压后**的相对路径（如 `pkg/app.py`），不是归档里的条目路径。

## 4. 安装流程

1. 校验组件名（白名单 `^[a-z0-9][a-z0-9._-]{0,63}$`）与"是否该组件正在运行"（运行中拒绝安装）。
2. 从组件源取 `manifest.json` 并解析（`parse_manifest`）。
3. 在 `<data-dir>/components/.tmp/<name>-<version>-xxxx/` 建临时目录。
4. 逐文件下载/复制到 `payload/`，边下边算 sha256：
   - 不匹配 → `400 component_integrity_failed`（带 `file` / `expected` / `actual`），**不落位**；
   - 单文件 ≤ 256 MiB、总量 ≤ 1 GiB，超限 → `502 component_source_unavailable`。
5. 解压归档（`zipfile` / `tarfile`），逐条目校验：
   - `..`、绝对路径、盘符 → `400 component_unsafe_path`；
   - 符号链接 / 硬链接 / 设备文件 → 同上（zip 查 `external_attr` 的 `S_IFLNK`，tar 查 `issym/islnk`）；
   - 解压总量 ≤ 1 GiB（防 tar bomb）。
6. 校验 `entry` 真实存在于 payload 内（且解析后仍在安装目录内）。
7. 落位：删掉同版本旧目录 → `shutil.move(payload, <data-dir>/components/<name>/<version>)`。
8. 写 `<version>/installed.json`，随后清理该组件其它**由本模块安装**的旧版本目录
   （手工放进来的目录不动）。
9. 无论成功失败，临时目录都会删掉。

落位后的目录：

```
<data-dir>/components/<name>/<version>/
  manifest 里的所有文件（归档已解压、已删除）
  manifest.json        # 原样副本，便于排查
  installed.json
<data-dir>/components/.tmp/            # 安装期临时目录，装完即空
<data-dir>/logs/components/<name>.log  # 组件 stdout+stderr 追加写
```

`installed.json`：

```json
{
  "name": "pageindex",
  "version": "1.4.0",
  "entry": "pkg/app.py",
  "args": [],
  "health": "/healthz",
  "files": [{"path": "bundle.zip", "sha256": "9f2c…", "bytes": 812345}],
  "source": "/opt/chiron-components",
  "sourceKind": "dir",
  "installedAt": "2026-01-01T00:00:00+00:00"
}
```

**手工安装**仍被接受：把 `manifest.json` 直接放进 `<data-dir>/components/<name>/`，
`installed()` 会把它当成 `manual` 记录（没有版本子目录），列表里显示 `installed: true`。

## 5. 启动与停止

启动：

1. 取 `installed.json` 指向的版本目录；没有 → `404 component_not_installed`。
2. 借一个 loopback 空闲端口（`bind(127.0.0.1:0)` 立刻关闭，只取号）。
3. 按 `entry` 后缀决定命令：

| entry | 命令 |
|---|---|
| `*.py` | `$CHIRON_PYTHON` 或当前 Python 解释器 + `entry` + `args` |
| `*.js` / `.cjs` / `.mjs` | `$CHIRON_NODE` 或 `node` |
| `.exe` / `.bat` / `.cmd` / `.sh` / `.com` / 无可执行扩展但有执行权限 | 直接执行 `entry` |
| 其它 | `400 component_entry_unsupported` |

4. 子进程环境 = 当前进程环境 + 下列覆盖（**不下发** `CHIRON_COMPONENT_BASE_URL`，那是 core 的配置）：

```
CHIRON_DATA_DIR            <data-dir>
CHIRON_INFERENCE_PROXY     settings.inference_proxy（为空则不下发）
CHIRON_COMPONENT_NAME      pageindex / langextract
CHIRON_COMPONENT_VERSION   1.4.0
CHIRON_COMPONENT_HOST      127.0.0.1
CHIRON_COMPONENT_PORT      借到的端口
```

5. `cwd` = 版本目录；`stdin` = DEVNULL；`stdout`/`stderr` 都追加写
   `<data-dir>/logs/components/<name>.log`（**不读 PIPE**：读不完的 PIPE 会把组件卡死）。
6. 若 manifest 声明了 `health`：轮询 `http://127.0.0.1:<port><path>`（默认 20s，可用 `timeoutMs` 调），
   期间进程退出或超时 → `500 component_start_failed` 并已把进程停掉。

响应：`{"data": {"name","version","entry","running","pid","port","baseUrl","startedAt","log","exitCode","health":{"path","checked","ok"}}}`。

停止：`terminate()` → 等 5s → `kill()` → 再等 3s → 关日志句柄。
**幂等**：没在运行时返回 `200` + `stopped: false`（不是错误），UI 点"停止"不会炸。

卸载（`DELETE /v1/components/{name}`）：正在运行 → `409 component_running`；
不存在 → `404 component_not_installed`；否则 `shutil.rmtree(<data-dir>/components/<name>)`。

## 6. HTTP API

统一响应包装与 `contracts.md` §4 一致：业务数据在 `{"data": ...}`；错误是
`{"error": "<code>", "message": "<人话>", ...}`。

> 路由已在 `app/main.py` 挂载：`from app.api import (... components ...)` +
> `app.include_router(components.router, tags=["components"])`。测试里同样通过
> `app.include_router(components_api.router)` 单独挂载，不依赖主应用。

| 方法 | 路径 | 成功 | 说明 |
|---|---|---|---|
| GET | `/v1/components` | 200 | `{"data": ComponentInfo[], "source": {...}}`；未配置源也是 200 |
| POST | `/v1/components/{name}/install` | 201 | 返回安装结果（含 `dir` / `files[].sha256`） |
| POST | `/v1/components/{name}/start` | 200 | 返回 `{pid, baseUrl, ...}` |
| POST | `/v1/components/{name}/stop` | 200 | 未运行时 `stopped: false` |
| DELETE | `/v1/components/{name}` | 200 | 删除组件目录 |

`ComponentInfo`（在 `contracts.md` §2 的五个字段之外追加诊断字段）：

```json
{
  "name": "pageindex",
  "title": "PageIndex",
  "description": "把长文档切成层级树并给出可追溯的章节索引，供知识库构建与引用定位使用",
  "version": "1.4.0",
  "installed": true,
  "running": false,
  "sizeBytes": 4096,
  "entry": "pkg/app.py",
  "dir": "<data-dir>/components/pageindex",
  "pid": null,
  "baseUrl": ""
}
```

`source`：

```json
{"configured": true, "kind": "dir", "location": "/opt/chiron-components", "available": true, "detail": "本地目录组件源"}
```

### 错误码

| HTTP | `error` | 场景 |
|---|---|---|
| 400 | `invalid_component_name` | 组件名含 `/`、`..` 或不符合白名单 |
| 400 | `component_manifest_invalid` | manifest 形状/版本/entry 不合法，缺 sha256，或 entry 在包里不存在 |
| 400 | `component_integrity_failed` | sha256 不匹配（附 `file` / `expected` / `actual`） |
| 400 | `component_unsafe_path` | `..` / 绝对路径 / 盘符 / 符号链接 |
| 400 | `component_entry_unsupported` | entry 扩展名不支持且没有执行权限 |
| 404 | `unknown_component` | 不在受管组件表里（只有 `pageindex` / `langextract`） |
| 404 | `component_not_in_source` | 组件源里没有该组件或该文件 |
| 404 | `component_not_installed` | 启动/卸载时未安装 |
| 409 | `component_source_unconfigured` | 没配 `CHIRON_COMPONENT_BASE_URL`，**安装**被拒 |
| 409 | `component_running` | 正在运行却要求安装/卸载 |
| 500 | `component_start_failed` | spawn 失败、立即退出、健康检查超时 |
| 502 | `component_source_unavailable` | 源目录不存在、HTTP 不可达、文件超限 |

## 7. Electron 壳层（`frontend-vue/electron/components.cjs`）

- `listComponents()`：core 在跑就以 `GET /v1/components` 为准（同时把 `running` 缓存到
  `state.children`）；core 不在跑或请求失败 → 磁盘探测 + `error` 字段。
  `core` 项始终来自本地 `getCoreState()`（它的版本/体积仍按 `userData/components/core` 探测）。
- `installComponent / startComponent / stopComponent / removeComponent`：
  `core` 走上表的本地实现（行为不变），其余走 `POST/DELETE /v1/components/{name}/…`。
- core 的 `{error, message}` 由 `describeCoreError()` 翻成一句人话（映射表 `CORE_ERROR_HINTS`），
  IPC 的 reject 里带着可直接展示的文案。
- 请求带 `Authorization: Bearer <device token>`（token 只在主进程内用于 loopback 调用，不进日志）。
- 超时：列表 5s、安装 120s、启停 60s。

preload 契约（`contracts.md` §2）不变：`listComponents/installComponent/removeComponent/startComponent/stopComponent`。

## 8. 测试

```bash
# Python：安装/校验/路径穿越/启停/HTTP 层（会起本机 python 子进程，不联网）
cd desktop-local && python -m pytest tests/test_components.py -q

# Electron：假 spawn + 假 fetch，走真实 startCore/stopCore 代码路径
cd frontend-vue && node --test electron/__tests__/components.test.cjs

# 构建脚本：用假包覆盖打包与 sha256 校验路径（不联网、不建真实 venv）
cd desktop-local && python -m pytest tests/test_component_build.py -q

# 门禁
cd desktop-local && python -m ruff check .
cd desktop-local && python -m pytest -q
node --check frontend-vue/electron/components.cjs
```

测试**没有**覆盖的：真实 LangExtract/PageIndex 包的下载与运行（没有真实组件包，
`test_component_build.py` 用假包覆盖打包与校验路径，真实包尚未在本机完成过一次
"构建 → 安装 → 启动 → 调用"）、Windows 服务/权限提升场景、多实例同时 `start`
同一组件（各自起一个进程，互不知情）。

## 9. 未实现 / 风险

- **没有内置组件仓库**：所有真实组件包必须由用户/发布流程提供，仓库不含任何二进制。
- **不做签名校验**：只有 sha256 完整性校验。sha256 来自同一个组件源，源被篡改时校验无意义；
  要防这一层需要 manifest 签名（未实现）。
- **不支持断点续传 / 镜像回退**：HTTP 源失败即失败，没有重试。
- **跨进程锁只覆盖目录写操作**：`install` 落位与 `remove` 共用 `<data-dir>/components/.lock`
  （`app/components/lockfile.py`，Windows `msvcrt` / 其它 `fcntl`，非阻塞轮询 + 超时）。
  启动 / 停止只动本进程的进程表，**不拿这把锁** —— 健康检查最长 20s，占着锁会让另一实例的安装
  一直等到超时。两个 core 实例同时 `start` 同一组件仍会各起一个进程，互不知情。
- **组件进程有监督，但有边界**：core 每秒检查一次（`SUPERVISE_INTERVAL`），崩溃后按
  1s / 2s / 4s 退避自动拉起（`RESTART_BACKOFF`），连续崩溃超过 3 次（`MAX_CRASH_RESTARTS`）
  就**放弃拉起**并把原因写进 `ComponentInfo.supervisionGaveUp`——继续拉只会变成崩溃循环把日志写满。
  跑满 60s（`STABLE_UPTIME`）算稳定，崩溃计数清零，所以"偶发崩溃"不会被永久判死。
  监督只盯本进程 spawn 的进程：`running` 仍只反映"我们起的那个进程还在不在"，
  组件自己再 fork 出去的子进程不在视野内。
  主动 `stop` 会注销"期望运行"并打 `stopping` 标记，监督不会把用户停掉的组件拉回来。
  core 退出时 lifespan 的 finally 里**先停监督线程、再 `stop_all()`**（组件先于 PG/Redis 停）；
  顺序反了会变成拉锯——`stop_all` 收掉进程后监督会当成崩溃又拉起来。
  但 core 被强杀（`SIGKILL` / 任务管理器结束进程）时这一步不会执行，组件会残留。
- **健康检查只支持 HTTP GET**：TCP 探测、自定义校验逻辑未实现；不声明 `health` 就完全不探测。
- **旧版本清理**：安装成功后删除同组件的旧版本目录，但只在"该目录里有 `installed.json`"时才删，
  手工放进来的目录不会被动。
- **`core` 不能通过组件管理安装/卸载**：它随桌面端发布，接口会明确拒绝。

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `desktop-local/app/components/manager.py` | 安装 / 校验 / 解压 / spawn / 停止的唯一实现 |
| `desktop-local/app/components/pageindex.py`、`langextract.py` | 组件元数据与薄封装 |
| `desktop-local/app/components/__init__.py` | 导出管理器、异常与 `COMPONENT_SPECS` |
| `desktop-local/app/api/components.py` | `/v1/components*` 路由（只定义 `router`） |
| `desktop-local/tests/test_components.py` | 安装 / 穿越 / 启停 / HTTP 层测试 |
| `frontend-vue/electron/components.cjs` | 壳层：core 启停 + 组件端点调用与错误翻译 |
| `frontend-vue/electron/__tests__/components.test.cjs` | 壳层单测（假 spawn / 假 fetch） |
