/**
 * Chiron Desktop — Electron 主进程
 *
 * 形态：纯 Web 壳。客户端只承载已构建的前端（dist/），业务请求发往用户自己的
 * Chiron 服务；不打包 Go 后端、不依赖 Python（run.py 只是仓库内的编排脚本）。
 *
 * 为什么选 Electron 而不是 Tauri/Wails：本仓样式对现代 CSS 是硬依赖
 * （`color-mix` 86 处、`@container` 4 处、`backdrop-filter` 25 处），而
 * Tauri/Wails 在 Linux 上借用系统 WebKitGTK —— Ubuntu 22.04 的 2.36 不支持
 * color-mix，主题阴影会静默失效。Electron 自带 Chromium，开发环境所见即用户所见。
 *
 * 启动分流：
 *   环境变量 CHIRON_API_URL 或已保存的配置 → 直接进主界面
 *   两者都没有 → 进 setup.html 让用户填服务地址（首次启动）
 *
 * 安全基线：contextIsolation 开、nodeIntegration 关、preload 只暴露两个明确动作，
 * 外部链接一律交给系统浏览器。
 */
const { app, BrowserWindow, shell, Menu, ipcMain, session, dialog } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const { fileURLToPath } = require('node:url')
const { resolveApiUrl, saveApiUrl, probe } = require('./config.cjs')
const { API_URL_ARG_PREFIX, DEFAULT_DEV_URL } = require('./shared.cjs')
const { readToken, writeToken, clearToken } = require('./token-store.cjs')

/** 开发模式：`electron . --dev` 时连 Vite dev server，否则加载 dist/ */
const isDev = process.argv.includes('--dev')
const DEV_URL = process.env.CHIRON_DEV_URL || DEFAULT_DEV_URL

/** 窗口底色取 --bg-page 的浅色值：主进程拿不到 CSS 变量，只能写字面值。 */
const WINDOW_BACKGROUND = '#f9f9f7'

let mainWindow = null

/** 当前窗口使用的 API 地址；CORS 响应头只补给它，不碰其它站点 */
let currentApiUrl = ''

/** 「连接设置…」菜单强制进配置页（即使已有地址），见 relaunchIntoSetup */
let forceSetup = false

const APP_ID = 'com.chiron.agent'
const APP_ROOT = path.resolve(__dirname, '..')
const DEV_ORIGIN = (() => {
  try {
    return new URL(DEV_URL).origin
  } catch {
    return ''
  }
})()

/** 本地诊断日志：不依赖网络，崩溃/加载失败时至少能给用户一个文件 */
function logMain(level, message) {
  const line = `${new Date().toISOString()} [${level}] ${message}\n`
  try {
    const file = path.join(app.getPath('userData'), 'logs', 'main.log')
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.appendFileSync(file, line)
  } catch {
    /* 日志失败不能影响主流程 */
  }
  if (level === 'error') console.error(message)
}

/**
 * 允许的页面地址：本地 app 根目录下的 file://，或开发模式的 Vite origin。
 * 导航限制与 IPC sender 校验共用这一份判断，避免两处规则漂移。
 */
function isTrustedUrl(rawUrl) {
  try {
    const url = new URL(rawUrl)
    if (url.protocol === 'file:') {
      const filePath = path.resolve(fileURLToPath(url))
      return filePath === APP_ROOT || filePath.startsWith(APP_ROOT + path.sep)
    }
    return isDev && url.origin === DEV_ORIGIN
  } catch {
    return false
  }
}

/**
 * file:// 页面没有 CSP 时 Electron 会直接报安全警告；XSS 也能任意外连。
 * 只放行本地资源与用户自己配置的 API/WS origin，其余 connect 一律拒绝。
 */
function buildCsp() {
  const local = "'self' file:"
  const connect = ["'self'", 'file:']
  if (currentApiUrl) {
    try {
      const api = new URL(currentApiUrl)
      connect.push(api.origin)
      connect.push(`${api.protocol === 'https:' ? 'wss' : 'ws'}://${api.host}`)
    } catch {
      /* 地址已在 config.cjs 校验；这里失败就不额外放行 */
    }
  }
  return [
    `default-src ${local}`,
    `script-src ${local} 'wasm-unsafe-eval'`,
    `style-src ${local} 'unsafe-inline'`,
    `img-src ${local} data: blob:`,
    `media-src ${local} data: blob:`,
    `font-src ${local} data:`,
    `connect-src ${connect.join(' ')}`,
    `worker-src ${local} blob:`,
    `child-src ${local} blob:`,
    `frame-src ${local}`,
    "object-src 'none'",
    "base-uri 'none'",
    `form-action ${local}`,
  ].join('; ')
}

/** 主文档加载失败时切到本地错误页，而不是留一个空白窗口 */
function showLoadError(code, description, failedUrl) {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (code === -3) return // ERR_ABORTED：用户主动取消导航
  if (failedUrl.includes('/error.html')) return
  logMain('error', `load failed (${code}) ${description} ${failedUrl}`)
  mainWindow
    .loadFile(path.join(__dirname, 'error.html'), {
      query: {
        code: String(code),
        desc: description || '',
        url: failedUrl || '',
        retry: failedUrl || '',
      },
    })
    .catch(err => logMain('error', `error page load failed: ${err.message}`))
}

process.on('uncaughtException', err => logMain('error', `uncaughtException: ${err?.stack || err}`))
process.on('unhandledRejection', reason => logMain('error', `unhandledRejection: ${reason}`))

/** IPC 只接受来自应用自身页面（或开发服务器）的调用 */
function assertTrustedSender(event) {
  const frameUrl = event.senderFrame?.url || event.sender.getURL()
  if (!isTrustedUrl(frameUrl)) {
    logMain('warn', `blocked IPC from ${frameUrl}`)
    throw new Error('forbidden')
  }
}

function createWindow() {
  const apiUrl = resolveApiUrl()
  currentApiUrl = apiUrl
  const target = !forceSetup && apiUrl ? 'app' : 'setup'

  mainWindow = new BrowserWindow({
    width: target === 'setup' ? 520 : 1440,
    height: target === 'setup' ? 620 : 900,
    minWidth: target === 'setup' ? 420 : 960,
    minHeight: target === 'setup' ? 480 : 640,
    resizable: target !== 'setup',
    backgroundColor: WINDOW_BACKGROUND,
    show: false, // 等首帧就绪再显示，避免白屏闪一下
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      // preload 要读 process.argv / versions，故不开 sandbox；
      // contextIsolation 已经隔离了页面与 preload 的执行上下文。
      sandbox: false,
      // 每次创建窗口现传地址：渲染进程的 process.env 是启动时继承的，
      // 用户保存新地址后旧 env 不会更新。
      additionalArguments: apiUrl ? [`${API_URL_ARG_PREFIX}${apiUrl}`] : [],
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  // 外部链接交给系统浏览器：壳内不该出现第二个浏览器的地址栏
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })

  // 同窗口导航限制：只允许本地 app 根目录 / 开发服务器；外部 http(s) 交系统浏览器
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (isTrustedUrl(url)) return
    event.preventDefault()
    if (/^https?:/i.test(url)) shell.openExternal(url)
    else logMain('warn', `blocked navigation: ${url}`)
  })

  // 主文档加载失败：切到本地错误页，而不是留一个空白窗口
  mainWindow.webContents.on('did-fail-load', (_event, code, description, validatedURL, isMainFrame) => {
    if (!isMainFrame) return
    showLoadError(code, description, validatedURL)
  })

  // 渲染进程崩溃：记录并给用户选择重载
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    logMain('error', `renderer gone: ${details.reason} exitCode=${details.exitCode}`)
    if (details.reason === 'clean-exit' || !mainWindow || mainWindow.isDestroyed()) return
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'error',
      title: '页面进程已退出',
      message: `界面进程异常退出（${details.reason}）。`,
      buttons: ['重新加载', '关闭'],
      defaultId: 0,
      cancelId: 1,
    })
    if (choice === 0) mainWindow.reload()
  })

  // 界面长时间无响应：记录并让用户决定是否重载
  mainWindow.on('unresponsive', () => {
    logMain('warn', 'window unresponsive')
    if (!mainWindow || mainWindow.isDestroyed()) return
    const choice = dialog.showMessageBoxSync(mainWindow, {
      type: 'warning',
      title: '界面无响应',
      message: '界面暂时没有响应，是否重新加载？',
      buttons: ['继续等待', '重新加载'],
      defaultId: 0,
      cancelId: 1,
    })
    if (choice === 1) mainWindow.reload()
  })

  if (target === 'setup') {
    mainWindow.loadFile(path.join(__dirname, 'setup.html'))
  } else if (isDev) {
    mainWindow.loadURL(DEV_URL)
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

/** 保存地址后重建窗口：尺寸、preload 注入的地址、加载目标都要换 */
function relaunchIntoApp() {
  const old = mainWindow
  mainWindow = null
  if (old) old.destroy()
  createWindow()
}

/**
 * 从「连接设置…」菜单进入配置页。
 * 走"重建窗口"而不是在同窗口 loadFile：配置页是 520×620 的单栏布局，
 * 塞进 1440 宽的主窗口会显得空荡；重建一次反而干净（代价是主界面重载，可接受）。
 */
function relaunchIntoSetup() {
  const old = mainWindow
  mainWindow = null
  if (old) old.destroy()
  // createWindow 会重新读配置：此时若已有地址，会直接进主界面 —— 所以这里
  // 先把窗口配置成"强制 setup"模式，避免用户点了"连接设置"却又弹回主界面。
  forceSetup = true
  createWindow()
  forceSetup = false
}

/**
 * 打包后才检查更新：开发环境没有 app-update.yml，也没有签名产物。
 * 更新源在 electron-builder.yml 的 publish 段（GitHub Releases）。
 */
function setupAutoUpdater() {
  if (!app.isPackaged) return
  let autoUpdater
  try {
    ;({ autoUpdater } = require('electron-updater'))
  } catch (err) {
    logMain('warn', `electron-updater unavailable: ${err.message}`)
    return
  }
  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = true
  autoUpdater.on('error', err => logMain('warn', `update error: ${err?.message || err}`))
  autoUpdater.on('update-available', info => logMain('info', `update available: ${info?.version || ''}`))
  autoUpdater.on('update-not-available', () => logMain('info', 'update not available'))
  autoUpdater.on('update-downloaded', info => {
    logMain('info', `update downloaded: ${info?.version || ''}`)
    void dialog
      .showMessageBox({
        type: 'info',
        title: '更新已下载',
        message: `Chiron ${info?.version || '新版本'} 已准备好，重启后生效。`,
        buttons: ['立即重启', '稍后'],
        defaultId: 0,
        cancelId: 1,
      })
      .then(({ response }) => {
        if (response === 0) autoUpdater.quitAndInstall()
      })
  })
  // 延迟到首屏之后再检查，避免和首页资源抢带宽
  setTimeout(() => {
    autoUpdater.checkForUpdatesAndNotify().catch(err => {
      logMain('warn', `update check failed: ${err?.message || err}`)
    })
  }, 8000)
}

// 单实例：第二次启动时聚焦已有窗口，而不是再开一个进程
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.focus()
  })

  app.whenReady().then(() => {
    // Windows 通知/任务栏需要显式的 AppUserModelId，且要与打包 appId 一致
    if (process.platform === 'win32') app.setAppUserModelId(APP_ID)

    // 桌面壳从 file:// 访问 API：浏览器把 Origin 序列化成 null，后端 CORS 白名单
    // 不会为 null 返回 Access-Control-Allow-Origin，带 Authorization 的预检会被
    // 浏览器直接拒绝（表现为登录后所有请求失败）。这里只给用户配置的 API 前缀
    // 补上精确匹配的 null（不是 *），其它站点响应不受影响。
    session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
      const responseHeaders = details.responseHeaders || {}
      const isApiRequest =
        currentApiUrl &&
        (details.url === currentApiUrl || details.url.startsWith(`${currentApiUrl}/`))
      if (isApiRequest) {
        responseHeaders['Access-Control-Allow-Origin'] = ['null']
      }
      // file:// 主文档默认没有 CSP，Electron 会直接报安全警告；只放行本地资源
      // 与用户配置的 API/WS origin，XSS 无法把数据外传到任意站点。
      const isMainDocument =
        details.resourceType === 'mainFrame' ||
        details.url.split('#')[0].split('?')[0].endsWith('/index.html')
      if (isMainDocument && details.url.startsWith('file:')) {
        responseHeaders['Content-Security-Policy'] = [buildCsp()]
      }
      callback({ responseHeaders })
    })

    // 权限：只允许本地应用页面使用麦克风（语音输入）、剪贴板读取与全屏
    const allowedPermissions = new Set(['media', 'clipboard-read', 'fullscreen'])
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
      const requestingUrl = details?.requestingUrl || wc.getURL()
      if (!isTrustedUrl(requestingUrl) || !allowedPermissions.has(permission)) {
        logMain('warn', `denied permission ${permission} from ${requestingUrl}`)
        callback(false)
        return
      }
      if (permission === 'media') {
        const types = details?.mediaTypes || []
        callback(types.length === 0 || types.every(type => type === 'audio'))
        return
      }
      callback(true)
    })
    session.defaultSession.setPermissionCheckHandler((wc, permission, requestingOrigin, details) => {
      const requestingUrl = details?.requestingUrl || wc.getURL() || requestingOrigin
      if (!isTrustedUrl(requestingUrl) || !allowedPermissions.has(permission)) return false
      if (permission === 'media' && details?.mediaType === 'video') return false
      return true
    })

    // 下载走 Electron 默认保存流程，记录到本地日志便于排查
    session.defaultSession.on('will-download', (_event, item) => {
      logMain('info', `download start: ${item.getFilename()} ${item.getURL()}`)
      item.once('done', (_e, state) => {
        logMain(
          state === 'completed' ? 'info' : 'warn',
          `download ${state}: ${item.getFilename()} -> ${item.getSavePath()}`,
        )
      })
    })

    // 精简菜单：保留复制/粘贴/全选/重载/开发者工具，去掉 Electron 默认的多余项
    Menu.setApplicationMenu(
      Menu.buildFromTemplate([
        {
          label: app.name,
          submenu: [
            { role: 'about' },
            { type: 'separator' },
            { role: 'hide' },
            { role: 'hideOthers' },
            { role: 'unhide' },
            { type: 'separator' },
            { role: 'quit' },
          ],
        },
        {
          label: '编辑',
          submenu: [
            { role: 'undo' },
            { role: 'redo' },
            { type: 'separator' },
            { role: 'cut' },
            { role: 'copy' },
            { role: 'paste' },
            { role: 'selectAll' },
          ],
        },
        {
          label: '视图',
          submenu: [
            { role: 'reload' },
            { role: 'forceReload' },
            { role: 'toggleDevTools' },
            { type: 'separator' },
            { role: 'resetZoom' },
            { role: 'zoomIn' },
            { role: 'zoomOut' },
            { type: 'separator' },
            { role: 'togglefullscreen' },
            { type: 'separator' },
            // 应用内修改服务地址的唯一入口：填错地址后不必再去删 config.json
            { label: '连接设置…', accelerator: 'CmdOrCtrl+,', click: () => relaunchIntoSetup() },
          ],
        },
      ]),
    )

    // 配置页的两个动作。地址校验与持久化都在 config.cjs 里，主进程只做转发。
    // 同时校验 sender：只有应用自身页面（或开发服务器）能改服务地址。
    ipcMain.handle('desktop:test-api-url', (event, url) => {
      assertTrustedSender(event)
      return probe(url)
    })
    ipcMain.handle('desktop:set-api-url', (event, url) => {
      assertTrustedSender(event)
      saveApiUrl(url)
      relaunchIntoApp()
      return true
    })

    // token：异步读取（safeStorage 的 async API），保存/清除走 invoke；全部校验 sender。
    ipcMain.handle('desktop:get-token', async event => {
      assertTrustedSender(event)
      return readToken()
    })
    ipcMain.handle('desktop:save-token', (event, token) => {
      assertTrustedSender(event)
      return writeToken(String(token || ''))
    })
    ipcMain.handle('desktop:clear-token', event => {
      assertTrustedSender(event)
      return clearToken()
    })

    createWindow()
    setupAutoUpdater()

    // macOS：点 Dock 图标且没有窗口时重开
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  // macOS 习惯：关掉窗口不退出应用
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit()
  })
}
