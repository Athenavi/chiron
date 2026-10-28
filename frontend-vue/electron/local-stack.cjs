'use strict'
/**
 * 本地数据栈编排：**引导器（PG + Redis）→ Go 网关**。
 *
 * 为什么本地模式要多一个进程而不是"直接 spawn 网关"：
 *   网关对 PG 与 Redis 都是**硬依赖**（`FATAL: PostgreSQL is required but unavailable`、
 *   `Redis is required but unavailable`），而它自己没有任何拉起数据库的能力。
 *   把 embedded PG 的 initdb、端口挑选、迁移这些脏活放进 Go 意味着把已经做实的
 *   Python 侧实现重写一遍；反过来让 Python 只做脏活、Go 只做业务，两边职责都干净。
 *   实测：引导器给出 DSN/端口后，网关能完整启动并通过 `/health`、`/ready`。
 *
 * 三件必须做对的事：
 *   1. **先引导器，后网关**。顺序反了网关会因为 PG 不可用直接退出，且报错点远离真因。
 *   2. **只认 JSON 行**。引导器的 stdout 混有 alembic 的 `[Alembic] ...` 日志，
 *      按 `event === 'launcher-ready'` 精确匹配，不做"包含某字符串"的模糊判断。
 *   3. **停的顺序与启动相反**。先停网关再停数据栈：反了会让网关拿着已死的连接报一堆
 *      噪声日志，把真正的关闭动作埋掉。
 */

const childProcess = require('node:child_process')
const components = require('./components.cjs')
const DEFAULTS = components.DEFAULTS

/** 引导器就绪事件（与 `desktop-local/app/launcher.py::READY_EVENT` 一致） */
const LAUNCHER_READY_EVENT = 'launcher-ready'
/** 引导器失败事件（与 launcher.py::ERROR_EVENT 一致） */
const LAUNCHER_ERROR_EVENT = 'launcher-error'

/**
 * 首启 initdb 在慢盘上实测能到 20s+，再叠加迁移；给足余量但不无限等。
 * 之所以不用 `pg_start_timeout`（60s）：那是单次 PG 起的超时，这里还包含 Redis 与迁移。
 */
const LAUNCHER_TIMEOUT_MS = 180_000

/**
 * 网关启动超时。网关自身只做连接与校验（实测 <1s），但冷启动时磁盘io/杀软扫描
 * 会让 exe 被拖慢，90s 足够且不会让 UI 长时间无反馈。
 */
const GATEWAY_TIMEOUT_MS = 90_000

/** 网关健康探针路径。**不是** `/healthz`：网关用的是 `/health` 与 `/ready`。 */
const GATEWAY_HEALTH_PATH = '/health'
const GATEWAY_READY_PATH = '/ready'

/** APP_SECRET 的最短长度要求（`config/config.go` 强制 32+） */
const APP_SECRET_MIN_LENGTH = 32

let injected = null
const state = {
  /** 引导器进程记录（含它给出的连接信息） */
  launcher: null,
  /** Go 网关进程记录 */
  gateway: null,
}

function runtime() {
  if (injected) return { spawn: childProcess.spawn, ...injected }
  return { spawn: childProcess.spawn }
}

function _setDeps(deps = {}) {
  injected = deps
}

function _resetDeps() {
  injected = null
}

function _resetState() {
  state.launcher = null
  state.gateway = null
}

/** 按行切分流式输出（与 components.cjs 同一套做法，那个未导出故自备一份） */
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
    if (buffer.length > 64 * 1024) buffer = buffer.slice(-4096)
  }
}

/**
 * 解析引导器的一行 stdout。
 *
 * 只返回**认识的事件**，其余（alembic 日志、警告、traceback）一律 null ——
 * 引导器的 stdout 是混合流，把非 JSON 行当错误会让正常启动失败。
 */
function parseLauncherLine(line) {
  const text = String(line || '').trim()
  if (!text.startsWith('{')) return null
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null

  if (parsed.event === LAUNCHER_ERROR_EVENT) {
    return { event: LAUNCHER_ERROR_EVENT, error: String(parsed.error || '未知错误') }
  }
  if (parsed.event !== LAUNCHER_READY_EVENT) return null

  const pgDsn = typeof parsed.pg_dsn === 'string' ? parsed.pg_dsn.trim() : ''
  const redisAddr = typeof parsed.redis_addr === 'string' ? parsed.redis_addr.trim() : ''
  return {
    event: LAUNCHER_READY_EVENT,
    pgDsn,
    redisAddr,
    dataDir: typeof parsed.data_dir === 'string' ? parsed.data_dir : '',
    pgBackend: typeof parsed.pg_backend === 'string' ? parsed.pg_backend : '',
    pgPort: Number(parsed.pg_port) || 0,
    redisBackend: typeof parsed.redis_backend === 'string' ? parsed.redis_backend : '',
    redisPort: Number(parsed.redis_port) || 0,
    migration: parsed.migration && typeof parsed.migration === 'object' ? parsed.migration : null,
    startupMs: Number(parsed.startup_ms) || 0,
  }
}

/**
 * 组装网关的环境变量。
 *
 * `APP_SECRET` 是网关的部署级主密钥（`JWT_SECRET`、`INTERNAL_TOKEN` 都由它派生），
 * 缺失或不足 32 字符网关会**直接拒绝启动**——所以这里必须显式校验，而不是传下去等它报。
 *
 * 注意：本函数不生成密钥。密钥要跨重启保持稳定（否则每次重启 JWT 全失效、
 * 用户被踢下线），持久化是调用方的事，这里只做契约校验。
 */
function buildGatewayEnv(info, options = {}) {
  if (!info || typeof info !== 'object') throw new Error('缺少引导器连接信息，无法启动网关')
  const pgDsn = String(info.pgDsn || '').trim()
  if (!pgDsn) {
    // 网关没有 DSN 会在自己的日志里报 FATAL 然后退出，报错点离这里很远；
    // 在交接处直接拦住，错误信息才指向真正的原因。
    throw new Error('引导器未提供 PostgreSQL DSN，网关无法启动')
  }

  const appSecret = String(options.appSecret || '').trim()
  if (appSecret.length < APP_SECRET_MIN_LENGTH) {
    throw new Error(
      `APP_SECRET 至少需要 ${APP_SECRET_MIN_LENGTH} 个字符（网关会拒绝启动），当前 ${appSecret.length} 个`,
    )
  }

  const port = Number(options.port)
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`网关端口非法：${options.port}`)
  }

  const env = { ...process.env, ...(options.env || {}) }
  env.POSTGRES_DSN = pgDsn
  env.PORT = String(port)
  env.APP_SECRET = appSecret

  // Redis 缺失时不写这一项：网关会用它自己的默认值，并且在 Redis 不可用时
  // 明确报 `Redis is required but unavailable`，比我们塞一个空串更可诊断。
  const redisAddr = String(info.redisAddr || '').trim()
  if (redisAddr) env.REDIS_ADDR = redisAddr

  const engineAddr = String(options.pythonEngineAddress || '').trim()
  if (engineAddr) env.PYTHON_ENGINE_ADDRESS = engineAddr

  return env
}

/**
 * 起引导器，等 `launcher-ready`。
 *
 * 成功返回 `{child, info, stderrTail}`；失败抛错（带 stderr 尾部，便于直接看出是
 * "PG 起不来"还是"Redis 二进制缺失"）。
 */
function startLauncher(options = {}) {
  const { spawn: spawnImpl } = runtime()
  const launcher = options.launcher
  if (!launcher || !launcher.command) throw new Error('缺少引导器启动信息（command）')

  const args = ['-m', 'app.launcher']
  if (options.dataDir) args.push('--data-dir', String(options.dataDir))

  let child
  try {
    child = spawnImpl(launcher.command, args, {
      // stdin 保持 pipe：父进程退出时管道关闭，引导器据此自行停掉 PG/Redis（见 launcher.py）
      stdio: ['pipe', 'pipe', 'pipe'],
      cwd: launcher.cwd || process.cwd(),
      windowsHide: true,
      env: { ...process.env, ...(options.env || {}) },
    })
  } catch (err) {
    throw new Error(`引导器启动失败：${(err && err.message) || 'spawn 出错'}`)
  }

  const record = {
    child,
    dataDir: options.dataDir || '',
    info: null,
    stderrTail: '',
    exited: false,
    exitCode: null,
    startedAt: new Date().toISOString(),
  }
  state.launcher = record

  return new Promise((resolve, reject) => {
    let settled = false

    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else resolve(value)
    }

    const timer = setTimeout(() => {
      finish(
        makeLauncherError(
          `引导器在 ${Math.round(LAUNCHER_TIMEOUT_MS / 1000)}s 内没有就绪`,
          record,
        ),
      )
    }, options.timeoutMs || LAUNCHER_TIMEOUT_MS)
    if (typeof timer.unref === 'function') timer.unref()

    if (child.stdout) {
      const feed = createLineReader(line => {
        const parsed = parseLauncherLine(line)
        if (!parsed) return
        if (parsed.event === LAUNCHER_ERROR_EVENT) {
          finish(makeLauncherError(`引导器内部错误：${parsed.error}`, record))
          return
        }
        record.info = parsed
        finish(null, { child, info: parsed, stderrTail: record.stderrTail })
      })
      child.stdout.on('data', chunk => feed(chunk.toString('utf8')))
    }

    if (child.stderr) {
      child.stderr.on('data', chunk => {
        record.stderrTail = tailOf(`${record.stderrTail}${chunk.toString('utf8')}`, 8192)
      })
    }

    child.once('error', err => {
      record.exited = true
      finish(makeLauncherError(`引导器进程错误：${(err && err.message) || err}`, record))
    })

    child.once('exit', code => {
      record.exited = true
      record.exitCode = code
      // 正常路径下这里不会走到（就绪后才会退出）；走到这里说明没等到就绪就死了
      finish(makeLauncherError(`引导器未就绪即退出（退出码 ${code}）`, record))
    })
  })
}

function tailOf(text, max) {
  const value = String(text || '')
  return value.length <= max ? value : value.slice(-max)
}

function makeLauncherError(message, record) {
  const err = new Error(`${message}${describeRecord(record)}`)
  err.stderrTail = record ? record.stderrTail : ''
  return err
}

/** 把 stderr 尾部拼进错误消息：引导器的诊断信息几乎全在这里 */
function describeRecord(record) {
  const tail = record && record.stderrTail ? String(record.stderrTail).trim() : ''
  return tail ? `\n\n引导器输出（尾部）：\n${tail}` : ''
}

/** 停引导器。先礼后兵：正常退出优先，超时再按进程树收。 */
async function stopLauncher() {
  const record = state.launcher
  if (!record) return false
  state.launcher = null
  if (!record.child || record.exited) return true

  const child = record.child
  // 关 stdin 是最"客气"的停止信号：引导器在 _watch_stdin_eof 里等 EOF 后自行收尾。
  try {
    if (child.stdin && !child.stdin.destroyed) child.stdin.end()
  } catch {
    /* stdin 可能已随进程关闭，忽略 */
  }

  if (await waitExit(child, 8000)) return true

  // 没能在宽限期内退出：整棵树一起收。
  // **必须收树**：引导器下面挂着 postgres 与 redis-server，只杀父进程会留下孤儿占着
  // 端口，下次启动 initdb / redis 就会撞端口，且日志里看不出是谁占的。
  try {
    components.killProcessTree(child)
  } catch {
    try {
      child.kill('SIGKILL')
    } catch {
      /* 进程已消失 */
    }
  }
  await waitExit(child, 5000)
  return true
}

function waitExit(child, timeoutMs) {
  if (!child || child.exitCode !== null) return Promise.resolve(true)
  return new Promise(resolve => {
    let done = false
    const finish = value => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => finish(false), timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()
    child.once('exit', () => finish(true))
  })
}

/**
 * 起 Go 网关并等它就绪。
 *
 * 卫生说明：网关启动后 `POSTGRES_DSN` 里的密码会出现在**子进程环境**里。
 * 这是不可避免的（网关需要它），边界在于：不写日志、不经渲染进程、不落盘。
 */
async function startGateway(info, options = {}) {
  const { spawn: spawnImpl } = runtime()
  const exe = options.exe
  if (!exe) throw new Error('未找到 Go 网关可执行文件')

  const port = Number(options.port)
  const env = buildGatewayEnv(info, { ...options, port })

  const child = spawnImpl(exe, [], {
    stdio: ['ignore', 'pipe', 'pipe'],
    cwd: options.cwd || process.cwd(),
    windowsHide: true,
    env,
  })

  const record = {
    child,
    port,
    baseUrl: `http://${DEFAULTS.DEFAULT_HOST}:${port}`,
    stderrTail: '',
    stdoutTail: '',
    exited: false,
    exitCode: null,
    startedAt: new Date().toISOString(),
  }
  state.gateway = record

  if (child.stdout) {
    child.stdout.on('data', chunk => {
      record.stdoutTail = tailOf(`${record.stdoutTail}${chunk.toString('utf8')}`, 8192)
    })
  }
  if (child.stderr) {
    child.stderr.on('data', chunk => {
      record.stderrTail = tailOf(`${record.stderrTail}${chunk.toString('utf8')}`, 8192)
    })
  }
  child.once('error', err => {
    record.exited = true
    record.spawnError = (err && err.message) || String(err)
  })
  child.once('exit', code => {
    record.exited = true
    record.exitCode = code
  })

  const deadline = Date.now() + (options.timeoutMs || GATEWAY_TIMEOUT_MS)
  while (Date.now() < deadline) {
    if (record.exited) {
      throw gatewayError(`网关启动后立即退出（退出码 ${record.exitCode}）`, record)
    }
    if (await probeHttp(record.baseUrl, GATEWAY_HEALTH_PATH)) {
      // `/health` 只证明进程活着（不查依赖）；`/ready` 才证明 PG/Redis 都通了。
      // 本地模式的"可用"必须以 `/ready` 为准，否则会把"启动了但连不上库"报成成功。
      const ready = await probeReadyJson(record.baseUrl)
      if (ready.ok) {
        return { child, baseUrl: record.baseUrl, port, ready, stderrTail: record.stderrTail }
      }
    }
    await sleep(options.intervalMs || 500)
  }

  throw gatewayError(
    `网关在 ${Math.round((options.timeoutMs || GATEWAY_TIMEOUT_MS) / 1000)}s 内未就绪`,
    record,
  )
}

function gatewayError(message, record) {
  const err = new Error(`${message}${describeRecord(record)}`)
  err.stderrTail = record ? record.stderrTail : ''
  err.stdoutTail = record ? record.stdoutTail : ''
  return err
}

async function probeHttp(baseUrl, pathname) {
  try {
    const res = await fetch(`${baseUrl}${pathname}`, { method: 'GET' })
    return res.ok
  } catch {
    // 连接被拒 = 还没监听；这里不做区分，交给外层超时与退出码判断
    return false
  }
}

/** `/ready` 返回 `{success:true, data:{postgres:'up', redis:'up'}}` */
async function probeReadyJson(baseUrl) {
  try {
    const res = await fetch(`${baseUrl}${GATEWAY_READY_PATH}`, { method: 'GET' })
    if (!res.ok) return { ok: false, status: res.status }
    const body = await res.json()
    const data = (body && body.data) || {}
    return {
      ok: data.postgres === 'up' && data.redis === 'up',
      postgres: data.postgres || '',
      redis: data.redis || '',
    }
  } catch (err) {
    return { ok: false, reason: (err && err.message) || String(err) }
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

/** 停网关。先 SIGTERM，超时再收树（网关是单进程，但仍按树收以防它派生）。 */
async function stopGateway() {
  const record = state.gateway
  if (!record) return false
  state.gateway = null
  if (!record.child || record.exited) return true
  const child = record.child
  try {
    child.kill('SIGTERM')
  } catch {
    /* 已消失 */
  }
  if (await waitExit(child, 5000)) return true
  try {
    components.killProcessTree(child)
  } catch {
    try {
      child.kill('SIGKILL')
    } catch {
      /* 已消失 */
    }
  }
  await waitExit(child, 5000)
  return true
}

/**
 * 一次性起本地整条链：引导器 → 网关。
 *
 * 失败时**回滚已启动的部分**：引导器起来了但网关失败，必须把引导器（连同它拉起的
 * PG/Redis）一起停掉，否则下次启动会撞上残留 postmaster。
 */
async function startLocalStack(options = {}) {
  const launcherResult = await startLauncher(options)
  try {
    const gatewayResult = await startGateway(launcherResult.info, options)
    return {
      baseUrl: gatewayResult.baseUrl,
      port: gatewayResult.port,
      ready: gatewayResult.ready,
      launcher: launcherResult.info,
    }
  } catch (err) {
    await stopLauncher()
    throw err
  }
}

async function stopLocalStack() {
  // 与启动相反：先网关，再数据栈
  const gatewayStopped = await stopGateway()
  const launcherStopped = await stopLauncher()
  return { gatewayStopped, launcherStopped }
}

function getStackState() {
  return {
    launcher: state.launcher
      ? { pid: state.launcher.child && state.launcher.child.pid, info: state.launcher.info }
      : null,
    gateway: state.gateway
      ? { pid: state.gateway.child && state.gateway.child.pid, port: state.gateway.port, baseUrl: state.gateway.baseUrl }
      : null,
  }
}

module.exports = {
  LAUNCHER_READY_EVENT,
  LAUNCHER_ERROR_EVENT,
  GATEWAY_HEALTH_PATH,
  GATEWAY_READY_PATH,
  LAUNCHER_TIMEOUT_MS,
  GATEWAY_TIMEOUT_MS,
  APP_SECRET_MIN_LENGTH,
  parseLauncherLine,
  buildGatewayEnv,
  startLauncher,
  stopLauncher,
  startGateway,
  stopGateway,
  startLocalStack,
  stopLocalStack,
  getStackState,
  _setDeps,
  _resetDeps,
  _resetState,
}
