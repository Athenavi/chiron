'use strict'
/**
 * 首次启动时**自动准备** local core 的 Python 运行时。
 *
 * 为什么不再"构建期打包整个解释器 + site-packages"：
 *   那等于把"构建机当时装了什么"冻进安装包。漏一个依赖（历史上就是 `httpx`）时，
 *   自检脚本也是手维护的模块清单、跟着一起漏 ⇒ 打包绿、用户侧崩，表现为所有页面
 *   "服务繁忙"。而且每次调依赖都要重新发版。改由**首次启动时就地创建 venv 并装依赖**，
 *   依赖清单只有 `pyproject.toml` 一份，缺什么装什么，坏了能就地重建。
 *
 * 做法沿用 ComfyUI Desktop 的思路：**把 Python 版本管理外包给 uv**。
 *   - 安装包只带 `uv` 二进制（约 20MB/平台，见 scripts/prepare_uv.py 与 uv_manifest.json）；
 *   - `uv venv --python <ver> --python-preference only-managed` 让 uv 自己取受管 CPython，
 *     不会去用用户机器上那个可能缺模块、可能是 Microsoft Store 占位符的 Python；
 *   - `uv pip install -r pyproject.toml` 装依赖。
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
 */
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const childProcess = require('node:child_process')
const { probeRuntime } = require('./runtime-probe.cjs')
const fetchTools = require('./runtime-fetch.cjs')

/** 受管 CPython 版本。desktop-local 的 `requires-python` 是 >=3.11，3.12 与 CI 一致。 */
const RUNTIME_PYTHON_VERSION = '3.12'

/**
 * uv 的版本 / 摘要 / 候选源。**安装包不再携带 uv**，首次启动来这里取。
 *
 * 与 `desktop-local/scripts/uv_manifest.json` 必须一致（那边是构建期校验用），
 * 由 `runtime-install.test.cjs` 断言两边版本与摘要相同，防止两份清单漂移。
 *
 * 每个平台给**多个候选源**：写死单一源等于把某家服务商的可用性变成产品前提
 * （实测 github.com 不通、官方 PyPI 只有 190KB/s、镜像能到 16MB/s）。
 * 运行期 `rankSources` 各取 64KB 计时，谁快用谁。
 */
const RUNTIME_UV = Object.freeze({
  version: '0.13.6',
  assets: Object.freeze({
    'win32-x64': { file: 'uv-0.13.6-py3-none-win_amd64.whl', sha256: '7c9bc35ccb65932d8dc10a0669f9c805eaa3c9827ce18ecbecb1bd67944b2cdf', path: 'packages/7c/9d/714523188aea3054644d8e69ae8089aac70325a3cee2d3d05509d4f23398/uv-0.13.6-py3-none-win_amd64.whl', binary: 'uv.exe' },
    'win32-arm64': { file: 'uv-0.13.6-py3-none-win_arm64.whl', sha256: '722c7f96803f115e53918b8ee8858d1680ea6077c614b6db750747d709ab8d9a', path: 'packages/6a/7d/5ebb4cccbb11ae9e10f6cda704ce382722ab9dc68c9280a5b810378aee3e/uv-0.13.6-py3-none-win_arm64.whl', binary: 'uv.exe' },
    'darwin-arm64': { file: 'uv-0.13.6-py3-none-macosx_11_0_arm64.whl', sha256: '51361029b7cf91185d0092c4b356d110b4ad629e9cb11593f1e006ac72a72ca9', path: 'packages/f4/14/77230cc4f6a73a31578cb8070a5f67f58a76946ead643ae8a4155620d6da/uv-0.13.6-py3-none-macosx_11_0_arm64.whl', binary: 'uv' },
    'darwin-x64': { file: 'uv-0.13.6-py3-none-macosx_10_12_x86_64.whl', sha256: '95f042b95e67ef8c3d3569bb6e9d895e592002c5e871cf3f1afe407c9d77a294', path: 'packages/33/55/ff10d3f756c39a239e492e57262bd22fcda412a9f85a268175447cf33b09/uv-0.13.6-py3-none-macosx_10_12_x86_64.whl', binary: 'uv' },
    'linux-x64': { file: 'uv-0.13.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl', sha256: '22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba', path: 'packages/5c/43/0688ff5142934238f2812e348f4a24e09b57d48bfb38dd8f850325a0d9de/uv-0.13.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl', binary: 'uv' },
    'linux-arm64': { file: 'uv-0.13.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl', sha256: '7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b', path: 'packages/05/3b/f78583af6504f1270da95fd0d0935e381f58330c007260f450e81a1e5e1b/uv-0.13.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl', binary: 'uv' },
  }),
  /** PyPI 兼容镜像的 base（末尾不带 /）：文件路径对各家相同，只有前缀不同 */
  mirrors: Object.freeze([
    'https://mirrors.aliyun.com/pypi',
    'https://files.pythonhosted.org',
    'https://registry.npmmirror.com/-/binary/pypi',
  ]),
})

/** 受管 CPython 的候选源（python-build-standalone 镜像），同样测速择优 */
const PYTHON_MIRRORS = Object.freeze([
  'https://registry.npmmirror.com/-/binary/python-build-standalone',
  'https://github.com/astral-sh/python-build-standalone/releases/download',
])

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

/** 单步超时：建 venv 要下载 CPython（几十 MB），装依赖要下载整个依赖闭包 */
const VENV_TIMEOUT_MS = 10 * 60 * 1000
const INSTALL_TIMEOUT_MS = 30 * 60 * 1000
const VERIFY_TIMEOUT_MS = 60000

function runtimeRoot(userDataPath) {
  return path.join(userDataPath, 'local', 'runtime')
}

function venvDir(userDataPath) {
  return path.join(runtimeRoot(userDataPath), 'venv')
}

/** venv 里的解释器路径（Windows 是 Scripts\python.exe，POSIX 是 bin/python3） */
function venvPython(userDataPath) {
  const base = venvDir(userDataPath)
  return process.platform === 'win32'
    ? path.join(base, 'Scripts', 'python.exe')
    : path.join(base, 'bin', 'python3')
}

/** 首次启动下载到 userData 的 uv（安装包不携带任何运行时）。与 venv 同根，自动更新不会清掉 */
function downloadedUv(userDataPath) {
  if (!userDataPath) return ''
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const candidate = path.join(runtimeRoot(userDataPath), 'bin', exe)
  return fs.existsSync(candidate) ? candidate : ''
}

/**
 * 解析 uv 可执行文件：只有 userData 里已下载的那份，找不到就返回空串。
 *
 * **刻意不再看 `resources/uv`**：安装包不携带任何运行时。留着那条查找路径的
 * 后果是——有人把 uv 塞回包里，测试也照样通过，而"首次启动按需下载"这套逻辑
 * 就再没有真正被执行过。空串交给 `ensureUv` 现取。
 */
function resolveUv(userDataPath) {
  return downloadedUv(userDataPath)
}

/**
 * 按需下载 uv。**安装包不携带任何运行时**，这是首次启动的第一步。
 *
 * 多源测速择优：`RUNTIME_UV.mirrors` 各取 64KB 计时，最快的那个完整下载；
 * 下完先校验 SHA256（清单里固化），不符就删掉并报错 —— 绝不"先装上再说"。
 * 解析 wheel 用内置 zip 解析器，不依赖系统是否有 unzip/tar。
 */
async function ensureUv(options = {}) {
  const userDataPath = String(options.userDataPath || '')
  if (!userDataPath) throw new Error('ensureUv 需要 userDataPath')
  const existing = resolveUv(userDataPath)
  if (existing) return existing

  const key = `${process.platform}-${process.arch}`
  const asset = RUNTIME_UV.assets[key]
  if (!asset) {
    // 平台不支持是**明确的事实**，不能假装"等会儿再试"
    throw new Error(
      `本地运行时暂不支持当前平台 ${key}。\n` +
        `已支持的平台：${Object.keys(RUNTIME_UV.assets).join('、')}。`,
    )
  }

  const preferred = String(options.uvMirror || process.env[UV_MIRROR_ENV] || '').trim()
  const urls = RUNTIME_UV.mirrors.map(base => `${base.replace(/\/+$/, '')}/${asset.path}`)
  const ranked = await fetchTools.rankSources(urls, {
    fetch: options.fetch,
    preferred,
    timeout: options.probeTimeout,
  })
  if (!ranked.length) {
    throw new Error(
      `无法下载安装所需的组件（uv ${RUNTIME_UV.version}）：所有候选源都不可用。\n` +
        `已尝试：\n${urls.map(u => `  - ${u}`).join('\n')}\n` +
        '请检查网络/代理，或用 CHIRON_UV_MIRROR 指定可用的 PyPI 镜像后重启。',
    )
  }

  const dest = path.join(runtimeRoot(userDataPath), 'bin', asset.binary)
  const wheels = path.join(runtimeRoot(userDataPath), 'download')
  fs.mkdirSync(wheels, { recursive: true })
  const wheel = path.join(wheels, asset.file)

  const errors = []
  for (const source of ranked) {
    try {
      if (typeof options.onLine === 'function') options.onLine(`下载 uv ${RUNTIME_UV.version}：${source.url}`)
      await fetchTools.downloadTo(source.url, wheel, {
        fetch: options.fetch,
        timeout: options.downloadTimeout,
        onProgress: options.onProgress,
      })
      const digest = fetchTools.sha256File(wheel)
      if (digest !== asset.sha256) {
        // 多源下载后唯一的把关就是内容摘要：不符即失败，保留坏文件供排查
        throw new Error(`校验不通过（期望 ${asset.sha256}，实际 ${digest}）`)
      }
      // wheel 内二进制位置形如 `uv-<ver>.data/scripts/uv[.exe]`，按文件名匹配不依赖版本目录
      fetchTools.extractFromZip(wheel, name => name.endsWith(`/${asset.binary}`) || name === asset.binary, dest)
      try {
        fs.rmSync(wheel, { force: true })
      } catch {
        /* 清理失败不影响使用 */
      }
      if (!fs.existsSync(dest)) throw new Error(`解压后仍找不到 ${dest}`)
      return dest
    } catch (err) {
      errors.push(`${source.url}：${err && err.message ? err.message : err}`)
    }
  }
  throw new Error(
    `下载 uv 失败（所有源都试过了）：\n${errors.map(e => `  - ${e}`).join('\n')}\n` +
      '请检查网络/代理，或用 CHIRON_UV_MIRROR 指定可用的 PyPI 镜像后重启。',
  )
}

/**
 * 选一个 CPython 下载镜像：各候选源探测一次，取最快的。
 *
 * uv 默认从 GitHub 取 python-build-standalone，而实测 github.com 在很多网络下
 * 连 TCP 都不通 ⇒ 会卡死在"创建 Python 环境"。所以默认就把可达的镜像交给 uv。
 * 用户显式设置 CHIRON_PYTHON_MIRROR / UV_PYTHON_INSTALL_MIRROR 时以用户为准。
 */
async function pickPythonMirror(options = {}) {
  // 显式传值（含空串）时以调用方为准：空串表示"不探测、不设镜像"，
  // 测试与离线场景靠它避免真的联网。
  if (options.pythonMirror !== undefined) return String(options.pythonMirror || '').trim()
  const explicit = String(process.env[PYTHON_MIRROR_ENV] || process.env.UV_PYTHON_INSTALL_MIRROR || '').trim()
  if (explicit) return explicit
  const ranked = await fetchTools.rankSources(PYTHON_MIRRORS, {
    fetch: options.fetch,
    timeout: options.probeTimeout,
  })
  return ranked.length ? ranked[0].url : ''
}

/**
 * 解释器怎么来：**随包不带，也不在这里探测**。
 *
 * 只传版本号（见 `RUNTIME_PYTHON_VERSION`），由 uv 自己下到
 * `UV_PYTHON_INSTALL_DIR`，来源是 `pickPythonMirror()` 测速选出的镜像。
 * 以前这里还会去 `resources/python-runtime/` 找一个随包解释器——那条路已删除：
 * 安装包不携带任何运行时，留着它只会让"按需下载"在开发机上被悄悄绕过。
 */

/** 本地 core 源码目录（打包态在 resources/desktop-local） */
function coreSources(resourcesPath = process.resourcesPath) {
  if (!resourcesPath) return ''
  const candidate = path.join(resourcesPath, 'desktop-local')
  return fs.existsSync(path.join(candidate, 'app', 'main.py')) ? candidate : ''
}

/** 安装清单路径（与 venv 平级，放在 runtimeRoot 下） */
function manifestPath(userDataPath) {
  return path.join(runtimeRoot(userDataPath), MANIFEST_FILE)
}

/**
 * 依赖指纹：`pyproject.toml` 的内容摘要。
 *
 * **为什么用内容摘要而不是包版本**：判断"要不要重装依赖"只需要知道"依赖声明变没变"。
 * 拿 pyproject 的内容算摘要，零成本、纯本地、离线可用；而 `uv pip install --dry-run`
 * 需要解析 index（离线必失败，还慢），不适合放在开机路径上。
 *
 * 摘要里带上 `RUNTIME_PYTHON_VERSION`：以后升到 3.13 时旧 venv 必须重建，
 * 否则会拿 3.12 的解释器去跑声明为 3.13 的依赖。
 */
function dependencyFingerprint(sources) {
  const spec = path.join(sources, 'pyproject.toml')
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
function readManifest(userDataPath) {
  try {
    const raw = fs.readFileSync(manifestPath(userDataPath), 'utf8')
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
function writeManifest(userDataPath, fields) {
  const target = manifestPath(userDataPath)
  const payload = {
    version: MANIFEST_VERSION,
    pythonVersion: RUNTIME_PYTHON_VERSION,
    installedAt: new Date().toISOString(),
    ...fields,
  }
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true })
    const staging = `${target}.tmp`
    fs.writeFileSync(staging, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
    fs.renameSync(staging, target)
    return payload
  } catch {
    // 清单只是加速缓存，写不进去不该让整个安装失败
    return payload
  }
}

/**
 * 运行时现状：只读判断，不做任何安装动作。
 *
 * 返回 `{ ready, venvPython, reason, message }`。`ready: true` 才允许直接起 core。
 */
function inspectRuntime(options = {}) {
  const userDataPath = String(options.userDataPath || '')
  const sources = options.sources !== undefined ? options.sources : coreSources(options.resourcesPath)
  const python = userDataPath ? venvPython(userDataPath) : ''

  if (!python || !fs.existsSync(python)) {
    return {
      ready: false,
      venvPython: python,
      reason: 'venv-missing',
      message: '尚未创建本地运行时（首次启动需要安装）',
    }
  }
  if (!sources) {
    return {
      ready: false,
      venvPython: python,
      reason: 'sources-missing',
      message: '安装包内缺少 desktop-local 源码，无法校验运行时',
    }
  }

  // 唯一可信判据：真实 import 整个 app 包（与启动前那道探测同一份实现）
  // `options.probe` 可注入带缓存的版本，避免"安装时探一次、启动前再探一次"重复付 1s
  const probeFn =
    typeof options.probe === 'function'
      ? options.probe
      : (python_, cwd_) =>
          probeRuntime(options.spawnSync || childProcess.spawnSync, python_, cwd_, {
            timeout: VERIFY_TIMEOUT_MS,
          })
  const probe = probeFn(python, sources)
  if (probe.ok) {
    return { ready: true, venvPython: python, reason: 'ok', message: '' }
  }
  return {
    ready: false,
    venvPython: python,
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
 * 首次安装 / 就地修复。
 *
 * `options.force` 为真时即使探测通过也重建 venv（用户点"修复"时用）。
 * 返回 `{ ok, venvPython, reinstalled, stages }`；失败抛错（带指引）。
 */
/** 组装 `uv pip install` 参数：依赖清单只有 pyproject.toml 一份 */
function pipInstallArgs(vpython, sources, indexUrl) {
  const args = [
    'pip',
    'install',
    '--python',
    vpython,
    // pyproject.toml 是依赖的唯一来源（uv 能直接解析它的 [project].dependencies），
    // 不另存一份 requirements.txt —— 两份清单必然漂移，而漂移的代价就是漏装 httpx 那类事故
    '-r',
    path.join(sources, 'pyproject.toml'),
  ]
  // 镜像：国内网络下官方源常常超时。用环境变量显式覆盖，不写死任何第三方地址
  if (indexUrl) args.push('--index-url', indexUrl)
  return args
}

/** 覆盖 uv 下载受管 CPython 的来源（不写死第三方地址，见 `uvEnv` 的说明） */
const PYTHON_MIRROR_ENV = 'CHIRON_PYTHON_MIRROR'
/** 覆盖 uv 自身的下载源（PyPI 镜像前缀）；报错信息里承诺过这个名字，必须真的读它 */
const UV_MIRROR_ENV = 'CHIRON_UV_MIRROR'

/**
 * uv 的运行环境：让受管 CPython 落在 userData，不污染全局 uv 缓存。
 *
 * 还要把「CPython 从哪下载」暴露出去。uv 默认从 GitHub Releases 取
 * python-build-standalone，而**很多网络环境下 github.com 根本不可达**
 * （实测连 TCP 都不通，uv 报 `invalid peer certificate: UnknownIssuer`）。
 * 这时包里带了 uv 也没用：工具在、原料取不到，照样装不出解释器 —— 这正是
 * "随包了却仍然起不来"的那类问题。
 *
 * 所以镜像要能从外部指定：
 *   - `CHIRON_PYTHON_MIRROR` —— 我们自己的约定，优先；
 *   - 用户已设好的 `UV_PYTHON_INSTALL_MIRROR` —— uv 的约定，原样传递。
 * **不写死任何第三方镜像地址**：镜像可达性因网络而异，写死等于把某一家服务商的
 * 可用性变成产品前提。
 */
function uvEnv(userDataPath, mirror) {
  const env = { UV_PYTHON_INSTALL_DIR: path.join(runtimeRoot(userDataPath), 'python') }
  const source = String(mirror || process.env[PYTHON_MIRROR_ENV] || '').trim()
  if (source) env.UV_PYTHON_INSTALL_MIRROR = source
  return env
}

/** 删掉旧 venv。半损坏的目录留着会让 uv 复用坏状态，重建等于没修 */
function removeVenv(userDataPath) {
  const dir = venvDir(userDataPath)
  if (!fs.existsSync(dir)) return
  try {
    fs.rmSync(dir, { recursive: true, force: true })
  } catch (err) {
    throw new Error(
      `无法清理旧的本地运行时（${dir}）：${err && err.message ? err.message : err}\n` +
        '通常是它有进程还在运行（例如上次没退干净的 core），请重启电脑后再试。',
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
  const userDataPath = String(options.userDataPath || '')
  if (!userDataPath) throw new Error('ensureRuntime 需要 userDataPath')

  const sources = options.sources !== undefined ? options.sources : coreSources(options.resourcesPath)
  if (!sources) {
    throw new Error(
      '安装包内缺少 desktop-local 源码（resources/desktop-local），无法准备运行时。请重新安装。',
    )
  }

  const onStage = typeof options.onStage === 'function' ? options.onStage : () => {}
  const vpython = venvPython(userDataPath)
  const indexUrl = String(options.indexUrl || process.env.CHIRON_PIP_INDEX_URL || '').trim()
  const fingerprint = dependencyFingerprint(sources)

  const stages = []
  const stage = (id, message) => {
    stages.push(id)
    onStage({ stage: id, message })
  }

  onStage({ stage: INSTALL_STAGES.probing, message: '检查本地运行时' })

  let probeNote = ''
  let trySync = false
  if (!options.force && fs.existsSync(vpython)) {
    const current = inspectRuntime({ ...options, userDataPath, sources })
    if (current.ready) {
      onStage({ stage: INSTALL_STAGES.ready, message: '本地运行时就绪' })
      return {
        ok: true,
        venvPython: vpython,
        reinstalled: false,
        repaired: false,
        stages: [INSTALL_STAGES.ready],
      }
    }
    probeNote = `（原状态：${current.message}）`
    // 只有"环境本身在、依赖声明也没变"时才值得先试增量同步：
    // 依赖变了说明 uv 需要重新解析整个依赖图，增量同步救不回来，直接重建更干脆。
    const manifest = readManifest(userDataPath)
    trySync = Boolean(manifest && fingerprint && manifest.dependencyFingerprint === fingerprint)
    if (!trySync) probeNote += '，依赖声明已变更或无安装记录，将重建'
  }

  const uv = options.uvPath !== undefined ? options.uvPath : resolveUv(userDataPath)
  if (!uv) {
    // 安装包不再携带 uv：首次启动在这里按需下载（多源测速 + SHA256 强校验）
    stage(INSTALL_STAGES.probing, '下载安装组件')
    if (typeof options.onLine === 'function') options.onLine('安装包不携带运行时，开始按需下载')
  }
  const uvPath = uv || (await ensureUv({ ...options, userDataPath }))
  // CPython 的下载源同样测速择优：不指定时默认给 uv 一个可达镜像，
  // 否则它会去 github.com 取 python-build-standalone（实测不可达）而卡死在建环境。
  const pythonMirror = await pickPythonMirror(options)

  const runUv = (args, timeout, label) =>
    runCommand(uvPath, args, {
      timeout,
      spawn: options.spawn,
      onLine: options.onLine,
      env: uvEnv(userDataPath, pythonMirror),
    }).catch(err => {
      err.message = `${label}失败：${err.message}`
      throw err
    })

  // —— 路径 2：增量同步。venv 在、依赖没变，只重装依赖 ——
  if (trySync) {
    stage(INSTALL_STAGES.syncingDeps, `同步依赖${probeNote}`)
    try {
      await runUv(pipInstallArgs(vpython, sources, indexUrl), INSTALL_TIMEOUT_MS, '同步依赖')
      stage(INSTALL_STAGES.verifying, '验证运行时')
      const afterSync = inspectRuntime({ ...options, userDataPath, sources })
      if (afterSync.ready) {
        writeManifest(userDataPath, { dependencyFingerprint: fingerprint })
        stage(INSTALL_STAGES.ready, '本地运行时就绪')
        return {
          ok: true,
          venvPython: vpython,
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
  removeVenv(userDataPath)

  // 解释器：安装包**不再携带**任何 CPython。默认只传版本号，由 uv 从上面选出的
  // 镜像下载受管解释器（`--python-preference only-managed` 保证不去碰用户机上那个
  // 可能缺模块、或是 Microsoft Store 占位符的 Python）。
  // `options.pythonPath` 仅用于开发/测试注入一个本地解释器绝对路径。
  const pythonArg =
    options.pythonPath !== undefined && options.pythonPath ? String(options.pythonPath) : RUNTIME_PYTHON_VERSION

  stage(INSTALL_STAGES.creatingVenv, `创建 Python ${RUNTIME_PYTHON_VERSION} 环境${probeNote}`)
  await runUv(
    [
      'venv',
      // 只用我们自己的解释器：用户机器上的 Python 可能缺模块、也可能是
      // Microsoft Store 占位符（能 spawn 但立刻退出），都不能用
      '--python',
      pythonArg,
      '--python-preference',
      'only-managed',
      venvDir(userDataPath),
    ],
    VENV_TIMEOUT_MS,
    '创建 Python 环境',
  )

  if (!fs.existsSync(vpython)) {
    throw new Error(`uv 报告建好了环境，但找不到解释器 ${vpython}。请重试；若反复出现请反馈此日志。`)
  }

  stage(INSTALL_STAGES.installingDeps, '安装依赖')
  await runUv(pipInstallArgs(vpython, sources, indexUrl), INSTALL_TIMEOUT_MS, '安装依赖')

  stage(INSTALL_STAGES.verifying, '验证运行时')
  const verified = inspectRuntime({ ...options, userDataPath, sources })
  if (!verified.ready) {
    throw new Error(
      `依赖装完了，但本地核心仍无法导入：${verified.message}\n` +
        '这通常意味着依赖清单里少了某个包。请把这段信息连同日志一起反馈。',
    )
  }

  writeManifest(userDataPath, { dependencyFingerprint: fingerprint })
  stage(INSTALL_STAGES.ready, '本地运行时就绪')
  return { ok: true, venvPython: vpython, reinstalled: true, repaired: false, stages }
}

module.exports = {
  RUNTIME_PYTHON_VERSION,
  INSTALL_STAGES,
  runtimeRoot,
  venvDir,
  venvPython,
  manifestPath,
  dependencyFingerprint,
  readManifest,
  writeManifest,
  downloadedUv,
  resolveUv,
  ensureUv,
  pickPythonMirror,
  RUNTIME_UV,
  PYTHON_MIRRORS,
  coreSources,
  inspectRuntime,
  ensureRuntime,
  _internal: { runCommand },
}
