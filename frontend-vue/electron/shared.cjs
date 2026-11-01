/**
 * 主进程与 preload 共享的启动契约。
 *
 * 两边各写一份 `--chiron-api-url=` 前缀时，改一处就会让地址注入静默失效：
 * 主进程换了前缀、preload 还按旧前缀找，页面会静默回落到同源请求。
 * 模式前缀与分区名同理，所以都放在这里。
 */
const path = require('node:path')

/**
 * userData 根目录名。
 *
 * **不能再用 Electron 默认值**：默认值来自 `package.json` 的 `name`（`frontend-vue`），
 * 于是本地模式的全部数据落在 `%APPDATA%\frontend-vue\` —— 一个看不出是谁家的目录，
 * 用户清理 AppData 时既不敢删也找不到。这里钉死一个域名反写的可识别名字。
 *
 * 改名的代价是一次「看起来像丢了数据」：老目录不会被搬，本地模式从零开始
 * （两套 venv 重装、PG 重新 initdb）。契约 §1 明确不删 `userData/local/**`，
 * 所以老目录原样留在磁盘上，需要时可手工取回。
 */
const USER_DATA_DIR_NAME = 'gh_Athenavi_chiron'

/**
 * 本次进程该用的 userData 根。
 *
 * 优先级：`CHIRON_DESKTOP_USER_DATA`（真机 smoke / 多环境隔离 / 便携部署）> `appData` + 固定名。
 *
 * 抽成纯函数是因为 `main.cjs` 一 require 就会起 Electron，`app.setPath` 那一步没法在
 * 单测里跑；而这个判据错一次的后果是**两个目录各存一半**（config.json 在新根、
 * venv 在旧根），表现得像"运行时莫名其妙又要重装一遍"。
 *
 * @param {object} options `{ appData, override }`
 * @returns {string} 绝对路径；两者都拿不到时返回空串（调用方保持 Electron 默认值）
 */
function resolveUserDataDir(options = {}) {
  const override = typeof options.override === 'string' ? options.override.trim() : ''
  if (override) return override
  const appData = typeof options.appData === 'string' ? options.appData.trim() : ''
  if (!appData) return ''
  return path.join(appData, USER_DATA_DIR_NAME)
}

const API_URL_ARG_PREFIX = '--chiron-api-url='
/** 主进程创建窗口时用它把当前运行模式告诉 preload（契约 §2 的 window.chironDesktop.mode） */
const MODE_ARG_PREFIX = '--chiron-mode='
/** 主进程建窗时把本地 core 的设备 token 注入 preload（契约 §2 的 deviceToken） */
const DEVICE_TOKEN_ARG_PREFIX = '--chiron-device-token='
const DEFAULT_DEV_URL = 'http://localhost:5173'

/**
 * 双模式的会话分区（契约 §1/§3）：模式和 partition 一一对应，
 * 切模式时清的就是「离开模式」的那个 partition。
 * 前缀必须带 `persist:` —— 否则 session 只在内存里，重启又会串味。
 */
const PARTITION_PREFIX = 'persist:chiron-'

function partitionFor(mode) {
  return `${PARTITION_PREFIX}${mode === 'cloud' ? 'cloud' : 'local'}`
}

/**
 * 窗口全关时到底该不该退出进程。
 *
 * 看起来是"非 macOS 就退出"一行，但启动流程里有两个**没有窗口是正常状态**的窗口期：
 *
 *   - 首次启动：安装进度窗是当时唯一的窗口，装完 `destroy()` 掉、主窗口还没建出来；
 *   - 重建窗口：`relaunchIntoApp` / `relaunchIntoSetup` 先销毁旧窗再建新窗。
 *
 * 而 `BrowserWindow#destroy()` 会**同步**派发 window-all-closed（electron 45 实测确认，
 * 不是等下个 tick）。照直 quit 的话，app.quit() 在 destroy() 内部就排进队列，
 * 紧接着建好的主窗口没机会显示 —— 用户看到的就是"检查本地运行时就闪退"。
 *
 * 抽成纯函数放这里，是因为 main.cjs 一 require 就会起 Electron，单测没法直接覆盖它。
 */
function shouldQuitOnAllWindowsClosed({ platform, startupComplete, holdingWindows }) {
  // macOS 习惯：关窗口不退应用
  if (platform === 'darwin') return false
  // 启动期 / 窗口重建期的空窗口不是用户意图
  if (!startupComplete) return false
  if (holdingWindows > 0) return false
  return true
}

/**
 * 把 `dialog.showOpenDialog` 的结果翻译成 `pickDocument` 的返回值（契约 §2）。
 *
 * 抽成纯函数放这里，同样是因为 main.cjs 一 require 就会起 Electron：
 * 对话框的回调没法在单测里跑，但"取消 / 空选 / 多选"这几条分支必须被钉住。
 *
 * 三条约定：
 *   - 用户取消 → `null`，不是错误（调用方按"没选"处理）；
 *   - 多选也只取第一个 —— 契约的返回类型是单个 `{ path, name }`；
 *   - 不做扩展名过滤：本地抽取接受任意文件，限死类型只会让用户选不到自己的文档。
 */
function pickDocumentResult(result) {
  if (!result || result.canceled) return null
  const filePath = Array.isArray(result.filePaths) ? result.filePaths[0] : ''
  if (!filePath) return null
  return { path: filePath, name: path.basename(filePath) }
}

/**
 * 建窗分流：这次启动该加载哪个页面，以及把哪个地址交给 preload。
 *
 * 抽成纯函数放这里还是那条老理由 —— main.cjs 一 require 就会起 Electron，单测没法直接
 * 覆盖它；而这条判据恰好是「切到云模式后改不了服务地址」的全部因果：旧实现只看
 * `apiUrl` 空不空，config.json 里一个陈旧地址（上个时代的自托管端口、写错的域名）
 * 就足以把窗口送进主界面 —— 配置页永不出现，而主界面里当时没有任何改地址的入口：
 * 设置页在登录之后才进得去，后端不可达就登不了录。
 *
 * `pendingApiUrl` 由 mode-switch.cjs 在「切到云端模式」时置位、建窗时消费，含义是
 * 「这次进云端还没确认过服务地址」。切模式必然重启，所以标记要落盘才活得到新进程；
 * 但只用一次 —— 云端模式下正常重开应用不该再被拦到配置页。
 */
function resolveStartupWindow(options = {}) {
  const { mode, apiUrl, localBackend, pendingApiUrl, forceSetup } = options
  if (mode === 'cloud') {
    return {
      target: forceSetup || pendingApiUrl || !apiUrl ? 'setup' : 'app',
      apiUrl: apiUrl || '',
    }
  }
  const backend = localBackend || {}
  // 本地后端没就绪：落到通用错误页并写清楚原因，而不是留给用户一个空白窗口
  if (backend.ok && backend.baseUrl) return { target: 'app', apiUrl: backend.baseUrl }
  return { target: 'error', apiUrl: '' }
}

module.exports = {
  USER_DATA_DIR_NAME,
  resolveUserDataDir,
  API_URL_ARG_PREFIX,
  MODE_ARG_PREFIX,
  DEVICE_TOKEN_ARG_PREFIX,
  DEFAULT_DEV_URL,
  PARTITION_PREFIX,
  partitionFor,
  shouldQuitOnAllWindowsClosed,
  pickDocumentResult,
  resolveStartupWindow,
}
