/**
 * 桌面端 Bearer token 的加密存储。
 *
 * 不放 localStorage：Electron 的 file:// 页面里任何 XSS 都能读 localStorage。
 * 这里用系统凭据库（Windows DPAPI / macOS Keychain / Linux libsecret）加密后
 * 落到 userData/token.bin。safeStorage 不可用时返回失败，token 只保留在当前
 * 进程内存中，绝不退化成明文持久化。
 *
 * Electron 45 起同步的 isEncryptionAvailable / encryptString / decryptString
 * 已弃用，这里统一使用 async API；decryptStringAsync 在密钥轮换后会返回
 * shouldReEncrypt，此时立刻用新密钥重写一次。
 */
const { app, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

function tokenPath() {
  return path.join(app.getPath('userData'), 'token.bin')
}

async function readToken() {
  try {
    if (!(await safeStorage.isAsyncEncryptionAvailable())) return ''
    const encrypted = await fs.promises.readFile(tokenPath())
    const { result, shouldReEncrypt } = await safeStorage.decryptStringAsync(encrypted)
    if (shouldReEncrypt) await writeToken(result)
    return result || ''
  } catch {
    return ''
  }
}

async function writeToken(token) {
  try {
    if (!(await safeStorage.isAsyncEncryptionAvailable())) return false
    const file = tokenPath()
    if (!token) {
      await fs.promises.rm(file, { force: true })
      return true
    }
    await fs.promises.mkdir(path.dirname(file), { recursive: true })
    const encrypted = await safeStorage.encryptStringAsync(token)
    await fs.promises.writeFile(file, encrypted, { mode: 0o600 })
    return true
  } catch {
    return false
  }
}

async function clearToken() {
  return writeToken('')
}

module.exports = { tokenPath, readToken, writeToken, clearToken }
