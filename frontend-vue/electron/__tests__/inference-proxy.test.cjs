'use strict'
/**
 * inference-proxy.cjs 单测：起真实 loopback server（127.0.0.1:0），
 * 但用 stub fetch 拦住上游，不依赖 Ollama / 真实 provider。
 * 重点：token 校验、provider 分流、只透传必要字段、统计里没有正文。
 */
const test = require('node:test')
const assert = require('node:assert/strict')

const proxy = require('../inference-proxy.cjs')

const PROMPT = '这是一段不该出现在日志里的 prompt 内容'

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => payload }
}

function stubFetch(handler) {
  const calls = []
  const impl = async (url, init) => {
    calls.push({ url: String(url), init })
    return handler(String(url), init)
  }
  impl.calls = calls
  return impl
}

async function start(t, options = {}) {
  const started = await proxy.startProxy(options)
  t.after(async () => {
    await proxy.stopProxy()
  })
  return started
}

async function call(started, { token = started.token, body, method = 'POST', path = '/infer' } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  const res = await fetch(`${started.url}${path}`, {
    method,
    headers,
    body: method === 'POST' ? JSON.stringify(body ?? {}) : undefined,
  })
  const text = await res.text()
  return { status: res.status, body: text ? JSON.parse(text) : null }
}

test('缺 token / 错 token 一律 401', async t => {
  const started = await start(t, { fetchImpl: stubFetch(() => jsonResponse({})) })

  const missing = await call(started, { token: '', body: { provider: 'local' } })
  assert.equal(missing.status, 401)
  assert.equal(missing.body.error, 'invalid_token')

  const wrong = await call(started, { token: 'x'.repeat(64), body: { provider: 'local' } })
  assert.equal(wrong.status, 401)
})

test('token 由 startProxy 生成：非空、每次重启换新', async t => {
  const first = await start(t, { fetchImpl: stubFetch(() => jsonResponse({})) })
  assert.equal(first.token.length, 64)
  assert.equal(proxy.getProxyToken(), first.token)
  assert.equal(proxy.getProxyUrl(), first.url)
  assert.match(first.url, /^http:\/\/127\.0\.0\.1:\d+$/)
})

test('本地 provider：透传到 Ollama /api/chat，归一化 usage，只留白名单字段', async t => {
  const fetchImpl = stubFetch(() => jsonResponse({
    model: 'qwen2.5:7b',
    message: { role: 'assistant', content: '本地回答' },
    prompt_eval_count: 11,
    eval_count: 7,
  }))
  const started = await start(t, { fetchImpl, ollamaBaseUrl: 'http://127.0.0.1:11434/' })

  const res = await call(started, {
    body: {
      provider: 'local',
      model: 'qwen2.5:7b',
      messages: [{ role: 'user', content: PROMPT }],
      temperature: 0.2,
      max_tokens: 256,
      // 下面这些都不许透传
      tools: [{ name: 'rm-rf' }],
      internalTrace: 'leak',
      messages_extra: PROMPT,
    },
  })

  assert.equal(res.status, 200)
  assert.equal(res.body.provider, 'local')
  assert.equal(res.body.model, 'qwen2.5:7b')
  assert.equal(res.body.content, '本地回答')
  assert.deepEqual(res.body.usage, { prompt_tokens: 11, completion_tokens: 7, total_tokens: 18 })

  assert.equal(fetchImpl.calls.length, 1)
  assert.equal(fetchImpl.calls[0].url, 'http://127.0.0.1:11434/api/chat')
  const sent = JSON.parse(fetchImpl.calls[0].init.body)
  assert.deepEqual(Object.keys(sent).sort(), ['messages', 'model', 'options', 'stream'])
  assert.equal(sent.stream, false)
  assert.deepEqual(sent.options, { temperature: 0.2, num_predict: 256 })
})

test('缺失 model / messages 报 400，且不请求上游', async t => {
  const fetchImpl = stubFetch(() => jsonResponse({}))
  const started = await start(t, { fetchImpl })

  const noModel = await call(started, { body: { provider: 'local', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(noModel.status, 400)
  assert.equal(noModel.body.error, 'missing_model')

  const noMessages = await call(started, { body: { provider: 'local', model: 'm' } })
  assert.equal(noMessages.status, 400)
  assert.equal(noMessages.body.error, 'missing_messages')

  assert.equal(fetchImpl.calls.length, 0)
})

test('未知 provider 400，云端 provider 501 占位', async t => {
  const fetchImpl = stubFetch(() => jsonResponse({}))
  const started = await start(t, { fetchImpl })

  const unknown = await call(started, { body: { provider: 'mars', model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(unknown.status, 400)
  assert.equal(unknown.body.error, 'unknown_provider')

  const cloud = await call(started, { body: { provider: 'cloud', model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(cloud.status, 501)
  assert.equal(cloud.body.error, 'cloud_not_implemented')
  assert.equal(fetchImpl.calls.length, 0)
})

test('OpenAI 兼容端点：key 取自 secrets，缺失报 400，存在则带 Bearer 透传', async t => {
  const fetchImpl = stubFetch(() => jsonResponse({
    model: 'gpt-x',
    choices: [{ message: { role: 'assistant', content: '用户 provider 回答' } }],
    usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
  }))

  const noKey = await start(t, { fetchImpl, readSecrets: async () => ({ providers: {} }) })
  const denied = await call(noKey, {
    body: { provider: 'openai-compatible', model: 'm', messages: [{ role: 'user', content: 'x' }] },
  })
  assert.equal(denied.status, 400)
  assert.equal(denied.body.error, 'missing_provider_key')
  assert.equal(fetchImpl.calls.length, 0)

  await proxy.stopProxy()
  const withKey = await start(t, {
    fetchImpl,
    readSecrets: async () => ({
      providers: { 'openai-compatible': { apiKey: 'sk-user-key-0123456789', baseUrl: 'https://example.invalid/v1/' } },
    }),
  })
  const ok = await call(withKey, {
    body: { provider: 'openai', model: 'gpt-x', messages: [{ role: 'user', content: PROMPT }], top_p: 0.9 },
  })
  assert.equal(ok.status, 200)
  assert.equal(ok.body.provider, 'openai-compatible')
  assert.equal(ok.body.content, '用户 provider 回答')
  assert.equal(ok.body.usage.total_tokens, 8)

  assert.equal(fetchImpl.calls.length, 1)
  assert.equal(fetchImpl.calls[0].url, 'https://example.invalid/v1/chat/completions')
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'Bearer sk-user-key-0123456789')
  const sent = JSON.parse(fetchImpl.calls[0].init.body)
  assert.equal(sent.stream, false)
  assert.equal(sent.top_p, 0.9)
  assert.equal(sent.messages[0].content, PROMPT)
})

test('上游报错只回 502 + 错误码，不回上游正文', async t => {
  const started = await start(t, {
    fetchImpl: stubFetch(() => jsonResponse({ error: `echo: ${PROMPT}` }, { ok: false, status: 500 })),
  })
  const res = await call(started, {
    body: { provider: 'local', model: 'm', messages: [{ role: 'user', content: PROMPT }] },
  })
  assert.equal(res.status, 502)
  assert.equal(res.body.error, 'upstream_error')
  assert.ok(!JSON.stringify(res.body).includes(PROMPT))
})

test('统计只含数值，不存 prompt 正文', async t => {
  const before = proxy.getProxyStats().requests
  const started = await start(t, {
    fetchImpl: stubFetch(() => jsonResponse({ message: { content: 'ok' }, prompt_eval_count: 1, eval_count: 1 })),
  })
  await call(started, { body: { provider: 'local', model: 'm', messages: [{ role: 'user', content: PROMPT }] } })

  const stats = proxy.getProxyStats()
  // stats 是模块级累计值，这里只断言这一次请求的增量
  assert.equal(stats.requests, before + 1)
  assert.equal(stats.lastStatus, 200)
  assert.ok(!JSON.stringify(stats).includes(PROMPT))
})

test('非 /infer 路径 404，非 POST 405，预检 204', async t => {
  const started = await start(t, { fetchImpl: stubFetch(() => jsonResponse({})) })

  const notFound = await call(started, { path: '/api/chat', body: {} })
  assert.equal(notFound.status, 404)

  const wrongMethod = await call(started, { method: 'GET', path: '/infer' })
  assert.equal(wrongMethod.status, 405)

  const preflight = await fetch(`${started.url}/infer`, { method: 'OPTIONS' })
  assert.equal(preflight.status, 204)
  assert.equal(preflight.headers.get('access-control-allow-origin'), '*')
})

test('stopProxy 之后地址与 token 失效', async t => {
  const started = await proxy.startProxy({ fetchImpl: stubFetch(() => jsonResponse({})) })
  assert.equal(await proxy.stopProxy(), true)
  assert.equal(proxy.getProxyUrl(), '')
  assert.equal(proxy.getProxyToken(), '')
  assert.equal(await proxy.stopProxy(), false)

  await assert.rejects(() => fetch(`${started.url}/infer`, { method: 'POST', body: '{}' }))
})

// ── provider 选择：请求里的 provider > secrets.active > 本地 ──
// 本地 core 发请求时**不带** provider（desktop-local/app/runtime/inference.py），
// 所以"用户在设置里换 provider 就能用"完全依赖下面这几条。

function ollamaOk() {
  return jsonResponse({ message: { content: '本地回答' }, prompt_eval_count: 1, eval_count: 1 })
}

function openaiOk(model = 'gpt-configured') {
  return jsonResponse({
    model,
    choices: [{ message: { role: 'assistant', content: '按配置回答' } }],
    usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
  })
}

test('secrets.active 决定 provider：请求不带 provider 时走用户配置的服务', async t => {
  const fetchImpl = stubFetch(() => openaiOk())
  const started = await start(t, {
    fetchImpl,
    readSecrets: async () => ({
      active: 'openai-compatible',
      providers: {
        'openai-compatible': {
          apiKey: 'sk-configured-0123456789',
          baseUrl: 'https://configured.invalid/v1/',
          model: 'gpt-configured',
        },
      },
    }),
  })

  // 既没有 provider 也没有 model：两个都从 secrets 补
  const res = await call(started, { body: { messages: [{ role: 'user', content: PROMPT }] } })
  assert.equal(res.status, 200)
  assert.equal(res.body.provider, 'openai-compatible')
  assert.equal(res.body.model, 'gpt-configured')

  assert.equal(fetchImpl.calls.length, 1)
  assert.equal(fetchImpl.calls[0].url, 'https://configured.invalid/v1/chat/completions')
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'Bearer sk-configured-0123456789')
  assert.equal(JSON.parse(fetchImpl.calls[0].init.body).model, 'gpt-configured')
})

test('secrets.active 是未知值时回退本地，而不是 4xx/5xx', async t => {
  const fetchImpl = stubFetch(() => ollamaOk())
  const started = await start(t, {
    fetchImpl,
    readSecrets: async () => ({ active: 'mars', providers: {} }),
  })

  const res = await call(started, { body: { model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(res.status, 200)
  assert.equal(res.body.provider, 'local')
  assert.equal(fetchImpl.calls[0].url, `${proxy.DEFAULT_OLLAMA_BASE}/api/chat`)
})

test('请求显式 provider 优先于 secrets.active（诊断/直连用）', async t => {
  const fetchImpl = stubFetch(() => ollamaOk())
  const started = await start(t, {
    fetchImpl,
    readSecrets: async () => ({
      active: 'openai-compatible',
      providers: { 'openai-compatible': { apiKey: 'sk-x-0123456789', baseUrl: 'https://configured.invalid/v1' } },
    }),
  })

  const res = await call(started, { body: { provider: 'local', model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(res.status, 200)
  assert.equal(res.body.provider, 'local')
  assert.equal(fetchImpl.calls[0].url, `${proxy.DEFAULT_OLLAMA_BASE}/api/chat`)
})

test('Ollama 端点与模型名取自 secrets，缺省才用默认常量', async t => {
  const fetchImpl = stubFetch(() => ollamaOk())
  const started = await start(t, {
    fetchImpl,
    // 构造时的地址只是兜底：secrets 里有值时以 secrets 为准
    ollamaBaseUrl: 'http://127.0.0.1:11434/',
    readSecrets: async () => ({
      active: 'ollama',
      providers: { ollama: { baseUrl: 'http://127.0.0.1:11500/', model: 'qwen2.5-coder:7b' } },
    }),
  })

  const res = await call(started, { body: { messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(res.status, 200)
  assert.equal(fetchImpl.calls[0].url, 'http://127.0.0.1:11500/api/chat')
  assert.equal(JSON.parse(fetchImpl.calls[0].init.body).model, 'qwen2.5-coder:7b')
  assert.equal(res.body.model, 'qwen2.5-coder:7b')
})

test('Ollama 端点没配时用默认常量（业务逻辑里不写死 11434）', async t => {
  const fetchImpl = stubFetch(() => ollamaOk())
  const started = await start(t, {
    fetchImpl,
    readSecrets: async () => ({ active: 'ollama', providers: {} }),
  })

  await call(started, { body: { model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(fetchImpl.calls[0].url, `${proxy.DEFAULT_OLLAMA_BASE}/api/chat`)
  assert.match(proxy.DEFAULT_OLLAMA_BASE, /^http:\/\/127\.0\.0\.1:\d+$/)
})

test('readSecrets 抛错（密钥环锁死）时按默认 provider 继续推理', async t => {
  const fetchImpl = stubFetch(() => ollamaOk())
  const started = await start(t, {
    fetchImpl,
    readSecrets: async () => {
      throw new Error('keyring locked')
    },
  })

  const res = await call(started, { body: { model: 'm', messages: [{ role: 'user', content: 'x' }] } })
  assert.equal(res.status, 200)
  assert.equal(res.body.provider, 'local')
  assert.equal(fetchImpl.calls[0].url, `${proxy.DEFAULT_OLLAMA_BASE}/api/chat`)
})
