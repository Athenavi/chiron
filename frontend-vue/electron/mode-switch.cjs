/**
 * Chiron Desktop — 运行模式切换
 *
 * 契约（docs/desktop-local/contracts.md §1）：
 *   切模式只清「离开模式」的 cookie / localStorage / IndexedDB / CacheStorage /
 *   Service Worker / HTTP 缓存 / token；**默认不删** userData/local/**；
 *   切换后必须重启应用，避免两个后端并存。
 *
 * 顺序是硬要求，不能换：
 *   停后端 → 清离开模式的 partition → 清云 token →（可选）删本地数据
 *          → 写 mode → relaunch + exit
 * 先清后写：写盘一旦成功，重启就是既成事实；反过来若清理失败还没写盘，
 * 用户仍停在原模式，不会出现「模式已切但旧 cookie 还在」的串味状态。
 *
 * 每个清理步骤都幂等（重复执行无副作用），失败则回滚 mode 并抛可读错误。
 */
const {
  MODE_LABELS,
  clearMode,
  configPath,
  normalizeMode,
  readDeviceSettings,
  saveMode,
} = require('./config.cjs')
const { partitionFor } = require('./shared.cjs')

/** 离开模式要清的存储项（契约 §1 的枚举，键名用 Electron storages 的写法） */
const CLEAR_STORAGES = ['cookies', 'localstorage', 'indexdb', 'cachestorage', 'serviceworkers']

/** 可选依赖：纯 Node 单测里没有 electron / token-store，取不到就退化成「跳过 + 警告」 */
function optionalRequire(id) {
  try {
    return require(id)
  } catch {
    return null
  }
}

function electronApi() {
  const electron = optionalRequire('electron')
  // 纯 Node 环境下 require('electron') 返回二进制路径字符串，解构出 undefined，必须挡掉
  return electron && typeof electron === 'object' ? electron : {}
}

function tokenStore() {
  const store = optionalRequire('./token-store.cjs')
  return store && typeof store.clearToken === 'function' ? store : null
}

/**
 * 停掉当前后端。没有接线（开发/老版本）就跳过；一旦接线，失败必须中止切换 ——
 * 两个后端同时跑会抢端口和同一个数据库目录。
 */
async function stopBackend(options, prevMode, result) {
  if (typeof options.stopBackend !== 'function') return
  await options.stopBackend({ mode: prevMode })
  result.cleaned.push('backend')
}

/**
 * 清掉离开模式的 partition：存储、HTTP 缓存、HTTP 认证缓存。
 * 拿不到 session 时直接抛错中止 —— 带着旧 cookie 换模式等于跨模式串身份。
 */
async function clearPartition(api, mode, result, log) {
  const partition = partitionFor(mode)
  const { session } = api
  if (!session || typeof session.fromPartition !== 'function') {
    throw new Error('拿不到 Electron session，无法清理旧模式的登录状态')
  }
  const ses = session.fromPartition(partition)
  if (!ses || typeof ses.clearStorageData !== 'function') {
    throw new Error(`拿不到会话分区 ${partition}，无法清理旧模式的登录状态`)
  }
  await ses.clearStorageData({ storages: CLEAR_STORAGES })
  result.cleaned.push(`storage:${partition}`)

  // 下面两个是可选能力：老版本 Electron 没有就跳过，不能因此把切换卡死
  if (typeof ses.clearCache === 'function') {
    await ses.clearCache()
    result.cleaned.push(`http-cache:${partition}`)
  } else {
    result.warnings.push('当前 Electron 不支持 clearCache，HTTP 缓存未清理')
  }
  if (typeof ses.clearAuthCache === 'function') {
    await ses.clearAuthCache()
    result.cleaned.push(`auth-cache:${partition}`)
  } else {
    result.warnings.push('当前 Electron 不支持 clearAuthCache，HTTP 认证缓存未清理')
  }
  log('info', `cleared partition ${partition}`)
}

/**
 * 删云 token。失败只记警告不中止：safeStorage 不可用时 token 从来没落过盘，
 * 硬失败会让用户永远切不了模式。
 */
async function clearCloudToken(options, result, log) {
  const store = options.tokenStore || tokenStore()
  if (!store) {
    result.warnings.push('token-store 不可用，未清除云端 token')
    return
  }
  const ok = await store.clearToken()
  if (ok === false) {
    result.warnings.push('云端 token 清除失败（系统凭据库不可用），下次登录会重新写入')
    log('warn', 'clearToken returned false')
    return
  }
  result.cleaned.push('cloud-token')
}

/** 仅 deleteLocalData=true 时删 userData/local（契约 §1：默认不删用户数据） */
async function removeLocalData(api, options, result, log) {
  const fsMod = options.fs || require('node:fs')
  const pathMod = options.path || require('node:path')
  const app = api.app
  const userData = app && typeof app.getPath === 'function' ? app.getPath('userData') : ''
  if (!userData) {
    result.warnings.push('拿不到 userData 目录，本地数据未删除')
    return
  }
  const target = pathMod.join(userData, 'local')
  // force: true ⇒ 目录不存在也不报错，重复调用幂等
  await fsMod.promises.rm(target, { recursive: true, force: true })
  result.deletedLocalData = true
  result.cleaned.push(`data:${target}`)
  log('warn', `deleted local data: ${target}`)
}

/** 重启：relaunch 之后把 exit 排到下一个 tick，让 IPC 回复与写盘先落定 */
function restartApp(api, result, log, schedule) {
  const app = api.app
  if (!app || typeof app.relaunch !== 'function') {
    result.warnings.push('拿不到 Electron app，模式已保存，需要手动重启才能生效')
    return
  }
  app.relaunch()
  result.restarted = true
  schedule(() => {
    try {
      if (typeof app.exit === 'function') app.exit(0)
      else if (typeof app.quit === 'function') app.quit()
    } catch (err) {
      log('error', `exit after mode switch failed: ${err && err.message}`)
    }
  })
}

/** 回滚：恢复切换前的模式选择（从未选过模式的老配置则重新删掉 mode） */
function rollback(previous) {
  try {
    if (previous.modeChosen) saveMode(previous.mode)
    else clearMode()
    return true
  } catch {
    return false
  }
}

/**
 * 切换运行模式。
 *
 * @param {object} options
 * @param {'local'|'cloud'} options.nextMode  目标模式（必填）
 * @param {boolean} [options.deleteLocalData] 是否连 userData/local/** 一起删（默认否）
 * @param {Function} [options.stopBackend]  停止当前后端的回调（可注入，便于测试）
 * @param {object} [options.electron]       注入的 electron 模块（测试用 mock）
 * @param {object} [options.tokenStore]     注入的 token-store
 * @param {Function} [options.log]          (level, message) => void
 * @param {boolean} [options.restart]       是否真的 relaunch + exit（测试可关）
 * @param {Function} [options.scheduleExit] 自定义 exit 调度（测试可同步执行）
 * @returns {Promise<{from:string,to:string,cleaned:string[],warnings:string[],skipped:boolean}>}
 */
async function switchMode(options = {}) {
  const nextMode = normalizeMode(options.nextMode)
  const deleteLocalData = Boolean(options.deleteLocalData)
  const restartAfter = options.restart !== false
  const api = options.electron || electronApi()
  const log = typeof options.log === 'function' ? options.log : () => {}
  const schedule =
    typeof options.scheduleExit === 'function' ? options.scheduleExit : fn => setImmediate(fn)

  const settings = readDeviceSettings()
  const prevMode = settings.mode
  const result = {
    from: prevMode,
    to: nextMode,
    cleaned: [],
    warnings: [],
    deletedLocalData: false,
    restarted: false,
    skipped: false,
  }

  if (prevMode === nextMode) {
    // 幂等：模式没变就不重启，否则用户连点两次会把正在用的进程杀掉
    result.skipped = true
    log('warn', `switchMode skipped: already in ${nextMode}`)
    return result
  }

  try {
    await stopBackend(options, prevMode, result)
    await clearPartition(api, prevMode, result, log)
    await clearCloudToken(options, result, log)
    if (deleteLocalData) await removeLocalData(api, options, result, log)
    saveMode(nextMode)
  } catch (err) {
    const rolledBack = rollback({ mode: prevMode, modeChosen: settings.modeChosen })
    const detail = (err && err.message) || String(err)
    const hint = rolledBack
      ? `仍停留在${MODE_LABELS[prevMode] || prevMode}`
      : `模式配置可能已损坏，请检查 ${safeConfigPath()}`
    const error = new Error(`切换运行模式失败：${detail}（${hint}）`)
    error.cause = err
    error.mode = prevMode
    error.rolledBack = rolledBack
    log('error', `switchMode failed: ${detail}`)
    throw error
  }

  if (restartAfter) restartApp(api, result, log, schedule)
  return result
}

function safeConfigPath() {
  try {
    return configPath()
  } catch {
    return 'config.json'
  }
}

module.exports = { switchMode, clearPartition, CLEAR_STORAGES, partitionFor }
