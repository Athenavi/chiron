/**
 * Chiron Desktop — preload
 *
 * 只暴露只读信息与两个明确的动作，不暴露 require / 任意 ipc 通道。
 * 页面通过 `window.chironDesktop` 读取。
 */
const { contextBridge, ipcRenderer } = require('electron')

/**
 * 服务地址从 `additionalArguments` 读取（主进程创建窗口时注入）。
 * 为什么不用 `process.env`：渲染进程的环境变量是**启动时**继承的，
 * 主进程在用户保存地址后才设置的值，preload 读不到。argv 是每次创建窗口时现传的。
 */
const API_ARG_PREFIX = '--chiron-api-url='
const apiFromArgv = process.argv.find(a => a.startsWith(API_ARG_PREFIX))
const apiUrl = apiFromArgv
  ? apiFromArgv.slice(API_ARG_PREFIX.length)
  : process.env.CHIRON_API_URL || ''

contextBridge.exposeInMainWorld('chironDesktop', {
  /** 'win32' | 'darwin' | 'linux' */
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
  /** 当前生效的服务地址；空串表示尚未配置 */
  apiUrl,
  isDesktop: true,

  /** 测试连通性（打后端 /health）：成功 resolve，失败 reject 出可读 message */
  testApiUrl: url => ipcRenderer.invoke('desktop:test-api-url', url),
  /** 保存地址并进入主界面 */
  setApiUrl: url => ipcRenderer.invoke('desktop:set-api-url', url),
})
