'use strict'
/**
 * 安装器的**目标泛化**（`electron/runtime-install.cjs`）：一套代码装两套环境。
 *
 * 本地模式现在有两个必须各自独立的 Python 环境：
 *   - `core`（随包 `resources/desktop-local`，清单 `pyproject.toml`，落在 `userData/local/runtime`）
 *   - `engine`（随包 `resources/python-engine`，清单 `requirements.txt`，落在 `userData/local/engine`）
 *
 * 这一组用例守的是"两套互相踩"的那几种坏法，都很安静：
 *   1. **落点重叠** → 装引擎时 `removeVenv` 顺手删掉好好的引导器环境，本地模式从
 *      "聊天不可用"变成"全废"；
 *   2. **清单串台** → 拿 pyproject 去装引擎（或反之），装完自检还是绿的，
 *      起进程时爆 `ModuleNotFoundError` —— 正是上一轮 httpx 事故的形态；
 *   3. **就绪判定串台** → core 装好了就判引擎也好了，于是引擎永不安装、进度窗永不出现；
 *   4. **指纹掺了 target id** → 老用户已落盘的 core 指纹对不上，升级即触发联网重建，
 *      网络不通就从"能用"变成"不能用"。
 *
 * 全程**不联网**：uv 由假 spawn 代替，解释器与清单都是临时目录里的假文件。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const crypto = require('node:crypto')
const { EventEmitter } = require('node:events')

const installer = require('../runtime-install.cjs')

/**
 * 造一个"两套源码都随包"的 resources 目录。
 *
 * 两边都放 `app/main.py`：`sourcesFor` 的判据是它，缺了会被当成开发态（返回空串），
 * 用例就测不到安装路径了。
 */
function makeResources(t) {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-res-'))

  const core = path.join(resources, 'desktop-local')
  fs.mkdirSync(path.join(core, 'app'), { recursive: true })
  fs.writeFileSync(path.join(core, 'app', 'main.py'), '# fake\n')
  fs.writeFileSync(path.join(core, 'pyproject.toml'), '[project]\nname="core"\n')

  const engine = path.join(resources, 'python-engine')
  fs.mkdirSync(path.join(engine, 'app'), { recursive: true })
  fs.writeFileSync(path.join(engine, 'app', 'main.py'), '# fake\n')
  fs.writeFileSync(path.join(engine, 'requirements.txt'), 'fastapi==0.115\n')

  const key = `${process.platform}-${process.arch}`
  const uv = path.join(resources, 'uv', key, process.platform === 'win32' ? 'uv.exe' : 'uv')
  fs.mkdirSync(path.dirname(uv), { recursive: true })
  fs.writeFileSync(uv, '')

  const pythonDir = path.join(resources, 'python-runtime', key, 'cpython-3.12.16-test')
  const python = path.join(pythonDir, process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3'))
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '')

  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  return {
    resources,
    core,
    engine,
    uv,
    python,
    coreManifest: path.join(core, 'pyproject.toml'),
    engineManifest: path.join(engine, 'requirements.txt'),
  }
}

function makeUserData(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** 在某目标的 venv 位置造出"解释器"，模拟 uv 建环境成功 */
function createVenvPython(userData, target) {
  const python = installer.venvPython(userData, target)
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '')
  return python
}

/** 假 uv：按子命令记录，并把"装到哪一步"暴露给探测替身 */
function fakeSpawn({ failOn = null, onVenv = null } = {}) {
  const calls = []
  const progress = { pip: false }
  const spawn = (command, args) => {
    calls.push({ command, args })
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => true
    const sub = args[0]
    setImmediate(() => {
      if (failOn && failOn(sub)) {
        child.stderr.emit('data', `error: ${sub} failed\n`)
        child.emit('close', 1)
        return
      }
      if (sub === 'venv' && onVenv) onVenv()
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

const readyProbe = () => ({ ok: true, reason: 'ok', message: '' })

// ------------------------------------------------------------------ 目标解析

test('resolveTarget：空值按 core，未知 id 直接抛错（不静默回落）', () => {
  assert.equal(installer.resolveTarget().id, 'core')
  assert.equal(installer.resolveTarget('').id, 'core')
  assert.equal(installer.resolveTarget(null).id, 'core')
  assert.equal(installer.resolveTarget('engine').id, 'engine')

  // 回落 core 等于"拿另一份清单去装另一套环境"，而装完自检还是绿的 —— 最难查的坏法
  assert.throws(() => installer.resolveTarget('worker'), /未知的安装目标/)
})

test('两套环境的落点互不重叠', t => {
  const userData = makeUserData(t)
  const core = installer.venvPython(userData, 'core').split(path.sep).join('/')
  const engine = installer.venvPython(userData, 'engine').split(path.sep).join('/')

  assert.match(core, /local\/runtime\/venv\//, 'core 沿用老路径：改了等于让老用户的环境凭空消失')
  assert.match(engine, /local\/engine\/venv\//)
  assert.ok(!core.startsWith(engine + '/'), `两套不能互相包含：${core} / ${engine}`)
  assert.ok(!engine.startsWith(core + '/'), `两套不能互相包含：${core} / ${engine}`)
})

test('sourcesFor：各取各的随包目录，没有 app/main.py 的目录不算源码', t => {
  const { resources, core, engine } = makeResources(t)

  assert.equal(installer.sourcesFor('core', resources), core)
  assert.equal(installer.sourcesFor('engine', resources), engine)
  assert.equal(installer.coreSources(resources), core, 'coreSources 是给 core 的兼容别名')

  // 空壳目录（只有目录没有入口）必须判"没有源码"，否则装到一半才炸
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  assert.equal(installer.sourcesFor('engine', empty), '')
})

// ------------------------------------------------------------------ 清单与指纹

test('pipInstallArgs：core 吃 pyproject.toml，引擎吃 requirements.txt', t => {
  const { core, engine } = makeResources(t)

  const coreArgs = installer._internal.pipInstallArgs('PY', core, '', 'core')
  const engineArgs = installer._internal.pipInstallArgs('PY', engine, '', 'engine')

  assert.ok(coreArgs.includes('-r'), '依赖必须来自清单文件，而不是命令行散装包名')
  assert.ok(coreArgs.some(a => a.endsWith('pyproject.toml')), 'core 的唯一依赖来源是 pyproject')
  assert.ok(engineArgs.some(a => a.endsWith('requirements.txt')), '引擎的唯一依赖来源是 requirements')
  // 反向：绝不能出现另一份清单 —— 两份混用就是"装绿了但起不来"的源头
  assert.ok(!coreArgs.some(a => a.endsWith('requirements.txt')))
  assert.ok(!engineArgs.some(a => a.endsWith('pyproject.toml')))
})

test('dependencyFingerprint：core 的公式里不掺 target（老用户升级不白重建）', t => {
  const { core, coreManifest } = makeResources(t)
  const body = fs.readFileSync(coreManifest, 'utf8')
  const expected = crypto
    .createHash('sha256')
    .update(`${installer.RUNTIME_PYTHON_VERSION}\u0000${body}`)
    .digest('hex')

  assert.equal(installer.dependencyFingerprint(core, 'core'), expected)
  // 不传 target 也必须得到同一个值：老版本调用点就是这个形状，落盘指纹不能漂
  assert.equal(installer.dependencyFingerprint(core), expected)
})

test('dependencyFingerprint：两套清单各自独立，改动只影响自己', t => {
  const { core, engine, engineManifest } = makeResources(t)
  const coreBefore = installer.dependencyFingerprint(core, 'core')
  const engineBefore = installer.dependencyFingerprint(engine, 'engine')

  fs.appendFileSync(engineManifest, 'anthropic==0.42.0\n')

  assert.notEqual(installer.dependencyFingerprint(engine, 'engine'), engineBefore, '引擎依赖声明变了，指纹必须跟着变')
  assert.equal(installer.dependencyFingerprint(core, 'core'), coreBefore, 'core 不该被引擎的改动带着重建')
  assert.notEqual(coreBefore, engineBefore, '两套指纹本来就不该相同')
})

test('清单：两套环境的安装记录各写各的，互不覆盖', t => {
  const userData = makeUserData(t)
  installer.writeManifest(userData, { dependencyFingerprint: 'core-fp' }, 'core')
  installer.writeManifest(userData, { dependencyFingerprint: 'engine-fp' }, 'engine')

  assert.equal(installer.readManifest(userData, 'core').dependencyFingerprint, 'core-fp')
  assert.equal(installer.readManifest(userData, 'engine').dependencyFingerprint, 'engine-fp')
  assert.notEqual(installer.manifestPath(userData, 'core'), installer.manifestPath(userData, 'engine'))

  // 只装过 core 的机器上，引擎必须读不到清单（当作没装过），否则会走"增量同步"
  const fresh = makeUserData(t)
  installer.writeManifest(fresh, { dependencyFingerprint: 'only-core' }, 'core')
  assert.equal(installer.readManifest(fresh, 'engine'), null)
})

// ------------------------------------------------------------------ 就绪判定

test('inspectRuntime：core 装好了不代表引擎装好了（各判各的 venv）', t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t)
  createVenvPython(userData, 'core')

  const core = installer.inspectRuntime({
    target: 'core',
    userDataPath: userData,
    sources: path.join(resources, 'desktop-local'),
    probe: readyProbe,
  })
  const engine = installer.inspectRuntime({
    target: 'engine',
    userDataPath: userData,
    sources: path.join(resources, 'python-engine'),
    probe: readyProbe,
  })

  assert.equal(core.ready, true)
  assert.equal(core.target, 'core')
  // 这条是整个泛化的立足点：探测恒成功也必须判引擎未就绪，否则引擎环境永不安装
  assert.equal(engine.ready, false, '引擎必须看自己那份 venv，不能蹭 core 的结论')
  assert.equal(engine.reason, 'venv-missing')
  assert.equal(engine.target, 'engine')
  assert.equal(engine.targetLabel, 'AI 引擎')
})

test('inspectRuntime：把目标名交给探测器，失败能说清是哪一套', t => {
  const userData = makeUserData(t)
  const { engine } = makeResources(t)
  const python = createVenvPython(userData, 'engine')

  const seen = []
  const result = installer.inspectRuntime({
    target: 'engine',
    userDataPath: userData,
    sources: engine,
    probe: (p, cwd, label) => {
      seen.push({ p, cwd, label })
      return { ok: false, reason: 'missing-dependency', message: `导入 ${label} 失败` }
    },
  })

  assert.equal(result.ready, false)
  assert.deepEqual(seen, [{ p: python, cwd: engine, label: 'AI 引擎' }], '探测器要拿到解释器、工作目录与目标名')
  assert.match(result.message, /AI 引擎/)
})

// ------------------------------------------------------------------ 安装流程

test('ensureRuntime：引擎装完的落点、清单与进度都只属于引擎', async t => {
  const userData = makeUserData(t)
  const { resources, engine, uv, python } = makeResources(t)
  const stages = []
  const { spawn, calls, progress } = fakeSpawn({
    onVenv: () => createVenvPython(userData, 'engine'),
  })

  const result = await installer.ensureRuntime({
    target: 'engine',
    pythonMirror: '',
    userDataPath: userData,
    resourcesPath: resources,
    sources: engine,
    uvPath: uv,
    spawn,
    onStage: info => stages.push(info),
    probe: probeByProgress(progress),
  })

  assert.equal(result.ok, true)
  assert.equal(result.target, 'engine')
  assert.equal(result.reinstalled, true)
  assert.deepEqual(calls.map(c => c.args[0]), ['venv', 'pip'])
  // 建环境的目标目录必须是引擎那一份：写错就等于把 core 重建一遍
  assert.ok(calls[0].args.includes(installer.venvDir(userData, 'engine')))
  assert.ok(calls[1].args.some(a => a.endsWith('requirements.txt')))
  // 解释器仍走随包 CPython 的绝对路径：两套共用同一个解释器，但不共用 venv
  assert.equal(calls[0].args[calls[0].args.indexOf('--python') + 1], python)
  assert.equal(calls[0].command, uv)

  // 每个阶段都带 target：进度页靠它区分"装 core"与"装引擎"两轮
  assert.ok(stages.length > 0)
  for (const info of stages) {
    assert.equal(info.target, 'engine', `阶段 ${info.stage} 的进度没带 target`)
    assert.equal(info.targetLabel, 'AI 引擎')
  }
  assert.equal(installer.readManifest(userData, 'engine').dependencyFingerprint, installer.dependencyFingerprint(engine, 'engine'))
})

test('ensureRuntime：装引擎绝不碰 core 的 venv 与清单', async t => {
  const userData = makeUserData(t)
  const { resources, engine, uv } = makeResources(t)

  // core 已经装好，venv 里放一个"只有 core 才有"的标记文件
  createVenvPython(userData, 'core')
  const coreMarker = path.join(installer.venvDir(userData, 'core'), 'core-marker')
  fs.writeFileSync(coreMarker, 'keep me')
  installer.writeManifest(userData, { dependencyFingerprint: 'core-fp' }, 'core')

  const { spawn, progress } = fakeSpawn({ onVenv: () => createVenvPython(userData, 'engine') })
  await installer.ensureRuntime({
    target: 'engine',
    userDataPath: userData,
    resourcesPath: resources,
    sources: engine,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  // removeVenv 打的是 core 目录的话，这里就是文件不存在 —— 那是"引擎装失败把本地模式全废"的直接证据
  assert.equal(fs.existsSync(coreMarker), true, '装引擎不该删掉 core 的 venv')
  assert.equal(installer.readManifest(userData, 'core').dependencyFingerprint, 'core-fp', 'core 的清单不该被改写')
})

test('ensureRuntime：引擎已就绪时一条命令都不跑（即便 core 从没装过）', async t => {
  const userData = makeUserData(t)
  const { resources, engine, uv } = makeResources(t)
  createVenvPython(userData, 'engine')
  const { spawn, calls } = fakeSpawn()

  const result = await installer.ensureRuntime({
    target: 'engine',
    userDataPath: userData,
    resourcesPath: resources,
    sources: engine,
    uvPath: uv,
    spawn,
    probe: readyProbe,
  })

  assert.equal(result.ok, true)
  assert.equal(result.reinstalled, false)
  assert.equal(calls.length, 0, '已经就绪就不该再调 uv')
})

test('ensureRuntime：缺随包引擎源码时报错点名 python-engine，不指向 desktop-local', async t => {
  const userData = makeUserData(t)
  const { resources } = makeResources(t)

  await assert.rejects(
    () =>
      installer.ensureRuntime({
        target: 'engine',
        userDataPath: userData,
        sources: '',
        resourcesPath: resources,
        probe: readyProbe,
      }),
    err => {
      assert.match(err.message, /python-engine/, '要指出缺的是哪一份源码')
      assert.match(err.message, /requirements\.txt|重新安装/, '要么给清单要么给出路')
      assert.doesNotMatch(err.message, /desktop-local/, '别把用户指向另一套环境')
      return true
    },
  )
})

test('ensureRuntime：引擎依赖装完仍导入失败时，错误点名 requirements.txt', async t => {
  const userData = makeUserData(t)
  const { resources, engine, uv } = makeResources(t)
  const { spawn } = fakeSpawn({ onVenv: () => createVenvPython(userData, 'engine') })

  await assert.rejects(
    () =>
      installer.ensureRuntime({
        target: 'engine',
        userDataPath: userData,
        resourcesPath: resources,
        sources: engine,
        uvPath: uv,
        spawn,
        probe: () => ({ ok: false, reason: 'missing-dependency', message: '缺少模块 pymupdf' }),
      }),
    err => {
      assert.match(err.message, /AI 引擎/, '要说清失败的是哪一套环境')
      assert.match(err.message, /requirements\.txt/, '清单漏项要指向引擎那份清单')
      assert.match(err.message, /pymupdf/)
      return true
    },
  )
})

test('增量同步按目标各自的清单与指纹：core 的同步不会去装引擎依赖', async t => {
  const userData = makeUserData(t)
  const { resources, core, uv } = makeResources(t)
  createVenvPython(userData, 'core')
  installer.writeManifest(userData, { dependencyFingerprint: installer.dependencyFingerprint(core, 'core') }, 'core')

  const { spawn, calls, progress } = fakeSpawn()
  const result = await installer.ensureRuntime({
    target: 'core',
    userDataPath: userData,
    resourcesPath: resources,
    sources: core,
    uvPath: uv,
    spawn,
    probe: probeByProgress(progress),
  })

  assert.equal(result.repaired, true, '依赖没变但导入失败，应走增量同步')
  assert.equal(calls.length, 1)
  assert.ok(calls[0].args.some(a => a.endsWith('pyproject.toml')), 'core 的同步只吃 pyproject')
})

/**
 * 安装进度窗（`electron/install.html`）是这套事件的**唯一消费者**。
 *
 * 这里不用 jsdom：脚本只碰四个 id、`className`、`textContent`、`style.width` 和
 * `dataset.stage`，一个手写替身就够，而且替身"缺什么就报错"——比 jsdom 更容易
 * 暴露脚本对新字段的依赖。跑的是从 `<script>` 里抠出来的**原文**，不是抄一份
 * （`new Function` 的输入是仓库自己这份被版本管理的 html，不是任何外部数据）。
 */
const ORDER_IDS = ['probing', 'creating-venv', 'installing-deps', 'syncing-deps', 'verifying']

function loadInstallWindow() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'install.html'), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)
  assert.ok(script, 'install.html 里应能找到内联脚本')

  const liFor = stage => {
    const mark = { textContent: '·' }
    return {
      dataset: { stage },
      className: '',
      querySelector: () => mark,
      get markText() {
        return mark.textContent
      },
    }
  }
  const els = {
    stages: { children: ORDER_IDS.map(liFor) },
    progress: { style: { width: '' } },
    detail: { textContent: '' },
    target: { textContent: '正在准备：本地核心' },
  }
  const document = { getElementById: id => els[id] }
  let handler = null
  const window = { chironDesktop: { onRuntimeInstallStage: fn => { handler = fn } } }
  new Function('document', 'window', script[1])(document, window)
  assert.ok(handler, '页面必须订阅安装进度事件')
  return { els, send: info => handler(info) }
}

test('进度窗：切到引擎时换标题，阶段列表从第一步重来', () => {
  const { els, send } = loadInstallWindow()
  for (const stage of ORDER_IDS) send({ target: 'core', targetLabel: '本地核心', stage })
  send({ target: 'core', targetLabel: '本地核心', stage: 'ready' })
  assert.equal(els.progress.style.width, '100%')
  assert.ok(els.stages.children.every(li => li.className === 'done'), 'core 装完应全是勾')

  send({ target: 'engine', targetLabel: 'AI 引擎', stage: 'probing' })

  assert.equal(els.target.textContent, '正在准备：AI 引擎', '标题要说清现在装的是哪一套')
  assert.ok(
    els.stages.children.every(li => li.className !== 'done'),
    '第二套环境从第一步重来：上一套的勾必须清掉',
  )
  assert.equal(els.stages.children[0].className, 'active', '新环境应显示成"第一步进行中"')
  assert.ok(
    els.stages.children.slice(1).every(li => li.markText === '·'),
    '后面的步骤不能留着上一套的勾',
  )
  assert.ok(parseFloat(els.progress.style.width) < 20, '进度条必须回落，不能停在 100%')
})

test('进度窗：同一套环境内的连续事件不复位', () => {
  const { els, send } = loadInstallWindow()
  send({ target: 'engine', targetLabel: 'AI 引擎', stage: 'creating-venv' })
  const title = els.target.textContent
  send({ target: 'engine', targetLabel: 'AI 引擎', stage: 'installing-deps' })

  assert.equal(els.target.textContent, title, '同一套环境不该每来一条事件就重画一次标题')
  const byId = stage => els.stages.children.find(li => li.dataset.stage === stage)
  assert.equal(byId('creating-venv').className, 'done', '前一步应记为已完成')
  assert.equal(byId('installing-deps').className, 'active')
})

test('进度窗：老载荷（没有 target）按本地核心处理，标题不会被清空', () => {
  const { els, send } = loadInstallWindow()
  send({ stage: 'installing-deps', message: '正在装依赖' })

  assert.equal(els.target.textContent, '正在准备：本地核心')
  assert.equal(els.detail.textContent, '正在装依赖', 'message 仍然要显示')
})