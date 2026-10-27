/**
 * 用户自配 provider 密钥的加密存储。
 *
 * 与 token-store.cjs 同一套约束：key 只经 safeStorage（Windows DPAPI /
 * macOS Keychain / Linux libsecret）加密后落到 userData/secrets.bin；
 * safeStorage 不可用时 writeSecrets 返回 false、readSecrets 返回 {}，
 * 绝不退化成明文落盘，也不写日志。
 *
 * 明文结构（加密前的 JSON，形状固定，避免各 worktree 自己发明字段）：
 *
 *   {
 *     "version": 1,
 *     "providers": {
 *       "openai-compatible": { "apiKey": "sk-...", "baseUrl": "https://..." },
 *       "ollama": { "baseUrl": "http://127.0.0.1:11434" }
 *     }
 *   }
 *
 * Electron 45 起同步的 isEncryptionAvailable / encryptString / decryptString
 * 已弃用（token-store.cjs 用的是 async API），这里两种都支持：优先 async，
 * 没有就退回同步，两者都没有则视为不可用。
 */
const fs = require('node:fs')
const path = require('node:path')

const SECRETS_FILE = 'secrets.bin'
const SECRETS_VERSION = 1

/**
 * 依赖注入位：纯 Node 单测（node --test）里没有 electron 运行时，
 * `require('electron')` 会拿到一个可执行文件路径字符串而没有 app/safeStorage。
 * 只有 __tests__ 会调用 _setDeps/_resetDeps，生产代码永远走默认运行时。
 */
let injected = null

function runtime() {
  if (injected) return injected
  const electron = require('electron')
  return { app: electron.app, safeStorage: electron.safeStorage }
}

function _setDeps(deps) {
  injected = deps || null
}

function _resetDeps() {
  injected = null
}

function secretsPath() {
  const { app } = runtime()
  if (!app || typeof app.getPath !== 'function') {
    throw new Error('secrets.cjs 需要 Electron 的 app.getPath（纯 Node 环境请注入依赖）')
  }
  return path.join(app.getPath('userData'), SECRETS_FILE)
}

function hasSafeStorage() {
  const { safeStorage } = runtime()
  return Boolean(safeStorage)
}

async function encryptionAvailable(safeStorage) {
  try {
    if (typeof safeStorage.isAsyncEncryptionAvailable === 'function') {
      return Boolean(await safeStorage.isAsyncEncryptionAvailable())
    }
    if (typeof safeStorage.isEncryptionAvailable === 'function') {
      return Boolean(safeStorage.isEncryptionAvailable())
    }
  } catch {
    // 后端密钥环坏掉时 Electron 会抛异常，按不可用处理
  }
  return false
}

async function encryptText(safeStorage, text) {
  if (typeof safeStorage.encryptStringAsync === 'function') {
    const out = await safeStorage.encryptStringAsync(text)
    return Buffer.isBuffer(out) ? out : Buffer.from(out)
  }
  const out = safeStorage.encryptString(text)
  return Buffer.isBuffer(out) ? out : Buffer.from(out)
}

async function decryptText(safeStorage, encrypted) {
  if (typeof safeStorage.decryptStringAsync === 'function') {
    const out = await safeStorage.decryptStringAsync(encrypted)
    if (out && typeof out === 'object' && 'result' in out) {
      return { text: String(out.result || ''), shouldReEncrypt: Boolean(out.shouldReEncrypt) }
    }
    return { text: String(out || ''), shouldReEncrypt: false }
  }
  return { text: String(safeStorage.decryptString(encrypted) || ''), shouldReEncrypt: false }
}

/** 只保留已知字段和字符串值：写盘前收敛形状，读取时同样过滤脏数据 */
function normalizeSecrets(raw) {
  const result = { version: SECRETS_VERSION, providers: {} }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return result
  const providers = raw.providers
  if (!providers || typeof providers !== 'object' || Array.isArray(providers)) return result
  for (const [provider, entry] of Object.entries(providers)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const clean = {}
    for (const field of ['apiKey', 'baseUrl', 'model']) {
      const value = entry[field]
      if (typeof value === 'string' && value) clean[field] = value
    }
    if (Object.keys(clean).length) result.providers[String(provider)] = clean
  }
  return result
}

/** 读：不可用 / 文件缺失 / 解密失败 / JSON 坏掉 一律返回空对象，调用方不必 try */
async function readSecrets() {
  try {
    if (!hasSafeStorage()) return {}
    const { safeStorage } = runtime()
    if (!(await encryptionAvailable(safeStorage))) return {}
    let encrypted
    try {
      encrypted = await fs.promises.readFile(secretsPath())
    } catch {
      return {}
    }
    if (!encrypted || encrypted.length === 0) return {}
    const { text, shouldReEncrypt } = await decryptText(safeStorage, encrypted)
    const parsed = normalizeSecrets(JSON.parse(text))
    // 系统密钥轮换后立刻用新密钥重写，否则下次解密失败就丢配置
    if (shouldReEncrypt) await writeSecrets(parsed)
    return parsed
  } catch {
    return {}
  }
}

/** 写：safeStorage 不可用时返回 false，且不创建任何文件 */
async function writeSecrets(next) {
  try {
    if (!hasSafeStorage()) return false
    const { safeStorage } = runtime()
    if (!(await encryptionAvailable(safeStorage))) return false
    const payload = JSON.stringify(normalizeSecrets(next))
    const file = secretsPath()
    await fs.promises.mkdir(path.dirname(file), { recursive: true })
    const encrypted = await encryptText(safeStorage, payload)
    if (!encrypted || encrypted.length === 0) return false
    // 先写临时文件再 rename：中途崩掉不会留下半截密文
    const tmp = `${file}.tmp`
    await fs.promises.writeFile(tmp, encrypted, { mode: 0o600 })
    await fs.promises.rename(tmp, file)
    return true
  } catch {
    return false
  }
}

/** 清：删文件不需要加密能力，不可用时也要尽力删掉残留密文 */
async function clearSecrets() {
  try {
    await fs.promises.rm(secretsPath(), { force: true })
    return true
  } catch {
    return false
  }
}

/**
 * 展示用掩码：短 key 直接全遮，长 key 只留首尾各 4 位。
 * 渲染进程拿到的永远是掩码，明文 key 不出主进程。
 */
function maskKey(key) {
  const text = String(key == null ? '' : key)
  if (!text) return ''
  if (text.length <= 12) return '••••••••'
  return `${text.slice(0, 4)}••••••${text.slice(-4)}`
}

module.exports = {
  SECRETS_FILE,
  SECRETS_VERSION,
  secretsPath,
  normalizeSecrets,
  readSecrets,
  writeSecrets,
  clearSecrets,
  maskKey,
  _setDeps,
  _resetDeps,
}
