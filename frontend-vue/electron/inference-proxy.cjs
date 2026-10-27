/**
 * loopback 推理代理。
 *
 * 渲染进程不直接碰 provider key，也不直接访问 11434 或任意 baseUrl：
 * 它只连 `127.0.0.1:<随机端口>`，带一次性 token 调 `POST /infer`，
 * 由主进程决定走本地 Ollama、用户自配的 OpenAI 兼容端点，还是云端（501 占位）。
 *
 * 硬约束（contracts.md §8）：
 *   - 只绑 127.0.0.1，随机端口；token 每次 startProxy 重新生成，stop 后即失效，
 *     不写 config.json、不落盘。
 *   - 只透传必要字段（model / messages / 少量采样参数），其它字段一律丢弃。
 *   - 不记录 messages / content：日志侧只留计数、状态码、耗时。
 *   - 云端片段推理第二批再做，现在明确回 501，不要假装成功。
 */
const http = require('node:http')
const crypto = require('node:crypto')

/** 本地 Ollama 的默认地址（用户可用 CHIRON_OLLAMA_URL 覆盖） */
const DEFAULT_OLLAMA_BASE = 'http://127.0.0.1:11434'
const MAX_BODY_BYTES = 1024 * 1024
const UPSTREAM_TIMEOUT_MS = 120000

/** 允许透传给上游的采样参数白名单 */
const ALLOWED_OPTIONS = Object.freeze(['temperature', 'max_tokens', 'top_p', 'stop'])

const PROVIDER_LOCAL = 'local'
const PROVIDER_OPENAI_COMPATIBLE = 'openai-compatible'
const PROVIDER_CLOUD = 'cloud'

let current = null

/** 统计只含数值，供主进程诊断；永远不含请求/响应正文 */
const stats = {
  requests: 0,
  rejected: 0,
  upstreamErrors: 0,
  lastStatus: 0,
  lastDurationMs: 0,
}

function getProxyUrl() {
  return current ? current.url : ''
}

function getProxyToken() {
  return current ? current.token : ''
}

function isProxyRunning() {
  return Boolean(current)
}

function getProxyStats() {
  return { ...stats, requests: stats.requests, running: Boolean(current) }
}

function sendJson(res, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  })
  res.end(body)
}

/** 预检请求不带 token，必须在鉴权之前处理，否则浏览器侧 POST 一律失败 */
function applyCors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*')
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS')
  res.setHeader('Access-Control-Allow-Headers', 'authorization, content-type, x-chiron-proxy-token')
  res.setHeader('Access-Control-Max-Age', '600')
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let size = 0
    req.on('data', chunk => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        const err = new Error('request_too_large')
        err.status = 413
        reject(err)
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (!size) return resolve({})
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch {
        const err = new Error('invalid_json')
        err.status = 400
        reject(err)
      }
    })
    req.on('error', reject)
  })
}

function isAuthorized(req, token) {
  const header = String(req.headers.authorization || '')
  const bearer = header.toLowerCase().startsWith('bearer ') ? header.slice(7).trim() : ''
  const provided = bearer || String(req.headers['x-chiron-proxy-token'] || '').trim()
  if (!provided || !token) return false
  const a = Buffer.from(provided, 'utf8')
  const b = Buffer.from(token, 'utf8')
  if (a.length !== b.length) return false
  return crypto.timingSafeEqual(a, b)
}

function normalizeProvider(raw) {
  const value = String(raw || PROVIDER_LOCAL).toLowerCase()
  if (value === 'local' || value === 'ollama' || value === 'localhost') return PROVIDER_LOCAL
  if (value === 'openai' || value === 'openai-compatible' || value === 'custom' || value === 'user') {
    return PROVIDER_OPENAI_COMPATIBLE
  }
  if (value === 'cloud' || value === 'remote') return PROVIDER_CLOUD
  return ''
}

/** 只留 role + content 的纯文本消息；其余形状（工具调用、图片块）首批不支持 */
function normalizeMessages(raw) {
  if (!Array.isArray(raw)) return []
  const out = []
  for (const message of raw) {
    if (!message || typeof message !== 'object') continue
    const role = ['system', 'user', 'assistant'].includes(message.role) ? message.role : 'user'
    const content = typeof message.content === 'string' ? message.content : ''
    if (!content) continue
    out.push({ role, content })
  }
  return out
}

function pickOptions(input) {
  const source = input && typeof input.options === 'object' && input.options ? input.options : {}
  const picked = {}
  for (const key of ALLOWED_OPTIONS) {
    const value = input && input[key] !== undefined ? input[key] : source[key]
    if (value === undefined || value === null) continue
    picked[key] = value
  }
  return picked
}

function buildRequest(input) {
  const model = typeof input.model === 'string' ? input.model.trim() : ''
  if (!model) {
    const err = new Error('missing_model')
    err.status = 400
    throw err
  }
  const messages = normalizeMessages(input.messages)
  if (!messages.length) {
    const err = new Error('missing_messages')
    err.status = 400
    throw err
  }
  return { model, messages, options: pickOptions(input) }
}

async function readProviderSecrets(readSecrets) {
  try {
    const secrets = await readSecrets()
    const providers = secrets && typeof secrets === 'object' ? secrets.providers : null
    if (!providers || typeof providers !== 'object') return {}
    return providers[PROVIDER_OPENAI_COMPATIBLE] || providers.openai || {}
  } catch {
    return {}
  }
}

function httpError(status, message) {
  const err = new Error(message)
  err.status = status
  return err
}

async function callOllama(deps, request) {
  const body = { model: request.model, messages: request.messages, stream: false }
  const options = {}
  if (request.options.temperature !== undefined) options.temperature = request.options.temperature
  if (request.options.top_p !== undefined) options.top_p = request.options.top_p
  if (request.options.max_tokens !== undefined) options.num_predict = request.options.max_tokens
  if (request.options.stop !== undefined) options.stop = request.options.stop
  if (Object.keys(options).length) body.options = options

  const res = await deps.fetchImpl(`${deps.ollamaBaseUrl.replace(/\/+$/, '')}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
  })
  if (!res.ok) throw httpError(502, 'upstream_error')
  const payload = await res.json()
  const content = payload && payload.message && typeof payload.message.content === 'string'
    ? payload.message.content
    : ''
  const promptTokens = Number(payload && payload.prompt_eval_count) || 0
  const completionTokens = Number(payload && payload.eval_count) || 0
  return {
    content,
    usage: {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: promptTokens + completionTokens,
    },
    provider: PROVIDER_LOCAL,
    model: (payload && typeof payload.model === 'string' && payload.model) || request.model,
  }
}

async function callOpenAICompatible(deps, request) {
  const settings = await readProviderSecrets(deps.readSecrets)
  const apiKey = settings && typeof settings.apiKey === 'string' ? settings.apiKey : ''
  const baseUrl = settings && typeof settings.baseUrl === 'string' ? settings.baseUrl : ''
  if (!apiKey) throw httpError(400, 'missing_provider_key')
  if (!baseUrl) throw httpError(400, 'missing_provider_base_url')

  const body = { model: request.model, messages: request.messages, stream: false, ...request.options }
  const res = await deps.fetchImpl(`${baseUrl.replace(/\/+$/, '')}/chat/completions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
  })
  if (!res.ok) throw httpError(502, 'upstream_error')
  const payload = await res.json()
  const choice = payload && Array.isArray(payload.choices) ? payload.choices[0] : null
  const content = choice && choice.message && typeof choice.message.content === 'string'
    ? choice.message.content
    : ''
  const usage = payload && payload.usage && typeof payload.usage === 'object'
    ? {
      prompt_tokens: Number(payload.usage.prompt_tokens) || 0,
      completion_tokens: Number(payload.usage.completion_tokens) || 0,
      total_tokens: Number(payload.usage.total_tokens) || 0,
    }
    : { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
  return {
    content,
    usage,
    provider: PROVIDER_OPENAI_COMPATIBLE,
    model: (payload && typeof payload.model === 'string' && payload.model) || request.model,
  }
}

async function handleInfer(req, res, deps) {
  const startedAt = Date.now()
  stats.requests += 1
  let input
  try {
    input = await readBody(req)
  } catch (err) {
    stats.rejected += 1
    return sendJson(res, err.status || 400, { error: err.message })
  }
  try {
    const provider = normalizeProvider(input.provider)
    if (!provider) throw httpError(400, 'unknown_provider')
    if (provider === PROVIDER_CLOUD) throw httpError(501, 'cloud_not_implemented')

    const request = buildRequest(input)
    const result = provider === PROVIDER_LOCAL
      ? await callOllama(deps, request)
      : await callOpenAICompatible(deps, request)

    stats.lastStatus = 200
    stats.lastDurationMs = Date.now() - startedAt
    return sendJson(res, 200, result)
  } catch (err) {
    const status = err && err.status ? err.status : 502
    if (status === 502) stats.upstreamErrors += 1
    else stats.rejected += 1
    stats.lastStatus = status
    stats.lastDurationMs = Date.now() - startedAt
    // 只回错误码，不回上游正文：上游可能把 prompt 回显在错误信息里
    return sendJson(res, status, { error: err && err.message ? err.message : 'infer_failed' })
  }
}

function createHandler(token, deps) {
  return (req, res) => {
    applyCors(res)
    const pathname = (() => {
      try {
        return new URL(req.url || '/', 'http://127.0.0.1').pathname
      } catch {
        return ''
      }
    })()

    if (req.method === 'OPTIONS') {
      res.writeHead(204)
      res.end()
      return
    }
    if (!isAuthorized(req, token)) {
      stats.rejected += 1
      sendJson(res, 401, { error: 'invalid_token' })
      return
    }
    if (pathname !== '/infer') {
      sendJson(res, 404, { error: 'not_found' })
      return
    }
    if (req.method !== 'POST') {
      sendJson(res, 405, { error: 'method_not_allowed' })
      return
    }
    handleInfer(req, res, deps).catch(() => {
      if (!res.headersSent) sendJson(res, 500, { error: 'internal_error' })
      else res.end()
    })
  }
}

/**
 * 起代理（幂等：已在跑就返回当前地址与 token）。
 * @param {{ port?: number, readSecrets?: Function, fetchImpl?: Function, ollamaBaseUrl?: string }} options
 * @returns {Promise<{ url: string, token: string, port: number }>}
 */
async function startProxy(options = {}) {
  if (current) return { url: current.url, token: current.token, port: current.port }

  const deps = {
    // 需要时才 require，避免纯 Node 单测把 electron 拉进来
    readSecrets: options.readSecrets || (() => require('./secrets.cjs').readSecrets()),
    fetchImpl: options.fetchImpl || globalThis.fetch,
    ollamaBaseUrl: options.ollamaBaseUrl || process.env.CHIRON_OLLAMA_URL || DEFAULT_OLLAMA_BASE,
  }
  const token = crypto.randomBytes(32).toString('hex')
  const server = http.createServer(createHandler(token, deps))

  await new Promise((resolve, reject) => {
    const onError = err => reject(err)
    server.once('error', onError)
    server.listen(options.port || 0, '127.0.0.1', () => {
      server.removeListener('error', onError)
      resolve()
    })
  })

  const { port } = server.address()
  current = { server, port, url: `http://127.0.0.1:${port}`, token, deps }
  return { url: current.url, token, port }
}

async function stopProxy() {
  const snapshot = current
  if (!snapshot) return false
  current = null
  await new Promise(resolve => {
    snapshot.server.close(() => resolve())
    if (typeof snapshot.server.closeAllConnections === 'function') snapshot.server.closeAllConnections()
  })
  return true
}

module.exports = {
  DEFAULT_OLLAMA_BASE,
  MAX_BODY_BYTES,
  startProxy,
  stopProxy,
  getProxyUrl,
  getProxyToken,
  getProxyStats,
  isProxyRunning,
}
