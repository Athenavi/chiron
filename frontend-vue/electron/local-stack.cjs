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
const runtimeState = require('./runtime-state.cjs')
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

/** 引擎活跃探针。**不是** `/readyz`：只要进程在监听就够，依赖问题由降级模式兜住。 */
const ENGINE_HEALTH_PATH = '/healthz'

/**
 * 引擎启动超时。uvicorn 冷导入 pydantic-ai 等重依赖实测约 5-15s，
 * 杀软扫描会更慢；给 120s，但不参与"整栈失败"判定（见 startLocalStack）。
 */
const ENGINE_TIMEOUT_MS = 120_000

/**
 * 端口冲突时最多尝试几次（含首次）。与 `components.cjs` 的 `CORE_PORT_ATTEMPTS` 一致。
 *
 * 为什么必须重试：`pickFreePort()` / `find_free_port()` 都只是**借号**——它们 listen 到
 * 0 端口拿到号后立刻 close，到目标进程真正 bind 之间存在无法消除的竞态。Windows 上
 * `127.0.0.1` 还会撞 Hyper-V 保留段与 TIME_WAIT，所以"挑到空闲端口"永远不等于
 * "这个端口一定用得上"。只挑一次就 spawn，等于把一次毫秒级的竞态直接升级成
 * "本地模式起不来"，而这恰恰是用户最不容易自查的那类失败。
 */
const PORT_ATTEMPTS = DEFAULTS.CORE_PORT_ATTEMPTS

let injected = null
const state = {
  /** 引导器进程记录（含它给出的连接信息） */
  launcher: null,
  /** python-engine 进程记录 */
  engine: null,
  /** Go 网关进程记录 */
  gateway: null,
  /**
   * 接管到的既有栈（不是本进程拉起的）。
   *
   * 有值意味着 `stopLocalStack` **不该**去停任何进程——那套 PG/Redis/网关属于
   * 别的实例，停了会让对方崩掉。只有在接管方自己退出时才清状态文件。
   */
  attached: null,
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
  state.engine = null
  state.engineFailure = null
  state.gateway = null
  state.attached = null
  state.ownedDataDir = ''
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
 *
 * 端口冲突会**换号重试**（见 `PORT_ATTEMPTS`）：调用方给的端口是 `pickFreePort()`
 * 借来的，从它 close 到网关真正 bind 之间随时可能被别的进程抢走。只试一次就把一次
 * 毫秒级竞态升级成"本地模式起不来"，代价与概率完全不成比例。
 */
async function startGateway(info, options = {}) {
  const exe = options.exe
  if (!exe) throw new Error('未找到 Go 网关可执行文件')

  // 显式钉死端口时不做换号重试：那时换号等于让已经知道地址的一方连不上，
  // 如实报冲突比"起来了但没人连得到"更有用。
  const attempts = options.retryPortConflict === false ? 1 : PORT_ATTEMPTS
  const tried = []

  for (let index = 0; index < attempts; index += 1) {
    const port = index === 0 ? Number(options.port) : await components.pickFreePort()
    try {
      return await spawnGatewayOnce(info, { ...options, port })
    } catch (err) {
      const text = `${(err && err.stderrTail) || ''}\n${(err && err.message) || ''}`
      const portConflict = DEFAULTS.isPortConflict(text)
      // 每次失败都先收干净：残留的网关进程会继续占着端口，下一轮更没机会
      await stopGateway()
      tried.push({
        attempt: index + 1,
        port,
        portConflict,
        message: (err && err.message) || String(err),
      })
      // 不可恢复的 spawn 错误（ENOENT/EACCES）与普通启动失败换号也没用
      if (!portConflict) break
    }
  }

  const last = tried[tried.length - 1]
  const err = new Error(last ? last.message : '网关启动失败')
  err.attempts = tried
  err.portConflict = Boolean(last && last.portConflict)
  throw err
}

/** 单次尝试：端口冲突的判定与换号重试由 `startGateway` 负责。 */
async function spawnGatewayOnce(info, options = {}) {
  const { spawn: spawnImpl } = runtime()
  const port = Number(options.port)
  const env = buildGatewayEnv(info, { ...options, port })

  const child = spawnImpl(options.exe, [], {
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
 * 组装 python-engine 的环境变量。
 *
 * 与网关的差异（都是从两侧代码里读出来的，不是猜的）：
 *   - Redis 用 `REDIS_URL`（`redis://host:port`），网关用 `REDIS_ADDR`（`host:port`）；
 *   - 监听地址用 `HTTP_HOST` / `HTTP_PORT`（uvicorn），网关用 `PORT`；
 *   - 需要 `DEGRADED_MODE=true` 才能在单机启动：`verify_sandbox_backend()` 在
 *     没有 `SANDBOX_SERVICE_URL` 时会拒绝启动，降级模式是唯一放行路径（见阶段三实测）。
 *
 * 仍然校验 DSN 与 APP_SECRET：引擎同样拿它们连库、派生 INTERNAL_TOKEN，
 * 缺了会在引擎自己的日志深处报错，不如在这里拦住。
 */
function buildEngineEnv(info, options = {}) {
  if (!info || typeof info !== 'object') throw new Error('缺少引导器连接信息，无法启动引擎')

  const pgDsn = String(info.pgDsn || '').trim()
  if (!pgDsn) throw new Error('引导器未提供 PostgreSQL DSN，引擎无法启动')

  const appSecret = String(options.appSecret || '').trim()
  if (appSecret.length < APP_SECRET_MIN_LENGTH) {
    throw new Error(
      `APP_SECRET 至少需要 ${APP_SECRET_MIN_LENGTH} 个字符（引擎会拒绝启动），当前 ${appSecret.length} 个`,
    )
  }

  const port = Number(options.port)
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`引擎端口非法：${options.port}`)
  }

  const env = { ...process.env, ...(options.env || {}) }
  env.APP_SECRET = appSecret
  env.POSTGRES_DSN = pgDsn
  env.HTTP_HOST = '127.0.0.1'
  env.HTTP_PORT = String(port)
  // 单机模式：放行沙箱门禁，并让 Redis 从"必需"降为"可选"
  env.DEGRADED_MODE = 'true'

  const redisAddr = String(info.redisAddr || '').trim()
  if (redisAddr) {
    // 引导器给的是 host:port，引擎要的是 URI。已经带 scheme 时原样用，避免拼成
    // `redis://redis://...`。
    env.REDIS_URL = redisAddr.includes('://') ? redisAddr : `redis://${redisAddr}`
  }

  return env
}

/**
 * 起 python-engine 并等它开始监听。
 *
 * 引擎是**强依赖**（与 PG、Redis 同级）：起不来就没有可用的本地模式。这里仍按
 * `{ok:false, reason}` 返回而不是直接抛，是为了让 `startLocalStack` 能在回滚前
 * 把失败原因（含 stderr 尾部）完整带进错误消息里，恢复对话框才有东西可显示。
 *
 * 端口冲突会**换号重试**（见 `PORT_ATTEMPTS`），理由与 `startGateway` 相同。
 */
async function startEngine(info, options = {}) {
  const launch = options.engine
  if (!launch || !launch.command) {
    return { ok: false, reason: 'no-launch', message: '未找到 python-engine 启动信息' }
  }

  const attempts = options.retryPortConflict === false ? 1 : PORT_ATTEMPTS
  const tried = []
  let last = null

  for (let index = 0; index < attempts; index += 1) {
    const port = index === 0 ? Number(options.port) : await components.pickFreePort()
    const result = await spawnEngineOnce(info, { ...options, port })
    if (result.ok) return result

    tried.push({ attempt: index + 1, port, reason: result.reason, message: result.message })
    last = result
    // 只有端口冲突值得换号；其它失败（缺依赖、配置错）换多少个端口都一样
    if (!DEFAULTS.isPortConflict(`${result.stderrTail || ''}\n${result.message || ''}`)) break
    await stopEngine()
  }

  return { ...last, attempts: tried }
}

/** 单次尝试：换号重试与最终判定由 `startEngine` 负责。 */
async function spawnEngineOnce(info, options = {}) {
  const { spawn: spawnImpl } = runtime()
  const launch = options.engine
  const port = Number(options.port)
  const env = buildEngineEnv(info, { ...options, port })

  let child
  try {
    child = spawnImpl(launch.command, launch.args || ['-m', 'app.main'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: launch.cwd || process.cwd(),
      windowsHide: true,
      env,
    })
  } catch (err) {
    return { ok: false, reason: 'spawn-failed', message: (err && err.message) || 'spawn 出错' }
  }

  const record = {
    child,
    port,
    address: `127.0.0.1:${port}`,
    baseUrl: `http://127.0.0.1:${port}`,
    stderrTail: '',
    stdoutTail: '',
    exited: false,
    exitCode: null,
    startedAt: new Date().toISOString(),
  }
  state.engine = record

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

  const deadline = Date.now() + (options.timeoutMs || ENGINE_TIMEOUT_MS)
  while (Date.now() < deadline) {
    if (record.exited) {
      return {
        ok: false,
        reason: 'exited',
        message: `引擎启动后立即退出（退出码 ${record.exitCode}）`,
        stderrTail: record.stderrTail,
      }
    }
    if (await probeHttp(record.baseUrl, ENGINE_HEALTH_PATH)) {
      return { ok: true, address: record.address, port, baseUrl: record.baseUrl }
    }
    await sleep(options.intervalMs || 500)
  }

  return {
    ok: false,
    reason: 'timeout',
    message: `引擎在 ${Math.round((options.timeoutMs || ENGINE_TIMEOUT_MS) / 1000)}s 内未开始监听`,
    stderrTail: record.stderrTail,
  }
}

/** 停引擎。引擎是单进程（uvicorn 不分叉），SIGTERM 足够；超时再收树。 */
async function stopEngine() {
  const record = state.engine
  if (!record) return false
  state.engine = null
  if (!record.child || record.exited) return true
  const child = record.child
  try {
    child.kill('SIGTERM')
  } catch {
    /* 已消失 */
  }
  if (await waitExit(child, 8000)) return true
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
 * 一次性起本地整条链：引导器 → 引擎 → 网关。
 *
 * 失败时**回滚已启动的部分**：引导器起来了但引擎/网关失败，必须把已启动的进程
 * （连同 PG/Redis）一起停掉，否则下次启动会撞上残留 postmaster。
 *
 * 三者都是**强依赖**：PG、Redis、python-engine 缺任何一个都不算本地模式可用。
 * 引擎的失败原因会被完整带进抛出的错误里，恢复对话框才有东西可显示。
 */
async function startLocalStack(options = {}) {
  const dataDir = String(options.dataDir || '')

  // **接管已有实例**：同一用户、同一 dataDir 下若已有一套健康栈在跑，就直接复用它。
  // 这条路径覆盖 Electron 单实例锁管不到的情况：手动起的 CLI 引导器、上次被强杀后
  // 仍活着的网关。判活不只看状态文件——那个文件在进程被强杀时来不及删，所以还要
  // 实际打一次 /ready，确认 PG 与 Redis 都 up 才算数（见 runtime-state.cjs）。
  if (dataDir && options.reuseExisting !== false) {
    const existing = runtimeState.readRuntimeState(dataDir)
    const probe = await runtimeState.probeExistingStack(existing, { fetch: options.fetch })
    if (probe.alive) {
      // 记下来，但**不拥有**这些进程：stopLocalStack 不该去停别人拉起的栈
      state.attached = { dataDir, baseUrl: probe.baseUrl, port: probe.port }
      return {
        baseUrl: probe.baseUrl,
        port: probe.port,
        ready: { postgres: 'up', redis: 'up' },
        reused: true,
        launcher: existing.launcher || null,
        engine: existing.engine || { ok: true },
      }
    }
  }

  const launcherResult = await startLauncher(options)
  try {
    // 引擎先于网关：网关启动时会读 PYTHON_ENGINE_ADDRESS 建客户端，地址得在它起来前就定好。
    // 没有引擎启动信息时不必挑端口（挑端口会真的 listen 一次）。
    const enginePort = options.engine ? options.enginePort || (await components.pickFreePort()) : 0
    const engineResult = await startEngine(launcherResult.info, { ...options, port: enginePort })
    if (!engineResult.ok) {
      // 强依赖：没有引擎就没有可用的本地模式（聊天/知识库/记忆全在引擎侧）
      const err = new Error(
        `python-engine 未能启动（${engineResult.reason || 'unknown'}）：${engineResult.message || '原因未知'}`,
      )
      err.reason = engineResult.reason
      err.engineFailure = engineResult
      throw err
    }

    const gatewayResult = await startGateway(launcherResult.info, {
      ...options,
      pythonEngineAddress: engineResult.address,
    })

    // 记录本次运行占用的端口，供下一个实例接管（见 runtime-state.cjs）。
    // 不写 DSN：里面有密码，接管方只要有端口就能重建。
    if (dataDir) {
      runtimeState.writeRuntimeState(dataDir, {
        gatewayPort: gatewayResult.port,
        enginePort: engineResult.port,
        pgPort: launcherResult.info && launcherResult.info.pgPort,
        redisPort: launcherResult.info && launcherResult.info.redisPort,
        pgBackend: launcherResult.info && launcherResult.info.pgBackend,
        redisBackend: launcherResult.info && launcherResult.info.redisBackend,
        launcher: {
          pgPort: launcherResult.info && launcherResult.info.pgPort,
          redisPort: launcherResult.info && launcherResult.info.redisPort,
          pgBackend: launcherResult.info && launcherResult.info.pgBackend,
          redisBackend: launcherResult.info && launcherResult.info.redisBackend,
        },
        engine: { ok: true, address: engineResult.address, port: engineResult.port },
      })
    }
    state.ownedDataDir = dataDir

    return {
      baseUrl: gatewayResult.baseUrl,
      port: gatewayResult.port,
      ready: gatewayResult.ready,
      launcher: launcherResult.info,
      engine: { ok: true, address: engineResult.address, port: engineResult.port },
    }
  } catch (err) {
    // 回滚顺序与启动相反：先引擎，再引导器（连同 PG/Redis）
    await stopEngine()
    await stopLauncher()
    throw err
  }
}

async function stopLocalStack() {
  // 接管来的栈不属于本进程：停掉会让那个正在用它实例崩掉。
  // 这里只放松手，并把状态文件留着——还在跑的实例仍然需要它。
  if (state.attached) {
    state.attached = null
    return { gatewayStopped: false, engineStopped: false, launcherStopped: false, detached: true }
  }

  // 与启动相反：先网关，再引擎，最后数据栈
  const gatewayStopped = await stopGateway()
  const engineStopped = await stopEngine()
  const launcherStopped = await stopLauncher()
  // 自己拉起的栈已经停了，状态文件必须删掉：留着会让下一个实例去连一堆死端口，
  // 白白多等一轮探测超时。
  if (state.ownedDataDir) {
    runtimeState.clearRuntimeState(state.ownedDataDir)
    state.ownedDataDir = ''
  }
  return { gatewayStopped, engineStopped, launcherStopped }
}

function getStackState() {
  return {
    launcher: state.launcher
      ? { pid: state.launcher.child && state.launcher.child.pid, info: state.launcher.info }
      : null,
    engine: state.engine
      ? {
          pid: state.engine.child && state.engine.child.pid,
          port: state.engine.port,
          address: state.engine.address,
        }
      : null,
    engineFailure: state.engineFailure || null,
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
  ENGINE_HEALTH_PATH,
  LAUNCHER_TIMEOUT_MS,
  GATEWAY_TIMEOUT_MS,
  ENGINE_TIMEOUT_MS,
  APP_SECRET_MIN_LENGTH,
  parseLauncherLine,
  buildGatewayEnv,
  buildEngineEnv,
  startLauncher,
  stopLauncher,
  startEngine,
  stopEngine,
  startGateway,
  stopGateway,
  startLocalStack,
  stopLocalStack,
  getStackState,
  _setDeps,
  _resetDeps,
  _resetState,
}
