/**
 * 本地组件管理（core / pageindex / langextract）。
 *
 * 约定（见 docs/desktop-local/contracts.md §2 §3）：
 *   - 组件目录固定 `userData/components/<name>`，是否安装以该目录下的
 *     `manifest.json` 能否解析为准（不猜、不缓存）。
 *   - core 的启动/停止是本文件唯一真正实现的动作：优先用
 *     `userData/components/core/chiron-local[.exe]`，开发态回退
 *     `python desktop-local/app/main.py`。
 *   - pageindex / langextract 只做占位：未安装给明确错误，已安装也明确
 *     说明启动逻辑属于 P3。
 *   - install / remove 不实现真实下载与解压：只校验本地目录与 manifest，
 *     缺东西就返回“需 P3 组件源”，绝不静默成功。
 *   - spawn 一律 `stdio: 'pipe'`：stdout/stderr 只累计字节数，不落盘、不打印，
 *     避免把本地后端的请求体/密钥刷进日志。
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

const state = {
  /** 当前 core 进程记录；null 表示本进程没起过或已停 */
  core: null,
  /** pageindex / langextract 的占位子进程（P3 前不会有内容） */
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

/** contracts.md §2 的 ComponentInfo[]；不含内部路径，交给渲染进程只读展示 */
async function listComponents() {
  const items = []
  for (const name of COMPONENT_NAMES) {
    const info = await describeComponent(name)
    items.push({
      name: info.name,
      version: info.version,
      installed: info.installed,
      running: info.running,
      sizeBytes: info.sizeBytes,
    })
  }
  return items
}

function repoRoot() {
  // frontend-vue/electron/components.cjs → 仓库根
  return path.resolve(__dirname, '..', '..')
}

/**
 * core 可执行文件解析：装好的二进制 > 开发态 python 脚本。
 * 两者都没有就明确报错，不要 spawn 一个不存在的命令（会抛难懂的 ENOENT）。
 * root 可注入，单测用它模拟“仓库里还没有 desktop-local”。
 */
function resolveCoreLaunch(root = repoRoot()) {
  const exeName = process.platform === 'win32' ? 'chiron-local.exe' : 'chiron-local'
  const exe = path.join(componentsRoot(), 'core', exeName)
  if (fs.existsSync(exe)) return { command: exe, args: [], kind: 'binary' }
  const script = path.join(root, 'desktop-local', 'app', 'main.py')
  if (fs.existsSync(script)) {
    return {
      command: process.env.CHIRON_PYTHON || 'python',
      args: [script],
      kind: 'dev',
    }
  }
  throw new Error(
    `未找到本地 core：请安装 ${exe}，或先建好 desktop-local/app/main.py 以开发态启动`,
  )
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

async function waitForHealth(record) {
  const deadline = Date.now() + CORE_HEALTH_TIMEOUT_MS
  while (Date.now() < deadline) {
    if (record.exited) {
      const reason = record.spawnError ? `spawn ${record.spawnError}` : `退出码 ${record.exitCode}`
      throw new Error(`本地 core 启动失败（${reason}，stdout ${record.stdoutBytes}B / stderr ${record.stderrBytes}B）`)
    }
    try {
      const res = await fetch(`${record.baseUrl}/healthz`, { signal: AbortSignal.timeout(1000) })
      if (res.ok) {
        const body = await res.json().catch(() => null)
        if (!body || body.status === undefined || body.status === 'ok') return true
      }
    } catch {
      // 还没起来：继续轮询
    }
    await sleep(CORE_HEALTH_INTERVAL_MS)
  }
  throw new Error(
    `本地 core 在 ${CORE_HEALTH_TIMEOUT_MS}ms 内没有响应 /healthz（stdout ${record.stdoutBytes}B / stderr ${record.stderrBytes}B）`,
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
    dataDir: record ? record.dataDir : '',
    startedAt: record ? record.startedAt : '',
    stdoutBytes: record ? record.stdoutBytes : 0,
    stderrBytes: record ? record.stderrBytes : 0,
    exitCode: record ? record.exitCode : null,
  }
}

/**
 * 起本地 core。
 * @param {{ dataDir?: string, port?: number, env?: Record<string,string>, inferenceProxyUrl?: string, repoRoot?: string }} options
 * @returns {Promise<{ baseUrl: string, token: string, pid: number|undefined }>}
 *
 * 与 W3（desktop-local）的约定只走环境变量，不传 CLI 参数：两边的参数名还没
 * 对齐，env 是更松的耦合面。等 main.py 定了 CLI 再加。
 */
async function startCore(options = {}) {
  const existing = state.core
  if (existing) {
    if (!existing.exited) {
      return { baseUrl: existing.baseUrl, token: existing.token, pid: existing.child ? existing.child.pid : undefined }
    }
    state.core = null
  }

  const dataDir = options.dataDir ? String(options.dataDir) : path.join(userDataDir(), 'local')
  const port = options.port ? Number(options.port) : await pickFreePort()
  const token = crypto.randomBytes(32).toString('hex')
  const root = options.repoRoot ? String(options.repoRoot) : repoRoot()
  const launch = resolveCoreLaunch(root)
  const { spawn: spawnImpl } = runtime()

  const env = {
    ...process.env,
    ...(options.env || {}),
    CHIRON_DATA_DIR: dataDir,
    CHIRON_PORT: String(port),
    CHIRON_DEVICE_TOKEN: token,
  }
  if (options.inferenceProxyUrl) env.CHIRON_INFERENCE_PROXY = String(options.inferenceProxyUrl)

  let child
  try {
    child = spawnImpl(launch.command, launch.args, {
      stdio: 'pipe',
      env,
      cwd: root,
      windowsHide: true,
    })
  } catch (err) {
    throw new Error(`本地 core 启动失败：${err && err.message ? err.message : 'spawn 出错'}`)
  }

  const record = {
    child,
    token,
    port,
    baseUrl: `http://127.0.0.1:${port}`,
    dataDir,
    startedAt: new Date().toISOString(),
    stdoutBytes: 0,
    stderrBytes: 0,
    exited: false,
    exitCode: null,
    spawnError: '',
  }
  state.core = record

  // 只累计长度：子进程输出里可能有用户数据，不能进日志
  if (child.stdout) child.stdout.on('data', chunk => { record.stdoutBytes += chunk.length })
  if (child.stderr) child.stderr.on('data', chunk => { record.stderrBytes += chunk.length })
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
  return { baseUrl: record.baseUrl, token: record.token, pid: child.pid }
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

const PLACEHOLDER_ERROR = target =>
  new Error(`组件 ${target} 的启动/停止属于 P3（当前只做占位，未实现进程管理）`)

/** contracts.md §2 startComponent(name)；core 走 startCore，其余为占位 */
async function startComponent(name, options = {}) {
  const target = assertComponentName(name)
  if (target === 'core') {
    const started = await startCore(options)
    return { name: 'core', running: true, ...started }
  }
  const info = await describeComponent(target)
  if (!info.installed) {
    throw new Error(`组件 ${target} 未安装（缺少 ${path.join(info.dir, 'manifest.json')}），请先安装再启动`)
  }
  throw PLACEHOLDER_ERROR(target)
}

async function stopComponent(name) {
  const target = assertComponentName(name)
  if (target === 'core') {
    const wasRunning = await stopCore()
    return { name: 'core', running: false, stopped: wasRunning }
  }
  const info = await describeComponent(target)
  if (!info.installed) {
    throw new Error(`组件 ${target} 未安装，无需停止`)
  }
  throw PLACEHOLDER_ERROR(target)
}

/**
 * 安装：只校验本地目录 + manifest，不做下载/解压（P3 才有组件源）。
 * 已经装好的目录直接返回现状，避免前端误以为“点一下就能更新”。
 */
async function installComponent(name) {
  const target = assertComponentName(name)
  const info = await describeComponent(target)
  if (info.installed) {
    return { name: target, version: info.version, installed: true, requiresSource: false }
  }
  const reason = fs.existsSync(info.dir) ? '目录存在但缺少可解析的 manifest.json' : '目录不存在'
  throw new Error(
    `需要 P3 组件源：组件 ${target} 需要下载/解压（${reason}）；当前版本请手动放入 ${info.dir} 并附带 manifest.json`,
  )
}

/** 卸载：同样只做校验，不删用户目录（删除是不可逆操作，留给 P3 的显式确认流程） */
async function removeComponent(name) {
  const target = assertComponentName(name)
  const info = await describeComponent(target)
  if (info.running) throw new Error(`组件 ${target} 正在运行，请先停止`)
  if (!info.installed) {
    if (fs.existsSync(info.dir)) {
      throw new Error(`组件 ${target} 的目录 ${info.dir} 存在但缺少可解析的 manifest.json，请手动检查`)
    }
    throw new Error(`组件 ${target} 未安装`)
  }
  throw new Error(`需要 P3 组件源：当前版本不支持卸载 ${target}，请手动删除 ${info.dir}`)
}

module.exports = {
  COMPONENT_NAMES,
  CORE_HEALTH_TIMEOUT_MS,
  componentsRoot,
  componentDir,
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
}
