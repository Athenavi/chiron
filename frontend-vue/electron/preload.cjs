/**
 * Chiron Desktop — preload
 *
 * 只暴露只读信息与明确的几个动作，不暴露 require / 任意 ipc 通道。
 * 页面通过 `window.chironDesktop` 读取（契约见 docs/desktop-local/contracts.md §2）。
 */
const { contextBridge, ipcRenderer } = require('electron')
const shared = require('./shared.cjs')
const { API_URL_ARG_PREFIX, MODE_ARG_PREFIX } = shared

/**
 * 设备 token 的注入前缀，必须与 main.cjs 的 DEVICE_TOKEN_ARG_PREFIX 完全一致。
 * 两处都先读 shared.cjs（契约 §3.1 要求共享常量集中在那里），读不到才用同一个
 * 兜底字面量 —— 这样把常量搬进 shared.cjs 时两边会同时生效，不会单边漂移。
 */
const DEVICE_TOKEN_ARG_PREFIX = shared.DEVICE_TOKEN_ARG_PREFIX || '--chiron-device-token='

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

/**
 * 本地设备 token：local 模式下由主进程注入（core 的握手结果），cloud 模式不注入。
 *
 * 为什么可以放在 argv 里：它不是云端的用户密码，只是本机 core 认的"这是本机主人"的
 * 设备标识（desktop-local/app/api/auth.py）。云端 token 仍走 safeStorage + IPC
 * （getToken/saveToken/clearToken），两条路径互不影响。
 * argv 缺失时回落到 process.env（只对开发态 `CHIRON_DEVICE_TOKEN=... electron .` 有效）。
 */
const deviceTokenFromArgv = process.argv.find(a => a.startsWith(DEVICE_TOKEN_ARG_PREFIX))
const deviceToken = deviceTokenFromArgv
  ? deviceTokenFromArgv.slice(DEVICE_TOKEN_ARG_PREFIX.length)
  : // env 只在 local 模式回退：cloud 模式恒为空串，别把本地凭据带进另一个分区
    mode === 'local'
    ? process.env.CHIRON_DEVICE_TOKEN || ''
    : ''

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
  /**
   * 本地设备 token（只读）：local 模式由主进程注入，cloud 模式恒为空串。
   * 页面在 local 模式下可以拿它做 `Authorization: Bearer`；也可以在空串时调
   * POST /v1/auth/session 从 core 换一个（第一批 auth disabled，两者都能用）。
   */
  deviceToken,
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

  /**
   * 本地模型配置（provider / 端点 / 模型名 / key）。
   *
   * 读回来的**永远不含明文 key**：只有 `{ active, providers: { <name>: { baseUrl, model, hasKey } } }`。
   * 明文 key 只往主进程方向流动（saveModelConfig），保存后就只剩 hasKey 一个布尔。
   * 三个动作统一 resolve `{ ok, message? }` 形态，不抛错给页面整段回显。
   */
  getModelConfig: () => ipcRenderer.invoke('desktop:get-model-config'),
  /** 保存（合并语义）：providers.<name>.apiKey 为空 = 保留已存的 key */
  saveModelConfig: config => ipcRenderer.invoke('desktop:save-model-config', config || {}),
  /** 重启本地后端，让新的 provider 配置生效（仅本地模式） */
  restartLocalBackend: () => ipcRenderer.invoke('desktop:restart-local-backend'),
})
