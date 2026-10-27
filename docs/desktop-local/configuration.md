# 桌面端配置面与硬编码审计

本文件回答一个问题：**桌面端（Electron 壳 + `desktop-local` 本地 core）里哪些"配置"其实是写死在代码里的，改起来需要动哪里。**

- 审计对象：`frontend-vue/electron/*.cjs`、`frontend-vue/electron-builder.yml`、`desktop-local/app/**`、`desktop-local/scripts/*`。
- 审计方式：人工通读 + 机器基线扫描（[`desktop-local/tests/test_hardcode_audit.py`](../../desktop-local/tests/test_hardcode_audit.py)）。
  扫描器只输出清单、**不因发现而失败**，用来把现状钉住而不是让 CI 常红。
- 本文件是**只读结论**：不修改任何业务代码、不新增依赖。所有"建议"都落在第 7 节，供后续改动取舍。

图例（下文表格统一使用）：

| 标记 | 含义 |
|---|---|
| 🟢 | 已参数化：环境变量或 `config.json` 能覆盖，不改代码 |
| 🟡 | 有覆盖口，但**默认值写死**在代码里（改默认值要改代码并重打包） |
| 🔴 | 硬编码：用户改不了（除非改代码 / 重打包 / 手工塞文件） |

---

## 1. 一页结论

按"风险 × 修复成本"排序的 8 条：

| # | 问题 | 位置 | 判定 | 建议 |
|---|---|---|---|---|
| 1 | 契约名 `CHIRON_*` 与 core 真正读的 `CHIRON_LOCAL_*` **两套并存**，只写契约名会静默失效 | `frontend-vue/electron/components.cjs:70-77`（`CORE_ENV_ALIASES`）、`desktop-local/app/config.py:30` | 🔴 | 让 core 直接认契约名（`AliasChoices`），删掉桥接表；这是唯一"写错了不报错、只是行为变了"的配置面 |
| 2 | `python` 解释器名在两处写死字面量，另有一处 `process.env.CHIRON_PYTHON \|\| 'python'` | `components.cjs:413`、`components.cjs:423` | 🟡 | 收敛成 `DEFAULT_PYTHON` 常量；打包态优先 `resources/python/**` 的行为保持不变 |
| 3 | 本地 Ollama 地址 `http://127.0.0.1:11434` 既是代码默认值，又是 `secrets.bin` 里的可配置项 | `inference-proxy.cjs:19`、`inference-proxy.cjs:338` | 🟡 | 单一来源：默认值只在 constants 里出现一次，运行期以 `providers.ollama.baseUrl` > `CHIRON_OLLAMA_URL` > 常量 的顺序解析 |
| 4 | embedded PG 口令 `chiron` / 用户 `chiron` / 库名 `chiron` 是默认值，且 `--auth=trust` | `desktop-local/app/config.py:84-86`、`storage/pg.py:624` | 🟡 | 只绑 loopback + trust 可接受，但应在 `configuration.md`/启动日志明确"这不是安全边界"；若要会话级隔离，改成每次启动随机口令 |
| 5 | 超时/容量上限全散在模块常量里（core 侧 20+ 个、Electron 侧 10+ 个），没有一处能整体调 | 见第 5 节 | 🟡 | 与业务相关的（安装、健康检查、推理）进 `Settings` 字段；纯实现细节（行缓冲上限、stderr 尾巴）保持常量 |
| 6 | 端口策略不统一：core/proxy 用随机空闲端口（好），但错误提示与文档里到处是 `8080`/`11434`/`6379` 字面量 | 见第 4.1 节 | 🟡 | 保持"随机端口 + 握手拿地址"，把示例端口收进文档而非代码 |
| 7 | 无连接池/并发上限配置：每次请求新建 `httpx.AsyncClient`，每次 DB 访问 `asyncpg.connect` | `runtime/inference.py:185`、`runtime/store.py:111`、`knowledge/store.py:193` | 🔴 | 本地单用户场景可接受，但并发/长跑时会踩文件描述符；需要时按第 7 节加 `Settings` 字段 |
| 8 | 打包清单里 `appId`、更新源、`extraResources` 路径都是单点写死 | `frontend-vue/electron-builder.yml:10,43-57,113-116`、`main.cjs:82` | 🟡 | `appId` 已在两处（`main.cjs:82`、`electron-builder.yml:10`）重复，收敛成构建期注入 |

---

## 2. 环境变量

### 2.1 Electron 外壳（主进程 / preload）

| 变量 | 默认值 | 读取位置 | 作用 | 判定 |
|---|---|---|---|---|
| `CHIRON_DESKTOP_USER_DATA` | 空 → `app.getPath('userData')` | `config.cjs:75`、`components.cjs:115` | 覆盖整个 userData 根（config.json / 本地数据 / 组件 / 日志都跟着搬）。真机 smoke 与多环境隔离靠它 | 🟢 |
| `CHIRON_DESKTOP_MODE` | 空 → `config.json` 的 `mode` | `config.cjs:283`、`preload.cjs:34` | 强制 `local` / `cloud`，不改配置直接换壳模式 | 🟢 |
| `CHIRON_API_URL` | 空 → `config.json` 的 `apiUrl`（仅 cloud） | `config.cjs:294`、`preload.cjs:26` | 强制云端服务地址 | 🟢 |
| `CHIRON_DEV_URL` | `http://localhost:5173`（`shared.cjs:13`） | `main.cjs:53` | 开发态（`--dev`）加载的 Vite dev server | 🟡 默认值写死 |
| `CHIRON_OLLAMA_URL` | `http://127.0.0.1:11434`（`inference-proxy.cjs:19`） | `inference-proxy.cjs:338` | 本地 Ollama 基址 | 🟡 |
| `CHIRON_PYTHON` | `python`（字面量） | `components.cjs:413,423` | 开发/打包态启动 core 的解释器 | 🟡 |
| `CHIRON_DATA_DIR` + `CHIRON_LOCAL_DATA_DIR` | `<userData>/local`（`components.cjs:655`） | `components.cjs:445`、`config.py:68` | core 数据目录（两套名字一起下发） | 🟡 默认值写死 |
| `CHIRON_PORT` + `CHIRON_LOCAL_PORT` | 不传 → 壳层先借一个空闲端口（`components.cjs:657`） | `components.cjs:446` | core 监听端口 | 🟢 |
| `CHIRON_DEVICE_TOKEN` | 不传 → `crypto.randomBytes(32).hex`（`components.cjs:667`） | `components.cjs:447`、`api/auth.py:51` | 本地设备 token（**不是**云端凭据） | 🟢 |
| `CHIRON_INFERENCE_PROXY` + `CHIRON_LOCAL_INFERENCE_PROXY` | 空（不传） | `components.cjs:448` | core 侧推理代理地址 | 🟢 |
| `CHIRON_LOCAL_AUTH_ENABLED` | 壳层默认写 `1`（`components.cjs:451-453`） | `components.cjs:451` | 打开 core 的设备 token 校验 | 🟢 |
| 无（仅错误文案提到） | — | `components.cjs:254` | 提示用户设置 `CHIRON_COMPONENT_BASE_URL`，但**壳层不读也不转发**它 | 🔴 文档化缺口 |

进程内参数（不是环境变量，但同属配置面）：`--dev`（`main.cjs:52`）、`additionalArguments` 前缀
`--chiron-mode=` / `--chiron-api-url=` / `--chiron-device-token=`（`shared.cjs:8-12`）、
session 分区 `persist:chiron-local|cloud`（`shared.cjs:20`）。

> 注：`DEVICE_TOKEN_ARG_PREFIX` 在 `main.cjs:47` 与 `preload.cjs:16` 各有一份**兜底字面量**。
> 这不是遗漏——`shared.cjs` 仍未导出该常量，两处写法一致，搬进 `shared.cjs` 时同时生效。

### 2.2 本地 core（`desktop-local/app/config.py`，前缀 `CHIRON_LOCAL_`）

`Settings` 是 pydantic-settings，`env_prefix = "CHIRON_LOCAL_"`，另外 `env_file=".env"`
（相对于**进程 cwd**：开发态是 `desktop-local/`，打包态是 `resources/desktop-local/`）。

| 环境变量 | 字段 | 默认值 | 作用 | 判定 |
|---|---|---|---|---|
| `CHIRON_LOCAL_HOST` | `host` | `127.0.0.1` | 监听地址（契约要求只绑 loopback） | 🟡 |
| `CHIRON_LOCAL_PORT` | `port` | `0`（内核随机） | 监听端口 | 🟢 |
| `CHIRON_LOCAL_LOG_LEVEL` | `log_level` | `info` | uvicorn 日志级别 | 🟢 |
| `CHIRON_LOCAL_DATA_DIR` | `data_dir` | 平台默认（`config.py:36-48`） | 数据目录根 | 🟡 默认路径写死 |
| `CHIRON_LOCAL_INFERENCE_PROXY` | `inference_proxy` | `""` | 推理代理地址；空 = 未配置（`/v1/models/verify` 503） | 🟢 |
| `CHIRON_LOCAL_PG_ENABLED` | `pg_enabled` | `True` | 是否拉起 embedded PostgreSQL | 🟢 |
| `CHIRON_LOCAL_REDIS_ENABLED` | `redis_enabled` | `True` | 是否启用 Redis 适配层 | 🟢 |
| `CHIRON_LOCAL_RUN_MIGRATIONS` | `run_migrations` | `True` | 启动时是否 `alembic upgrade head` | 🟢 |
| `POSTGRES_DSN` 或 `CHIRON_LOCAL_POSTGRES_DSN` | `postgres_dsn` | `None` | 给了就复用外部 PG（优先级最高，不自己起进程） | 🟢 |
| `CHIRON_LOCAL_REDIS_URL` | `redis_url` | `""` | 有值走真实 Redis/Valkey，否则 `fakeredis` | 🟢 |
| `CHIRON_LOCAL_PG_DATABASE` | `pg_database` | `chiron` | embedded PG 库名 | 🟡 |
| `CHIRON_LOCAL_PG_USER` | `pg_user` | `chiron` | embedded PG 用户 | 🟡 |
| `CHIRON_LOCAL_PG_PASSWORD` | `pg_password` | `chiron` | 仅本机 trust/loopback 场景，不落日志 | 🟡 |
| `CHIRON_LOCAL_PG_PORT` | `pg_port` | `0`（每次选空闲端口） | embedded PG 端口（embedded-pack 后端会自行挑端口，此字段不生效） | 🟢 |
| `CHIRON_LOCAL_PG_BIN_DIR` | `pg_bin_dir` | `""` | `initdb`/`pg_ctl` 目录（PATH 上找不到时兜底） | 🟢 |
| `CHIRON_LOCAL_PG_START_TIMEOUT` | `pg_start_timeout` | `60.0` | PG 启动等待上限 | 🟢 |
| `CHIRON_LOCAL_SSE_HEARTBEAT_SECONDS` | `sse_heartbeat_seconds` | `15.0` | SSE 心跳间隔 | 🟢 |
| `CHIRON_LOCAL_SSE_MAX_HEARTBEATS` | `sse_max_heartbeats` | `0`（不设上限） | 心跳条数上限（测试用 1~2 让流自然结束） | 🟢 |
| `CHIRON_LOCAL_AUTH_ENABLED` | `auth_enabled` | `False` | 是否校验设备 token | 🟢 |

**不经 `Settings`、直接 `os.environ` 读的变量**（这些字段在 `Settings` 里不存在，无法从 `.env` 走 pydantic 校验）：

| 变量 | 默认 | 位置 | 作用 | 判定 |
|---|---|---|---|---|
| `CHIRON_DEVICE_TOKEN` | 空 → 读/建 `<data-dir>/auth/device-token` | `api/auth.py:51` | 设备 token（壳层注入时优先采用） | 🟢 |
| `CHIRON_LOCAL_MODELS` | 空（= 无模型） | `runtime/inference.py:39` | 可用模型清单（JSON 数组） | 🟢 |
| `CHIRON_LOCAL_INFERENCE_TOKEN` | 空 | `runtime/inference.py:36` | 调代理时带的 Bearer | 🟢 |
| `CHIRON_LOCAL_KNOWLEDGE_ENABLED` | 关 | `knowledge/__init__.py:39` | 知识库总开关（只认 `1/true/yes/on`） | 🟢 |
| `CHIRON_COMPONENT_BASE_URL` / `CHIRON_LOCAL_COMPONENT_BASE_URL` | 空（= 未配置 → 409） | `components/manager.py:53-54` | 组件源（本地目录或 HTTP 基址） | 🟢 |
| `CHIRON_PYTHON` / `CHIRON_NODE` | 空 → 当前解释器 / `node` | `components/manager.py:56,58` | 启动 `.py` / `.js` 组件的运行时 | 🟢 |
| `LOCALAPPDATA` / `XDG_DATA_HOME` | 空 → `~/AppData` / `~/.local/share` | `config.py:43,47` | 平台默认数据目录的计算输入 | 🟡 |
| `CHIRON_P0_STACKS` | 空 | `scripts/verify_p0.py:265` | 只影响验证脚本 | 🟢 |

> `components/manager.py:525` 还会读 `settings.component_base_url`，但 `Settings` 里**没有这个字段**
> （`extra="ignore"` 所以不会报错，取值恒为 `""`）。实际生效路径是上面两个环境变量。

### 2.3 契约名 ↔ 生效名桥接（唯一"静默失效"风险）

`components.cjs:70-77` 的 `CORE_ENV_ALIASES` 把 `CHIRON_*` 同时写成 `CHIRON_LOCAL_*`：

| 契约名（`docs/desktop-local/contracts.md` §3.1） | core 真正读的名字 |
|---|---|
| `CHIRON_DATA_DIR` | `CHIRON_LOCAL_DATA_DIR` |
| `CHIRON_PORT` | `CHIRON_LOCAL_PORT` |
| `CHIRON_INFERENCE_PROXY` | `CHIRON_LOCAL_INFERENCE_PROXY` |
| `CHIRON_DEVICE_TOKEN` | 无别名（core 自己读 `os.environ`，见 §2.2） |

失败模式：只下发契约名 → core 用平台默认数据目录 / 随机端口，**壳层却以为参数已生效**。
（`docs/desktop-local/handshake.md` §2 有同一结论的实测记录。）

### 2.4 下发给子组件的变量（`manager.py:1052-1058`）

`CHIRON_DATA_DIR`、`CHIRON_INFERENCE_PROXY`、`CHIRON_COMPONENT_NAME`、`CHIRON_COMPONENT_VERSION`、
`CHIRON_COMPONENT_PORT`、`CHIRON_COMPONENT_HOST`（写死 `127.0.0.1`）。
反向：`scripts/build_component.py:498-503` 读 `CHIRON_COMPONENT_HOST`/`CHIRON_COMPONENT_PORT`。

---

## 3. 路径

### 3.1 userData 布局（Electron）

根：`app.getPath('userData')`，可用 `CHIRON_DESKTOP_USER_DATA` 整体改写。

```
<userData>/
├── config.json                  config.cjs:84     设备设置唯一真相（mode / apiUrl / local / ui / migration）
├── token.bin                    token-store.cjs:18 云端 Bearer token（safeStorage 加密，mode 0o600）
├── secrets.bin                  secrets.cjs:26     provider 密钥（safeStorage 加密，version:1）
├── logs/main.log                main.cjs:96        主进程诊断日志
├── local/                       components.cjs:655 本地 core 的 data-dir（默认值，可被 CHIRON_DATA_DIR 覆盖）
└── components/                  components.cjs:124-138
    ├── core/chiron-local[.exe]  components.cjs:388 已安装 core 二进制（优先于源码启动）
    ├── pageindex/               仅作兜底展示（真实安装目录在 core 的 data-dir 下）
    └── langextract/
```

### 3.2 安装包 resources（`electron-builder.yml`）

| 包内路径 | 来源 | 消费者 | 判定 |
|---|---|---|---|
| `resources/desktop-local/` | `extraResources` ← `../desktop-local`（`electron-builder.yml:43-50`） | `components.cjs:372-376`（`packagedCoreDir()`） | 🟡 目录名写死 |
| `resources/migrations/` | `extraResources` ← `../migrations`（`:51-55`） | `config.py` 的 `REPO_ROOT/migrations`（打包后 `REPO_ROOT` = `resources/`） | 🟡 |
| `resources/alembic.ini` | `extraResources` ← `../alembic.ini`（`:56-57`） | `config.py:134-136` | 🟡 |
| `resources/python/python.exe` 或 `resources/python/bin/python3` | **本轮不随包**（`:58-59` 注释） | `components.cjs:379-384`（`bundledPython()`，优先于 `CHIRON_PYTHON`） | 🟡 |
| `resources/components/**` | `extraResources` glob 过滤（`:34-39`，目录平时不存在） | 契约 §3 的可选组件目录 | 🟡 |

`REPO_ROOT = Path(__file__).resolve().parents[2]`（`config.py:33`）是唯一把"打包后 `resources/`"
与"开发态仓库根"统一起来的地方——它同时决定 `migrations/` 与 `alembic.ini` 的位置。

### 3.3 core 的 data-dir 子目录（`desktop-local`）

| 路径 | 定义 | 用途 |
|---|---|---|
| `<data-dir>/pg/` | `config.py:114-116` | embedded PG 数据目录（`PG_VERSION` 判已初始化） |
| `<data-dir>/logs/postgres.log` | `config.py:118-120` | PG 日志（`pg_ctl -l`） |
| `<data-dir>/auth/device-token` | `config.py:126-128` + `api/auth.py` | 设备 token 落盘（`0o600`） |
| `<data-dir>/components/<name>/<version>` | `components/manager.py:692` | 组件安装位置 |
| `<data-dir>/components/.tmp/` | `components/manager.py:865` | 安装暂存（校验通过才 move） |
| `<data-dir>/logs/components/<name>.log` | `components/manager.py:699` | 组件子进程输出（不读 PIPE） |
| `<data-dir>/media/` | `storage/files.py:36,112` | 上传媒体（`MAX_MEDIA_BYTES` 25MB） |
| `<data-dir>/knowledge/` | `api/knowledge.py:99` | 知识库文档根 |
| `<cwd>/chiron-local` | `components/manager.py:676` | **兜底**：既没传 `data_dir` 也没有 `settings` 时的相对路径 |

平台默认数据目录（`config.py:36-48`）：

| 平台 | 默认 |
|---|---|
| Windows | `%LOCALAPPDATA%\Chiron\local`（无 `LOCALAPPDATA` 时 `~/AppData/Local/...`） |
| macOS | `~/Library/Application Support/Chiron/local` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/chiron/local` |

注意 Windows 侧 Electron 的 userData 是 `%APPDATA%\Chiron`，而 core 的**独立**默认值是
`%LOCALAPPDATA%\Chiron\local`——两者只在壳层未下发 `CHIRON_DATA_DIR` 时才有差别，
这也是"配置没生效"最容易被误判成"数据丢了"的原因。

### 3.4 仓库内 / 打包内的相对路径

| 路径 | 位置 | 说明 |
|---|---|---|
| `desktop-local`（`-m app.main` 的 cwd） | `components.cjs:364-366,426` | 开发态 core 源码根 |
| `desktop-local/app/main.py` | `components.cjs:420` | 存在即认为可走开发态 |
| `dist/index.html` | `main.cjs:604` | 渲染层主界面 |
| `electron/setup.html`、`electron/error.html` | `main.cjs:592,595,162` | 配置页 / 错误页（`loadFile`，`file://`） |
| `electron/resources/` | `electron-builder.yml:16` | `buildResources`（图标等） |
| `release/` | `electron-builder.yml:15` | 构建产物输出目录 |

---

## 4. 端口与端点

### 4.1 端口

| 谁 | 默认端口 | 位置 | 判定 |
|---|---|---|---|
| 本地 core（FastAPI/uvicorn） | `0` → 内核随机；壳层先 `net.listen(0,'127.0.0.1')` 借一个（`components.cjs:522-533`） | `config.py:66` | 🟢 无字面量端口 |
| loopback 推理代理 | `0` → 随机（`inference-proxy.cjs:346`） | 同左 | 🟢 |
| embedded PostgreSQL | `0` → `find_free_port()`（`storage/pg.py:267,668-671`）；embedded-pack 后端自行挑 | `config.py:87` | 🟢 |
| Redis | 空 → `fakeredis`；提示文案里的 `127.0.0.1:6379/0` 只是示例（`storage/redis.py:14-15,41-44`） | `config.py:81` | 🟢 |
| 组件子进程 | 借空闲端口后经 `CHIRON_COMPONENT_PORT` 告知（`manager.py:1057`） | `manager.py:1016-1018` | 🟢 |
| Ollama | `11434` | `inference-proxy.cjs:19` | 🟡 |
| Vite dev server | `5173` | `shared.cjs:13` | 🟡 |
| 文档/错误文案里的示例 | `8080`（`config.cjs:57`）、`6379`、`5432`（`storage/pg.py:74`） | — | 🟡 仅文案，不参与运行 |

`components.cjs:725-727` 会把 stdout 播报里的 host 强行压回 loopback：
`announce.host === 'localhost' || '127.0.0.1' ? it : '127.0.0.1'`——这是**有意的**收敛，不是硬编码缺陷。

### 4.2 HTTP 端点

| 端点 | 谁提供 / 谁调 | 位置 |
|---|---|---|
| `GET /health`（云端探测，5s 超时） | Electron `config.cjs::probe` → 云端 API | `config.cjs:311` |
| `GET /healthz`（存活） | core → Electron 轮询（1000ms/次） | `api/health.py:18`、`components.cjs:546-549` |
| `GET /readyz`（存储就绪，逐项如实报） | core | `api/health.py:23` |
| `GET /events`（SSE 心跳 15s） | core | `api/events.py:42` |
| `POST /v1/auth/session`（免鉴权换设备 token） | core | `api/auth.py:73` |
| `GET /v1/models`、`POST /v1/models/verify` | core | `api/models.py:45,58` |
| `GET/POST/DELETE /v1/components*` | core ← Electron `components.cjs` | `api/components.py`、`components.cjs:41` |
| `POST /infer`（推理代理唯一路由） | Electron proxy（`127.0.0.1:<随机>`，一次性 token） | `inference-proxy.cjs:311,331-355` |
| `POST http://127.0.0.1:11434/api/chat` | proxy → Ollama | `inference-proxy.cjs:193` |
| `POST {baseUrl}/chat/completions` | proxy → 用户自配 OpenAI 兼容端点（`secrets.bin`） | `inference-proxy.cjs:226` |
| cloud provider（`provider=cloud`） | **明确 501**，不要假装成功 | `inference-proxy.cjs:269` |

免鉴权端点固定为三个（`middleware.py:33`）：`/healthz`、`/readyz`、`/v1/auth/session`。

---

## 5. 超时 / 重试 / 并发 / 容量

### 5.1 Electron

| 项 | 值 | 位置 | 判定 |
|---|---|---|---|
| 云端连通性探测 | 5s（`AbortController`） | `config.cjs:309` | 🟡 |
| core 健康等待总时长 | 30s | `components.cjs:34` | 🟡 |
| core 健康轮询间隔 | 200ms（单次探测 1000ms） | `components.cjs:35,546` | 🟡 |
| core 停止：SIGTERM→SIGKILL | 5s / 2s | `components.cjs:36-37` | 🟡 |
| 列组件 | 5s | `components.cjs:43` | 🟡 |
| 安装组件 | 120s | `components.cjs:45` | 🟡 |
| 其它组件动作（start/stop） | 60s | `components.cjs:47` | 🟡 |
| 推理上游 | 120s（单次，**无重试**） | `inference-proxy.cjs:21` | 🟡 |
| 代理请求体上限 | 1 MiB | `inference-proxy.cjs:20` | 🟡 |
| 代理 CORS 预检缓存 | 600s | `inference-proxy.cjs:73` | 🟡 |
| 本地后端启动重试 | 首次失败后再试 **2** 次 | `main.cjs:706` | 🟡 |
| 自动更新检查延迟 | 8s（首屏后） | `main.cjs:674` | 🟡 |
| core 行缓冲上限 | 64 KiB（回溯保留 4 KiB） | `components.cjs:479` | 🟡 |
| stderr 报错回显上限 | 2000 B | `components.cjs:39` | 🟡 |
| 窗口尺寸 | 主界面 1440×900（min 960×640）；配置/错误页 520×620（min 420×480） | `main.cjs:514-518` | 🟡 |
| 单实例 | `requestSingleInstanceLock()` | `main.cjs:678` | 🟢 有意 |
| 并发度 | core / proxy 各一个进程（`state.core`、`current`），session 监听用 WeakSet 防重复注册 | `components.cjs:79-87`、`main.cjs:77` | 🟡 无配置项 |

### 5.2 core

| 项 | 值 | 位置 |
|---|---|---|
| PG 启动等待 | 60s（`pg_start_timeout`） | `config.py:89` |
| `wait_for_port` | 60s（socket 0.5s 超时） | `storage/pg.py:273-278` |
| `pg_ctl start` / `stop` | 30s / 60s | `storage/pg.py:646,657` |
| `psycopg2.connect` / `asyncpg.connect` | 10s | `storage/pg.py:314`、`runtime/store.py:48,111` |
| `/readyz` 的 PG 健康检查 | 5s | `storage/pg.py:841` |
| 建表超时 | 10s | `runtime/schema.py:99` |
| 组件 HTTP 下载 | 30s | `components/manager.py:77` |
| 组件健康检查 | 20s / 100ms 轮询 / 单次 1s | `components/manager.py:78-81,1016-1018,1129` |
| 组件 stop / kill | 5s / 3s | `components/manager.py:79-80` |
| 推理客户端 | 120s | `runtime/inference.py:33` |
| `POST /v1/models/verify` 预算 | 16 token + `ping` | `runtime/inference.py:42-43` |
| SSE 心跳 | 15s（可配） | `config.py:92-93` |
| 聊天历史窗口 / 流式分片 / 标题长度 | 24 / 24 / 40 | `runtime/chat.py:26,29,32` |
| 会话列表 limit | 默认 50 / 上限 200 | `runtime/chat.py:35-36` |
| 消息长度 / 列表 limit | 200_000 字符 / 默认 200、上限 500 | `api/chat.py:48,51-52` |
| Agent 名字/提示词/列表 | 120 / 32_000 / 100（上限 500） | `api/agents.py:22-25` |
| 媒体文件 | 25 MiB / 名字 100 字符 | `storage/files.py:33-34` |
| 知识库文档 | 8 MiB / 名字 200 字符 | `api/knowledge.py:48,52` |
| 分块 / 重叠 / 检索 | 800 / 100 / 8 词、快照 200、答案 1200 | `knowledge/chunk.py:13-14`、`knowledge/search.py:20-23` |
| 组件包体积 | 单文件 256 MiB / 解压总 1 GiB / manifest 1 MiB | `components/manager.py:72-75` |
| `manifest.health.timeoutMs` 合法区间 | `(0, 300000]` ms | `components/manager.py:372-373` |
| 记忆字段上限 | content 100_000 / title 500 / kind 64 / source 500 / tags 64 / keyword 200 | `storage/memory_store.py:24-29` |
| usage 估算 | 4 字符 ≈ 1 token（标明 `usage_estimated`） | `runtime/inference.py:246-248` |
| 并发 | **无池化**：每次推理新建 `httpx.AsyncClient`；每次 DB 访问新建连接 | `runtime/inference.py:185`、`runtime/store.py:111` |

---

## 6. 硬编码清单（按"改起来最疼"排序）

| 位置 | 写死的内容 | 判定 | 建议 |
|---|---|---|---|
| `components.cjs:70-77` | `CHIRON_*` → `CHIRON_LOCAL_*` 别名表 | 🔴 | core 侧改用 `AliasChoices("CHIRON_DATA_DIR", "CHIRON_LOCAL_DATA_DIR")`，删表 |
| `components.cjs:413,423` | 解释器字面量 `'python'` | 🟡 | `DEFAULT_PYTHON` 常量 + `CHIRON_PYTHON` 覆盖（已有） |
| `components.cjs:388` | core 二进制名 `chiron-local` / `chiron-local.exe` | 🟡 | `CORE_BINARY_NAME` 常量；文件名也算契约的一部分 |
| `components.cjs:41` | `CORE_COMPONENTS_PATH = '/v1/components'` | 🟡 | 保持常量（核心路由，不该配） |
| `inference-proxy.cjs:19` | `http://127.0.0.1:11434` | 🟡 | 与 `secrets.providers.ollama.baseUrl` 合并成单一来源 |
| `inference-proxy.cjs:24` | 透传参数白名单 `temperature/max_tokens/top_p/stop` | 🟡 | 有意收窄，保留 |
| `inference-proxy.cjs:269` | cloud provider 恒 501 | 🟡 | 第二批实现后删 |
| `shared.cjs:13` | `http://localhost:5173` | 🟡 | 仅开发态；`CHIRON_DEV_URL` 已可覆盖 |
| `main.cjs:82` + `electron-builder.yml:10` | `com.chiron.agent`（两处） | 🟡 | 构建期注入一份，代码侧读 `app.getAppUserModelId()` 或 package.json |
| `main.cjs:56` | 窗口底色 `#f9f9f7`（CSS 变量 `--bg-page` 的手抄值） | 🟡 | 与前端主题变量同步时容易漂移；建议前端导出 `theme.color.bgPage` |
| `main.cjs:307` | 允许的权限集合 `media/clipboard-read/fullscreen` | 🟡 | 有意白名单，保留 |
| `secrets.cjs:26-27`、`token-store.cjs:18` | 文件名 `secrets.bin` / `token.bin`、`SECRETS_VERSION=1` | 🟡 | 版本字段已为迁移留口；文件名保持 |
| `config.py:84-86` | `pg_database/pg_user/pg_password = "chiron"` | 🟡 | 见 §1 #4 |
| `config.py:33` | `REPO_ROOT = parents[2]` | 🟡 | 打包/开发两态的隐式约定，改动时务必同步 `electron-builder.yml` |
| `storage/pg.py:75` | Windows 路径示例 `C:\Program Files\PostgreSQL\16\bin` | 🟡 | 仅提示文案 |
| `storage/pg.py:631` | `-c listen_addresses=127.0.0.1` | 🟡 | 有意（loopback only） |
| `storage/redis.py:41-46` | `VALKEY_HINT` 里的 `6379` | 🟡 | 仅提示文案 |
| `components/manager.py:676` | 兜底数据目录 `Path.cwd()/"chiron-local"` | 🟡 | 只在不传 settings 时生效；建议改成报错而不是落 cwd |
| `components/manager.py:1058` | 子组件 `CHIRON_COMPONENT_HOST = "127.0.0.1"` | 🟡 | 有意（loopback only） |
| `api/auth.py:36-41` | `tenant_id = 1`、`user = local/owner` | 🟡 | 本地单租户占位，保留 |
| `runtime/store.py:26-27` | `DEFAULT_TENANT_ID=1`、`DEFAULT_USER_ID='local'` | 🟡 | 同上 |
| `middleware.py:33,53` | 免鉴权路径集合、`Bearer` scheme | 🟡 | 保持常量（安全基线） |
| `asr`/`misc` 常量（`knowledge/search.py`、`runtime/chat.py` 等） | 检索与截断尺寸 | 🟡 | 纯调参项，需要时进 `Settings` |
| `electron-builder.yml:113-116` | `publish: github Athenavi/chiron` | 🟡 | 换发布方需改构建配置 |
| `electron-builder.yml:76-109` | 仅 x64，无 arm64 / universal / .blockmap | 🟡 | 发行策略，已注释说明 |

---

## 7. 建议的提取落点（供后续改动取舍，本次未实施）

1. **一个常量模块**：`frontend-vue/electron/defaults.cjs`（或扩展 `shared.cjs`），收纳
   `LOOPBACK = '127.0.0.1'`、`DEFAULT_PYTHON = 'python'`、`CORE_BINARY_NAME = 'chiron-local'`、
   `DEFAULT_OLLAMA_BASE`、`DEFAULT_DEV_URL`、`APP_ID`。理由：这些值现在至少各出现两次
   （`main.cjs` / `preload.cjs` / `components.cjs` / `electron-builder.yml`）。
2. **core 侧超时进 `Settings`**：只有"用户可能想调"的那几个——组件安装/健康、推理超时、
   SSE、PG 启动。纯实现细节（行缓冲 64 KiB、stderr 尾巴 2000 B、usage 估算 4 字符/token）
   留在模块常量里，进 `Settings` 反而提高误配风险。
3. **端口的单一事实来源**：继续"随机端口 + 握手拿地址"，**不要**引入固定端口配置项；
   需要固定端口的场景（联调/防火墙）用 `CHIRON_PORT` / `CHIRON_LOCAL_PORT` 覆盖一次即可。
4. **契约名收敛**：core 认 `CHIRON_*`（`AliasChoices`），壳层删 `CORE_ENV_ALIASES`。
   在此之前，任何"新加一个下发给 core 的变量"都必须同时写两套名字。
5. **provider 地址单一来源**：`providers.ollama.baseUrl`（`secrets.bin`）> `CHIRON_OLLAMA_URL` > 常量，
   解析函数只写一处，`inference-proxy.cjs` 与 `/v1/models` 都走它。
6. **打包参数构建期注入**：`appId`、`publish`、`extraResources` 的目录名从一份 `build.config.json`
   读，`electron-builder.yml` 与 `main.cjs` 不再各写一份。
7. **`components/manager.py:676` 的 cwd 兜底改成显式报错**：`data_dir` 缺失是接线错误，
   静默落到 `<cwd>/chiron-local` 会让用户以为数据丢了。

---

## 8. 本次审计的边界

- **没有改任何业务代码**，也没有新增依赖：`docs/` 加一份说明，`desktop-local/tests/` 加一个基线扫描器。
- `desktop-local/scripts/*`（`smoke_desktop.py` / `verify_*.py` / `build_component.py`）只做了人工通读，
  **未纳入**自动扫描（它们是验证工具，不是运行期配置面）。
- 前端 `frontend-vue/src/**` 不在范围内：那里的 `localStorage` 只是 `config.json` 的镜像
  （契约 §1），不是配置真相。
- Go 网关 / `python-engine` / `docker-compose.yml` 的配置面属于云端形态，不在本文件讨论范围。
- 第 5 节的数值全部来自代码常量；未在真机上做时序验证（例如 30s 健康等待在慢机器上是否够）。
