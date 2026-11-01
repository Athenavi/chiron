'use strict'
/**
 * 首次启动时**自动准备** local core 的 Python 运行时。
 *
 * 为什么不再"构建期打包整个解释器 + site-packages"：
 *   那等于把"构建机当时装了什么"冻进安装包。漏一个依赖（历史上就是 `httpx`）时，
 *   自检脚本也是手维护的模块清单、跟着一起漏 ⇒ 打包绿、用户侧崩，表现为所有页面
 *   "服务繁忙"。而且每次调依赖都要重新发版。改由**首次启动时就地创建 venv 并装依赖**，
 *   依赖清单就是仓库里那一份（core 用 `pyproject.toml`、引擎用 `requirements.txt`），
 *   缺什么装什么，坏了能就地重建。
 *
 * 做法沿用 ComfyUI Desktop 的思路：**把 Python 版本管理外包给 uv**，但解释器随包。
 *   - 安装包**携带**受管 CPython（`resources/python-runtime/<platform>/cpython-*`，
 *     由 `scripts/prepare_python.py` 就位）与 `uv` 二进制（`scripts/prepare_uv.py`）；
 *   - `uv venv --python <随包解释器绝对路径>` 建出 venv，**不接受版本号**：传路径
 *     uv 就不会去 github.com 取 python-build-standalone（实测多数网络下不可达）；
 *   - `uv pip install -r <该目标的依赖清单>` 装依赖（这一步仍走 PyPI，可用
 *     `CHIRON_PIP_INDEX_URL` 换源）。清单只有一份：core 是 `pyproject.toml`，
 *     引擎是 `requirements.txt` —— 各自沿用自己仓库里的那份，不复制品、不另抄一份。
 *
 * 三个刻意的取舍：
 *   1. **装到 userData，不装到程序目录**。程序目录会在自动更新时被替换，装在那里等于
 *      每次升级都丢环境；userData 由 Electron 保证跨版本保留。
 *   2. **幂等判定靠"真的能 import"，不靠标记位**。标记位会与磁盘真实状态脱节
 *      （用户删了半个 venv、杀毒软件隔离了某个 pydantic 模块），而 import 探测不会骗人。
 *   3. **启动路径上不做联网的 dry-run**。ComfyUI 用 `uv pip install --dry-run` 判断
 *      "是否无变更"，但那需要解析 index、离线必失败。开机自检必须离线可用，
 *      所以这里只用 import 探测；只有在需要重装时才联网。
 *
 * 失败一律显式抛出（带可照做的指引），不静默降级到"随便找个 Python 试试"。
 *
 * **两个目标、两份 venv**：同一套流程既装本地核心（`desktop-local` +
 * `pyproject.toml` → `<userData>/local/runtime/venv`），也装 AI 引擎
 * （`python-engine` + `requirements.txt` → `<userData>/local/engine/venv`）。
 * 分开装的理由见 `RUNTIME_TARGETS` 的注释；`target` 缺省是 core，所以既有调用点行为不变。
 */
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const childProcess = require('node:child_process')
const { probeRuntime } = require('./runtime-probe.cjs')

/** 受管 CPython 版本。desktop-local 的 `requires-python` 是 >=3.11，3.12 与 CI 一致。 */
const RUNTIME_PYTHON_VERSION = '3.12'

/** 安装阶段（渲染层按 id 显示文案；顺序即真实顺序） */
const INSTALL_STAGES = Object.freeze({
  probing: 'probing',
  creatingVenv: 'creating-venv',
  installingDeps: 'installing-deps',
  syncingDeps: 'syncing-deps',
  verifying: 'verifying',
  ready: 'ready',
})

/** 安装清单文件名与版本（放在 runtimeRoot 下，与 venv 平级） */
const MANIFEST_FILE = 'install.json'
const MANIFEST_VERSION = 1

/**
 * 安装目标：**同一套流程服务于两个互相独立的 venv**。
 *
 * 为什么引擎单独一份，而不是"在引导器那份上再装一遍依赖"：
 *   - 依赖差一个数量级。引导器（desktop-local）只要 fastapi/uvicorn/asyncpg/redis/
 *     embedded-postgres；引擎还要 pydantic-ai / anthropic / openai / pymupdf / grpcio。
 *     全塞进一份 venv，等于让只用得上本地 core 的人先下载几百 MB 自己用不到的东西。
 *   - 更关键的是**失败各归其位**：重建环境的第一步是 removeVenv()，共用一份 venv 时
 *     装引擎失败会顺手毁掉本来就好的引导器环境 —— 本地模式从"聊天不可用"变成"全废"。
 *   - 清单也各是各的：core 用 pyproject.toml，引擎用 requirements.txt（引擎侧唯一的
 *     依赖声明，uv pip install -r 直接吃它）。两份混成一份必然漂移，而漂移的代价就是
 *     上一轮 httpx 那类"清单漏项、自检绿、运行时红"的事故。
 *
 * rootName 是 `<userData>/local/<rootName>` 里的那一段，**不能改**：改一次就等于让老用户
 * 已经装好的环境凭空消失（resolveCoreLaunch 与 resolveEngineLaunch 都按这个路径找解释器）。
 */
const RUNTIME_TARGETS = Object.freeze({
  core: Object.freeze({
    id: 'core',
    rootName: 'runtime',
    sourcesDir: 'desktop-local',
    manifest: 'pyproject.toml',
    label: '本地核心',
  }),
  engine: Object.freeze({
    id: 'engine',
    rootName: 'engine',
    sourcesDir: 'python-engine',
    manifest: 'requirements.txt',
    label: 'AI 引擎',
  }),
})

/**
 * 解析安装目标。**未知 id 直接抛错**，不静默回落到 core：
 * 传错 id 时回落 core 等于"照另一份清单去装另一个环境"，而且装完自检还是绿的
 * （core 那份本来就能 import），引擎照样起不来 —— 那是最难查的一类坏状态。
 */
function resolveTarget(target) {
  const id = String(target === undefined || target === null || target === '' ? 'core' : target)
  const found = RUNTIME_TARGETS[id]
  if (!found) {
    throw new Error(`未知的安装目标：${id}（可选：${Object.keys(RUNTIME_TARGETS).join(' / ')}）`)
  }
  return found
}

/** 单步超时：建 venv 要下载 CPython（几十 MB），装依赖要下载整个依赖闭包 */
const VENV_TIMEOUT_MS = 10 * 60 * 1000
const INSTALL_TIMEOUT_MS = 30 * 60 * 1000
const VERIFY_TIMEOUT_MS = 60000

function runtimeRoot(userDataPath, target = 'core') {
  return path.join(userDataPath, 'local', resolveTarget(target).rootName)
}

function venvDir(userDataPath, target = 'core') {
  return path.join(runtimeRoot(userDataPath, target), 'venv')
}

/** venv 里的解释器路径（Windows 是 Scripts\python.exe，POSIX 是 bin/python3） */
function venvPython(userDataPath, target = 'core') {
  const base = venvDir(userDataPath, target)
  return process.platform === 'win32'
    ? path.join(base, 'Scripts', 'python.exe')
    : path.join(base, 'bin', 'python3')
}

/** 老用户机器上那份按需下载的 uv。安装包现在随带 uv（见 resolveUv），这里只为兼容旧数据 */
function downloadedUv(userDataPath) {
  if (!userDataPath) return ''
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const candidate = path.join(runtimeRoot(userDataPath), 'bin', exe)
  return fs.existsSync(candidate) ? candidate : ''
}

/**
 * 随包资产的根目录（`resources/python-runtime`、`resources/uv`）。
 *
 * 与 `prepare_python.py` / `prepare_uv.py` 的平台键约定一致（如 `win32-x64`）。
 */
function bundledAssetRoot(resourcesPath = process.resourcesPath) {
  return resourcesPath || ''
}

function platformKey() {
  return `${process.platform}-${process.arch}`
}

/**
 * 随包解释器：`resources/python-runtime/<platform>/cpython-<ver>-<os>-<arch>/python.exe`。
 *
 * 目录名带版本（如 `cpython-3.12.16-windows-x86_64-none`），所以按 glob 找一层；
 * 找不到返回空串。**这是运行时唯一的正常来源** —— 安装包自带，不联网。
 *
 * 与改绑（`rebindVenvBinding`）共用 `bundledRuntime` 的判据：两处对"包里有没有可用的
 * CPython"必须给同一个答案，否则会出现"能建环境、却判不出可改绑的目标"这种分裂状态。
 */
function bundledPython(resourcesPath = process.resourcesPath) {
  const runtime = bundledRuntime(resourcesPath)
  if (!runtime.dir) return ''
  return interpreterUnderHome(homeForRuntime(runtime.dir))
}

/** 随包 uv：`resources/uv/<platform>/uv.exe`（只用来建 venv + 装依赖，不下载解释器） */
function bundledUv(resourcesPath = process.resourcesPath) {
  const root = bundledAssetRoot(resourcesPath)
  if (!root) return ''
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const candidate = path.join(root, 'uv', platformKey(), exe)
  return fs.existsSync(candidate) ? candidate : ''
}

/**
 * venv 与解释器之间的**绑定**。
 *
 * `userData/local/{runtime,engine}/venv` 是跨版本、跨包共享的（重建一份引擎环境实测
 * 340MB、两百个包、要联网），而它里面的 `Scripts\python.exe`（Windows）/
 * `bin/python3`（POSIX 是符号链接）**只是跳转器**：标准库与 DLL 由 `pyvenv.cfg` 里的
 * `home` 决定，而 `home` 写的是**当初创建它那份包的绝对路径**。真机取到两份：
 * core 指向 `D:\Program Files\Chiron\resources\python-runtime\…`，引擎指向
 * `X:\…\release-m47\win-unpacked\resources\python-runtime\…`（一次临时验证出包）。
 * 那个目录被删、或者换了安装位置，解释器就再也起不来 —— 实测把 home 改指一个不存在的
 * 目录，`Scripts\python.exe -c "import sys"` 直接
 * `No Python at '"X:\…\python.exe'` 并以 **103** 退出。此时 venv 文件明明都在，
 * 上层只会看到"探测失败"，于是走全量重建：慢、要网，离线时本地模式直接不可用，
 * 而日志里看不出真因是"换了个包"。
 *
 * 所以这里做一次**就地改绑**：home 失效时把它指向当前包里那一份 CPython。
 * 闸门是 **minor 版本相同**（`3.12.x`）：site-packages 里的 `pydantic_core` 这类扩展按
 * minor 的 ABI 编译（`.cp312-*.pyd`），3.12.16 → 3.12.17 换掉安全，而绑到 3.13 只会
 * 换一种崩法 —— 那种情况明确判"要重建"，由调用方走原来的重建路径。
 * 拿 minor 而不是整串版本号，是因为随包补丁号会跟着发版变；要求逐字相同会让每次升级
 * 都白重建一次 340MB 环境，而那正是本函数要避免的代价。
 */
const VENV_CFG_FILE = 'pyvenv.cfg'

/** 按平台取 path 实现：用例要在 Windows 上也能验 POSIX 那条口径 */
function pathFor(platform) {
  return platform === 'win32' ? path.win32 : path.posix
}

/**
 * `home` 与"随包包根"的对应关系**两个平台不一样**：
 * Windows 上受管 CPython 的 `python.exe` 就在包根，POSIX 上在 `<包根>/bin`
 * （uv 和 CPython 自己的 venv 都这么写 `pyvenv.cfg`）。
 * 改绑时不换算就会把 POSIX 的 home 写成包根 —— 解释器照样找不到，等于白改。
 */
function homeForRuntime(runtimeDir, platform = process.platform) {
  return platform === 'win32' ? runtimeDir : pathFor(platform).join(runtimeDir, 'bin')
}

/** `home` 那一层里的解释器文件名（两个平台不一样） */
function interpreterUnderHome(homeDir, platform = process.platform) {
  const pm = pathFor(platform)
  return platform === 'win32' ? pm.join(homeDir, 'python.exe') : pm.join(homeDir, 'python3')
}

/** `3.12.16` → `3.12`；认不出返回空串（不猜） */
function minorVersion(version) {
  const m = /^(\d+)\.(\d+)/.exec(String(version || ''))
  return m ? `${m[1]}.${m[2]}` : ''
}

/**
 * 读 `pyvenv.cfg` 的 home 与版本。
 *
 * 返回 `{ home, versionInfo, file, text }`；文件不存在、读不动、没有 home 行一律 null ——
 * 没有绑定可言时调用方按"从没装过"处理，不该在这里抛错。
 * 版本键先取 `version_info`（uv 写的），再退回 `version`（CPython `venv` 写的），
 * 两者都没有就是别的工具建的，改绑时拒绝。
 */
function readVenvCfg(venvPath) {
  if (!venvPath) return null
  const file = path.join(venvPath, VENV_CFG_FILE)
  let text
  try {
    text = fs.readFileSync(file, 'utf8')
  } catch {
    return null
  }
  const pick = key => {
    const m = text.match(new RegExp(`^${key}[ \\t]*=[ \\t]*(.*)$`, 'm'))
    return m ? m[1].trim() : ''
  }
  const home = pick('home')
  if (!home) return null
  return { home, versionInfo: pick('version_info') || pick('version'), file, text }
}

/**
 * 随包 CPython：`resources/python-runtime/<platform>/cpython-<ver>-<os>-<arch>`。
 *
 * 版本从目录名取（`3.12.16`，与 `pyvenv.cfg` 的 version_info 同形）。
 * 判据是解释器文件真在，不看目录名像不像 —— 半个包（安装中断）不该被当成可用运行时。
 */
function bundledRuntime(resourcesPath = process.resourcesPath) {
  const root = bundledAssetRoot(resourcesPath)
  const none = { dir: '', version: '' }
  if (!root) return none
  const base = path.join(root, 'python-runtime', platformKey())
  let entries
  try {
    entries = fs.readdirSync(base, { withFileTypes: true })
  } catch {
    return none
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const dir = path.join(base, entry.name)
    if (!fs.existsSync(interpreterUnderHome(homeForRuntime(dir)))) continue
    const m = /^cpython-(\d+\.\d+\.\d+)-/.exec(entry.name)
    return { dir, version: m ? m[1] : '' }
  }
  return none
}

/**
 * POSIX 的 venv 解释器是**指向 home 里解释器的符号链接**：home 没了它就是悬空链接，
 * `fs.existsSync` 为假，只改 `pyvenv.cfg` 修不好，必须把链接重新指过去。
 *
 * 只动"确实是符号链接、且目标已经不存在"的 `bin/python3` / `bin/python`，其它一律不碰；
 * 失败也不抛（调用方会按"没修好"继续走重建，在这里抛等于把兜底换成崩溃）。
 */
function relinkPosixInterpreter(venvPath, runtimeDir) {
  const target = interpreterUnderHome(homeForRuntime(runtimeDir))
  if (!fs.existsSync(target)) return []
  const relinked = []
  for (const name of ['python3', 'python']) {
    const link = path.join(venvPath, 'bin', name)
    let stat
    try {
      stat = fs.lstatSync(link)
    } catch {
      continue
    }
    // 已经是活链接就别动：那是 uv 建的、指向当前 home 的正常状态
    if (!stat.isSymbolicLink() || fs.existsSync(link)) continue
    try {
      fs.unlinkSync(link)
      fs.symlinkSync(target, link)
      relinked.push(link)
    } catch {
      /* 保持原样 */
    }
  }
  return relinked
}

/**
 * 就地改绑失效的解释器绑定。返回 `{ rebound, reason, from, to, relinked }`，**永不抛错**。
 *
 * `reason` 是给上层与人看的分流依据：
 * - `binding-ok`：绑定本来就是活的，一个字节都没动；
 * - `no-binding-file`：没有 venv 或没有 cfg —— 首次安装，走原来的创建流程；
 * - `no-bundled-runtime`：当前包里找不到解释器（包残缺，改绑帮不上）；
 * - `version-mismatch`：minor 版本对不上或 cfg 没记版本，**不能**改绑，只能重建；
 * - `write-failed`：cfg 写不进去（只读目录/占用），交给重建路径；
 * - `rebound`：改绑成功。
 */
function rebindVenvBinding(options = {}) {
  const target = resolveTarget(options.target)
  const userDataPath = String(options.userDataPath || '')
  if (!userDataPath) return { rebound: false, reason: 'no-user-data' }
  const venv = options.venvPath || venvDir(userDataPath, target.id)

  const cfg = readVenvCfg(venv)
  if (!cfg) return { rebound: false, reason: 'no-binding-file', venv }
  // 判"绑没绑坏"要看 home 里的**解释器**在不在，而不是目录在不在：
  // 目录还在但解释器被删（安全软件隔离、手动清理）是同一个死法。
  if (fs.existsSync(interpreterUnderHome(cfg.home))) {
    return { rebound: false, reason: 'binding-ok', venv }
  }

  const runtime = bundledRuntime(options.resourcesPath)
  if (!runtime.dir) return { rebound: false, reason: 'no-bundled-runtime', venv, from: cfg.home }
  const venvMinor = minorVersion(cfg.versionInfo)
  const runtimeMinor = minorVersion(runtime.version)
  if (!venvMinor || !runtimeMinor || venvMinor !== runtimeMinor) {
    return {
      rebound: false,
      reason: 'version-mismatch',
      venv,
      from: cfg.home,
      message:
        `这份 Python 环境是 ${cfg.versionInfo || '未记录版本'} 建的，当前安装包带的是 ` +
        `${runtime.version || '未记录版本'}，不能改绑（扩展按 Python minor 的 ABI 编译），需要重建环境`,
    }
  }
  const newHome = homeForRuntime(runtime.dir)

  // 只替换 home 这一行：uv 还往里写 uv / version_info / include-system-site-packages，
  // 重写整个文件等于把这些口径丢了。临时文件 + rename，不留半个 cfg。
  // 替换值用函数返回，不走字符串模板：路径里出现 `$`（如 `C:\Users\$x`）时
  // 模板写法会被当成 `$&` 这类特殊序列，把 home 写成另一个意思。
  const next = cfg.text.replace(/^(home[ \t]*=[ \t]*).*$/m, (matched, prefix) => `${prefix}${newHome}`)
  if (next === cfg.text) {
    return { rebound: false, reason: 'write-failed', venv, from: cfg.home, detail: 'home 行没被替换' }
  }
  try {
    const staging = `${cfg.file}.tmp`
    fs.writeFileSync(staging, next, 'utf8')
    fs.renameSync(staging, cfg.file)
  } catch (err) {
    return { rebound: false, reason: 'write-failed', venv, from: cfg.home, detail: err?.message || String(err) }
  }
  const relinked = process.platform === 'win32' ? [] : relinkPosixInterpreter(venv, runtime.dir)
  // target 跟着记录走：两套环境的改绑会汇成一个列表，没有这一字段就分不清哪条是 core、哪条是引擎
  return { rebound: true, reason: 'rebound', target: target.id, venv, from: cfg.home, to: newHome, relinked }
}

/**
 * 解析 uv 可执行文件：随包那份 → userData 里曾下载的那份。
 *
 * 顺序的理由：随包的是**确定的、离线可用**的，而 userData 里的那份只可能来自
 * 旧版本的下载流程，留作兼容（老用户升级后不重建环境也能用）。
 */
function resolveUv(userDataPath, resourcesPath = process.resourcesPath) {
  const bundled = bundledUv(resourcesPath)
  if (bundled) return bundled
  return downloadedUv(userDataPath)
}

/**
 * 某目标的源码目录（打包态在 `resources/<sourcesDir>`）。
 *
 * 判据是 `app/main.py` 存在 —— 光有目录不算，那可能是个空壳；`python-engine` 与
 * `desktop-local` 都以 `-m app.main` 启动，同一判据两边通用。
 */
function sourcesFor(target, resourcesPath = process.resourcesPath) {
  if (!resourcesPath) return ''
  const candidate = path.join(resourcesPath, resolveTarget(target).sourcesDir)
  return fs.existsSync(path.join(candidate, 'app', 'main.py')) ? candidate : ''
}

/** 本地 core 源码目录（打包态在 resources/desktop-local） */
function coreSources(resourcesPath = process.resourcesPath) {
  return sourcesFor('core', resourcesPath)
}

/** 安装清单路径（与 venv 平级，放在各自 runtimeRoot 下） */
function manifestPath(userDataPath, target = 'core') {
  return path.join(runtimeRoot(userDataPath, target), MANIFEST_FILE)
}

/**
 * 依赖指纹：**目标清单**的内容摘要（core 是 `pyproject.toml`，引擎是 `requirements.txt`）。
 *
 * **为什么用内容摘要而不是包版本**：判断"要不要重装依赖"只需要知道"依赖声明变没变"。
 * 拿清单文件的内容算摘要，零成本、纯本地、离线可用；而 `uv pip install --dry-run`
 * 需要解析 index（离线必失败，还慢），不适合放在开机路径上。
 *
 * 摘要里带上 `RUNTIME_PYTHON_VERSION`：以后升到 3.13 时旧 venv 必须重建，
 * 否则会拿 3.12 的解释器去跑声明为 3.13 的依赖。
 * 公式不含 target id —— 老用户已落盘的 core 指纹必须继续能对上，否则每次升级都要
 * 白重建一次环境（而重建要联网，网络不通就从"能用"变成"不能用"）。
 */
function dependencyFingerprint(sources, target = 'core') {
  const spec = path.join(sources, resolveTarget(target).manifest)
  let body = ''
  try {
    body = fs.readFileSync(spec, 'utf8')
  } catch {
    // 读不到就没有指纹：返回空串让调用方走"重建"，不要拿空串当"未变更"
    return ''
  }
  return crypto
    .createHash('sha256')
    .update(`${RUNTIME_PYTHON_VERSION}\u0000${body}`)
    .digest('hex')
}

/** 读安装清单；不存在或格式不对都返回 null（当作"没装过"，不猜） */
function readManifest(userDataPath, target = 'core') {
  try {
    const raw = fs.readFileSync(manifestPath(userDataPath, target), 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.version !== MANIFEST_VERSION) return null
    return parsed
  } catch {
    return null
  }
}

/**
 * 写安装清单。用"临时文件 + rename"落地：
 * 直接覆写时若进程被强杀，会留下半截 JSON，下次启动读不出来就当成没装过、白重装一遍。
 */
function writeManifest(userDataPath, fields, target = 'core') {
  const file = manifestPath(userDataPath, target)
  const payload = {
    version: MANIFEST_VERSION,
    pythonVersion: RUNTIME_PYTHON_VERSION,
    installedAt: new Date().toISOString(),
    ...fields,
  }
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    const staging = `${file}.tmp`
    fs.writeFileSync(staging, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
    fs.renameSync(staging, file)
    return payload
  } catch {
    // 清单只是加速缓存，写不进去不该让整个安装失败
    return payload
  }
}

/**
 * 随包资产的根目录：显式传入的 `resourcesPath` → `sources` 的上一级 → Electron 的。
 *
 * `inspectRuntime` 与 `ensureRuntime` 必须用同一口径（真机踩过一边找得到解释器、
 * 另一边找不到的分裂状态），所以只在这里写一次。
 */
function assetRootFor(options, sources) {
  if (options.resourcesPath) return String(options.resourcesPath)
  if (sources) return path.dirname(sources)
  return process.resourcesPath || ''
}

/**
 * 运行时现状：判断能不能直接用，必要时**就地修好解释器绑定**（不装依赖、不建 venv）。
 *
 * 返回 `{ ready, venvPython, reason, target, targetLabel, message, binding }`。
 * `ready: true` 才允许直接起对应的 core / 引擎；`binding` 是改绑的结果，供上层记日志。
 */
function inspectRuntime(options = {}) {
  const target = resolveTarget(options.target)
  const userDataPath = String(options.userDataPath || '')
  const sources =
    options.sources !== undefined ? options.sources : sourcesFor(target.id, options.resourcesPath)

  // 改绑要排在"环境在不在"之前：Windows 上 home 失效时 `Scripts\python.exe` 文件明明在、
  // 却以 103 退出（探测只会吐一句 `No Python at …`），POSIX 上 `bin/python3` 直接成了悬空
  // 链接 —— existsSync 为假就被判成"从没装过"，于是白重建一次 340MB、还要联网的环境。
  const binding = rebindVenvBinding({
    userDataPath,
    target: target.id,
    resourcesPath: assetRootFor(options, sources),
  })

  const python = userDataPath ? venvPython(userDataPath, target.id) : ''
  const base = { venvPython: python, target: target.id, targetLabel: target.label, binding }

  if (!python || !fs.existsSync(python)) {
    return {
      ...base,
      ready: false,
      reason: 'venv-missing',
      message: `尚未创建${target.label}的 Python 环境（首次启动需要安装）`,
    }
  }
  if (!sources) {
    return {
      ...base,
      ready: false,
      reason: 'sources-missing',
      message: `安装包内缺少 ${target.sourcesDir} 源码，无法校验${target.label}环境`,
    }
  }

  // 唯一可信判据：真实 import 整个 app 包（与启动前那道探测同一份实现）
  // `options.probe` 可注入带缓存的版本，避免"安装时探一次、启动前再探一次"重复付 1s
  // 第三参是目标名：注入的探测器要把"导入失败"说成是 core 还是引擎的失败。
  const probeFn =
    typeof options.probe === 'function'
      ? options.probe
      : (python_, cwd_) =>
          probeRuntime(options.spawnSync || childProcess.spawnSync, python_, cwd_, {
            timeout: VERIFY_TIMEOUT_MS,
          })
  const probe = probeFn(python, sources, target.label)
  if (probe.ok) {
    return { ...base, ready: true, reason: 'ok', message: '' }
  }
  return {
    ...base,
    ready: false,
    reason: probe.reason,
    missingModule: probe.missingModule || '',
    message: probe.message,
  }
}

/** 把 spawn 的 stdout/stderr 逐行喂给回调：安装要跑几分钟，没有进度用户会以为卡死 */
function streamLines(stream, onLine) {
  if (!stream) return () => {}
  let buffer = ''
  stream.on('data', chunk => {
    buffer += chunk.toString()
    let index
    while ((index = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line.trim()) onLine(line)
    }
  })
  stream.on('end', () => {
    if (buffer.trim()) onLine(buffer.trim())
  })
  return () => {}
}

/**
 * 跑一条命令并把最后若干行输出留在错误里（失败时用户/日志能直接看到原因）。
 */
function runCommand(command, args, options = {}) {
  const spawnImpl = options.spawn || childProcess.spawn
  const timeout = options.timeout || INSTALL_TIMEOUT_MS
  const tailLimit = options.tailLimit || 40

  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawnImpl(command, args, {
        cwd: options.cwd,
        env: { ...process.env, ...(options.env || {}) },
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (err) {
      reject(new Error(`无法执行 ${path.basename(command)}：${err && err.message ? err.message : err}`))
      return
    }

    const tail = []
    const remember = line => {
      tail.push(line)
      if (tail.length > tailLimit) tail.shift()
      if (typeof options.onLine === 'function') options.onLine(line)
    }
    streamLines(child.stdout, remember)
    streamLines(child.stderr, remember)

    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      try {
        child.kill()
      } catch {
        /* 杀不掉也要走超时分支 */
      }
      reject(new Error(`${path.basename(command)} 超时（${Math.round(timeout / 1000)}s）`))
    }, timeout)

    child.on('error', err => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`无法执行 ${path.basename(command)}：${err && err.message ? err.message : err}`))
    })
    child.on('close', code => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (code === 0) {
        resolve({ code, tail })
        return
      }
      reject(
        new Error(
          `${path.basename(command)} ${args[0] || ''} 失败（退出码 ${code}）：\n${tail.join('\n')}`,
        ),
      )
    })
  })
}

/**
 * 组装 `uv pip install` 参数：每个目标只有**一份**依赖清单。
 *
 * core 用 `pyproject.toml`（uv 直接解析 `[project].dependencies`），引擎用
 * `requirements.txt`（引擎侧唯一的声明）。两边都不另存第二份清单 ——
 * 两份清单必然漂移，而漂移的代价就是漏装 httpx 那类事故。
 */
function pipInstallArgs(vpython, sources, indexUrl, target = 'core') {
  const args = [
    'pip',
    'install',
    '--python',
    vpython,
    '-r',
    path.join(sources, resolveTarget(target).manifest),
  ]
  // 镜像：国内网络下官方源常常超时。用环境变量显式覆盖，不写死任何第三方地址
  if (indexUrl) args.push('--index-url', indexUrl)
  return args
}

function uvEnv(userDataPath, target = 'core') {
  // 解释器是随包 CPython 的绝对路径，uv 不需要下载任何东西；
  // 保留 UV_PYTHON_INSTALL_DIR 只是让 uv 若真要用受管解释器时落在 userData 里。
  return { UV_PYTHON_INSTALL_DIR: path.join(runtimeRoot(userDataPath, target), 'python') }
}

/** 删掉旧 venv。半损坏的目录留着会让 uv 复用坏状态，重建等于没修 */
function removeVenv(userDataPath, target = 'core') {
  const id = resolveTarget(target)
  const dir = venvDir(userDataPath, id.id)
  if (!fs.existsSync(dir)) return
  try {
    fs.rmSync(dir, { recursive: true, force: true })
  } catch (err) {
    throw new Error(
      `无法清理旧的${id.label}环境（${dir}）：${err && err.message ? err.message : err}\n` +
        '通常是它有进程还在运行（例如上次没退干净的 core 或引擎），请重启电脑后再试。',
    )
  }
}

/**
 * 首次安装 / 就地修复。
 *
 * 三条路径，按代价从低到高：
 *   1. **已就绪**：直接返回，一条命令都不跑（正常开机走这条）；
 *   2. **增量同步**：venv 还在、依赖声明也没变，但 import 失败。只重跑 `uv pip install`
 *      —— 通常是某个包被安全软件隔离、或用户手动删过 site-packages。比重建快得多。
 *   3. **全量重建**：venv 缺失/损坏，或依赖声明变了（指纹不一致）。删掉重建。
 *
 * `options.force` 为真时跳过前两条（用户点"修复"就意味着重来一遍）。
 * 返回 `{ ok, venvPython, reinstalled, repaired, stages }`；失败抛错（带指引）。
 */
async function ensureRuntime(options = {}) {
  const target = resolveTarget(options.target)
  const userDataPath = String(options.userDataPath || '')
  if (!userDataPath) throw new Error('ensureRuntime 需要 userDataPath')

  const sources =
    options.sources !== undefined ? options.sources : sourcesFor(target.id, options.resourcesPath)
  if (!sources) {
    throw new Error(
      `安装包内缺少 ${target.sourcesDir} 源码（resources/${target.sourcesDir}），` +
        `无法准备${target.label}环境。这是安装包不完整，请重新安装。`,
    )
  }

  const onStage = typeof options.onStage === 'function' ? options.onStage : () => {}
  const vpython = venvPython(userDataPath, target.id)
  const indexUrl = String(options.indexUrl || process.env.CHIRON_PIP_INDEX_URL || '').trim()
  const fingerprint = dependencyFingerprint(sources, target.id)

  const stages = []
  // 每个阶段都带上 target：进度页要能说清"现在装的是哪一套环境"，
  // 否则 core 装完接着装引擎时，界面看起来像是同一件事反复失败。
  const stage = (id, message) => {
    stages.push(id)
    onStage({ stage: id, message, target: target.id, targetLabel: target.label })
  }

  onStage({
    stage: INSTALL_STAGES.probing,
    message: `检查${target.label}环境`,
    target: target.id,
    targetLabel: target.label,
  })

  // 随包资产在 `sources` 的上一级（`resources/desktop-local` → `resources/`）。
  // 这样调用方传不传 `resourcesPath` 都能定位，测试与打包态行为一致。
  // 提到改绑之前算：两条路径要用同一个根。
  const assetRoot = assetRootFor(options, sources)

  // **解释器绑定要先修，再判"venv 在不在"**：POSIX 上 home 失效时 venv 里的解释器是
  // 悬空链接，`fs.existsSync` 为假会直接跳到全量重建（340MB、要联网），把"换了个包"
  // 这种本来零成本的事故放大成"离线就不能用"。这里修过一次之后，下面 inspectRuntime
  // 再修就是 `binding-ok`（幂等），不会重复写文件、也不会重复记日志。
  const binding = rebindVenvBinding({ userDataPath, target: target.id, resourcesPath: assetRoot })
  const notice = typeof options.onLine === 'function' ? options.onLine : () => {}
  if (binding.rebound) {
    notice(
      `${target.label}环境的 Python 绑定已失效（原指向 ${binding.from}），` +
        `改绑到安装包自带的 ${binding.to}` +
        (binding.relinked && binding.relinked.length ? `，重建解释器链接 ${binding.relinked.length} 个` : ''),
    )
  }

  let probeNote = ''
  let trySync = false
  if (!options.force && fs.existsSync(vpython)) {
    const current = inspectRuntime({ ...options, target: target.id, userDataPath, sources })
    if (current.ready) {
      // 补一条安装记录，但**只在从来没有记录时**补：探测通过说明这套 venv 确实满足
      // 当前的依赖声明，而"没有记录"会让下面的 `trySync` 永远为 false —— 一旦哪天探测
      // 再失败，只能删掉整个 venv 全量重建（真机实测引擎那份 340MB、要重下两百个包）。
      // 已有记录时不改写：指纹不符正是"依赖变了 → 重建一个干净环境"的触发器，
      // 每次正常启动都改写它等于把这个触发器抹掉。
      // 真机踩到的就是这个空档：环境先装好、探测随后失败（少 boto3 / 探测没带密钥），
      // 于是路径 3 末尾那次 `writeManifest` 从未执行；修好探测后环境直接走这一条返回，
      // 能用，但永远没有记录。
      if (!readManifest(userDataPath, target.id)) {
        writeManifest(userDataPath, { dependencyFingerprint: fingerprint }, target.id)
      }
      onStage({
        stage: INSTALL_STAGES.ready,
        message: `${target.label}环境就绪`,
        target: target.id,
        targetLabel: target.label,
      })
      return {
        ok: true,
        venvPython: vpython,
        target: target.id,
        reinstalled: false,
        repaired: false,
        stages: [INSTALL_STAGES.ready],
      }
    }
    probeNote = `（原状态：${current.message}）`
    // 只有"环境本身在、依赖声明也没变"时才值得先试增量同步：
    // 依赖变了说明 uv 需要重新解析整个依赖图，增量同步救不回来，直接重建更干脆。
    const manifest = readManifest(userDataPath, target.id)
    trySync = Boolean(manifest && fingerprint && manifest.dependencyFingerprint === fingerprint)
    // 绑定改不动（minor 版本对不上）时增量同步注定白费：那条路径要 uv 用这个解释器装包，
    // 而解释器本身就跑不起来。直接把真因写进阶段消息，别让它伪装成"依赖问题"。
    if (binding.reason === 'version-mismatch') {
      probeNote = `（${binding.message}）`
      trySync = false
    }
    if (!trySync && binding.reason !== 'version-mismatch') probeNote += '，依赖声明已变更或无安装记录，将重建'
  }

  const uv =
    options.uvPath !== undefined && options.uvPath
      ? String(options.uvPath)
      : resolveUv(userDataPath, assetRoot)
  if (!uv) {
    // 安装包自带 uv（resources/uv），所以"找不到"只可能是包不完整 ——
    // 这属于重装能解决的事故，不是网络问题，不要再去联网补救。
    throw new Error(
      `安装包内缺少 uv（resources/uv），无法准备${target.label}环境。这是安装包不完整，请重新安装。`,
    )
  }
  const uvPath = uv
  // CPython 的下载源同样测速择优：不指定时默认给 uv 一个可达镜像，
  // 否则它会去 github.com 取 python-build-standalone（实测不可达）而卡死在建环境。

  const runUv = (args, timeout, label) =>
    runCommand(uvPath, args, {
      timeout,
      spawn: options.spawn,
      onLine: options.onLine,
      env: uvEnv(userDataPath, target.id),
    }).catch(err => {
      err.message = `${label}失败：${err.message}`
      throw err
    })

  // —— 路径 2：增量同步。venv 在、依赖没变，只重装依赖 ——
  if (trySync) {
    stage(INSTALL_STAGES.syncingDeps, `同步${target.label}依赖${probeNote}`)
    try {
      await runUv(
        pipInstallArgs(vpython, sources, indexUrl, target.id),
        INSTALL_TIMEOUT_MS,
        '同步依赖',
      )
      stage(INSTALL_STAGES.verifying, `验证${target.label}环境`)
      const afterSync = inspectRuntime({ ...options, target: target.id, userDataPath, sources })
      if (afterSync.ready) {
        writeManifest(userDataPath, { dependencyFingerprint: fingerprint }, target.id)
        stage(INSTALL_STAGES.ready, `${target.label}环境就绪`)
        return {
          ok: true,
          venvPython: vpython,
          target: target.id,
          reinstalled: false,
          repaired: true,
          stages,
        }
      }
      probeNote = `（同步后仍不可用：${afterSync.message}）`
    } catch {
      // 同步失败不该直接把首次启动判死：环境可能已经彻底坏了，落到重建再试一次
      probeNote = '（增量同步失败）'
    }
  }

  // —— 路径 3：全量重建 ——
  removeVenv(userDataPath, target.id)

  // 解释器：**随包那份**（`resources/python-runtime/<platform>/cpython-*/`），
  // 引导器与 python-engine 共用它。传绝对路径给 uv，uv 因此不会联网取解释器。
  // `options.pythonPath` 仅用于开发/测试注入另一个本地解释器绝对路径。
  const bundled = bundledPython(assetRoot)
  const pythonArg =
    options.pythonPath !== undefined && options.pythonPath
      ? String(options.pythonPath)
      : bundled || RUNTIME_PYTHON_VERSION

  if (!options.pythonPath && !bundled) {
    // 回落到版本号意味着 uv 会去下载解释器 —— 而内置化之后这条路本就该消失。
    // 与其让它静默联网（可能卡在不可达的 github.com），不如把缺失讲清楚。
    throw new Error(
      `安装包内缺少 Python 运行时（resources/python-runtime/${platformKey()}）。` +
        '这是安装包不完整，请重新安装。',
    )
  }

  stage(
    INSTALL_STAGES.creatingVenv,
    `为${target.label}创建 Python ${RUNTIME_PYTHON_VERSION} 环境${probeNote}`,
  )
  await runUv(
    [
      'venv',
      // 只用我们自己的解释器：用户机器上的 Python 可能缺模块、也可能是
      // Microsoft Store 占位符（能 spawn 但立刻退出），都不能用
      '--python',
      pythonArg,
      '--python-preference',
      'only-managed',
      venvDir(userDataPath, target.id),
    ],
    VENV_TIMEOUT_MS,
    '创建 Python 环境',
  )

  if (!fs.existsSync(vpython)) {
    throw new Error(`uv 报告建好了环境，但找不到解释器 ${vpython}。请重试；若反复出现请反馈此日志。`)
  }

  stage(INSTALL_STAGES.installingDeps, `安装${target.label}依赖`)
  await runUv(pipInstallArgs(vpython, sources, indexUrl, target.id), INSTALL_TIMEOUT_MS, '安装依赖')

  stage(INSTALL_STAGES.verifying, `验证${target.label}环境`)
  const verified = inspectRuntime({ ...options, target: target.id, userDataPath, sources })
  if (!verified.ready) {
    throw new Error(
      `依赖装完了，但${target.label}仍无法导入：${verified.message}\n` +
        `这通常意味着 ${target.manifest} 里少了某个包。请把这段信息连同日志一起反馈。`,
    )
  }

  writeManifest(userDataPath, { dependencyFingerprint: fingerprint }, target.id)
  stage(INSTALL_STAGES.ready, `${target.label}环境就绪`)
  return {
    ok: true,
    venvPython: vpython,
    target: target.id,
    reinstalled: true,
    repaired: false,
    stages,
  }
}

module.exports = {
  RUNTIME_PYTHON_VERSION,
  INSTALL_STAGES,
  RUNTIME_TARGETS,
  resolveTarget,
  runtimeRoot,
  venvDir,
  venvPython,
  manifestPath,
  dependencyFingerprint,
  readManifest,
  writeManifest,
  downloadedUv,
  resolveUv,
  bundledPython,
  bundledRuntime,
  readVenvCfg,
  rebindVenvBinding,
  sourcesFor,
  coreSources,
  inspectRuntime,
  ensureRuntime,
  _internal: {
    runCommand,
    pipInstallArgs,
    homeForRuntime,
    interpreterUnderHome,
    minorVersion,
    relinkPosixInterpreter,
    assetRootFor,
  },
}
