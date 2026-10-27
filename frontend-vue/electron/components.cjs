/**
 * 本地组件管理（core / pageindex / langextract）。
 *
 * 职责划分（见 docs/desktop-local/contracts.md §2 §3 与 docs/desktop-local/components.md）：
 *   - **core** 由本文件启动/停止（startCore / stopCore 行为不变）：优先用
 *     `userData/components/core/chiron-local[.exe]`，开发态回退
 *     `python -m app.main`。
 *   - **pageindex / langextract** 的安装、启停全部委托给本地 core 的
 *     `/v1/components*` 端点（`desktop-local/app/api/components.py`）：本文件不再自己
 *     下载、解压或 spawn，只做 HTTP 调用 + 错误翻译 + 状态缓存。
 *   - 目录 `userData/components/<name>` 仍然存在，但**只用于 core 的二进制与回退展示**；
 *     pageindex / langextract 的真实安装目录以 core 的 data-dir 为准。
 *   - core 没在运行时不给“假装已安装”的答案：列表回退到磁盘探测并标 `error`，
 *     install/start/stop 直接抛“本地 core 未运行”，绝不静默成功。
 *   - spawn 一律 `stdio: 'pipe'`：stdout/stderr 只累计字节数；stderr 仅保留末尾
 *     一小段给**报错消息**（脱敏 + 截断），不落盘、不打印整段，避免把本地后端的
 *     请求体/密钥刷进日志。
 *   - 启动参数只走环境变量 + 少量 CLI（见 buildCoreEnv / resolveCoreLaunch）：
 *     `CHIRON_*` 是 contracts.md §3.1 的契约名，`CHIRON_LOCAL_*` 是当前
 *     desktop-local/app/config.py（pydantic-settings 的 env_prefix）真正读的名字，
 *     两套一起下发，见 docs/desktop-local/handshake.md。
 *   - 真实监听端口以 core 打在 stdout 的 `{"event":"listening","port":...}` 为准
 *     （main.py::serve 在绑成功之后才打印）；拿不到该事件时回退轮询 `/healthz`。
 */
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')

/** 与 contracts.md 的 ComponentInfo.name 一致，顺序即 UI 展示顺序 */
const COMPONENT_NAMES = Object.freeze(['core', 'pageindex', 'langextract'])

const CORE_HEALTH_TIMEOUT_MS = 30000
const CORE_HEALTH_INTERVAL_MS = 200
const CORE_STOP_TIMEOUT_MS = 5000
const CORE_KILL_TIMEOUT_MS = 2000
/** 报错消息里最多带多少字节 stderr（脱敏后）：够定位 ImportError / 端口占用即可 */
const CORE_STDERR_TAIL_BYTES = 2000
/** core 的组件端点（desktop-local/app/api/components.py） */
const CORE_COMPONENTS_PATH = '/v1/components'
/** 列组件应该很快：卡住就别让 UI 转圈 */
const CORE_LIST_TIMEOUT_MS = 5000
/** 安装要复制/解压组件包，给宽一点 */
const CORE_INSTALL_TIMEOUT_MS = 120000
/** 启动可能要等健康检查（core 侧默认 20s），再留点余量 */
const CORE_ACTION_TIMEOUT_MS = 60000

/**
 * core 的启动环境变量（contracts.md §3.1 的**契约名**）。
 * 值只在这里出现一次，其它地方引用这个表，避免字符串散落。
 */
const CORE_ENV = Object.freeze({
  dataDir: 'CHIRON_DATA_DIR',
  port: 'CHIRON_PORT',
  deviceToken: 'CHIRON_DEVICE_TOKEN',
  inferenceProxy: 'CHIRON_INFERENCE_PROXY',
  python: 'CHIRON_PYTHON',
})

/**
 * 契约名 → desktop-local/app/config.py 实际读的名字。
 *
 * config.py 是 pydantic-settings，`env_prefix = "CHIRON_LOCAL_"`，所以真正生效的是
 * `CHIRON_LOCAL_DATA_DIR` / `_PORT` / `_INFERENCE_PROXY`；只写契约名会被静默忽略
 * （表现为 core 用了平台默认数据目录、随机端口，壳层却以为参数已生效）。
 * 两套一起写是当前最松的耦合：等 core 直接支持契约名，把这张表删掉即可。
 * `desktop-local/app/**` 不在本 worktree 的改动范围内，所以桥接放在这一侧。
 */
const CORE_ENV_ALIASES = Object.freeze({
  CHIRON_DATA_DIR: ['CHIRON_LOCAL_DATA_DIR'],
  CHIRON_PORT: ['CHIRON_LOCAL_PORT'],
  CHIRON_INFERENCE_PROXY: ['CHIRON_LOCAL_INFERENCE_PROXY'],
  // auth.py 从 <data-dir>/auth/device-token 读设备 token，暂不读 env；先照样下发，
  // 等 core 支持后无需再改这一侧。
  CHIRON_DEVICE_TOKEN: [],
})

const state = {
  /** 当前 core 进程记录；null 表示本进程没起过或已停 */
  core: null,
  /**
   * pageindex / langextract 的运行状态缓存，内容来自 core 的 `/v1/components`。
   * 真进程由 core 管理，这里只用来在 core 短暂不可达时也能给出最后一次已知状态。
   */
  children: new Map(),
}

let injected = null

function runtime() {
  if (injected) return { spawn, ...injected }
  const electron = require('electron')
  return { app: electron.app, spawn }
}

/** 仅供 __tests__（纯 Node，无 electron）：注入 app.getPath */
function _setDeps(deps) {
  injected = deps || null
}

function _resetDeps() {
  injected = null
}

/** 仅供 __tests__：清掉进程记录与组件状态缓存（不会去 kill 真进程） */
function _resetState() {
  state.core = null
  state.children.clear()
}

function userDataDir() {
  const { app } = runtime()
  if (!app || typeof app.getPath !== 'function') {
    throw new Error('components.cjs 需要 Electron 的 app.getPath（纯 Node 环境请注入依赖）')
  }
  return app.getPath('userData')
}

function componentsRoot() {
  return path.join(userDataDir(), 'components')
}

function assertComponentName(name) {
  const target = String(name || '')
  if (!COMPONENT_NAMES.includes(target)) {
    throw new Error(`未知组件：${name}（可选：${COMPONENT_NAMES.join(' / ')}）`)
  }
  return target
}

function componentDir(name) {
  return path.join(componentsRoot(), assertComponentName(name))
}

/** manifest.json 读不出来（缺失 / 坏 JSON / 不是对象）就当未安装 */
async function readManifest(dir) {
  try {
    const raw = await fs.promises.readFile(path.join(dir, 'manifest.json'), 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    return parsed
  } catch {
    return null
  }
}

/** 目录体积；符号链接跳过（防止环），读不到的条目忽略 */
async function dirSize(dir) {
  let entries
  try {
    entries = await fs.promises.readdir(dir, { withFileTypes: true })
  } catch {
    return 0
  }
  let total = 0
  for (const entry of entries) {
    const full = path.join(dir, entry.name)
    try {
      if (entry.isDirectory()) total += await dirSize(full)
      else if (entry.isFile()) total += (await fs.promises.stat(full)).size
    } catch {
      // 权限/竞态：忽略这一项，返回已统计到的字节数
    }
  }
  return total
}

function isRunning(name) {
  if (name === 'core') {
    const core = state.core
    return Boolean(core && !core.exited)
  }
  return state.children.has(name)
}

async function describeComponent(name) {
  const target = assertComponentName(name)
  const dir = componentDir(target)
  const manifest = await readManifest(dir)
  return {
    name: target,
    dir,
    manifest,
    version: manifest && typeof manifest.version === 'string' ? manifest.version : '',
    installed: Boolean(manifest),
    running: isRunning(target),
    sizeBytes: await dirSize(dir),
  }
}

// ------------------------------------------------------------------ core HTTP
/** core 的 baseUrl；没在跑就抛可读错误（组件管理不能在没后端时假装成功） */
function requireCoreBaseUrl() {
  const record = state.core
  if (!record || record.exited || !record.baseUrl) {
    throw new Error('本地 core 未运行：组件管理需要本地后端在线，请先启动本地模式（或重启应用）')
  }
  return record.baseUrl
}

/**
 * 调 core 的 JSON 接口。返回解析后的 body（`{data, ...}` 原样），非 2xx 抛已翻译的错误。
 * 不带请求体：组件的输入参数（组件源、版本）由 core 自己从环境变量与 manifest 读。
 */
async function coreJson(method, pathname, options = {}) {
  const baseUrl = requireCoreBaseUrl()
  const timeoutMs = options.timeoutMs || CORE_ACTION_TIMEOUT_MS
  const headers = { Accept: 'application/json' }
  // 设备 token 只用于 loopback 调用；不进日志、不回渲染进程
  if (state.core && state.core.token) headers.Authorization = `Bearer ${state.core.token}`
  let response
  try {
    response = await httpFetch()(`${baseUrl}${pathname}`, {
      method,
      headers,
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (err) {
    throw new Error(`无法访问本地 core（${baseUrl}${pathname}）：${err && err.message ? err.message : err}`)
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(describeCoreError(response.status, body))
  return body && typeof body === 'object' ? body : {}
}

/** 取 `{data: ...}` 里的 data（core 的响应统一包装，契约 §4） */
function unwrapData(body) {
  if (body && typeof body === 'object' && 'data' in body) return body.data
  return body
}

/**
 * 把 core 的 `{error, message}` 翻成一句用户能看懂的话。
 * 错误码清单见 docs/desktop-local/components.md 的「错误码」一节。
 */
function describeCoreError(status, body) {
  const code = body && typeof body.error === 'string' ? body.error : ''
  const detail = body && typeof body.message === 'string' ? body.message : ''
  const hint = CORE_ERROR_HINTS[code] || ''
  if (detail && hint) return `${hint}（${detail}）`
  if (detail) return detail
  if (hint) return hint
  return `本地 core 返回 HTTP ${status}`
}

/** core 的错误码 → 人话。新增错误码时两处一起改（core: app/components/manager.py） */
const CORE_ERROR_HINTS = Object.freeze({
  component_source_unconfigured:
    '未配置组件源：请把环境变量 CHIRON_COMPONENT_BASE_URL 指向本地目录或 HTTP 基址，然后重启本地后端',
  component_source_unavailable: '组件源不可用（目录不存在或无法访问）',
  component_not_in_source: '组件源里没有这个组件（缺 manifest.json 或文件）',
  component_manifest_invalid: '组件清单不合法',
  component_integrity_failed: '组件包校验失败（sha256 不匹配），已中止安装',
  component_unsafe_path: '组件包含不安全路径（.. / 绝对路径 / 符号链接），已拒绝安装',
  component_not_installed: '组件未安装，请先安装',
  component_running: '组件正在运行，请先停止',
  component_entry_unsupported: '组件入口类型不支持（只支持 python / node / 可执行文件）',
  component_start_failed: '组件启动失败',
  unknown_component: '未知组件',
  invalid_component_name: '组件名不合法',
})

/** 记住 core 报回来的运行状态（core 暂时不可达时列表还能显示最后一次已知值） */
function rememberComponent(name, data) {
  const running = Boolean(data && (data.running === undefined ? data.pid : data.running))
  if (running) {
    state.children.set(name, {
      pid: data.pid,
      baseUrl: typeof data.baseUrl === 'string' ? data.baseUrl : '',
      version: typeof data.version === 'string' ? data.version : '',
    })
    return
  }
  state.children.delete(name)
}

/** 从 core 拉组件列表；core 没在跑或请求失败都返回 `{error}` 而不是抛 */
async function fetchCoreComponents() {
  if (!isCoreRunning()) return { items: null, error: 'core-not-running', source: null }
  try {
    const body = await coreJson('GET', CORE_COMPONENTS_PATH, { timeoutMs: CORE_LIST_TIMEOUT_MS })
    const items = Array.isArray(body.data) ? body.data : []
    for (const item of items) {
      if (item && typeof item.name === 'string' && item.name !== 'core') rememberComponent(item.name, item)
    }
    return { items, error: '', source: body.source || null }
  } catch (err) {
    return { items: null, error: err && err.message ? err.message : String(err), source: null }
  }
}

/** core 不可达时的 running：用缓存里的最后一次已知状态（不猜） */
function cachedRunning(name) {
  return state.children.has(name)
}

/**
 * contracts.md §2 的 ComponentInfo[]；不含内部路径，交给渲染进程只读展示。
 *
 * pageindex / langextract 的安装状态以本地 core 的 `/v1/components` 为准；core 没在跑时
 * 回退到磁盘探测（`userData/components/<name>/manifest.json`），并在每项上标 `error`，
 * 让 UI 能区分“确实没装”和“后端还没起来”。
 */
async function listComponents() {
  const remote = await fetchCoreComponents()
  const items = []
  for (const name of COMPONENT_NAMES) {
    if (name === 'core') {
      const local = await describeComponent(name)
      const coreState = getCoreState()
      items.push({
        name,
        version: local.version,
        installed: local.installed || Boolean(coreState.running),
        running: coreState.running,
        sizeBytes: local.sizeBytes,
        pid: coreState.pid,
        baseUrl: coreState.baseUrl,
        error: '',
      })
      continue
    }

    const local = await describeComponent(name)
    const found = remote.items ? remote.items.find(item => item && item.name === name) : null
    if (found) {
      items.push({
        name,
        version: typeof found.version === 'string' ? found.version : '',
        installed: Boolean(found.installed),
        running: Boolean(found.running),
        sizeBytes: Number(found.sizeBytes) || 0,
        pid: found.pid === undefined ? undefined : found.pid,
        baseUrl: typeof found.baseUrl === 'string' ? found.baseUrl : '',
        error: '',
      })
      continue
    }
    items.push({
      name,
      version: local.version,
      installed: local.installed,
      running: cachedRunning(name),
      sizeBytes: local.sizeBytes,
      pid: undefined,
      baseUrl: '',
      error: remote.error,
    })
  }
  return items
}

function repoRoot() {
  // frontend-vue/electron/components.cjs → 仓库根
  return path.resolve(__dirname, '..', '..')
}

/** desktop-local 目录：`-m app.main` 的 cwd，也是 `app` 包能被导入的前提 */
function coreSourceDir(root = repoRoot()) {
  return path.join(root, 'desktop-local')
}

/** core 二进制在 userData 里的固定位置（装好的 core 优先用它） */
function coreBinaryPath() {
  const exeName = process.platform === 'win32' ? 'chiron-local.exe' : 'chiron-local'
  return path.join(componentsRoot(), 'core', exeName)
}

/**
 * core 可执行文件解析：装好的二进制 > 开发态源码。
 * 两者都没有就明确报错，不要 spawn 一个不存在的命令（会抛难懂的 ENOENT）。
 * root 可注入，单测用它模拟“仓库里还没有 desktop-local”。
 *
 * 返回 `{ command, args, kind, cwd }`：
 *   - binary：只走 env（契约 §3.1 的耦合面）；不猜它是否认识本批次新增的 CLI 参数。
 *   - dev：额外带 `--data-dir/--port/--inference-proxy`（CLI 覆盖 env，最可靠），
 *     并用 `-m app.main` + `cwd=desktop-local`。**不能**写成
 *     `python desktop-local/app/main.py`：main.py 是包内导入（`from app import ...`），
 *     以脚本方式启动时 sys.path[0] 是 `desktop-local/app`，`app` 包不在搜索路径上，
 *     必然 ImportError。`-m` 会把 cwd 放进 sys.path。
 */
function resolveCoreLaunch(root = repoRoot()) {
  const exe = coreBinaryPath()
  if (fs.existsSync(exe)) return { command: exe, args: [], kind: 'binary', cwd: root }
  const script = path.join(root, 'desktop-local', 'app', 'main.py')
  if (fs.existsSync(script)) {
    return {
      command: process.env[CORE_ENV.python] || 'python',
      args: ['-m', 'app.main'],
      kind: 'dev',
      cwd: coreSourceDir(root),
    }
  }
  throw new Error(
    `未找到本地 core：请安装 ${exe}，或先建好 desktop-local/app/main.py 以开发态启动`,
  )
}

/**
 * 组装 core 的环境变量：进程环境 → 调用方覆盖 → 契约名 + config.py 真名。
 * 空值不下发（pydantic 会把 `""` 当成显式值，反而盖掉合理默认）。
 */
function buildCoreEnv(options = {}) {
  const env = { ...process.env, ...(options.env || {}) }
  const set = (name, value) => {
    if (value === undefined || value === null || value === '') return
    env[name] = String(value)
    for (const alias of CORE_ENV_ALIASES[name] || []) env[alias] = String(value)
  }
  set(CORE_ENV.dataDir, options.dataDir)
  set(CORE_ENV.port, options.port)
  set(CORE_ENV.deviceToken, options.token)
  set(CORE_ENV.inferenceProxy, options.inferenceProxyUrl)
  return env
}

/** dev 态额外下发的 CLI 参数：与 env 同义，但 CLI 优先级更高（main.py 的 argparse 覆盖） */
function coreCliArgs(options = {}) {
  const args = []
  if (options.dataDir) args.push('--data-dir', String(options.dataDir))
  if (options.port !== undefined && options.port !== null) args.push('--port', String(options.port))
  if (options.inferenceProxyUrl) args.push('--inference-proxy', String(options.inferenceProxyUrl))
  return args
}

/** 按行切分流式输出：JSON 播报可能被 chunk 截断，必须自己攒行 */
function createLineReader(onLine) {
  let buffer = ''
  return chunk => {
    buffer += chunk
    let index = buffer.indexOf('\n')
    while (index >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line.trim()) onLine(line)
      index = buffer.indexOf('\n')
    }
    // 单行异常长（比如 core 打了巨大 traceback）就别无限攒内存
    if (buffer.length > 64 * 1024) buffer = buffer.slice(-4096)
  }
}

/**
 * 解析 main.py::serve 打印的启动播报。只认 `event === "listening"` 且端口合法的那行，
 * 其它 stdout 一律忽略（core 未来加的日志不该让握手失败）。
 */
function parseListeningLine(line) {
  const text = String(line || '').trim()
  if (!text.startsWith('{') || !text.includes('"listening"')) return null
  try {
    const parsed = JSON.parse(text)
    if (!parsed || parsed.event !== 'listening') return null
    const port = Number(parsed.port)
    if (!Number.isInteger(port) || port <= 0 || port > 65535) return null
    return {
      port,
      host: typeof parsed.host === 'string' ? parsed.host : '',
      dataDir: typeof parsed.data_dir === 'string' ? parsed.data_dir : '',
      version: typeof parsed.version === 'string' ? parsed.version : '',
    }
  } catch {
    return null
  }
}

function tailOf(text, max) {
  const value = String(text || '')
  return value.length <= max ? value : value.slice(-max)
}

/**
 * 报错消息会进 main.log 与用户眼前的对话框，所以 DSN 里的口令、token 之类的
 * `key=value` 必须先抹掉：stderr 里的 Python traceback 经常带完整连接串。
 */
function redactSecrets(text) {
  return String(text || '')
    .replace(/(\b[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+):([^/\s@]+)@/gi, '$1:***@')
    .replace(/(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*["']?[^\s"',;]+/gi, '$1=***')
}


/** 让内核自己开 127.0.0.1:0 拿端口，再立刻关掉——只借端口号，不占坑 */
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/** 统一的 fetch 入口：单测通过 `_setDeps({fetch})` 注入，运行期用全局 fetch */
function httpFetch() {
  const injected = runtime()
  return injected.fetch || fetch
}

/** 单次健康探测：core 活着就 true。失败/超时一律当"还没起来"，不抛。 */
async function probeHealth(baseUrl, timeoutMs = 1000) {
  if (!baseUrl) return false
  try {
    const res = await httpFetch()(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) return false
    const body = await res.json().catch(() => null)
    // 契约 §4：{"status":"ok"}。字段缺失时按"活着"算，避免把简化实现判死。
    return !body || body.status === undefined || body.status === 'ok'
  } catch {
    return false
  }
}

function coreExitError(record) {
  const reason = record.spawnError ? `spawn ${record.spawnError}` : `退出码 ${record.exitCode}`
  const detail = record.stderrTail
    ? `；stderr 末尾：${redactSecrets(record.stderrTail).trim()}`
    : ''
  return new Error(
    `本地 core 启动失败（${reason}，stdout ${record.stdoutBytes}B / stderr ${record.stderrBytes}B${detail}）`,
  )
}

/**
 * 等 core 就绪：两种信源合并到同一个循环里。
 *   1. stdout 的 `{"event":"listening","port":...}` —— 唯一可靠的端口来源
 *      （CHIRON_PORT=0 时只有内核知道真实端口），到达后立刻用该端口探测；
 *   2. 端口已知（我们借来的空闲端口）时直接轮询 `/healthz`，兼容不打印播报的实现。
 * 进程提前退出立即失败，不要白等 30s。
 */
async function waitForHealth(record) {
  const deadline = Date.now() + CORE_HEALTH_TIMEOUT_MS
  while (Date.now() < deadline) {
    if (record.exited) throw coreExitError(record)
    if (await probeHealth(record.baseUrl)) return true
    await sleep(CORE_HEALTH_INTERVAL_MS)
  }
  const hint = record.baseUrl
    ? `在 ${CORE_HEALTH_TIMEOUT_MS}ms 内没有响应 ${record.baseUrl}/healthz`
    : `在 ${CORE_HEALTH_TIMEOUT_MS}ms 内没有收到 stdout 的 listening 事件（端口未知，无法探测 /healthz）`
  throw new Error(
    `本地 core ${hint}（stdout ${record.stdoutBytes}B / stderr ${record.stderrBytes}B${
      record.stderrTail ? `；stderr 末尾：${redactSecrets(record.stderrTail).trim()}` : ''
    }）`,
  )
}

async function waitForExit(record, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (record.exited) return true
    await sleep(100)
  }
  return record.exited
}

function snapshot(record) {
  return {
    running: Boolean(record && !record.exited),
    baseUrl: record ? record.baseUrl : '',
    token: record ? record.token : '',
    pid: record && record.child ? record.child.pid : undefined,
    port: record ? record.port : 0,
    dataDir: record ? record.dataDir : '',
    /** core 自己播报的数据目录（证明 CHIRON_DATA_DIR 真的生效了） */
    announcedDataDir: record ? record.announcedDataDir : '',
    listening: Boolean(record && record.listening),
    startedAt: record ? record.startedAt : '',
    stdoutBytes: record ? record.stdoutBytes : 0,
    stderrBytes: record ? record.stderrBytes : 0,
    exitCode: record ? record.exitCode : null,
  }
}

/** 统一的结果形状（契约 §3.1：壳层只关心这三个字段，其余是排查用的附加值） */
function coreResult(record, child = record.child) {
  return {
    baseUrl: record.baseUrl,
    token: record.token,
    pid: child ? child.pid : undefined,
    port: record.port,
    dataDir: record.announcedDataDir || record.dataDir,
    listening: Boolean(record.listening),
  }
}

/**
 * 起本地 core，返回 `{ baseUrl, token, pid }`（另附 port / dataDir / listening 供诊断）。
 *
 * @param {{
 *   dataDir?: string, port?: number, token?: string,
 *   env?: Record<string,string>, inferenceProxyUrl?: string, repoRoot?: string,
 * }} options
 *
 * 与 desktop-local 的对齐点（docs/desktop-local/handshake.md）：
 *   - 环境变量优先：`CHIRON_DATA_DIR` / `CHIRON_PORT` / `CHIRON_DEVICE_TOKEN` /
 *     `CHIRON_INFERENCE_PROXY` / `CHIRON_PYTHON`；同一份值也写成 `CHIRON_LOCAL_*`，
 *     因为当前 config.py 的 pydantic 前缀是后者（见 CORE_ENV_ALIASES）。
 *   - 真实端口以 stdout 的 listening 事件为准，拿不到再轮询 `/healthz`。
 *   - `options.port === 0` 表示"交给内核随机"，此时在收到 listening 事件前不知道该
 *     探测哪个端口；不传 port 时先借一个空闲端口，能立刻轮询又不占坑。
 */
async function startCore(options = {}) {
  const existing = state.core
  if (existing) {
    if (!existing.exited) return coreResult(existing)
    state.core = null
  }

  const dataDir = options.dataDir ? String(options.dataDir) : path.join(userDataDir(), 'local')
  let requestedPort
  if (options.port === undefined || options.port === null) requestedPort = await pickFreePort()
  else requestedPort = Number(options.port)
  if (!Number.isInteger(requestedPort) || requestedPort < 0 || requestedPort > 65535) {
    throw new Error(`本地 core 端口不合法：${options.port}（0..65535，0 = 随机）`)
  }

  // token 可以外部指定（verify 脚本 / 重连场景），否则沿用环境变量，最后才自己生成。
  const token =
    (options.token && String(options.token)) ||
    process.env[CORE_ENV.deviceToken] ||
    crypto.randomBytes(32).toString('hex')

  const root = options.repoRoot ? String(options.repoRoot) : repoRoot()
  const launch = resolveCoreLaunch(root)
  const { spawn: spawnImpl } = runtime()

  const launchOptions = {
    dataDir,
    port: requestedPort,
    token,
    inferenceProxyUrl: options.inferenceProxyUrl,
    env: options.env,
  }
  const env = buildCoreEnv(launchOptions)
  // dev 态再补 CLI：argparse 里的显式参数优先于 env，是"一定生效"的那条路
  const args = launch.kind === 'dev' ? [...launch.args, ...coreCliArgs(launchOptions)] : [...launch.args]

  let child
  try {
    child = spawnImpl(launch.command, args, {
      stdio: 'pipe',
      env,
      cwd: launch.cwd || root,
      windowsHide: true,
    })
  } catch (err) {
    throw new Error(`本地 core 启动失败：${err && err.message ? err.message : 'spawn 出错'}`)
  }

  const record = {
    child,
    token,
    /** 请求的端口（0 = 让内核挑） */
    requestedPort,
    /** 实际端口：requestedPort > 0 时先按它探测，listening 事件到达后以其为准 */
    port: requestedPort,
    baseUrl: requestedPort > 0 ? `http://127.0.0.1:${requestedPort}` : '',
    dataDir,
    announcedDataDir: '',
    listening: null,
    kind: launch.kind,
    startedAt: new Date().toISOString(),
    stdoutBytes: 0,
    stderrBytes: 0,
    stderrTail: '',
    exited: false,
    exitCode: null,
    spawnError: '',
  }
  state.core = record

  // stdout：只累计字节数 + 抓 listening 播报，不落盘、不回显（可能有用户数据）
  if (child.stdout) {
    const feed = createLineReader(line => {
      const announce = parseListeningLine(line)
      if (!announce) return
      record.listening = announce
      record.port = announce.port
      // 主机名只接受 loopback：core 只绑 127.0.0.1（契约 §4），别被 stdout 带去别处
      const host = announce.host === 'localhost' || announce.host === '127.0.0.1' ? announce.host : '127.0.0.1'
      record.baseUrl = `http://${host}:${announce.port}`
      if (announce.dataDir) record.announcedDataDir = announce.dataDir
    })
    child.stdout.on('data', chunk => {
      record.stdoutBytes += chunk.length
      feed(chunk.toString('utf8'))
    })
  }
  if (child.stderr) {
    child.stderr.on('data', chunk => {
      record.stderrBytes += chunk.length
      // 只留末尾一小段给报错消息（redactSecrets 在拼消息时再跑一次）
      record.stderrTail = tailOf(`${record.stderrTail}${chunk.toString('utf8')}`, CORE_STDERR_TAIL_BYTES)
    })
  }
  child.once('error', err => {
    record.exited = true
    record.spawnError = err && err.message ? err.message : String(err)
  })
  child.once('exit', code => {
    record.exited = true
    record.exitCode = code
  })

  try {
    await waitForHealth(record)
  } catch (err) {
    await stopCore()
    throw err
  }
  return coreResult(record, child)
}

/** 停 core：SIGTERM → 等 → SIGKILL；返回是否本来就有进程要停 */
async function stopCore() {
  const record = state.core
  if (!record) return false
  state.core = null
  if (!record.child || record.exited) return true
  try {
    record.child.kill('SIGTERM')
  } catch {
    // 已经死了：下面 waitForExit 会看到 exited
  }
  if (!(await waitForExit(record, CORE_STOP_TIMEOUT_MS))) {
    try {
      record.child.kill('SIGKILL')
    } catch {
      // 同上
    }
    await waitForExit(record, CORE_KILL_TIMEOUT_MS)
  }
  return true
}

function getCoreState() {
  return snapshot(state.core)
}

function isCoreRunning() {
  return isRunning('core')
}

/** core 随应用发布，不走组件源：给一句能直接照做的说明 */
function coreNotInstallable() {
  return new Error(
    `本地 core 由桌面端自身提供，不能通过组件管理安装：请把 ${coreBinaryPath()} 放到位` +
      '，或在仓库根保留 desktop-local/app/main.py 以开发态启动',
  )
}

/**
 * contracts.md §2 startComponent(name)。
 * core 走 startCore（行为不变）；pageindex / langextract 交给本地 core 的
 * `POST /v1/components/{name}/start`，返回 `{pid, baseUrl}` 加版本/健康检查结果。
 */
async function startComponent(name, options = {}) {
  const target = assertComponentName(name)
  if (target === 'core') {
    const started = await startCore(options)
    return { name: 'core', running: true, ...started }
  }
  const body = await coreJson('POST', `${CORE_COMPONENTS_PATH}/${encodeURIComponent(target)}/start`)
  const data = unwrapData(body) || {}
  rememberComponent(target, data)
  return {
    name: target,
    running: true,
    pid: data.pid,
    baseUrl: data.baseUrl || '',
    port: data.port,
    version: data.version || '',
    health: data.health || null,
  }
}

/** 停止：core 做 terminate → kill；没在跑时 `stopped:false`（幂等，不报错） */
async function stopComponent(name) {
  const target = assertComponentName(name)
  if (target === 'core') {
    const wasRunning = await stopCore()
    return { name: 'core', running: false, stopped: wasRunning }
  }
  const body = await coreJson('POST', `${CORE_COMPONENTS_PATH}/${encodeURIComponent(target)}/stop`)
  const data = unwrapData(body) || {}
  rememberComponent(target, { running: false })
  return { name: target, running: false, stopped: Boolean(data.stopped), pid: data.pid }
}

/**
 * 安装：core 负责取组件源、校验 sha256、拒绝路径穿越、解压到
 * `<data-dir>/components/<name>/<version>`。壳层只转达结果。
 * 未配置组件源时 core 返回 409 `component_source_unconfigured`，这里翻成可读错误。
 */
async function installComponent(name) {
  const target = assertComponentName(name)
  if (target === 'core') throw coreNotInstallable()
  const body = await coreJson('POST', `${CORE_COMPONENTS_PATH}/${encodeURIComponent(target)}/install`, {
    timeoutMs: CORE_INSTALL_TIMEOUT_MS,
  })
  const data = unwrapData(body) || {}
  rememberComponent(target, { running: false })
  return {
    name: target,
    version: data.version || '',
    installed: true,
    requiresSource: false,
    dir: data.dir || '',
    entry: data.entry || '',
    source: data.source || '',
  }
}

/** 卸载：core 删除整个 `<data-dir>/components/<name>`（正在运行时会拒绝） */
async function removeComponent(name) {
  const target = assertComponentName(name)
  if (target === 'core') {
    throw new Error(`本地 core 随桌面端发布，不支持通过组件管理卸载：${coreBinaryPath()}`)
  }
  const body = await coreJson('DELETE', `${CORE_COMPONENTS_PATH}/${encodeURIComponent(target)}`)
  const data = unwrapData(body) || {}
  rememberComponent(target, { running: false })
  return { name: target, removed: Boolean(data.removed), dir: data.dir || '' }
}

module.exports = {
  COMPONENT_NAMES,
  CORE_COMPONENTS_PATH,
  CORE_ERROR_HINTS,
  CORE_HEALTH_TIMEOUT_MS,
  CORE_ENV,
  CORE_ENV_ALIASES,
  componentsRoot,
  componentDir,
  coreBinaryPath,
  coreSourceDir,
  resolveCoreLaunch,
  buildCoreEnv,
  coreCliArgs,
  parseListeningLine,
  redactSecrets,
  describeCoreError,
  listComponents,
  describeComponent,
  startCore,
  stopCore,
  getCoreState,
  isCoreRunning,
  startComponent,
  stopComponent,
  installComponent,
  removeComponent,
  _setDeps,
  _resetDeps,
  _resetState,
}
