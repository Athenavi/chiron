/**
 * Chiron Desktop — 配置持久化
 *
 * 存放在 Electron 的 userData 目录（Windows: %APPDATA%/Chiron，
 * macOS: ~/Library/Application Support/Chiron，Linux: ~/.config/Chiron），
 * 不放仓库、不进安装包。
 *
 * 双模式（local / cloud）的设备级设置只在这里落盘：`config.json` 是唯一真相，
 * 渲染进程的 localStorage 只是镜像（见 docs/desktop-local/contracts.md §1）。
 *
 * 纯 Node 环境（electron/tests 下的单测）没有 Electron 运行时：
 * 那里 `require('electron')` 返回的是二进制路径（甚至直接抛错），所以本文件
 * 对 app 做了容错，并允许用 `CHIRON_DESKTOP_USER_DATA` 指定配置目录。
 */
const fs = require('node:fs')
const path = require('node:path')

let electronApp = null
try {
  const electron = require('electron')
  if (electron && typeof electron === 'object' && electron.app) electronApp = electron.app
} catch {
  /* 非 Electron 进程（单测 / 脚本）：走 CHIRON_DESKTOP_USER_DATA */
}

/** 契约 §1：模式取值只有这两个 */
const MODES = ['local', 'cloud']
const MODE_LABELS = { local: '本地模式', cloud: '云端模式' }
/** 契约 §1：新装默认本地 */
const NEW_INSTALL_MODE = 'local'

const DEFAULT_DEVICE_SETTINGS = {
  mode: NEW_INSTALL_MODE,
  apiUrl: '',
  local: { dataDir: '', lastStartedAt: '' },
  ui: { locale: 'zh-CN', theme: 'system' },
  migration: { legacyCloudDetected: false, modeChosenAt: '' },
}

/** 只认字符串：配置文件是用户可手改的，任何字段都可能是 null / 数字 / 对象 */
function asString(value) {
  return typeof value === 'string' ? value : ''
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

/** 服务地址的合法性：只接受 http(s)，去掉末尾斜杠；拒绝空串以外的怪东西 */
function normalizeApiUrl(raw) {
  const trimmed = String(raw || '').trim().replace(/\/+$/, '')
  if (!trimmed) return ''
  let parsed
  try {
    parsed = new URL(trimmed)
  } catch {
    throw new Error('地址格式不正确，需要形如 http://127.0.0.1:8080')
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('只支持 http:// 或 https://')
  }
  return trimmed
}

/** 模式校验：写盘前必须过这一关，避免把拼错的模式写进 config.json */
function normalizeMode(raw) {
  const mode = String(raw ?? '').trim().toLowerCase()
  if (!MODES.includes(mode)) {
    throw new Error(`运行模式只能是 ${MODES.join(' / ')}，收到 ${JSON.stringify(raw)}`)
  }
  return mode
}

function userDataDir() {
  const override = String(process.env.CHIRON_DESKTOP_USER_DATA || '').trim()
  if (override) return override
  if (!electronApp || typeof electronApp.getPath !== 'function') {
    throw new Error('Electron app 不可用，且未设置 CHIRON_DESKTOP_USER_DATA')
  }
  return electronApp.getPath('userData')
}

function configPath() {
  return path.join(userDataDir(), 'config.json')
}

/**
 * 读磁盘：区分「文件不存在」（全新安装）与「存在但没写 mode」（老配置待迁移）。
 * 契约 §1 的迁移提示只针对后者，混为一谈会让新装也弹一次迁移框。
 */
function readConfigFile() {
  try {
    const text = fs.readFileSync(configPath(), 'utf8')
    return { exists: true, data: JSON.parse(text) }
  } catch (err) {
    if (err && err.code === 'ENOENT') return { exists: false, data: {} }
    // 文件存在但 JSON 坏了：仍按「已存在配置」处理，宁可多问一次也不静默覆盖用户的选择
    if (err instanceof SyntaxError || (err && err.name === 'SyntaxError')) return { exists: true, data: {} }
    // 权限等其它错误：当成读不到，退回默认（配置目录不可写时后续写盘会报错）
    return { exists: false, data: {} }
  }
}

function readConfig() {
  return readConfigFile().data
}

function writeConfig(data) {
  const file = configPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf8')
}

/**
 * 把磁盘上的任意形状收敛成契约 §1 的 DeviceSettings。
 *
 * 额外返回两个**派生字段**（不落盘，供主进程判断流程用）：
 *   needsMigration  磁盘上还没有 mode（老配置），需要弹一次迁移提示
 *   modeChosen      用户是否显式选过模式
 */
function shapeDeviceSettings(raw, options = {}) {
  const configExists = options.configExists !== false
  const src = isPlainObject(raw) ? raw : {}
  const declaredMode = MODES.includes(src.mode) ? src.mode : ''
  const apiUrl = asString(src.apiUrl)
  const local = isPlainObject(src.local) ? src.local : {}
  const ui = isPlainObject(src.ui) ? src.ui : {}
  const migration = isPlainObject(src.migration) ? src.migration : {}

  const modeChosen = Boolean(declaredMode)
  const needsMigration = configExists && !modeChosen
  // 契约 §1：「老用户已有 apiUrl 且无 mode」才提示迁移；这里把这个判断固化成一处理
  const legacyCloudDetected =
    Boolean(migration.legacyCloudDetected) || (needsMigration && Boolean(apiUrl))

  return {
    // 没选过模式时先给新装默认值：主进程会先用迁移 dialog 让用户确认
    mode: declaredMode || NEW_INSTALL_MODE,
    apiUrl,
    local: {
      dataDir: asString(local.dataDir),
      lastStartedAt: asString(local.lastStartedAt),
    },
    ui: {
      locale: asString(ui.locale) || DEFAULT_DEVICE_SETTINGS.ui.locale,
      theme: asString(ui.theme) || DEFAULT_DEVICE_SETTINGS.ui.theme,
    },
    migration: {
      legacyCloudDetected,
      modeChosenAt: asString(migration.modeChosenAt),
    },
    needsMigration,
    modeChosen,
  }
}

function readDeviceSettings() {
  const { exists, data } = readConfigFile()
  return shapeDeviceSettings(data, { configExists: exists })
}

/** 逐层合并：patch 里没出现的键保留原值（undefined 视为没出现） */
function mergeDeep(base, patch) {
  const out = { ...(isPlainObject(base) ? base : {}) }
  for (const [key, value] of Object.entries(isPlainObject(patch) ? patch : {})) {
    if (value === undefined) continue
    if (isPlainObject(value)) out[key] = mergeDeep(out[key], value)
    else out[key] = value
  }
  return out
}

/** 只保留契约里的已知字段，且逐个校验类型；不认识的键一律丢弃 */
function sanitizePatch(patch) {
  const src = isPlainObject(patch) ? patch : {}
  const out = {}
  if (src.mode !== undefined) out.mode = normalizeMode(src.mode)
  if (src.apiUrl !== undefined) out.apiUrl = normalizeApiUrl(src.apiUrl)
  if (src.local !== undefined) {
    const local = isPlainObject(src.local) ? src.local : {}
    out.local = {}
    for (const key of ['dataDir', 'lastStartedAt']) {
      if (local[key] !== undefined) out.local[key] = asString(local[key])
    }
  }
  if (src.ui !== undefined) {
    const ui = isPlainObject(src.ui) ? src.ui : {}
    out.ui = {}
    for (const key of ['locale', 'theme']) {
      if (ui[key] !== undefined) out.ui[key] = asString(ui[key])
    }
  }
  if (src.migration !== undefined) {
    const migration = isPlainObject(src.migration) ? src.migration : {}
    out.migration = {}
    if (migration.legacyCloudDetected !== undefined) {
      out.migration.legacyCloudDetected = Boolean(migration.legacyCloudDetected)
    }
    if (migration.modeChosenAt !== undefined) {
      out.migration.modeChosenAt = asString(migration.modeChosenAt)
    }
  }
  return out
}

/** 落盘形状：契约 §1 的五个顶层键，派生字段不写 */
function toDiskShape(settings) {
  const local = isPlainObject(settings.local) ? settings.local : {}
  const ui = isPlainObject(settings.ui) ? settings.ui : {}
  const migration = isPlainObject(settings.migration) ? settings.migration : {}
  return {
    mode: normalizeMode(settings.mode),
    apiUrl: asString(settings.apiUrl),
    local: { dataDir: asString(local.dataDir), lastStartedAt: asString(local.lastStartedAt) },
    ui: {
      locale: asString(ui.locale) || DEFAULT_DEVICE_SETTINGS.ui.locale,
      theme: asString(ui.theme) || DEFAULT_DEVICE_SETTINGS.ui.theme,
    },
    migration: {
      legacyCloudDetected: Boolean(migration.legacyCloudDetected),
      modeChosenAt: asString(migration.modeChosenAt),
    },
  }
}

const KNOWN_KEYS = ['mode', 'apiUrl', 'local', 'ui', 'migration', 'needsMigration', 'modeChosen']

/**
 * 保存设备设置（局部更新）。
 * 未知顶层键（将来别的模块往 config.json 里加的）原样保留，避免互相踩踏。
 */
function saveDeviceSettings(patch = {}) {
  const { data } = readConfigFile()
  const current = shapeDeviceSettings(data, { configExists: true })
  const cleaned = sanitizePatch(patch)
  const next = mergeDeep(current, cleaned)
  const disk = {}
  for (const [key, value] of Object.entries(isPlainObject(data) ? data : {})) {
    if (!KNOWN_KEYS.includes(key)) disk[key] = value
  }
  const shape = toDiskShape(next)
  // 用户还没选过模式、这次也没写模式：不要把「新装默认值」固化进磁盘。
  // 固化会悄悄清掉 needsMigration，让老配置的迁移提示再也不会出现。
  if (!next.modeChosen && cleaned.mode === undefined) delete shape.mode
  Object.assign(disk, shape)
  writeConfig(disk)
  return shapeDeviceSettings(disk, { configExists: true })
}

/**
 * 写入模式选择。只动 mode 与 migration：apiUrl 等既有设置必须原样保留
 * （契约 §1：切模式不能丢掉用户填过的云端地址）。
 */
function saveMode(mode) {
  const nextMode = normalizeMode(mode)
  const settings = saveDeviceSettings({
    mode: nextMode,
    migration: { legacyCloudDetected: false, modeChosenAt: new Date().toISOString() },
  })
  return settings.mode
}

/**
 * 撤销模式选择：删掉 mode 字段，回到「老配置待迁移」状态。
 * 只给模式切换失败时的回滚用 —— 正常流程不该把用户的选择清掉。
 */
function clearMode() {
  const { data } = readConfigFile()
  const disk = { ...(isPlainObject(data) ? data : {}) }
  delete disk.mode
  const migration = isPlainObject(disk.migration) ? { ...disk.migration } : {}
  migration.modeChosenAt = ''
  disk.migration = migration
  writeConfig(disk)
  return readDeviceSettings()
}

/**
 * 模式解析：环境变量优先（开发与排障用），否则取设备设置。
 * `CHIRON_DESKTOP_MODE=cloud` 可以不改 config.json 直接跑云端壳。
 */
function resolveMode(settings = readDeviceSettings()) {
  const override = String(process.env.CHIRON_DESKTOP_MODE || '').trim().toLowerCase()
  if (MODES.includes(override)) return override
  return settings.mode
}

/**
 * 服务地址的来源优先级：环境变量 > 配置文件 > 空（触发首次配置页）。
 * 只有云端模式才用云端地址；本地模式的 baseUrl 由本地后端启动回调给出
 * （main.cjs 注入给渲染进程），这里返回空串表示「不要连远程服务」。
 */
function resolveApiUrl() {
  if (process.env.CHIRON_API_URL) return normalizeApiUrl(process.env.CHIRON_API_URL)
  const settings = readDeviceSettings()
  if (resolveMode(settings) !== 'cloud') return ''
  return normalizeApiUrl(settings.apiUrl)
}

function saveApiUrl(raw) {
  return saveDeviceSettings({ apiUrl: normalizeApiUrl(raw) }).apiUrl
}

/** 连通性探测：打后端的 /health（它只回存活，不查依赖，适合做客户端握手） */
async function probe(apiUrl) {
  const base = normalizeApiUrl(apiUrl)
  if (!base) throw new Error('请先填写服务地址')
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 5000)
  try {
    const res = await fetch(`${base}/health`, { signal: controller.signal })
    if (!res.ok) throw new Error(`服务返回 ${res.status}`)
    return true
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('连接超时（5 秒）')
    throw new Error(err.message || '连接失败')
  } finally {
    clearTimeout(timer)
  }
}

module.exports = {
  MODES,
  MODE_LABELS,
  NEW_INSTALL_MODE,
  DEFAULT_DEVICE_SETTINGS,
  normalizeApiUrl,
  normalizeMode,
  userDataDir,
  configPath,
  readConfigFile,
  readConfig,
  readDeviceSettings,
  saveDeviceSettings,
  saveMode,
  clearMode,
  resolveMode,
  resolveApiUrl,
  saveApiUrl,
  probe,
}
