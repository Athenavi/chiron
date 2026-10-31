/**
 * 主进程与 preload 共享的启动契约。
 *
 * 两边各写一份 `--chiron-api-url=` 前缀时，改一处就会让地址注入静默失效：
 * 主进程换了前缀、preload 还按旧前缀找，页面会静默回落到同源请求。
 * 模式前缀与分区名同理，所以都放在这里。
 */
const path = require('node:path')

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

module.exports = {
  API_URL_ARG_PREFIX,
  MODE_ARG_PREFIX,
  DEVICE_TOKEN_ARG_PREFIX,
  DEFAULT_DEV_URL,
  PARTITION_PREFIX,
  partitionFor,
  shouldQuitOnAllWindowsClosed,
  pickDocumentResult,
}
