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

/**
 * 随包 uv 的位置：`resources/uv/<platform>-<arch>/uv[.exe]`。
 *
 * 平台键与 `desktop-local/scripts/prepare_uv.py` 的 `current_platform_key()` 必须一致，
 * 否则构建期把 uv 放在一个目录、运行期去另一个目录找，永远找不到。
 */
function bundledUv(resourcesPath = process.resourcesPath) {
  if (!resourcesPath) return ''
  const key = `${process.platform}-${process.arch}`
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const candidate = path.join(resourcesPath, 'uv', key, exe)
  return fs.existsSync(candidate) ? candidate : ''
}

/**
 * 随包的受管 CPython 解释器（`resources/python-runtime/<platform>-<arch>/`）。
 *
 * **为什么解释器也要随包**：只带 uv 是**不够的**。`uv venv --python 3.12
 * --python-preference only-managed` 在本地没有受管 CPython 时，会去 GitHub Releases
 * 下载 python-build-standalone —— 而不少网络下 github.com 连 TCP 都不通（实测报
 * `invalid peer certificate: UnknownIssuer`）。于是"包里带了 uv"仍然装不出解释器，
 * 用户看到的就是首次启动永远卡在创建环境、页面全报服务繁忙。
 *
 * 所以随包一份**纯解释器**（不含 site-packages，约 67MB/平台，由
 * `scripts/prepare_python.py` 在构建期就位）；运行时用它的**绝对路径**建 venv，
 * uv 就不会去联网找解释器。依赖仍按 `pyproject.toml` 首次安装 —— 那一步走 PyPI。
 *
 * 与已废弃的旧形态 `resources/python` 的区别：那份是"解释器 + 已装依赖"的整包，
 * 会冻结构建机状态；这里只有解释器，依赖永远按清单现装。
 */
function bundledPython(resourcesPath = process.resourcesPath) {
  if (!resourcesPath) return ''
  const root = path.join(resourcesPath, 'python-runtime', `${process.platform}-${process.arch}`)
  let entries
  try {
    entries = fs.readdirSync(root, { withFileTypes: true })
  } catch {
    return ''
  }
  // uv 的安装目录形如 cpython-3.12.16-windows-x86_64-none/；逐个找解释器，
  // 不硬编码版本号——版本升上去时构建脚本换目录名，这里不用跟着改。
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const base = path.join(root, entry.name)
    const candidates =
      process.platform === 'win32'
        ? [path.join(base, 'python.exe')]
        : [path.join(base, 'bin', 'python3'), path.join(base, 'bin', 'python')]
    for (const exe of candidates) {
      if (fs.existsSync(exe)) return exe
    }
  }
  return ''
}

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
  const sources = options.sources !== undefined ? options.sources : coreSources()
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

  const sources = options.sources !== undefined ? options.sources : coreSources()
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

  const uv = options.uvPath !== undefined ? options.uvPath : bundledUv()
  if (!uv) {
    throw new Error(
      '安装包内缺少 uv（resources/uv）。这是安装包不完整导致的问题，请重新安装。\n' +
        `开发者请在打包前执行：python desktop-local/scripts/prepare_uv.py（目标平台 ${process.platform}-${process.arch}）`,
    )
  }

  const runUv = (args, timeout, label) =>
    runCommand(uv, args, {
      timeout,
      spawn: options.spawn,
      onLine: options.onLine,
      // 传 pythonMirror：建 venv 时要下载受管 CPython，那是唯一必须走 GitHub 的一步
      env: uvEnv(userDataPath, options.pythonMirror),
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

  // 解释器优先用随包的那份。传**绝对路径**时 uv 不会去联网找解释器；
  // 传版本号（如 "3.12"）则只能在本地没有受管 CPython 时才去 GitHub 下载，
  // 那条路在 github.com 不可达的网络下必然失败。详见 `bundledPython` 的说明。
  const bundledPythonPath = options.pythonPath !== undefined ? options.pythonPath : bundledPython()
  const pythonArg = bundledPythonPath || RUNTIME_PYTHON_VERSION

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
  bundledUv,
  bundledPython,
  coreSources,
  inspectRuntime,
  ensureRuntime,
  _internal: { runCommand },
}
