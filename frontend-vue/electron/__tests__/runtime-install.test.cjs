'use strict'
/**
 * 首次启动自装运行时的行为锁定（`electron/runtime-install.cjs`）。
 *
 * 这些用例守的是"首次启动能不能自己站起来"。它们出错的方式都很安静：
 *   - 装到程序目录 → 自动更新后环境消失；
 *   - 用标记位判"已安装" → venv 被删了一半仍然判就绪，启动时爆 `ModuleNotFoundError`；
 *   - 重装前不清旧 venv → uv 复用坏状态，重装等于没修；
 *   - 装完不清探测缓存 → 启动前那次"缺依赖"的结论还在缓存里，立刻再失败一次。
 *
 * 全部**不联网**：uv 的两次调用由假 spawn 代替，真正的下载与安装由端到端流程验证。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const installer = require('../runtime-install.cjs')

/** 造一个临时 resources 目录：有 desktop-local 源码，uv 是否在由调用方决定 */
function makeResources(t, { withUv = true, withPython = true } = {}) {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-inst-'))
  const sources = path.join(resources, 'desktop-local')
  fs.mkdirSync(path.join(sources, 'app'), { recursive: true })
  fs.writeFileSync(path.join(sources, 'app', 'main.py'), '# fake\n')
  fs.writeFileSync(path.join(sources, 'pyproject.toml'), '[project]\nname="x"\n')

  const key = `${process.platform}-${process.arch}`
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const uv = path.join(resources, 'uv', key, exe)
  if (withUv) {
    fs.mkdirSync(path.dirname(uv), { recursive: true })
    fs.writeFileSync(uv, '')
  }

  // 随包 CPython：目录名带版本，bundledPython() 靠遍历这一层找解释器
  const pythonDir = path.join(resources, 'python-runtime', key, 'cpython-3.12.16-test')
  const python = path.join(pythonDir, process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3'))
  if (withPython) {
    fs.mkdirSync(path.dirname(python), { recursive: true })
    fs.writeFileSync(python, '')
  }
  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  return { resources, sources, uv, python }
}

function makeUserData(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

/**
 * 假 spawn：按 uv 的两条子命令分别模拟。
 *
 * `uv venv` 必须**真的建出解释器文件** —— `ensureRuntime` 在 venv 之后会检查它，
 * 假 spawn 只返回退出码 0 而磁盘上什么都没有，等于把"装失败了却判成功"固化进测试。
 */
function fakeSpawn(t, { failOn = null, onVenv = null } = {}) {
  const calls = []
  // 记录真实进度：探测必须反映"磁盘上到底装到哪一步了"，用调用计数模拟会与
  // 真实顺序脱钩（第一次探测在 venv 不存在时直接返回，不会走 probe）
  const progress = { venv: false, pip: false }
  const spawn = (command, args) => {
    calls.push({ command, args })
    const child = new (require('node:events').EventEmitter)()
    child.stdout = new (require('node:events').EventEmitter)()
    child.stderr = new (require('node:events').EventEmitter)()
    child.kill = () => true

    const sub = args[0]
    const isFail = failOn && failOn(sub)

    setImmediate(() => {
      if (isFail) {
        child.stderr.emit('data', `error: ${sub} failed\n`)
        child.emit('close', 1)
        return
      }
      if (sub === 'venv') {
        progress.venv = true
        if (onVenv) onVenv()
      }
      if (sub === 'pip') progress.pip = true
      child.stdout.emit('data', `${sub} ok\n`)
      child.emit('close', 0)
    })
    return child
  }
  return { spawn, calls, progress }
}

/** 依"假 spawn 装到哪一步"回答探测：装了依赖才算就绪 */
function probeByProgress(progress) {
  return () =>
    progress.pip
      ? { ok: true, reason: 'ok', message: '' }
      : { ok: false, reason: 'venv-missing', message: '尚未创建' }
}

/** 让 venv 目录里出现"解释器"，模拟 uv 建环境成功 */
function createVenvPython(userData) {
  const python = installer.venvPython(userData)
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '')
  return python
}

test('inspectRuntime：venv 不存在时判未就绪，且不误报为可用', t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  const result = installer.inspectRuntime({ userDataPath: userData, sources })
  assert.equal(result.ready, false)
  assert.equal(result.reason, 'venv-missing')
})

test('inspectRuntime：venv 在但导入失败时不算就绪（不信文件存在）', t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  createVenvPython(userData)

  const result = installer.inspectRuntime({
    userDataPath: userData,
    sources,
    probe: () => ({ ok: false, reason: 'missing-dependency', message: '缺少模块 httpx' }),
  })
  assert.equal(result.ready, false)
  assert.equal(result.reason, 'missing-dependency')
})

test('inspectRuntime：导入通过才算就绪', t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  createVenvPython(userData)

  const result = installer.inspectRuntime({
    userDataPath: userData,
    sources,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })
  assert.equal(result.ready, true)
  assert.equal(result.reason, 'ok')
})

test('ensureRuntime：已就绪时直接返回，不跑 uv、不重建', async t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  createVenvPython(userData)
  const { spawn, calls } = fakeSpawn(t)

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    spawn,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, false)
  assert.equal(calls.length, 0, '已经就绪就不该再调 uv')
})

test('ensureRuntime：已就绪但没有安装记录时补记指纹', async t => {
  // 真机踩过的空档：依赖装完 → 探测因**别的原因**失败（当年是缺 boto3、以及探测没带
  // 可导入所需的密钥）→ 全量重建那次 writeManifest 根本没执行。后来探测修好了，
  // 环境直接走"已就绪"返回，能用却永远没有记录；而 `trySync` 判据是"指纹记过且相等"，
  // 于是下一次探测再失败只能删掉整个 venv 重装（引擎那份实测 340MB / 两百个包）。
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  createVenvPython(userData)
  const { spawn } = fakeSpawn(t)

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    spawn,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(result.ok, true)
  const recorded = installer.readManifest(userData, 'core')
  assert.ok(recorded, '探测通过却没留下任何记录')
  assert.equal(
    recorded.dependencyFingerprint,
    installer.dependencyFingerprint(sources, 'core'),
    '补记的必须是当前依赖声明的指纹，否则下次照样判"没装过"',
  )
})

test('ensureRuntime：已有安装记录时不改写它', async t => {
  // 指纹不符是"依赖变了 → 重建一个干净环境"的唯一触发器。
  // 每次正常启动都把当前指纹写回去，等于把这个触发器抹掉：被删掉的依赖
  // 永远留在环境里（探测只看能否 import，多装的东西不会让它失败）。
  const userData = makeUserData(t)
  const { sources } = makeResources(t)
  createVenvPython(userData)
  const { spawn } = fakeSpawn(t)
  installer.writeManifest(userData, { dependencyFingerprint: 'stale-fingerprint' }, 'core')

  await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    spawn,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(
    installer.readManifest(userData, 'core').dependencyFingerprint,
    'stale-fingerprint',
    '已就绪的返回路径不得覆盖既有记录',
  )
})

test('ensureRuntime：缺 uv 时报安装包残缺，不再联网下载', async t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t, { withUv: false })
  const { spawn } = fakeSpawn(t)

  // uv 随包分发，缺了只能是打包出错 —— 不该再走"下载 15MB 试试看"那条路，
  // 也不该让用户去调 CHIRON_UV_MIRROR：换源解决不了安装包残缺。
  await assert.rejects(
    () =>
      installer.ensureRuntime({
        pythonMirror: '',
        userDataPath: userData,
        sources,
        spawn,
        uvPath: '',
        probe: () => ({ ok: false, reason: 'venv-missing', message: '无' }),
      }),
    err => {
      assert.match(err.message, /uv/, '要说清缺的是什么')
      assert.match(err.message, /重新安装/, '安装包残缺的唯一出路就是重装')
      assert.doesNotMatch(err.message, /CHIRON_UV_MIRROR/, '换镜像解决不了包内缺文件')
      return true
    },
  )
})

test('ensureRuntime：完整流程先建 venv 再装依赖，最后做导入验证', async t => {
  const userData = makeUserData(t)
  const { sources, uv, python } = makeResources(t)
  const stages = []
  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    onStage: info => stages.push(info.stage),
    // 探测按"装到哪一步"回答，与真实顺序一致（venv 不存在时根本不会走 probe）
    probe: probeByProgress(progress),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, true)
  assert.equal(calls.length, 2, '应当正好两条 uv 命令：venv + pip install')
  assert.equal(calls[0].args[0], 'venv')
  assert.equal(calls[1].args[0], 'pip')
  assert.ok(calls[1].args.includes('install'))
  // venv 这条必须带 only-managed，否则会用用户机器上那个可能坏掉的 Python
  assert.ok(calls[0].args.includes('only-managed'))
  assert.ok(calls[0].args.includes(python), '解释器应传随包 CPython 的绝对路径')
  // 依赖来源必须是源码目录里的 pyproject.toml（唯一来源，不与 requirements.txt 漂移）
  assert.ok(
    calls[1].args.some(a => a.endsWith('pyproject.toml')),
    '依赖清单必须是 pyproject.toml',
  )
  assert.deepEqual(stages, [
    installer.INSTALL_STAGES.probing,
    installer.INSTALL_STAGES.creatingVenv,
    installer.INSTALL_STAGES.installingDeps,
    installer.INSTALL_STAGES.verifying,
    installer.INSTALL_STAGES.ready,
  ])
})

test('ensureRuntime：重装前删掉旧 venv，不让 uv 复用坏状态', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  // 造一个"上次装了一半"的 venv：目录在、解释器在，但探测判它坏
  createVenvPython(userData)
  const stale = path.join(installer.venvDir(userData), 'stale-marker')
  fs.writeFileSync(stale, 'old')

  let venvExistedAtSpawn = null
  const { spawn, progress } = fakeSpawn(t, {
    onVenv: () => {
      venvExistedAtSpawn = fs.existsSync(stale)
      createVenvPython(userData)
    },
  })

  await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  assert.equal(venvExistedAtSpawn, false, '开建新 venv 之前必须先把旧目录删干净')
})

test('ensureRuntime：依赖装完但导入仍失败时抛错，不谎报成功', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const { spawn } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  await assert.rejects(
    () =>
      installer.ensureRuntime({
    pythonMirror: '',
        userDataPath: userData,
        sources,
        uvPath: uv,
        spawn,
        // 全程判未就绪：模拟 pyproject 漏了某个依赖
        probe: () => ({ ok: false, reason: 'missing-dependency', message: '缺少模块 httpx' }),
      }),
    err => {
      assert.match(err.message, /仍无法导入/)
      assert.match(err.message, /httpx/)
      return true
    },
  )
})

test('ensureRuntime：uv 命令失败时把输出带进错误，便于定位', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const { spawn } = fakeSpawn(t, {
    failOn: sub => sub === 'pip',
    onVenv: () => createVenvPython(userData),
  })

  await assert.rejects(
    () =>
      installer.ensureRuntime({
    pythonMirror: '',
        userDataPath: userData,
        sources,
        uvPath: uv,
        spawn,
        probe: () => ({ ok: false, reason: 'venv-missing', message: '尚未创建' }),
      }),
    err => {
      assert.match(err.message, /退出码 1/)
      assert.match(err.message, /pip failed/, '要带上 uv 的真实输出')
      return true
    },
  )
})

test('ensureRuntime：镜像源通过环境变量显式覆盖，不写死任何第三方地址', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    indexUrl: 'https://mirror.example/simple',
    probe: probeByProgress(progress),
  })

  const pipArgs = calls[1].args
  assert.ok(pipArgs.includes('--index-url'))
  assert.ok(pipArgs.includes('https://mirror.example/simple'))
})

test('venvPython：路径落在 userData/local/runtime 下，不进程序目录', t => {
  const userData = makeUserData(t)
  const python = installer.venvPython(userData)
  const normalized = python.split(path.sep).join('/')

  assert.match(normalized, /local\/runtime\/venv\//)
  assert.ok(python.startsWith(userData), '必须装在 userData 下：程序目录会被自动更新替换')
})

test('resolveUv：只看 userData 里已下载的那份，绝不去 resources 里找随包 uv', t => {
  const { resources } = makeResources(t) // 即便在 resources/uv 下造了一份
  const userData = makeUserData(t)
  assert.equal(installer.resolveUv(userData), '', '安装包不携带运行时，这里必须为空')
  assert.equal(installer.resolveUv(''), '')

  // 只有落进 userData/local/runtime/bin 的那份才算数
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const bin = path.join(installer.runtimeRoot(userData), 'bin', exe)
  fs.mkdirSync(path.dirname(bin), { recursive: true })
  fs.writeFileSync(bin, '')
  assert.equal(installer.resolveUv(userData), bin)
  assert.ok(!installer.resolveUv(userData).startsWith(resources), '不能取自 resources')
})

test('dependencyFingerprint：pyproject 改动会改变指纹', t => {
  const { sources } = makeResources(t)
  const before = installer.dependencyFingerprint(sources)

  fs.appendFileSync(path.join(sources, 'pyproject.toml'), 'httpx>=0.27\n')
  const after = installer.dependencyFingerprint(sources)

  assert.notEqual(before, after, '依赖声明变了，指纹必须跟着变，否则会漏装新依赖')
  assert.match(before, /^[0-9a-f]{64}$/)
})

test('dependencyFingerprint：读不到 pyproject 时返回空串（不拿空串当未变更）', t => {
  const { sources } = makeResources(t)
  fs.rmSync(path.join(sources, 'pyproject.toml'))
  assert.equal(installer.dependencyFingerprint(sources), '')
})

test('writeManifest / readManifest：落地后可原样读回', t => {
  const userData = makeUserData(t)
  installer.writeManifest(userData, { dependencyFingerprint: 'abc123' })

  const manifest = installer.readManifest(userData)
  assert.equal(manifest.dependencyFingerprint, 'abc123')
  assert.equal(manifest.pythonVersion, installer.RUNTIME_PYTHON_VERSION)
  assert.ok(manifest.installedAt, '要留安装时间，排查时能对上版本')
})

test('readManifest：清单损坏时返回 null，不抛出（当作没装过）', t => {
  const userData = makeUserData(t)
  fs.mkdirSync(installer.runtimeRoot(userData), { recursive: true })
  fs.writeFileSync(installer.manifestPath(userData), '{ 半截 json')
  assert.equal(installer.readManifest(userData), null)
})

test('ensureRuntime：依赖声明未变时不重建 venv，只增量同步依赖', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  createVenvPython(userData)
  // 上次装好后写了清单，指纹与现在一致
  installer.writeManifest(userData, {
    dependencyFingerprint: installer.dependencyFingerprint(sources),
  })

  const { spawn, calls, progress } = fakeSpawn(t)
  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  assert.equal(result.ok, true)
  assert.equal(result.repaired, true, '应标记为就地修复')
  assert.equal(result.reinstalled, false, '不该重建 venv')
  assert.equal(calls.length, 1, '只该有一条 uv pip install，不该再跑 uv venv')
  assert.equal(calls[0].args[0], 'pip')
})

test('ensureRuntime：依赖声明变更时走全量重建，不做增量同步', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  createVenvPython(userData)
  // 记录的指纹是旧的（模拟打包升级后 pyproject 变了）
  installer.writeManifest(userData, { dependencyFingerprint: 'stale-fingerprint' })

  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })
  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  assert.equal(result.reinstalled, true, '依赖变了就该重建')
  assert.equal(calls[0].args[0], 'venv', '第一步必须是 uv venv')
  assert.equal(calls.length, 2)
})

test('ensureRuntime：增量同步失败时自动落到全量重建，不让首次启动直接死掉', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  createVenvPython(userData)
  installer.writeManifest(userData, {
    dependencyFingerprint: installer.dependencyFingerprint(sources),
  })

  // 第一次 pip（同步）失败，之后的 pip（重建里的安装）成功
  let pipCount = 0
  const { spawn, calls, progress } = fakeSpawn(t, {
    failOn: sub => sub === 'pip' && pipCount++ === 0,
    onVenv: () => createVenvPython(userData),
  })
  // 重建时的 uv venv 会重造解释器；这里让它保持"能探测通过"
  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, true)
  assert.deepEqual(
    calls.map(c => c.args[0]),
    ['pip', 'venv', 'pip'],
    '顺序应是：先试同步，失败后重建再装',
  )
})

test('ensureRuntime：安装成功后写入清单，下次启动才敢走增量同步', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const { spawn, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  const manifest = installer.readManifest(userData)
  assert.ok(manifest, '装完必须留下清单')
  assert.equal(manifest.dependencyFingerprint, installer.dependencyFingerprint(sources))
})

test('ensureRuntime：uv 与解释器都取自随包资源，不联网、不碰 userData 的下载版', async t => {
  const userData = makeUserData(t)
  // makeResources 造出 resources/uv/<platform>/uv.exe 与
  // resources/python-runtime/<platform>/cpython-*/python.exe
  const { resources, sources, uv: bundledUv, python: bundledPython } = makeResources(t)

  // 再放一份"旧版本下载到 userData 的 uv"：随包那份必须优先于它
  const exe = process.platform === 'win32' ? 'uv.exe' : 'uv'
  const downloaded = path.join(installer.runtimeRoot(userData), 'bin', exe)
  fs.mkdirSync(path.dirname(downloaded), { recursive: true })
  fs.writeFileSync(downloaded, '')

  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    resourcesPath: resources,
    sources,
    spawn,
    probe: probeByProgress(progress),
  })

  const venv = calls.find(c => c.args[0] === 'venv')
  assert.ok(venv, '应调用 uv venv')

  // 随包 uv 是确定的、离线可用的，必须优先于 userData 里的旧下载版
  assert.equal(venv.command, bundledUv, 'uv 应取自随包资源')
  assert.notEqual(venv.command, downloaded, '不该再优先用 userData 里的下载版')

  // 解释器直接传随包 CPython 的绝对路径：uv 因此不会去 github 取
  // python-build-standalone（那一步在不少网络下直接卡死）
  const pythonArg = venv.args[venv.args.indexOf('--python') + 1]
  assert.equal(pythonArg, bundledPython, '解释器应传随包 CPython 的绝对路径')
  assert.ok(
    pythonArg !== installer.RUNTIME_PYTHON_VERSION,
    '不该再退化成只传版本号让 uv 联网取',
  )
})

/* --------------------------------------------------------------------------
 * M50：venv 与随包解释器之间的绑定（pyvenv.cfg 的 home）
 *
 * 守的是"换了安装包之后，已装好的环境还能不能用"。真机现场：userData 里那两套 venv
 * 是跨包共享的（引擎那份实测 340MB、两百个包、重建要联网），而 `pyvenv.cfg` 的 home
 * 写的是**创建它那次出包的绝对路径** —— core 指向 `D:\Program Files\Chiron\…`，
 * 引擎指向某次临时验证出包的 `release-m47\…`。删掉那个目录或换安装位置，解释器就以
 * 103 退出（`No Python at '…'`），而上层只看到"探测失败"，于是全量重建。
 * ------------------------------------------------------------------------ */

/** 随包运行时的目录与"该写进 home 的那一层"（两个平台不一样，用例里按同一个口径造） */
function homeOf(bundledPythonPath) {
  return path.dirname(bundledPythonPath)
}

/** 一份指向别处的 pyvenv.cfg 文本（照 uv 0.13.6 真实落盘的键与顺序） */
function venvCfgText(home, versionInfo = '3.12.16') {
  return (
    `home = ${home}\n` +
    'implementation = CPython\n' +
    'uv = 0.13.6\n' +
    `version_info = ${versionInfo}\n` +
    'include-system-site-packages = false\n'
  )
}

/**
 * 造一份"看起来装着、其实跑不起来"的 venv。
 *
 * 解释器落的是**普通文件**（Windows 的 `Scripts\python.exe` 跳转器、POSIX `--copies`
 * 建出来的那份都是这样）：home 失效时文件明明还在，`existsSync` 会骗人 —— 这正是
 * 真机 103 退出的形态。POSIX 下"悬空符号链接"那种坏法由
 * `relinkPosixInterpreter` 那条用例直接覆盖（它是平台分支，在 Windows 上跑不到同一段）。
 */
function makeVenvWithBinding(t, userData, { target = 'core', home, versionInfo = '3.12.16' } = {}) {
  const venv = installer.venvDir(userData, target)
  const python = installer.venvPython(userData, target)
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '')
  const cfg = path.join(venv, 'pyvenv.cfg')
  fs.writeFileSync(cfg, venvCfgText(home, versionInfo), 'utf8')
  return { venv, python, cfg }
}

test('home 的口径：Windows 是包根，POSIX 是 <包根>/bin', () => {
  const { homeForRuntime, interpreterUnderHome } = installer._internal
  assert.equal(homeForRuntime('C:\\r\\cpython-3.12.16-windows-x86_64-none', 'win32'),
    'C:\\r\\cpython-3.12.16-windows-x86_64-none')
  assert.equal(interpreterUnderHome(homeForRuntime('C:\\r\\cpython-3.12.16-w', 'win32'), 'win32'),
    path.win32.join('C:\\r\\cpython-3.12.16-w', 'python.exe'))

  // POSIX：受管解释器在包根的 bin 里，home 必须写这一层，解释器名是 python3
  assert.equal(homeForRuntime('/app/resources/python-runtime/cpython-3.12.16-linux_x86_64', 'linux'),
    '/app/resources/python-runtime/cpython-3.12.16-linux_x86_64/bin')
  assert.equal(
    interpreterUnderHome(homeForRuntime('/app/r/cpython-3.12.16', 'darwin'), 'darwin'),
    '/app/r/cpython-3.12.16/bin/python3',
  )
})

test('minorVersion：只取 minor，认不出返回空串', () => {
  const { minorVersion } = installer._internal
  assert.equal(minorVersion('3.12.16'), '3.12')
  assert.equal(minorVersion('3.13.0'), '3.13')
  assert.equal(minorVersion(''), '')
  assert.equal(minorVersion(undefined), '')
  assert.equal(minorVersion('unknown'), '')
})

test('rebindVenvBinding：home 失效时改绑到随包解释器，且只动 home 那一行', t => {
  const userData = makeUserData(t)
  const { resources, python } = makeResources(t)
  const dead = path.join(resources, 'gone-package')
  const made = makeVenvWithBinding(t, userData, { home: dead })

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, true)
  assert.equal(result.reason, 'rebound')
  assert.equal(result.from, dead)
  assert.equal(result.to, homeOf(python), '改绑的目标就是随包解释器所在的那一层')
  assert.ok(fs.existsSync(installer._internal.interpreterUnderHome(result.to)), '改绑后 home 里要有解释器')

  const after = fs.readFileSync(made.cfg, 'utf8')
  assert.ok(after.startsWith(`home = ${homeOf(python)}\n`), `home 行应换成随包路径：${after}`)
  // uv 还往里写 implementation / uv / version_info / include-system-site-packages，
  // 重写整个文件等于丢掉这些口径（下次 uv 复用这份 cfg 时会按错的来判断）
  assert.ok(after.includes('uv = 0.13.6'), '其它键必须原样保留')
  assert.ok(after.includes('version_info = 3.12.16'), '其它键必须原样保留')
  assert.ok(after.includes('include-system-site-packages = false'), '其它键必须原样保留')
  assert.ok(!fs.existsSync(`${made.cfg}.tmp`), '临时文件要 rename 掉，不留半个 cfg')
})

test('rebindVenvBinding：绑定本来就是活的时一个字节都不改', t => {
  const userData = makeUserData(t)
  const { resources, python } = makeResources(t)
  const made = makeVenvWithBinding(t, userData, { home: homeOf(python) })
  const before = fs.readFileSync(made.cfg, 'utf8')

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, false)
  assert.equal(result.reason, 'binding-ok')
  assert.equal(fs.readFileSync(made.cfg, 'utf8'), before, '健康状态不该被"顺手重写"')
})

test('rebindVenvBinding：minor 版本对不上时拒绝改绑，并说明只能重建', t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t)
  const made = makeVenvWithBinding(t, userData, {
    home: path.join(resources, 'gone-package'),
    versionInfo: '3.13.1',
  })
  const before = fs.readFileSync(made.cfg, 'utf8')

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, false)
  assert.equal(result.reason, 'version-mismatch')
  assert.match(result.message, /3\.13\.1/, '要说清环境是哪一版建的')
  assert.match(result.message, /3\.12\.16/, '要说清包里是哪一版')
  assert.match(result.message, /重建/)
  assert.equal(fs.readFileSync(made.cfg, 'utf8'), before, '改不了就不该碰文件')
})

test('rebindVenvBinding：cfg 没记版本（不是 uv 建的）时拒绝改绑', t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t)
  const venv = installer.venvDir(userData)
  fs.mkdirSync(path.dirname(installer.venvPython(userData)), { recursive: true })
  fs.writeFileSync(installer.venvPython(userData), '')
  const cfg = path.join(venv, 'pyvenv.cfg')
  fs.writeFileSync(cfg, 'home = /does/not/exist/bin\nimplementation = CPython\n', 'utf8')

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, false)
  assert.equal(result.reason, 'version-mismatch')
})

test('rebindVenvBinding：包里没有可用解释器时不改绑（残缺包由重装解决）', t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t, { withPython: false })
  makeVenvWithBinding(t, userData, { home: path.join(resources, 'gone-package') })

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, false)
  assert.equal(result.reason, 'no-bundled-runtime')
})

test('rebindVenvBinding：没有 venv 或没有 cfg 时按"从没装过"分流，不抛错', t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t)

  const empty = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })
  assert.equal(empty.rebound, false)
  assert.equal(empty.reason, 'no-binding-file')

  const noUserData = installer.rebindVenvBinding({ resourcesPath: resources })
  assert.equal(noUserData.reason, 'no-user-data')

  // 目录在、cfg 缺 home 行：仍然没有绑定可言
  fs.mkdirSync(installer.venvDir(userData), { recursive: true })
  fs.writeFileSync(path.join(installer.venvDir(userData), 'pyvenv.cfg'), 'uv = 0.13.6\n', 'utf8')
  assert.equal(
    installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources }).reason,
    'no-binding-file',
  )
})

test('rebindVenvBinding：路径里有 $& 时按字面写入 home', t => {
  // String.replace 的**替换串**里 `$&`、`` $` ``、`$1` 都是特殊序列。用模板拼接
  // 写法时，装到这类路径下会把 home 写成别的东西 —— 文件是"改好了"，解释器照样找不到。
  const userData = makeUserData(t)
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-dollar-'))
  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  const key = `${process.platform}-${process.arch}`
  const runtimeDir = path.join(resources, 'python-runtime', key, 'cpython-3.12.16-$&x')
  const exe = path.join(runtimeDir, process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3'))
  fs.mkdirSync(path.dirname(exe), { recursive: true })
  fs.writeFileSync(exe, '')

  makeVenvWithBinding(t, userData, { home: path.join(resources, 'gone') })
  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, true)
  assert.equal(
    installer.readVenvCfg(installer.venvDir(userData)).home,
    homeOf(exe),
    'home 必须是字面路径，不能被 $& 之类的替换序列吃掉',
  )
})

test('rebindVenvBinding：补丁号不同也照改（只有 minor 变了才拒绝）', t => {
  // 随包 CPython 的补丁号会跟着发版动。要求整串版本号相同，等于每次升级都白重建
  // 一次 340MB、还要联网的环境 —— 那正是本函数要消掉的代价；而 `.cp312-*.pyd`
  // 这类扩展在 3.12 系列内是 ABI 兼容的，改绑安全。
  const userData = makeUserData(t)
  const { resources, python } = makeResources(t)
  makeVenvWithBinding(t, userData, {
    home: path.join(resources, 'gone-package'),
    versionInfo: '3.12.9',
  })

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, true, '3.12.9 → 3.12.16 应该改绑了事')
  assert.equal(result.to, homeOf(python))
})

test('rebindVenvBinding：home 目录还在但解释器没了，同样算绑坏', t => {
  // 判据只看目录 = 漏掉"安全软件把 python.exe 单独隔离走"这一种，而它的死法
  // 与目录被删完全一样（解释器跑不起来、只能改绑）。
  const userData = makeUserData(t)
  const { resources, python } = makeResources(t)
  // resources 这一层确实存在，但它根上没有 python.exe
  assert.ok(fs.existsSync(resources))
  makeVenvWithBinding(t, userData, { home: resources })

  const result = installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources })

  assert.equal(result.rebound, true)
  assert.equal(result.from, resources)
  assert.equal(result.to, homeOf(python))
})

/**
 * POSIX 上 home 失效的形态是**悬空符号链接**：`existsSync` 为假，旧的判法把它当成
 * "从没装过"，于是走全量重建。这里在 Windows 上用一条指向不存在目标的符号链接造出
 * 同样的"文件项在、解析不到"的状态 —— 修链接本身只在 POSIX 跑（由上面那条用例覆盖），
 * 这条用例锁的是**顺序**：改绑必须排在"解释器在不在"这道判定之前。
 */
function makeDanglingVenv(t, userData, home) {
  const venv = installer.venvDir(userData)
  const python = installer.venvPython(userData)
  fs.mkdirSync(path.dirname(python), { recursive: true })
  try {
    fs.symlinkSync(path.join(home, 'python-missing-here'), python)
  } catch (err) {
    return { skipped: err && err.code }
  }
  const cfg = path.join(venv, 'pyvenv.cfg')
  fs.writeFileSync(cfg, venvCfgText(home), 'utf8')
  return { venv, python, cfg }
}

test('bundledRuntime：目录名对得上但解释器不在，不算可用运行时', t => {
  // 半个包（安装中断 / 被隔离软件吃掉 python.exe）如果被当成可用，改绑就会把 home
  // 指向一个跑不起来的目标 —— 那比不修更糟：环境从此再也回不到"能重建"的状态。
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-halfpkg-'))
  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  const key = `${process.platform}-${process.arch}`
  fs.mkdirSync(path.join(resources, 'python-runtime', key, 'cpython-3.12.16-windows-x86_64-none'), {
    recursive: true,
  })

  assert.deepEqual(installer.bundledRuntime(resources), { dir: '', version: '' })
  assert.equal(installer.bundledPython(resources), '')

  const userData = makeUserData(t)
  makeVenvWithBinding(t, userData, { home: path.join(resources, 'gone') })
  assert.equal(
    installer.rebindVenvBinding({ userDataPath: userData, resourcesPath: resources }).reason,
    'no-bundled-runtime',
  )
})

test('inspectRuntime：改绑排在「解释器在不在」之前，不把坏绑定当成没装过', t => {
  const userData = makeUserData(t)
  const { resources, sources } = makeResources(t)
  const made = makeDanglingVenv(t, userData, path.join(resources, 'gone-package'))
  if (made.skipped) {
    t.skip(`这台机器建不了符号链接（${made.skipped}），顺序锁不住`)
    return
  }

  const result = installer.inspectRuntime({
    userDataPath: userData,
    sources,
    resourcesPath: resources,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(result.binding.rebound, true, 'cfg 已经坏了，就必须在判"在不在"之前先改绑')
  assert.equal(installer.readVenvCfg(made.venv).home, homeOf(installer.bundledPython(resources)))
})

test('ensureRuntime：坏绑定先修再判，POSIX 那种坏法不会被当成首次安装', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const made = makeDanglingVenv(t, userData, path.join(sources, '..', 'gone-package'))
  if (made.skipped) {
    t.skip(`这台机器建不了符号链接（${made.skipped}），顺序锁不住`)
    return
  }
  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })
  const lines = []

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: () => (progress.pip ? { ok: true, reason: 'ok', message: '' } : { ok: false, reason: 'venv-missing', message: '尚未创建' }),
    onLine: line => lines.push(line),
  })

  assert.ok(
    lines.some(line => /绑定已失效/.test(line)),
    `改绑要发生在判"venv 在不在"之前并留下日志：${JSON.stringify(lines)}`,
  )
  assert.equal(result.ok, true)
  assert.deepEqual(calls.map(c => c.args[0]), ['venv', 'pip'], '链接修不好（本机模拟不了 POSIX 的 relink）时仍要落到重建')
})

test('relinkPosixInterpreter：只把悬空链接指回去，活链接与真文件不碰', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-relink-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const runtimeDir = path.join(dir, 'cpython-3.12.16-test')
  // 随包解释器：Windows 在包根、POSIX 在 bin（用当前平台的真实口径造）
  const exe = installer._internal.interpreterUnderHome(
    installer._internal.homeForRuntime(runtimeDir),
  )
  fs.mkdirSync(path.dirname(exe), { recursive: true })
  fs.writeFileSync(exe, '')

  const venv = path.join(dir, 'venv')
  fs.mkdirSync(path.join(venv, 'bin'), { recursive: true })
  const dangling = path.join(venv, 'bin', 'python3')
  fs.symlinkSync(path.join(dir, 'gone', 'python3'), dangling)
  const live = path.join(venv, 'bin', 'python')
  fs.symlinkSync(exe, live)

  const relinked = installer._internal.relinkPosixInterpreter(venv, runtimeDir)

  assert.deepEqual(relinked, [dangling], '只有那个悬空的链接需要重指')
  assert.equal(fs.existsSync(dangling), true, '重指后要能解析到随包解释器')
  assert.equal(fs.realpathSync(dangling).toLowerCase(), fs.realpathSync(exe).toLowerCase())
  assert.equal(fs.existsSync(live), true, '活链接保持原样')
})

test('inspectRuntime：先改绑再探测，探测器看到的 home 已经是修好的', t => {
  const userData = makeUserData(t)
  const { resources, sources, python } = makeResources(t)
  const made = makeVenvWithBinding(t, userData, { home: path.join(resources, 'gone-package') })

  const seen = []
  const result = installer.inspectRuntime({
    userDataPath: userData,
    sources,
    resourcesPath: resources,
    probe: (py, cwd, label) => {
      // 探测是在**改绑之后**跑的：此刻 cfg 的 home 必须已经是随包那一份。
      // 顺序写反（先判 existsSync / 先探测，再改绑）时这里读到的还是失效路径。
      seen.push({ label, home: installer.readVenvCfg(made.venv).home })
      return { ok: true, reason: 'ok', message: '' }
    },
  })

  assert.equal(seen.length, 1)
  assert.equal(seen[0].home, homeOf(python), '探测前 home 必须已经修好')
  assert.equal(result.ready, true)
  assert.equal(result.binding.rebound, true)
  assert.equal(result.binding.reason, 'rebound')
})

test('inspectRuntime：健康环境的绑定不被改动，返回值里带 binding-ok', t => {
  const userData = makeUserData(t)
  const { sources, python } = makeResources(t)
  const made = makeVenvWithBinding(t, userData, { home: homeOf(python) })
  const before = fs.readFileSync(made.cfg, 'utf8')

  const result = installer.inspectRuntime({
    userDataPath: userData,
    sources,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(result.ready, true)
  assert.equal(result.binding.reason, 'binding-ok')
  assert.equal(fs.readFileSync(made.cfg, 'utf8'), before)
})

test('ensureRuntime：换包导致 home 失效时改绑了事，一条 uv 命令都不跑', async t => {
  const userData = makeUserData(t)
  const { sources, uv, python } = makeResources(t)
  makeVenvWithBinding(t, userData, { home: path.join(sources, '..', 'gone-package') })
  const { spawn, calls } = fakeSpawn(t)
  const lines = []

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
    onLine: line => lines.push(line),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, false, '不该为此重建 340MB 环境')
  assert.equal(result.repaired, false)
  assert.deepEqual(calls, [], '改绑就修好了，不该跑任何 uv 命令')
  assert.equal(lines.length, 1, `应记一条改绑日志：${JSON.stringify(lines)}`)
  assert.match(lines[0], /绑定已失效/)
  assert.ok(lines[0].includes(homeOf(python)), `日志要写清改绑到了哪：${lines[0]}`)
})

test('ensureRuntime：minor 版本对不上时不做无用的增量同步，直接把真因写进阶段', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  makeVenvWithBinding(t, userData, {
    home: path.join(sources, '..', 'gone-package'),
    versionInfo: '3.13.1',
  })
  // 先写一份"依赖声明没变"的清单：没有 M50 的分支时这条用例走的是增量同步
  installer.writeManifest(userData, { dependencyFingerprint: installer.dependencyFingerprint(sources) })

  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })
  const stages = []

  const result = await installer.ensureRuntime({
    pythonMirror: '',
    userDataPath: userData,
    sources,
    uvPath: uv,
    spawn,
    probe: () => (progress.pip ? { ok: true, reason: 'ok', message: '' } : { ok: false, reason: 'probe-failed', message: 'No Python at' }),
    onStage: info => stages.push(info),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, true, '版本对不上只能重建')
  assert.ok(!stages.some(s => s.stage === installer.INSTALL_STAGES.syncingDeps),
    '解释器本身跑不起来时同步依赖注定白费')
  const create = stages.find(s => s.stage === installer.INSTALL_STAGES.creatingVenv)
  assert.ok(create, '应进入重建阶段')
  assert.match(create.message, /3\.13\.1/, '阶段消息要说清真因是版本，不是"缺依赖"')
  assert.deepEqual(calls.map(c => c.args[0]), ['venv', 'pip'], '直接重建：先建 venv 再装依赖')
})

