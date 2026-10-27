'use strict'
/**
 * secrets.cjs 纯 Node 单测：mock safeStorage + app.getPath，不起 Electron。
 * 重点：不可用时必须降级为 false / 空对象，且任何情况下都不落明文。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const secrets = require('../secrets.cjs')

const LONG_KEY = 'sk-test-abcdefghijklmnop'

function makeUserData(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-secrets-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

function bind(t, { userData, safeStorage }) {
  secrets._setDeps({ app: { getPath: () => userData }, safeStorage })
  t.after(() => secrets._resetDeps())
}

/**
 * 假的“加密”：XOR + enc:: 前缀。
 *
 * 不能用 `enc::${明文}` —— 那只是打标签，密文里仍含明文 key，测试断言
 * “文件里不能出现明文”会失败。XOR 能保证往返可逆，同时密文不含明文子串。
 */
const XOR_MASK = 0x5a
function fakeEncrypt(text) {
  const body = Buffer.from(text, 'utf8')
  for (let i = 0; i < body.length; i += 1) body[i] ^= XOR_MASK
  return Buffer.concat([Buffer.from('enc::', 'utf8'), body])
}
function fakeDecrypt(buffer) {
  const body = Buffer.from(buffer.subarray(5))
  for (let i = 0; i < body.length; i += 1) body[i] ^= XOR_MASK
  return body.toString('utf8')
}

/** 同步 safeStorage（Electron 45 之前的 API），密文带 enc:: 前缀便于断言 */
function syncStorage(available = true) {
  return {
    isEncryptionAvailable: () => available,
    encryptString: fakeEncrypt,
    decryptString: fakeDecrypt,
  }
}

/** 异步 safeStorage（Electron 45 的 API），可指定 shouldReEncrypt */
function asyncStorage({ shouldReEncrypt = false } = {}) {
  return {
    isAsyncEncryptionAvailable: async () => true,
    encryptStringAsync: async text => fakeEncrypt(text),
    decryptStringAsync: async buffer => ({
      result: fakeDecrypt(buffer),
      shouldReEncrypt,
    }),
  }
}

test('safeStorage 不可用：writeSecrets 返回 false 且不建文件，readSecrets 返回空', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(false) })

  const written = await secrets.writeSecrets({ providers: { openai: { apiKey: LONG_KEY } } })
  assert.equal(written, false)
  assert.equal(fs.existsSync(path.join(userData, secrets.SECRETS_FILE)), false)
  assert.deepEqual(await secrets.readSecrets(), {})
  // 降级路径下仍然要能清残留
  assert.equal(await secrets.clearSecrets(), true)
})

test('safeStorage 抛异常时按不可用处理', async t => {
  const userData = makeUserData(t)
  bind(t, {
    userData,
    safeStorage: {
      isEncryptionAvailable: () => {
        throw new Error('keyring locked')
      },
    },
  })
  assert.equal(await secrets.writeSecrets({ providers: {} }), false)
  assert.deepEqual(await secrets.readSecrets(), {})
})

test('同步 safeStorage：加密往返成功，文件里没有明文 key', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(true) })

  const payload = {
    providers: { 'openai-compatible': { apiKey: LONG_KEY, baseUrl: 'http://127.0.0.1:11434/v1' } },
  }
  assert.equal(await secrets.writeSecrets(payload), true)

  const file = path.join(userData, secrets.SECRETS_FILE)
  const raw = fs.readFileSync(file, 'utf8')
  assert.ok(raw.startsWith('enc::'), '应写入加密后的密文')
  assert.ok(!raw.includes(LONG_KEY), '密文里不能出现明文 key')

  const back = await secrets.readSecrets()
  assert.equal(back.version, secrets.SECRETS_VERSION)
  assert.equal(back.providers['openai-compatible'].apiKey, LONG_KEY)
  assert.equal(back.providers['openai-compatible'].baseUrl, 'http://127.0.0.1:11434/v1')
})

test('异步 safeStorage：往返成功，且未知字段被收敛掉', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: asyncStorage() })

  const written = await secrets.writeSecrets({
    version: 99,
    extra: 'drop-me',
    providers: { ollama: { baseUrl: 'http://127.0.0.1:11434', evil: 'drop-me' } },
  })
  assert.equal(written, true)

  const back = await secrets.readSecrets()
  assert.equal(back.version, secrets.SECRETS_VERSION)
  assert.equal(back.extra, undefined)
  assert.deepEqual(back.providers.ollama, { baseUrl: 'http://127.0.0.1:11434' })
})

test('shouldReEncrypt 为真时用新密钥重写，配置不丢', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: asyncStorage({ shouldReEncrypt: true }) })

  assert.equal(await secrets.writeSecrets({ providers: { ollama: { baseUrl: 'http://127.0.0.1:11434' } } }), true)
  const first = fs.readFileSync(path.join(userData, secrets.SECRETS_FILE), 'utf8')

  const back = await secrets.readSecrets()
  assert.equal(back.providers.ollama.baseUrl, 'http://127.0.0.1:11434')
  // 重写后仍是密文，且内容没变
  assert.equal(fs.readFileSync(path.join(userData, secrets.SECRETS_FILE), 'utf8'), first)
})

test('密文损坏 / 解密失败：readSecrets 返回空对象而不是抛错', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(true) })
  fs.writeFileSync(path.join(userData, secrets.SECRETS_FILE), Buffer.from('not-json-at-all'))
  assert.deepEqual(await secrets.readSecrets(), {})

  bind(t, {
    userData,
    safeStorage: {
      isEncryptionAvailable: () => true,
      encryptString: text => Buffer.from(text, 'utf8'),
      decryptString: () => {
        throw new Error('dpapi failure')
      },
    },
  })
  assert.deepEqual(await secrets.readSecrets(), {})
})

test('clearSecrets 删除文件后 readSecrets 为空', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(true) })
  assert.equal(await secrets.writeSecrets({ providers: { ollama: { baseUrl: 'http://x' } } }), true)
  assert.equal(fs.existsSync(path.join(userData, secrets.SECRETS_FILE)), true)

  assert.equal(await secrets.clearSecrets(), true)
  assert.equal(fs.existsSync(path.join(userData, secrets.SECRETS_FILE)), false)
  assert.deepEqual(await secrets.readSecrets(), {})
})

test('maskKey 不泄露完整 key', () => {
  assert.equal(secrets.maskKey(''), '')
  assert.equal(secrets.maskKey(null), '')

  const short = secrets.maskKey('sk-short')
  assert.equal(short, '••••••••')
  assert.ok(!short.includes('short'))

  const masked = secrets.maskKey(LONG_KEY)
  assert.ok(!masked.includes(LONG_KEY))
  assert.ok(masked.startsWith('sk-t'))
  assert.ok(masked.endsWith('mnop'))
  assert.ok(masked.includes('••••••'))
})

// ── active / provider.model ──
// 用户在「设置 → 本地模型」里选的 provider 与模型名都落在 secrets 里，
// 推理代理按 active 选路（见 inference-proxy.cjs）。这两个字段丢了，
// 症状是"设置保存了却不生效"，所以形状收敛处必须有断言。

test('normalizeSecrets：active 与 provider 的 model 保留，脏值被收敛', () => {
  const parsed = secrets.normalizeSecrets({
    active: '  OpenAI-Compatible  ',
    providers: {
      ollama: { baseUrl: ' http://127.0.0.1:11434 ', model: 'qwen2.5:7b' },
      'openai-compatible': {
        apiKey: ' sk-x ',
        baseUrl: 'https://example.invalid/v1',
        model: 'gpt-x',
        evil: 'drop-me',
      },
      broken: 'not-an-object',
    },
    junk: 'drop-me',
  })
  assert.equal(parsed.active, 'openai-compatible')
  assert.deepEqual(parsed.providers.ollama, {
    baseUrl: 'http://127.0.0.1:11434',
    model: 'qwen2.5:7b',
  })
  assert.deepEqual(parsed.providers['openai-compatible'], {
    apiKey: 'sk-x',
    baseUrl: 'https://example.invalid/v1',
    model: 'gpt-x',
  })
  assert.equal(parsed.providers.broken, undefined)
  assert.equal(parsed.junk, undefined)

  // active 只接受字符串：对象/数组不能借道写进配置
  assert.equal(secrets.normalizeSecrets({ active: { name: 'ollama' } }).active, '')
  assert.equal(secrets.normalizeSecrets(null).active, '')
})

test('active 与 model 能加密往返（不落明文）', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(true) })

  assert.equal(await secrets.writeSecrets({
    active: 'openai-compatible',
    providers: { 'openai-compatible': { apiKey: LONG_KEY, baseUrl: 'https://example.invalid/v1', model: 'gpt-x' } },
  }), true)

  const raw = fs.readFileSync(path.join(userData, secrets.SECRETS_FILE), 'utf8')
  assert.ok(!raw.includes(LONG_KEY), '密文里不能出现明文 key')

  const back = await secrets.readSecrets()
  assert.equal(back.active, 'openai-compatible')
  assert.equal(back.providers['openai-compatible'].model, 'gpt-x')
})

// ── readPublicSecrets：渲染进程只能看到脱敏快照 ──

test('readPublicSecrets：只给 baseUrl / model / hasKey，绝不含 apiKey', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(true) })
  assert.equal(await secrets.writeSecrets({
    active: 'openai-compatible',
    providers: {
      'openai-compatible': { apiKey: LONG_KEY, baseUrl: 'https://example.invalid/v1', model: 'gpt-x' },
      ollama: { baseUrl: 'http://127.0.0.1:11500', model: 'qwen2.5:7b' },
    },
  }), true)

  const publicConfig = await secrets.readPublicSecrets()
  assert.equal(publicConfig.active, 'openai-compatible')
  assert.equal(publicConfig.safeStorageAvailable, true)
  assert.deepEqual(publicConfig.providers['openai-compatible'], {
    baseUrl: 'https://example.invalid/v1',
    model: 'gpt-x',
    hasKey: true,
  })
  // 没配 key 的 provider 也必须有完整形状，界面才好直接绑定
  assert.deepEqual(publicConfig.providers.ollama, {
    baseUrl: 'http://127.0.0.1:11500',
    model: 'qwen2.5:7b',
    hasKey: false,
  })

  const serialized = JSON.stringify(publicConfig)
  assert.ok(!serialized.includes(LONG_KEY), '脱敏快照里不能出现明文 key')
  assert.ok(!serialized.includes('apiKey'), '连字段名都不该出现（避免调用方以为能拿到）')
  assert.ok(!serialized.includes('••'), '也不给掩码：掩码的每一位都是从真 key 派生的')
})

test('readPublicSecrets：safeStorage 不可用时返回空壳而不是抛错', async t => {
  const userData = makeUserData(t)
  bind(t, { userData, safeStorage: syncStorage(false) })

  const publicConfig = await secrets.readPublicSecrets()
  assert.equal(publicConfig.safeStorageAvailable, false)
  assert.equal(publicConfig.active, '')
  for (const name of secrets.PROVIDER_NAMES) {
    assert.deepEqual(publicConfig.providers[name], { baseUrl: '', model: '', hasKey: false })
  }
})
