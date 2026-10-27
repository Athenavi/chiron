/**
 * Chiron Desktop — preload
 *
 * 只暴露只读信息与明确的几个动作，不暴露 require / 任意 ipc 通道。
 * 页面通过 `window.chironDesktop` 读取（契约见 docs/desktop-local/contracts.md §2）。
 */
const { contextBridge, ipcRenderer } = require('electron')
const { API_URL_ARG_PREFIX, MODE_ARG_PREFIX } = require('./shared.cjs')

/**
 * 服务地址从 `additionalArguments` 读取（主进程创建窗口时注入）。
 * 为什么不用 `process.env`：渲染进程的环境变量是**启动时**继承的，
 * 主进程在用户保存地址后才设置的值，preload 读不到。argv 是每次创建窗口时现传的。
 */
const apiFromArgv = process.argv.find(a => a.startsWith(API_URL_ARG_PREFIX))
const apiUrl = apiFromArgv
  ? apiFromArgv.slice(API_URL_ARG_PREFIX.length)
  : process.env.CHIRON_API_URL || ''

/**
 * 运行模式同样从 argv 读。argv 缺失时（手工 `electron .` 且没走主进程建窗）
 * 回落到 cloud —— 那是老版本唯一的行为，误判成 local 会让页面去等一个不存在的本地后端。
 */
const modeFromArgv = process.argv.find(a => a.startsWith(MODE_ARG_PREFIX))
const rawMode = (
  modeFromArgv ? modeFromArgv.slice(MODE_ARG_PREFIX.length) : process.env.CHIRON_DESKTOP_MODE || ''
).toLowerCase()
const mode = rawMode === 'local' || rawMode === 'cloud' ? rawMode : 'cloud'

// 主进程用 safeStorage 加密保存；读取是异步的（Electron 45 的 async API），
// 由 store 在首屏挂载前 await，避免首屏请求缺少 Bearer token。
contextBridge.exposeInMainWorld('chironDesktop', {
  /** 'win32' | 'darwin' | 'linux' */
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  },
  /** 当前生效的服务地址；空串表示尚未配置 */
  apiUrl,
  /** 'local' | 'cloud'：当前运行模式（真相在主进程 config.json） */
  mode,
  isDesktop: true,

  /** 读取设备级设置（模式 / apiUrl / 本地数据目录 / UI / 迁移状态） */
  getDeviceSettings: () => ipcRenderer.invoke('desktop:get-device-settings'),
  /**
   * 切换运行模式：主进程会清掉离开模式的会话数据、写盘并重启应用。
   * deleteLocalData 默认 false —— 契约 §1 不允许默认删 userData/local。
   */
  setMode: (nextMode, opts) => ipcRenderer.invoke('desktop:set-mode', nextMode, opts || {}),
  /** 重启应用（不改模式） */
  restart: () => ipcRenderer.invoke('desktop:restart'),

  /** 测试连通性（打后端 /health）：成功 resolve，失败 reject 出可读 message */
  testApiUrl: url => ipcRenderer.invoke('desktop:test-api-url', url),
  /** 保存地址并进入主界面 */
  setApiUrl: url => ipcRenderer.invoke('desktop:set-api-url', url),
  /** 从系统凭据库异步读取 token */
  getToken: () => ipcRenderer.invoke('desktop:get-token'),
  /** 加密持久化 / 清除 Bearer token */
  saveToken: token => ipcRenderer.invoke('desktop:save-token', token),
  clearToken: () => ipcRenderer.invoke('desktop:clear-token'),

  /** 组件管理（契约 §2）：name ∈ 'core' | 'pageindex' | 'langextract' */
  listComponents: () => ipcRenderer.invoke('desktop:list-components'),
  installComponent: name => ipcRenderer.invoke('desktop:install-component', name),
  removeComponent: name => ipcRenderer.invoke('desktop:remove-component', name),
  startComponent: name => ipcRenderer.invoke('desktop:start-component', name),
  stopComponent: name => ipcRenderer.invoke('desktop:stop-component', name),
})
