/**
 * Chiron Desktop — preload
 *
 * 只暴露只读信息与两个明确的动作，不暴露 require / 任意 ipc 通道。
 * 页面通过 `window.chironDesktop` 读取。
 */
const { contextBridge, ipcRenderer } = require('electron')
const { API_URL_ARG_PREFIX } = require('./shared.cjs')

/**
 * 服务地址从 `additionalArguments` 读取（主进程创建窗口时注入）。
 * 为什么不用 `process.env`：渲染进程的环境变量是**启动时**继承的，
 * 主进程在用户保存地址后才设置的值，preload 读不到。argv 是每次创建窗口时现传的。
 */
const apiFromArgv = process.argv.find(a => a.startsWith(API_URL_ARG_PREFIX))
const apiUrl = apiFromArgv
  ? apiFromArgv.slice(API_URL_ARG_PREFIX.length)
  : process.env.CHIRON_API_URL || ''

// 主进程用 safeStorage 加密保存；这里同步取一次（本地文件解密，开销很小），
// 让 Pinia store 初始化时就能拿到 token，不必等异步 IPC 造成首屏 401。
let storedToken = ''
try {
  storedToken = ipcRenderer.sendSync('desktop:get-token-sync') || ''
} catch {
  storedToken = ''
}

contextBridge.exposeInMainWorld('chironDesktop', {
  /** 'win32' | 'darwin' | 'linux' */
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
  /** 当前生效的服务地址；空串表示尚未配置 */
  apiUrl,
  /** 启动时从系统凭据库解出的 token；空串表示未登录或加密存储不可用 */
  token: storedToken,
  isDesktop: true,

  /** 测试连通性（打后端 /health）：成功 resolve，失败 reject 出可读 message */
  testApiUrl: url => ipcRenderer.invoke('desktop:test-api-url', url),
  /** 保存地址并进入主界面 */
  setApiUrl: url => ipcRenderer.invoke('desktop:set-api-url', url),
  /** 加密持久化 / 清除 Bearer token */
  saveToken: token => ipcRenderer.invoke('desktop:save-token', token),
  clearToken: () => ipcRenderer.invoke('desktop:clear-token'),
})
