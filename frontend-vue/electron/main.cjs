/**
 * Chiron Desktop — Electron 主进程
 *
 * 形态：双模式桌面壳。渲染层始终是已构建的前端（dist/），后端按模式分流：
 *   local —— 本机后端（embedded-postgres + 本地 API，由 components.cjs 负责拉起）
 *   cloud —— 用户自己的 Chiron 服务（原来的纯 Web 壳行为）
 * 契约见 docs/desktop-local/contracts.md：config.json 是模式真相，两个模式各用一个
 * Electron session partition，切换模式时清掉离开模式的会话数据并重启。
 *
 * 为什么选 Electron 而不是 Tauri/Wails：本仓样式对现代 CSS 是硬依赖
 * （`color-mix` 86 处、`@container` 4 处、`backdrop-filter` 25 处），而
 * Tauri/Wails 在 Linux 上借用系统 WebKitGTK —— Ubuntu 22.04 的 2.36 不支持
 * color-mix，主题阴影会静默失效。Electron 自带 Chromium，开发环境所见即用户所见。
 *
 * 启动分流：
 *   local 且本地后端拉起成功 → 主界面
 *   local 且本地后端未安装/启动失败 → 给重试 / 切回云端 / 退出三个选择，仍打不开则落到 error.html
 *   cloud 且有地址（env / config.json） → 主界面
 *   cloud 且没有地址 → setup.html 让用户填服务地址
 *   老配置（有 apiUrl 但没有 mode） → 先弹一次迁移提示，默认本地，apiUrl 保留
 *
 * 安全基线：contextIsolation 开、nodeIntegration 关、preload 只暴露明确的几个动作，
 * 外部链接一律交给系统浏览器。
 */
const { app, BrowserWindow, shell, Menu, ipcMain, session, dialog } = require('electron')
const fs = require('node:fs')
const crypto = require('node:crypto')
const path = require('node:path')
const { fileURLToPath } = require('node:url')
const {
  resolveApiUrl,
  resolveMode,
  readDeviceSettings,
  saveApiUrl,
  saveDeviceSettings,
  saveMode,
  probe,
} = require('./config.cjs')
const shared = require('./shared.cjs')
const {
  API_URL_ARG_PREFIX,
  MODE_ARG_PREFIX,
  DEFAULT_DEV_URL,
  partitionFor,
  shouldQuitOnAllWindowsClosed,
} = shared
/**
 * 本地设备 token 的注入前缀（`--chiron-device-token=<token>`）。
 *
 * 首选 shared.cjs 里的同名常量（契约 §3.1 要求共享常量集中在那里），本 worktree 没有
 * 改 shared.cjs 的授权，所以缺失时用这里的兜底字面量 —— preload.cjs 用同一套写法，
 * 两边只要有一处把常量搬到 shared.cjs 就会同时生效。
 */
const DEVICE_TOKEN_ARG_PREFIX = shared.DEVICE_TOKEN_ARG_PREFIX || '--chiron-device-token='
const { readToken, writeToken, clearToken } = require('./token-store.cjs')
const { switchMode } = require('./mode-switch.cjs')

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

/** 本次运行的运行模式（whenReady 时定一次；切换模式一律走重启） */
let currentMode = 'local'

/**
 * 本地后端状态：{ ok, baseUrl, token, dataDir, reason, message }，由 startLocalBackend 填充。
 * token 是 core 的设备 token（components.startCore 的返回值），只注入给 preload，
 * 不写日志 —— 日志会随用户反馈外传。
 */
let localBackend = { ok: false, baseUrl: '', token: '', dataDir: '', reason: 'not-started', message: '' }

/** 已经注册过 webRequest / 权限 / 下载监听的 session（重建窗口时不能重复注册） */
const configuredSessions = new WeakSet()

/** 本地后端只停一次：before-quit 与模式切换都会触发停止 */
let localBackendStopped = false

/**
 * 启动流程（whenReady 里那串 await）是否已经走到建主窗口这一步。
 *
 * 存在的唯一原因：首次启动时安装进度窗是**当时唯一的窗口**，而 `destroy()`
 * 会**同步**派发 window-all-closed（electron 45 实测确认，不是"下个 tick 才发"）。
 * 不在启动期间拦一下，closeInstallWindow() 会立刻把 app.quit() 排进队列，
 * 紧接着建好的主窗口还没显示，进程就退了 —— 用户看到的就是
 * "检查本地运行时就闪退"。
 */
let startupComplete = false

/**
 * 正在做的"先销毁旧窗口、再建新窗口"的同步重建次数。
 *
 * relaunchIntoApp / relaunchIntoSetup 都是 `destroy()` 紧接着 `createWindow()`，
 * 和启动期同一个坑：destroy 同步触发 window-all-closed。计数 >0 期间不允许因此退出。
 */
let holdingWindows = 0

/** 把"销毁 + 重建"包成原子操作：中间那一下空窗口不触发退出 */
function holdingWindowQuota(fn) {
  holdingWindows += 1
  try {
    return fn()
  } finally {
    holdingWindows -= 1
  }
}

/**
 * 首次启动安装进度窗口。安装发生在 createWindow 之前，没有窗口就没有任何东西
 * 能显示进度 —— 用户只会看到"点了图标没反应"。装完立刻关掉，主界面照旧走 createWindow。
 */
let installWindow = null

/** 安装进度推送通道名，必须与 preload.cjs 的订阅名一致 */
const RUNTIME_STAGE_CHANNEL = 'desktop:runtime-stage'

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

/**
 * 可选模块接线：components.cjs / secrets.cjs / inference-proxy.cjs 由另一个
 * worktree 实现并按需发布。本文件只 require + try/catch 接线，绝不在这里写它们的实现，
 * 模块不存在时安静降级（未安装本地组件是用户的正常状态）。
 */
const optionalModules = new Map()
function loadOptionalModule(name) {
  if (optionalModules.has(name)) return optionalModules.get(name)
  let mod = null
  try {
    mod = require(name)
  } catch (err) {
    logMain('info', `optional module ${name} unavailable: ${err?.message || err}`)
  }
  optionalModules.set(name, mod)
  return mod
}

/**
 * 启动本地后端（mode=local）。
 *
 * 启动回调由 W4 的 components.cjs 提供；这里全部走可选探测 —— 模块缺失、接口缺失、
 * 启动抛错都要变成一条可读状态再显示本地错误页，而不是让主进程崩掉。
 */
async function tryStartGoStack(options = {}) {
  const stack = loadOptionalModule('./local-stack.cjs')
  const components = loadOptionalModule('./components.cjs')
  if (!stack || !components) return null

  const gatewayExe = resolveGatewayExe()
  if (!gatewayExe) {
    logMain('info', '本地网关不可用（未找到 chiron.exe），退回 desktop-local core')
    return null
  }

  let launch
  try {
    launch = components.resolveCoreLaunch()
  } catch (err) {
    // 运行时没装：这是首启的正常状态，交给旧路径去触发安装流程
    logMain('info', `引导器运行时不可用：${err?.message || err}`)
    return null
  }

  try {
    const dataDir = options.dataDir
      ? String(options.dataDir)
      : path.join(app.getPath('userData'), 'local')
    const port = Number(options.gatewayPort) > 0 ? Number(options.gatewayPort) : await components.pickFreePort()

    const started = await stack.startLocalStack({
      launcher: { command: launch.command, cwd: launch.cwd },
      dataDir,
      exe: gatewayExe,
      port,
      appSecret: ensureAppSecret(dataDir),
    })

    logMain(
      'info',
      `本地网关就绪 ${started.baseUrl}（pg=${started.launcher?.pgBackend} redis=${started.launcher?.redisBackend}）`,
    )
    return {
      ok: true,
      baseUrl: started.baseUrl,
      token: '',
      dataDir,
      reason: '',
      message: '',
    }
  } catch (err) {
    const message = err?.message || String(err)
    logMain('error', `本地网关启动失败：${message}`)
    // 返回 null 让旧路径再试一次；旧路径也会失败，但错误信息更贴近既有 UI
    return null
  }
}

/**
 * 找 Go 网关可执行文件。
 *
 * 打包态在 `resources/chiron.exe`（见 electron-builder.yml 的 extraResources），
 * 开发态在仓库根。找不到就返回空串，由调用方退回旧路径。
 */
function resolveGatewayExe() {
  const exeName = process.platform === 'win32' ? 'chiron.exe' : 'chiron'
  const candidates = []
  if (process.resourcesPath) candidates.push(path.join(process.resourcesPath, exeName))
  candidates.push(path.join(__dirname, '..', '..', exeName))
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate
  }
  return ''
}

/**
 * 取（或首次生成）网关的 APP_SECRET。
 *
 * 必须**跨重启稳定**：JWT_SECRET 与 INTERNAL_TOKEN 都由它派生，每次启动换新密钥
 * 会让所有已登录会话失效、用户被反复踢下线。
 * 长度 32 是网关的硬要求（`config/config.go` 强制），生成的 hex 有 64 字符。
 */
function ensureAppSecret(dataDir) {
  const file = path.join(dataDir, 'auth', 'app-secret')
  try {
    const existing = fs.readFileSync(file, 'utf8').trim()
    if (existing.length >= 32) return existing
  } catch {
    /* 不存在则生成 */
  }
  const secret = crypto.randomBytes(32).toString('hex')
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, secret, { mode: 0o600 })
  } catch (err) {
    logMain('warn', `无法持久化 APP_SECRET：${err?.message || err}`)
  }
  return secret
}

async function startLocalBackend(options = {}) {
  // 显式守卫：云端模式下**一律不启动**本地 core（以及它拉起的 PG / redis）。
  // 调用点目前都在 `currentMode === 'local'` 分支里，但这条不变量不能只靠调用方自律 ——
  // 以后新增一处调用就可能让云端用户白起一套数据库。放在这里，误用会立刻变成可读状态。
  if (currentMode !== 'local') {
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: 'not-local',
      message: `当前是${currentMode === 'cloud' ? '云端' : currentMode}模式，不启动本地后端`,
    }
  }
  const components = loadOptionalModule('./components.cjs')
  if (!components) {
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: 'components-missing',
      message: '本地组件未随此版本安装',
    }
  }
  // 优先走"引导器 → Go 网关"这条链：本地模式的业务端点（skills/graphs/kb/workflows…）
  // 全在 Go 网关里，desktop-local core 只在网关不可用时作为回退。
  // 返回 null 表示不适用（无网关二进制 / 运行时未装），此时继续走下面的旧路径。
  try {
    const goStack = await tryStartGoStack(options)
    if (goStack && goStack.ok) return goStack
  } catch (err) {
    logMain('warn', `本地网关链路不可用：${err?.message || err}`)
  }

  const start =
    components.startLocalBackend || components.ensureLocalBackend || components.startCore
  if (typeof start !== 'function') {
    return {
      ok: false,
      baseUrl: '',
      token: '',
      dataDir: '',
      reason: 'api-missing',
      message: '本地组件模块没有提供启动接口',
    }
  }
  try {
    const started = await start(options)
    const baseUrl =
      typeof started === 'string' ? started : started?.baseUrl || started?.apiUrl || ''
    const dataDir = (started && typeof started === 'object' && started.dataDir) || ''
    // core 的握手结果里有设备 token（契约 §3.1 的 CHIRON_DEVICE_TOKEN 一侧）；
    // 拿不到就让 token 为空串，preload 会暴露空串，页面据此走 /v1/auth/session。
    const token = (started && typeof started === 'object' && started.token) || ''
    if (!baseUrl) {
      return { ok: false, baseUrl: '', token, dataDir, reason: 'no-url', message: '本地后端未返回服务地址' }
    }
    // 只记地址与"有没有 token"，token 本身绝不进日志
    logMain('info', `local backend ready at ${baseUrl} (device token: ${token ? 'injected' : 'absent'})`)
    return { ok: true, baseUrl, token, dataDir, reason: '', message: '' }
  } catch (err) {
    const message = err?.message || String(err)
    logMain('error', `local backend start failed: ${message}`)
    return { ok: false, baseUrl: '', token: '', dataDir: '', reason: 'start-failed', message }
  }
}

/**
 * 建安装进度窗口。
 *
 * 为什么单独建一个 520×620 的窗口而不是复用主窗口：安装发生在 createWindow 之前，
 * 那时主窗口还不存在；而安装一旦开始就是几分钟的网络下载，没有反馈会被当成卡死。
 * 装完立刻销毁，主界面完全不感知它的存在。
 */
function openInstallWindow() {
  if (installWindow && !installWindow.isDestroyed()) return installWindow
  installWindow = new BrowserWindow({
    width: 520,
    height: 620,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    backgroundColor: WINDOW_BACKGROUND,
    show: false,
    title: 'Chiron — 正在准备本地环境',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      partition: partitionFor('local'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 进度页也用 preload 的订阅能力；模式按 local 注入，与它实际服务的东西一致
      additionalArguments: [`${MODE_ARG_PREFIX}local`],
    },
  })
  installWindow.once('ready-to-show', () => {
    if (installWindow && !installWindow.isDestroyed()) installWindow.show()
  })
  installWindow.on('closed', () => {
    installWindow = null
  })
  installWindow.loadFile(path.join(__dirname, 'install.html')).catch(err => {
    logMain('warn', `install page load failed: ${err?.message || err}`)
  })
  return installWindow
}

/** 关掉进度窗口。安装结束（成功或失败）必须调，否则它会一直盖在屏幕上 */
function closeInstallWindow() {
  const win = installWindow
  installWindow = null
  if (win && !win.isDestroyed()) win.destroy()
}

/**
 * 把安装阶段推给进度窗口。
 * 窗口不在就丢弃：安装也可能由"重启本地后端"触发，那时用户已经在主界面里了。
 */
function broadcastRuntimeStage(info) {
  const win = installWindow
  if (!win || win.isDestroyed()) return
  try {
    win.webContents.send(RUNTIME_STAGE_CHANNEL, info)
  } catch {
    /* 窗口正在销毁：进度丢了不影响安装本身 */
  }
}

/**
 * 只读询问：本地运行时现在能不能直接用。
 * 内部任何失败都当作"就绪"，让启动照原路走 —— 进度窗只是个提示，不该成为新的失败点。
 */
function inspectLocalRuntime() {
  const components = loadOptionalModule('./components.cjs')
  if (!components || typeof components.inspectLocalRuntime !== 'function') {
    return { ready: true, reason: 'unknown', message: '' }
  }
  try {
    const result = components.inspectLocalRuntime()
    return result && typeof result === 'object' ? result : { ready: true, reason: 'unknown', message: '' }
  } catch (err) {
    logMain('warn', `runtime inspect failed: ${err?.message || err}`)
    return { ready: true, reason: 'unknown', message: '' }
  }
}

/** 停止本地后端：退出前与切换模式时都要调；没接线就当作已经停了 */async function stopLocalBackend() {
  if (localBackendStopped) return false
  localBackendStopped = true
  const components = loadOptionalModule('./components.cjs')
  const stop = components && (components.stopLocalBackend || components.stopCore)
  if (typeof stop !== 'function') return false
  try {
    await stop()
    logMain('info', 'local backend stopped')
    return true
  } catch (err) {
    logMain('warn', `local backend stop failed: ${err?.message || err}`)
    return false
  }
}

/**
 * 窗口 session 的通用配置：CORS/CSP 补头、权限、下载日志。
 *
 * 双模式各用一个 partition（persist:chiron-local / -cloud），所以这些监听必须挂在
 * 窗口真正使用的 session 上，不能再写死 session.defaultSession —— 否则补头会落在
 * 没人用的默认 session 上，本地与云端界面都会缺 CSP / CORS。
 */
function configureSession(partition) {
  const winSession = session.fromPartition(partition)
  if (configuredSessions.has(winSession)) return winSession
  configuredSessions.add(winSession)

  // 桌面壳从 file:// 访问 API：浏览器把 Origin 序列化成 null，后端 CORS 白名单
  // 不会为 null 返回 Access-Control-Allow-Origin，带 Authorization 的预检会被
  // 浏览器直接拒绝（表现为登录后所有请求失败）。这里只给用户配置的 API 前缀
  // 补上精确匹配的 null（不是 *），其它站点响应不受影响。
  winSession.webRequest.onHeadersReceived((details, callback) => {
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
  winSession.setPermissionRequestHandler((wc, permission, callback, details) => {
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
  winSession.setPermissionCheckHandler((wc, permission, requestingOrigin, details) => {
    const requestingUrl = details?.requestingUrl || wc.getURL() || requestingOrigin
    if (!isTrustedUrl(requestingUrl) || !allowedPermissions.has(permission)) return false
    if (permission === 'media' && details?.mediaType === 'video') return false
    return true
  })

  // 下载走 Electron 默认保存流程，记录到本地日志便于排查
  winSession.on('will-download', (_event, item) => {
    logMain('info', `download start: ${item.getFilename()} ${item.getURL()}`)
    item.once('done', (_e, state) => {
      logMain(
        state === 'completed' ? 'info' : 'warn',
        `download ${state}: ${item.getFilename()} -> ${item.getSavePath()}`,
      )
    })
  })

  return winSession
}

/**
 * 老配置迁移（契约 §1）：磁盘上有 apiUrl 但没有 mode。
 * 默认本地，选择只写 mode 与 migration —— apiUrl 原样保留，用户切回云端时不用重填。
 */
async function askLegacyMode(settings) {
  if (!settings.needsMigration) return false
  const detail = settings.migration.legacyCloudDetected
    ? `检测到上一次使用的云端服务地址：${settings.apiUrl}\n选择云端模式会继续使用它；选择本地模式也不会删除这个地址。`
    : '检测到上一版本的配置，但没有记录运行模式。请选择这台机器的运行方式。'
  try {
    const { response } = await dialog.showMessageBox({
      type: 'question',
      title: '选择运行模式',
      message: '这是第一次以双模式启动 Chiron',
      detail,
      buttons: ['使用本地模式', '继续使用云端模式'],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    })
    const mode = response === 1 ? 'cloud' : 'local'
    saveMode(mode)
    logMain('info', `legacy config migrated to ${mode}`)
    return true
  } catch (err) {
    // 弹不出来就别写盘：下次启动再问一次，也好过替用户瞎选
    logMain('warn', `legacy migration dialog failed: ${err?.message || err}`)
    return false
  }
}

/** 菜单里的模式切换：默认保留本地数据（契约 §1 规定默认不删 userData/local） */
async function confirmModeSwitch(nextMode) {
  const label = nextMode === 'local' ? '本地模式' : '云端模式'
  const detail =
    nextMode === 'local'
      ? '应用会重启并尝试启动本机后端；本地组件未安装时会显示提示页。本地数据目录不会被删除。'
      : '应用会重启，并清空本地模式下的登录状态与缓存。本地数据目录不会被删除。'
  const { response } = await dialog.showMessageBox({
    type: 'question',
    title: '切换运行模式',
    message: `切换到${label}？`,
    detail,
    buttons: ['切换并重启', '取消'],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  })
  if (response !== 0) return
  try {
    await applyModeSwitch(nextMode, {})
  } catch (err) {
    dialog.showErrorBox('切换运行模式失败', err?.message || String(err))
  }
}

/** 记下最近一次本地后端的启动信息（契约 §1 的 local.lastStartedAt / local.dataDir） */
function recordLocalStart(status) {
  if (!status || !status.ok) return
  try {
    const patch = { local: { lastStartedAt: new Date().toISOString() } }
    if (status.dataDir) patch.local.dataDir = String(status.dataDir)
    saveDeviceSettings(patch)
  } catch (err) {
    logMain('warn', `record local start failed: ${err?.message || err}`)
  }
}

/**
 * 切换模式：清零离开模式的会话数据 → 写盘 → relaunch。
 * 实际流程在 mode-switch.cjs（可单测），这里只负责注入 Electron 侧的回调：
 * 停止本地后端是必须的，否则重启后两个后端会抢同一个数据目录。
 */
async function applyModeSwitch(mode, opts) {
  const result = await switchMode({
    nextMode: mode,
    deleteLocalData: Boolean(opts && opts.deleteLocalData),
    stopBackend: async () => {
      if (currentMode === 'local') {
        await stopLocalBackend()
        localBackendStopped = true
      }
    },
    electron: { app, session },
    log: logMain,
  })
  return result
}

/** 重启应用（不改模式）：本地后端先停，避免 restart 后两个后端抢端口 */
function restartNow() {
  void (async () => {
    try {
      if (currentMode === 'local') {
        await stopLocalBackend()
        localBackendStopped = true
      }
    } catch (err) {
      logMain('warn', `stop before restart failed: ${err?.message || err}`)
    }
    app.relaunch()
    // 让 IPC 回复先发出，再杀进程，否则渲染进程只会看到 "frame was disposed"
    setImmediate(() => app.exit(0))
  })()
}

/**
 * 本地模式启动失败时的恢复选项。
 * 用原生对话框而不是自建页面：这时前端 dist 还没被信任能跑（后端可能压根没起），
 * 而用户需要的是三个明确动作：重试 / 切回云端 / 退出，不要静默卡在白屏上。
 */
async function askLocalBackendRecovery(status) {
  const actions = ['retry', 'cloud', 'quit']
  try {
    const { response } = await dialog.showMessageBox({
      type: 'warning',
      title: '本地模式未就绪',
      message: '本机后端没能启动，Chiron 打不开主界面',
      detail: `${status.message || '未知原因'}\n\n可以重试一次，或先切回云端模式（应用会重启）。`,
      buttons: ['重试启动', '切换到云端模式', '退出'],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    })
    return actions[response] || 'quit'
  } catch (err) {
    logMain('warn', `local recovery dialog failed: ${err?.message || err}`)
    return 'quit'
  }
}

/**
 * 建窗口：模式决定加载目标与 session 分区。
 *   local 且本地后端就绪 → 主界面（apiUrl = 本地 baseUrl）
 *   local 且未就绪 → 先给恢复选项，仍失败则落到 error.html（提示原因，不崩溃）
 *   cloud 且已有地址 → 主界面；没有地址 → setup.html 填写服务地址
 */
function createWindow() {
  const settings = readDeviceSettings()
  currentMode = resolveMode(settings)
  const partition = partitionFor(currentMode)

  let apiUrl = ''
  let target = 'app'
  if (currentMode === 'cloud') {
    apiUrl = resolveApiUrl()
    target = !forceSetup && apiUrl ? 'app' : 'setup'
  } else if (localBackend.ok && localBackend.baseUrl) {
    apiUrl = localBackend.baseUrl
  } else {
    // 本地后端没就绪：落到通用错误页并写清楚原因，而不是留给用户一个空白窗口
    target = 'error'
  }
  currentApiUrl = apiUrl
  // 会话监听必须在建窗之前挂好：窗口一创建就开始发首次请求
  configureSession(partition)

  // 配置页与本机错误页都是单栏布局，主界面才用大窗口
  const isDialog = target !== 'app'

  // 每次创建窗口现传模式 / 地址 / token：渲染进程的 process.env 是启动时继承的，
  // 用户保存新地址后旧 env 不会更新。
  const additionalArguments = [`${MODE_ARG_PREFIX}${currentMode}`]
  if (apiUrl) additionalArguments.push(`${API_URL_ARG_PREFIX}${apiUrl}`)
  // 本地模式的设备 token 由 core 的握手结果提供（components.startCore 的 token）：
  // 只有 local 且确实拿到 token 才注入，cloud 一律不注入，避免把本地凭据带进云端分区。
  if (currentMode === 'local' && localBackend.token) {
    additionalArguments.push(`${DEVICE_TOKEN_ARG_PREFIX}${localBackend.token}`)
  }

  mainWindow = new BrowserWindow({
    width: isDialog ? 520 : 1440,
    height: isDialog ? 620 : 900,
    minWidth: isDialog ? 420 : 960,
    minHeight: isDialog ? 480 : 640,
    resizable: !isDialog,
    backgroundColor: WINDOW_BACKGROUND,
    show: false, // 等首帧就绪再显示，避免白屏闪一下
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      // 双模式各用一个持久分区：cookie / localStorage / IndexedDB 互不可见；
      // 切换模式时 mode-switch.cjs 清的就是「离开模式」的那个分区。
      partition,
      contextIsolation: true,
      nodeIntegration: false,
      // preload 要读 process.argv / versions，故不开 sandbox；
      // contextIsolation 已经隔离了页面与 preload 的执行上下文。
      sandbox: false,
      additionalArguments,
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

  if (isDev && target === 'app') {
    mainWindow.loadURL(DEV_URL)
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else if (target === 'setup') {
    mainWindow.loadFile(path.join(__dirname, 'setup.html'))
  } else if (target === 'error') {
    // 本地模式但后端没起来：复用通用错误页，把原因写进详情
    mainWindow.loadFile(path.join(__dirname, 'error.html'), {
      query: {
        code: localBackend.reason || '',
        desc: localBackend.message || '本地后端未就绪',
        url: '',
        retry: '',
      },
    })
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
  holdingWindowQuota(() => {
    if (old) old.destroy()
    createWindow()
  })
}

/**
 * 从「连接设置…」菜单进入配置页。
 * 走"重建窗口"而不是在同窗口 loadFile：配置页是 520×620 的单栏布局，
 * 塞进 1440 宽的主窗口会显得空荡；重建一次反而干净（代价是主界面重载，可接受）。
 */
function relaunchIntoSetup() {
  const old = mainWindow
  mainWindow = null
  holdingWindowQuota(() => {
    if (old) old.destroy()
    // createWindow 会重新读配置：此时若已有地址，会直接进主界面 —— 所以这里
    // 先把窗口配置成"强制 setup"模式，避免用户点了"连接设置"却又弹回主界面。
    forceSetup = true
    createWindow()
    forceSetup = false
  })
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

  app.whenReady().then(async () => {
    // Windows 通知/任务栏需要显式的 AppUserModelId，且要与打包 appId 一致
    if (process.platform === 'win32') app.setAppUserModelId(APP_ID)

    // 1) 老配置迁移：必须在建窗之前完成 —— 它决定 session 分区、启动分流与注入的地址
    try {
      await askLegacyMode(readDeviceSettings())
    } catch (err) {
      logMain('warn', `legacy migration failed: ${err?.message || err}`)
    }

    // 2) 按模式准备后端：本地模式先把本机后端拉起来（未安装则提示并给恢复选项）
    currentMode = resolveMode(readDeviceSettings())
    logMain('info', `starting in ${currentMode} mode`)
    if (currentMode === 'local') {
      // 首次启动（或运行时被破坏）时，安装要跑几分钟。此时主窗口还不存在，
      // 所以先弹一个进度窗口 —— 否则用户看到的是"点了图标，什么都没有"。
      // 已就绪就完全不建这个窗口，避免正常启动时闪一下。
      const runtimeState = inspectLocalRuntime()
      const needsInstall = !runtimeState.ready
      if (needsInstall) {
        logMain('info', `local runtime not ready (${runtimeState.reason}): ${runtimeState.message || ''}`)
        openInstallWindow()
      }

      // 进度/输出行只推给安装窗口；它在装完后会被销毁，广播会自行丢弃
      const runtimeHooks = needsInstall
        ? {
            onRuntimeStage: info => broadcastRuntimeStage(info),
            onRuntimeLine: line => logMain('info', `[runtime] ${line}`),
          }
        : {}

      try {
        localBackend = await startLocalBackend(runtimeHooks)
        let attempts = 0
        // 最多再重试两次；仍失败就继续建窗口，让错误页把原因写在界面上
        while (!localBackend.ok && attempts < 2) {
          attempts += 1
          const action = await askLocalBackendRecovery(localBackend)
          if (action === 'quit') {
            app.quit()
            return
          }
          if (action === 'cloud') {
            // applyModeSwitch 内部会写盘并 relaunch + exit，这里不用再建窗口
            try {
              await applyModeSwitch('cloud', {})
            } catch (err) {
              dialog.showErrorBox('切换运行模式失败', err?.message || String(err))
              app.quit()
            }
            return
          }
          localBackend = await startLocalBackend(runtimeHooks)
        }
      } finally {
        // 无论成败都必须关掉：失败时留着它会盖住后面的错误页
        closeInstallWindow()
      }
      recordLocalStart(localBackend)
    }

    // 3) 精简菜单：保留复制/粘贴/全选/重载/开发者工具，去掉 Electron 默认的多余项
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
            // 应用内修改服务地址的唯一入口（仅云端模式有意义）：填错地址后不必去删 config.json
            {
              label: '连接设置…',
              accelerator: 'CmdOrCtrl+,',
              enabled: currentMode === 'cloud',
              click: () => relaunchIntoSetup(),
            },
            // 模式的图形入口：设置页接上模式切换之前，这里是唯一出口
            {
              label: currentMode === 'local' ? '切换到云端模式…' : '切换到本地模式…',
              click: () => confirmModeSwitch(currentMode === 'local' ? 'cloud' : 'local'),
            },
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

    // 设备设置与模式：真相在主进程的 config.json（契约 §1），渲染进程读到的只是它。
    ipcMain.handle('desktop:get-device-settings', event => {
      assertTrustedSender(event)
      return readDeviceSettings()
    })
    ipcMain.handle('desktop:set-mode', (event, mode, opts) => {
      assertTrustedSender(event)
      return applyModeSwitch(mode, opts)
    })
    ipcMain.handle('desktop:restart', event => {
      assertTrustedSender(event)
      restartNow()
      return true
    })

    // 组件管理（契约 §2）。components.cjs 可能不存在（旧包/未安装组件），
    // 统一返回结构化错误，不让渲染进程拿到未处理的 reject。
    function requireComponents() {
      const mod = loadOptionalModule('./components.cjs')
      if (!mod) throw new Error('本地组件管理不可用（components.cjs 未随包发布）')
      return mod
    }
    ipcMain.handle('desktop:list-components', event => {
      assertTrustedSender(event)
      const mod = loadOptionalModule('./components.cjs')
      return typeof mod?.listComponents === 'function' ? mod.listComponents() : []
    })
    ipcMain.handle('desktop:install-component', (event, name) => {
      assertTrustedSender(event)
      return requireComponents().installComponent(String(name || ''))
    })
    ipcMain.handle('desktop:remove-component', (event, name) => {
      assertTrustedSender(event)
      return requireComponents().removeComponent(String(name || ''))
    })
    ipcMain.handle('desktop:start-component', (event, name) => {
      assertTrustedSender(event)
      return requireComponents().startComponent(String(name || ''))
    })
    ipcMain.handle('desktop:stop-component', (event, name) => {
      assertTrustedSender(event)
      return requireComponents().stopComponent(String(name || ''))
    })

    // ── 本地模型配置（provider / 端点 / 模型名 / key）──
    //
    // 三个动作都不抛异常给渲染进程：IPC 抛错会被 Electron 包成
    // "Error invoking remote method '...': Error: ..." 的一大串，界面只能整段回显。
    // 统一返回 { ok, message } —— message 是给人看的一句话（含原因），界面直接显示。
    //
    // 明文 apiKey 只在这一层与 secrets.cjs 之间流动：读回的配置里只有 hasKey 布尔，
    // 保存时也不回显、不写日志（契约 §8：不要把用户 provider key 写入日志/明文）。
    const MODEL_CONFIG_PROVIDERS = new Set(['ollama', 'openai-compatible'])

    function optionalSecrets() {
      const mod = loadOptionalModule('./secrets.cjs')
      if (!mod || typeof mod.readSecrets !== 'function' || typeof mod.writeSecrets !== 'function') {
        return null
      }
      return mod
    }

    /**
     * 合并单个 provider 条目：
     *   · baseUrl / model 显式给出才覆盖（允许清成空串 —— 用户主动恢复默认）；
     *   · apiKey 空白视为"保持不变"（界面上不回显 key，用户不重填就不该被清掉）；
     *   · clearApiKey === true 时删除已存的 key（明确的删密钥动作）。
     */
    function mergeProviderEntry(previous, patch) {
      const merged = { ...(previous && typeof previous === 'object' ? previous : {}) }
      for (const field of ['baseUrl', 'model']) {
        if (typeof patch[field] === 'string') merged[field] = patch[field].trim()
      }
      if (typeof patch.apiKey === 'string' && patch.apiKey.trim()) merged.apiKey = patch.apiKey.trim()
      if (patch.clearApiKey === true) delete merged.apiKey
      return merged
    }

    ipcMain.handle('desktop:get-model-config', async event => {
      assertTrustedSender(event)
      const mod = optionalSecrets()
      if (!mod || typeof mod.readPublicSecrets !== 'function') {
        return { ok: false, reason: 'secrets-unavailable', message: '当前版本不支持模型配置（secrets.cjs 未随包发布）' }
      }
      try {
        return { ok: true, config: await mod.readPublicSecrets() }
      } catch (err) {
        return { ok: false, reason: 'read-failed', message: err?.message || String(err) }
      }
    })

    ipcMain.handle('desktop:save-model-config', async (event, payload) => {
      assertTrustedSender(event)
      const mod = optionalSecrets()
      if (!mod) {
        return { ok: false, reason: 'secrets-unavailable', message: '当前版本不支持模型配置（secrets.cjs 未随包发布）' }
      }
      try {
        const incoming = payload && typeof payload === 'object' ? payload : {}
        const current = await mod.readSecrets()
        const next = {
          version: current && current.version ? current.version : undefined,
          active: current && typeof current.active === 'string' ? current.active : '',
          providers: { ...(current && current.providers ? current.providers : {}) },
        }
        if (typeof incoming.active === 'string') next.active = incoming.active.trim().toLowerCase()
        const patches = incoming.providers && typeof incoming.providers === 'object' ? incoming.providers : {}
        for (const [name, patch] of Object.entries(patches)) {
          if (!MODEL_CONFIG_PROVIDERS.has(name)) {
            return { ok: false, reason: 'unknown-provider', message: `未知的模型服务：${name}` }
          }
          next.providers[name] = mergeProviderEntry(next.providers[name], patch && typeof patch === 'object' ? patch : {})
        }
        const written = await mod.writeSecrets(next)
        if (!written) {
          // 最常见的原因：Linux 上没有可用的系统密钥环 / Windows DPAPI 被策略禁用
          return {
            ok: false,
            reason: 'write-failed',
            message: '无法写入密钥库：系统加密存储（safeStorage）不可用或写入失败，配置未保存',
          }
        }
        logMain('info', `model config saved (active: ${next.active || 'default'})`)
        const config = typeof mod.readPublicSecrets === 'function' ? await mod.readPublicSecrets() : null
        return { ok: true, config }
      } catch (err) {
        return { ok: false, reason: 'save-failed', message: err?.message || String(err) }
      }
    })

    /**
     * 重启本地后端（改完模型配置后让核心重新握手；不需要重启整个应用）。
     * 只允许本地模式调用 —— 云端模式下没有本地后端，静默成功会让用户以为生效了。
     */
    ipcMain.handle('desktop:restart-local-backend', async event => {
      assertTrustedSender(event)
      if (currentMode !== 'local') {
        return { ok: false, reason: 'not-local', message: '当前不是本地模式，没有可重启的本地后端' }
      }
      try {
        await stopLocalBackend()
        // stopLocalBackend 里有"只停一次"的标志位，重启要把它复位，否则下次退出不会停
        localBackendStopped = false
        localBackend = await startLocalBackend()
        recordLocalStart(localBackend)
        return {
          ok: Boolean(localBackend.ok),
          reason: localBackend.reason,
          message: localBackend.message,
          baseUrl: localBackend.ok ? localBackend.baseUrl : '',
        }
      } catch (err) {
        return { ok: false, reason: 'restart-failed', message: err?.message || String(err) }
      }
    })

    createWindow()
    // 主窗口已建出来，启动流程到此结束。此后 window-all-closed 才代表"用户真的关了窗口"；
    // 之前没有窗口只是启动期的正常中间态（见 window-all-closed 里的说明）。
    startupComplete = true
    setupAutoUpdater()

    // macOS：点 Dock 图标且没有窗口时重开
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  // macOS 习惯：关掉窗口不退出应用
  app.on('window-all-closed', () => {
    if (
      !shouldQuitOnAllWindowsClosed({
        platform: process.platform,
        startupComplete,
        holdingWindows,
      })
    ) {
      // 不能静默：真·退出和"被启动流程误伤"在日志里必须能区分开
      logMain(
        'info',
        `window-all-closed ignored (startupComplete=${startupComplete}, holdingWindows=${holdingWindows})`,
      )
      return
    }
    app.quit()
  })

  // 退出前停掉本地后端：否则 embedded-postgres / 推理代理会留成孤儿进程。
  // 停的过程是异步的，所以先 preventDefault，停完再真的 quit（标志位保证只停一次）。
  app.on('before-quit', event => {
    if (currentMode !== 'local' || localBackendStopped || !localBackend.ok) return
    event.preventDefault()
    void stopLocalBackend().finally(() => {
      localBackendStopped = true
      app.quit()
    })
  })
}

/**
 * 仅供诊断脚本 / 单测引用注入前缀，避免第二处再抄一遍字面量。
 * `electron .` 加载主进程入口时不看这个导出，写在这里无副作用。
 */
module.exports = { DEVICE_TOKEN_ARG_PREFIX }
