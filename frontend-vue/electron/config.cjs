/**
 * Chiron Desktop — 配置持久化
 *
 * 存放在 Electron 的 userData 目录（Windows: %APPDATA%/Chiron，
 * macOS: ~/Library/Application Support/Chiron，Linux: ~/.config/Chiron），
 * 不放仓库、不进安装包。
 */
const { app } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

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

function configPath() {
  return path.join(app.getPath('userData'), 'config.json')
}

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(configPath(), 'utf8'))
  } catch {
    return {}
  }
}

/** 服务地址的来源优先级：环境变量 > 配置文件 > 空（触发首次配置页） */
function resolveApiUrl() {
  if (process.env.CHIRON_API_URL) return normalizeApiUrl(process.env.CHIRON_API_URL)
  return normalizeApiUrl(readConfig().apiUrl)
}

function saveApiUrl(raw) {
  const apiUrl = normalizeApiUrl(raw)
  const next = { ...readConfig(), apiUrl }
  fs.mkdirSync(path.dirname(configPath()), { recursive: true })
  fs.writeFileSync(configPath(), JSON.stringify(next, null, 2), 'utf8')
  return apiUrl
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

module.exports = { normalizeApiUrl, readConfig, resolveApiUrl, saveApiUrl, probe, configPath }
