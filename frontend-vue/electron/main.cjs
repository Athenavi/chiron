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
const { app, BrowserWindow, shell, Menu, ipcMain } = require('electron')
const path = require('node:path')
const { resolveApiUrl, saveApiUrl, probe } = require('./config.cjs')

/** 开发模式：`electron . --dev` 时连 Vite dev server，否则加载 dist/ */
const isDev = process.argv.includes('--dev')
const DEV_URL = 'http://localhost:5173'

/** 窗口底色取 --bg-page 的浅色值：主进程拿不到 CSS 变量，只能写字面值。 */
const WINDOW_BACKGROUND = '#f9f9f7'

let mainWindow = null

/** 「连接设置…」菜单强制进配置页（即使已有地址），见 relaunchIntoSetup */
let forceSetup = false

function createWindow() {
  const apiUrl = resolveApiUrl()
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
      additionalArguments: apiUrl ? [`--chiron-api-url=${apiUrl}`] : [],
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  // 外部链接交给系统浏览器：壳内不该出现第二个浏览器的地址栏
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
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
    ipcMain.handle('desktop:test-api-url', (_event, url) => probe(url))
    ipcMain.handle('desktop:set-api-url', (_event, url) => {
      saveApiUrl(url)
      relaunchIntoApp()
      return true
    })

    createWindow()

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
