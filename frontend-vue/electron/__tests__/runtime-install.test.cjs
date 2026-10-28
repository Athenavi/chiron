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
function makeResources(t, { withUv = true } = {}) {
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
  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  return { resources, sources, uv }
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
    userDataPath: userData,
    sources,
    spawn,
    probe: () => ({ ok: true, reason: 'ok', message: '' }),
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, false)
  assert.equal(calls.length, 0, '已经就绪就不该再调 uv')
})

test('ensureRuntime：缺 uv 时明确说安装包不完整，并给出构建侧动作', async t => {
  const userData = makeUserData(t)
  const { sources } = makeResources(t, { withUv: false })
  const { spawn } = fakeSpawn(t)

  await assert.rejects(
    () =>
      installer.ensureRuntime({
        userDataPath: userData,
        sources,
        spawn,
        uvPath: '',
        probe: () => ({ ok: false, reason: 'venv-missing', message: '无' }),
      }),
    err => {
      assert.match(err.message, /缺少 uv/)
      assert.match(err.message, /重新安装/, '要给出用户能照做的动作')
      assert.match(err.message, /prepare_uv\.py/, '要给出构建侧的动作')
      return true
    },
  )
})

test('ensureRuntime：完整流程先建 venv 再装依赖，最后做导入验证', async t => {
  const userData = makeUserData(t)
  const { sources, uv } = makeResources(t)
  const stages = []
  const { spawn, calls, progress } = fakeSpawn(t, { onVenv: () => createVenvPython(userData) })

  const result = await installer.ensureRuntime({
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
  assert.ok(calls[0].args.includes(installer.RUNTIME_PYTHON_VERSION))
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

test('bundledUv：平台键与构建脚本一致（uv/<platform>-<arch>/uv[.exe]）', t => {
  const { resources, uv } = makeResources(t)
  assert.equal(installer.bundledUv(resources), uv)
})

test('bundledUv：resourcesPath 缺失时返回空串而不是抛错', () => {
  assert.equal(installer.bundledUv(''), '')
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
