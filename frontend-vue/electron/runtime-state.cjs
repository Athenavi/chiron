'use strict'
/**
 * 本地栈的运行时状态文件（runtime state）与"接管已有实例"的探测。
 *
 * **解决什么问题**：同一台机器同一 Windows 用户下只允许有一套 PG / Redis / 引擎。
 * Electron 的 `requestSingleInstanceLock()` 只能保证"同一 userData 目录只有一个
 * 主进程"——它对"你手动起的 CLI 引导器""上次被强杀后残留的网关"都无能为力。所以
 * 真实的裁判得是两样东西：
 *
 *   1. 状态文件（本模块）：记录本次运行在哪些端口上、PID 是多少、版本是什么；
 *   2. 实际探测：拿状态里的端口去连一次，连得上才叫"活着"。
 *
 * 只信状态文件是不够的——进程被强杀时文件来不及删，端口会被别的程序复用。所以
 * 判定"可接管"必须同时满足：文件存在 + 版本匹配 + 端口上**确实是我们的服务**。
 *
 * **为什么不存 DSN**：DSN 里有密码（embedded PG 固定 postgres + trust 认证）。接管方
 * 只需要端口就能重建出同一条 DSN，没必要在磁盘上多留一份带密码的副本。
 */

const fs = require('node:fs')
const path = require('node:path')

/** 状态文件格式版本。字段变了就加一，读到旧版本一律当作"不可接管"。 */
const STATE_SCHEMA = 1

/** 状态文件名（落在 dataDir 之下，与其他运行时数据同级）。 */
const STATE_FILENAME = 'runtime-state.json'

function statePathFor(dataDir) {
  return path.join(String(dataDir || ''), STATE_FILENAME)
}

/**
 * 读状态文件。任何异常都当作"没有状态"——损坏的 JSON、被别的程序写坏的文件
 * 都不该让本地模式起不来，最坏情况只是退化成"重新拉起一套"。
 */
function readRuntimeState(dataDir) {
  if (!dataDir) return null
  try {
    const raw = fs.readFileSync(statePathFor(dataDir), 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    if (parsed.schema !== STATE_SCHEMA) return null
    return parsed
  } catch {
    return null
  }
}

/**
 * 原子写状态文件：先写临时文件再 rename。
 *
 * 直接 `writeFileSync` 的问题是留下半个文件——如果进程恰好在写入过程中被杀，
 * 下一次启动读到的就是坏 JSON。rename 在同一分区上是原子的，读到的要么是旧的、
 * 要么是新的完整内容。
 */
function writeRuntimeState(dataDir, payload) {
  if (!dataDir) return null
  const target = statePathFor(dataDir)
  const tmp = `${target}.${process.pid}.tmp`
  const body = {
    schema: STATE_SCHEMA,
    pid: process.pid,
    startedAt: new Date().toISOString(),
    ...payload,
  }
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(tmp, JSON.stringify(body, null, 2), { encoding: 'utf8', mode: 0o600 })
    fs.renameSync(tmp, target)
    return body
  } catch {
    // 写不了状态文件不是致命错误：它只影响"下次能不能接管"，不影响这次能不能跑
    try {
      fs.rmSync(tmp, { force: true })
    } catch {
      /* 临时文件可能根本没建出来 */
    }
    return null
  }
}

/** 删状态文件。只在"自己正常退出"时调用；被强杀就留着，由探测判定它已失效。 */
function clearRuntimeState(dataDir) {
  if (!dataDir) return false
  try {
    fs.rmSync(statePathFor(dataDir), { force: true })
    return true
  } catch {
    return false
  }
}

/**
 * 判断状态文件描述的那套栈是否还活着。
 *
 * 只做一件事：对每个已知端口发一次 HTTP 探测，全中才算"活着"。不做 PID 判活——
 * Windows 上 PID 会被复用，而且"进程在"不等于"服务在"。端口应答是更硬的证据。
 *
 * 超时给得很短（默认 800ms）：这只是启动前的一次快速判断，卡住比判错更糟。
 */
async function probeExistingStack(runtimeState, options = {}) {
  if (!runtimeState) return { alive: false, reason: 'no-state' }
  const fetchImpl = options.fetch || globalThis.fetch
  if (typeof fetchImpl !== 'function') return { alive: false, reason: 'no-fetch' }

  const timeoutMs = Number(options.timeoutMs) || 800
  const gatewayPort = Number(runtimeState.gatewayPort) || 0
  if (!gatewayPort) return { alive: false, reason: 'no-gateway-port' }

  const controller = typeof AbortController === 'function' ? new AbortController() : null
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null
  try {
    const res = await fetchImpl(`http://127.0.0.1:${gatewayPort}/ready`, {
      signal: controller ? controller.signal : undefined,
    })
    if (!res || !res.ok) return { alive: false, reason: 'gateway-unhealthy' }
    const body = typeof res.json === 'function' ? await res.json() : null
    const data = (body && body.data) || {}
    // 端口上有东西在应答 ≠ 那是我们的栈：必须看到 PG 与 Redis 都 up
    if (data.postgres !== 'up' || data.redis !== 'up') {
      return { alive: false, reason: 'dependencies-down' }
    }
    return { alive: true, baseUrl: `http://127.0.0.1:${gatewayPort}`, port: gatewayPort }
  } catch (err) {
    return { alive: false, reason: 'unreachable', message: (err && err.message) || String(err) }
  } finally {
    if (timer) clearTimeout(timer)
  }
}

module.exports = {
  STATE_SCHEMA,
  STATE_FILENAME,
  statePathFor,
  readRuntimeState,
  writeRuntimeState,
  clearRuntimeState,
  probeExistingStack,
}
