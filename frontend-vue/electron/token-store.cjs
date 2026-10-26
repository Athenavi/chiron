/**
 * 桌面端 Bearer token 的加密存储。
 *
 * 不放 localStorage：Electron 的 file:// 页面里任何 XSS 都能读 localStorage。
 * 这里用系统凭据库（Windows DPAPI / macOS Keychain / Linux libsecret）加密后
 * 落到 userData/token.bin。safeStorage 不可用时返回失败，token 只保留在当前
 * 进程内存中，绝不退化成明文持久化。
 */
const { app, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

function tokenPath() {
  return path.join(app.getPath('userData'), 'token.bin')
}

function readToken() {
  try {
    if (!safeStorage.isEncryptionAvailable()) return ''
    return safeStorage.decryptString(fs.readFileSync(tokenPath())) || ''
  } catch {
    return ''
  }
}

function writeToken(token) {
  try {
    if (!safeStorage.isEncryptionAvailable()) return false
    const file = tokenPath()
    if (!token) {
      fs.rmSync(file, { force: true })
      return true
    }
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, safeStorage.encryptString(token), { mode: 0o600 })
    return true
  } catch {
    return false
  }
}

function clearToken() {
  return writeToken('')
}

module.exports = { tokenPath, readToken, writeToken, clearToken }
