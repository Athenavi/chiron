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
 *     "active": "ollama",                      // 当前使用的 provider 名
 *     "providers": {
 *       "openai-compatible": {
 *         "apiKey": "sk-...",
 *         "baseUrl": "https://...",
 *         "model": "gpt-4o-mini"
 *       },
 *       "ollama": {
 *         "baseUrl": "http://127.0.0.1:11434",
 *         "model": "qwen2.5:7b"
 *       }
 *     }
 *   }
 *
 * 端点默认值**不在这里**：本模块只负责"用户填了什么"，默认值集中在
 * inference-proxy.cjs（Ollama）与面板的 placeholder 里，避免两处默认值漂移。
 *
 * `active` 只做 trim + 小写，**不做 provider 别名归一**（`openai` →
 * `openai-compatible` 之类由 inference-proxy.cjs 负责）：否则同一个别名表要维护
 * 两份，改一处漏一处就会表现为"设置保存了却不生效"。
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
 * 已知 provider 名（写盘前不裁剪它们，只在 readPublicSecrets 里补空壳）。
 * 这里是"界面上至少有这几行"的清单，不是白名单：secrets 里多出来的 provider
 * 原样保留，别因为某个 worktree 加了新 provider 就读不出来。
 */
const PROVIDER_NAMES = Object.freeze(['ollama', 'openai-compatible'])

/** provider 条目里会被保留的字符串字段（其余一律丢弃，防脏数据扩散） */
const PROVIDER_FIELDS = Object.freeze(['apiKey', 'baseUrl', 'model'])

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
  const result = { version: SECRETS_VERSION, active: '', providers: {} }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return result
  // 用户在界面上粘贴的 provider / 端点经常带空格，统一 trim；provider 名按小写比较
  if (typeof raw.active === 'string') result.active = raw.active.trim().toLowerCase()
  const providers = raw.providers
  if (!providers || typeof providers !== 'object' || Array.isArray(providers)) return result
  for (const [provider, entry] of Object.entries(providers)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const clean = {}
    for (const field of PROVIDER_FIELDS) {
      const value = entry[field]
      if (typeof value === 'string' && value.trim()) clean[field] = value.trim()
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

/**
 * 渲染进程可见的配置快照：**只含 baseUrl / model / 有没有 key**。
 *
 * 明文 apiKey 绝不出主进程（连掩码也不给：掩码的每一位都是从真 key 派生的信息，
 * 界面上"有没有配"用 hasKey 一个布尔就够了）。读取失败 / safeStorage 不可用时
 * 返回空壳而不是抛错 —— 设置页在没装密钥库的 Linux 上也要能正常显示。
 *
 * @returns {Promise<{ active: string, providers: Record<string, { baseUrl: string, model: string, hasKey: boolean }>, safeStorageAvailable: boolean }>}
 */
async function readPublicSecrets() {
  const config = await readSecrets()
  const providers = {}
  for (const name of PROVIDER_NAMES) {
    providers[name] = { baseUrl: '', model: '', hasKey: false }
  }
  const stored = config && typeof config === 'object' && config.providers ? config.providers : {}
  for (const [name, entry] of Object.entries(stored)) {
    if (!entry || typeof entry !== 'object') continue
    providers[name] = {
      baseUrl: typeof entry.baseUrl === 'string' ? entry.baseUrl : '',
      model: typeof entry.model === 'string' ? entry.model : '',
      hasKey: typeof entry.apiKey === 'string' && entry.apiKey.length > 0,
    }
  }
  return {
    active: config && typeof config.active === 'string' ? config.active : '',
    providers,
    safeStorageAvailable: await safeStorageUsable(),
  }
}

/** safeStorage 是否可用（不抛错；只用于界面上说明"为什么存不进去"） */
async function safeStorageUsable() {
  try {
    if (!hasSafeStorage()) return false
    const { safeStorage } = runtime()
    return await encryptionAvailable(safeStorage)
  } catch {
    return false
  }
}

module.exports = {
  SECRETS_FILE,
  SECRETS_VERSION,
  PROVIDER_NAMES,
  secretsPath,
  normalizeSecrets,
  readSecrets,
  readPublicSecrets,
  writeSecrets,
  clearSecrets,
  maskKey,
  _setDeps,
  _resetDeps,
}
