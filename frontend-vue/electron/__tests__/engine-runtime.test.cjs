'use strict'
/**
 * python-engine 的运行时接线（M41b / M41c）。
 *
 * 这一组用例钉的是"引擎这套环境到底谁负责"。之前的形状是：解释器按四条候选回退
 * （引擎 venv → 引导器 venv → `CHIRON_PYTHON` → PATH 上的 `python`），选错了只是让
 * 引擎返回 ok:false，而 `tryStartGoStack` 把整栈失败**吞成 null**，于是壳静默回落到
 * desktop-local 老 core —— 用户拿到一个"能打开、但聊天/知识库/图谱有一半是坏的"界面，
 * 屏幕上没有任何一处写着"引擎没起来"。
 *
 * 现在的不变量（每条都有对应用例）：
 *   1. 引擎环境**没人装过就必须装**，且装它是"用到才装"（本地模式真要起栈时）；
 *   2. 打包态起引擎**只认引擎自己那份 venv**（外加显式 CHIRON_PYTHON），不留任何回退；
 *   3. core 就绪但引擎没就绪时，`inspectLocalRuntime` 必须整体判未就绪（进度窗要出现）；
 *   4. 试过并失败了 ⇒ 抛错带 reason，绝不回落到老 core。
 *
 * 全部离线：uv 由假 spawn 代替；随包资产用临时目录模拟。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const installer = require('../runtime-install.cjs')

const components = require('../components.cjs')

const OK_PROBE = { status: 0, stdout: '', stderr: '' }

function makeResources(t, { withEngine = true, withUv = false } = {}) {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-res-'))

  const core = path.join(resources, 'desktop-local')
  fs.mkdirSync(path.join(core, 'app'), { recursive: true })
  fs.writeFileSync(path.join(core, 'app', 'main.py'), '# fake\n')
  fs.writeFileSync(path.join(core, 'pyproject.toml'), '[project]\nname="core"\n')

  if (withEngine) {
    const engine = path.join(resources, 'python-engine')
    fs.mkdirSync(path.join(engine, 'app'), { recursive: true })
    fs.writeFileSync(path.join(engine, 'app', 'main.py'), '# fake\n')
    fs.writeFileSync(path.join(engine, 'requirements.txt'), 'fastapi==0.115\n')
  }

  if (withUv) {
    const key = `${process.platform}-${process.arch}`
    const uv = path.join(resources, 'uv', key, process.platform === 'win32' ? 'uv.exe' : 'uv')
    fs.mkdirSync(path.dirname(uv), { recursive: true })
    fs.writeFileSync(uv, '')
  }

  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  return resources
}

/** process.resourcesPath 是 Electron 注入的只读属性，纯 Node 下用 defineProperty 覆盖 */
function withResourcesPath(t, dir) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'resourcesPath')
  Object.defineProperty(process, 'resourcesPath', { value: dir, configurable: true, writable: true })
  t.after(() => {
    if (descriptor) Object.defineProperty(process, 'resourcesPath', descriptor)
    else delete process.resourcesPath
  })
}

function withoutExplicitPython(t) {
  const saved = process.env.CHIRON_PYTHON
  delete process.env.CHIRON_PYTHON
  t.after(() => {
    if (saved === undefined) delete process.env.CHIRON_PYTHON
    else process.env.CHIRON_PYTHON = saved
  })
}

/**
 * 固定 userData，并可选择"core 环境已经装好"。
 *
 * 不固定它的话 `app.getPath` 每次返回新目录，探测与安装看到的根就不是同一个。
 */
function withUserData(t, { coreInstalled = false } = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-'))
  if (coreInstalled) createPython(userData, 'core')
  const saved = process.env.CHIRON_DESKTOP_USER_DATA
  process.env.CHIRON_DESKTOP_USER_DATA = userData
  t.after(() => {
    if (saved === undefined) delete process.env.CHIRON_DESKTOP_USER_DATA
    else process.env.CHIRON_DESKTOP_USER_DATA = saved
    fs.rmSync(userData, { recursive: true, force: true })
  })
  return userData
}

function createPython(userData, target) {
  const python = installer.venvPython(userData, target)
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '')
  return python
}
/** 随包 CPython：改绑总得有个去处（目录名带版本，minor 要与 pyvenv.cfg 对得上） */
function withBundledRuntime(t, resources, version = '3.12.16') {
  const dir = path.join(
    resources,
    'python-runtime',
    `${process.platform}-${process.arch}`,
    `cpython-${version}-test`,
  )
  const home = installer._internal.homeForRuntime(dir)
  fs.mkdirSync(home, { recursive: true })
  fs.writeFileSync(installer._internal.interpreterUnderHome(home), '')
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return { dir, home }
}

/** "装过、但 home 还跟着上一个安装包跑"的 venv：解释器在位，home 指向别处 */
function createVenvWithHome(userData, target, home) {
  const python = createPython(userData, target)
  fs.writeFileSync(
    path.join(installer.venvDir(userData, target), 'pyvenv.cfg'),
    `home = ${home}
version_info = 3.12.16
`,
    'utf8',
  )
  return python
}

/** 记录壳跑过多少进程、探测过哪些解释器 */
function installDeps(t) {
  const calls = { spawn: 0, spawnSync: 0, probed: [] }
  const child = new (require('node:events').EventEmitter)()
  child.stdout = new (require('node:events').EventEmitter)()
  child.stderr = new (require('node:events').EventEmitter)()
  child.kill = () => true
  components._setDeps({
    app: { getPath: () => fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-')) },
    spawn: () => {
      calls.spawn += 1
      return child
    },
    spawnSync: cmd => {
      calls.spawnSync += 1
      calls.probed.push(cmd)
      return OK_PROBE
    },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) }),
  })
  components._resetState()
  t.after(() => {
    components._resetDeps()
    components._resetState()
  })
  return calls
}

// ------------------------------------------------------------------ 随包引擎目录

test('packagedEngineDir：只认 app/main.py 齐全的目录，缺了就是开发态', t => {
  withResourcesPath(t, makeResources(t))
  assert.equal(path.basename(components.packagedEngineDir()), 'python-engine')

  withResourcesPath(t, makeResources(t, { withEngine: false }))
  assert.equal(components.packagedEngineDir(), '', '没有随包引擎源码 = 开发态，不许去猜别的位置')

  // 空壳目录（有目录、没入口）同样不算
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  fs.mkdirSync(path.join(empty, 'python-engine'), { recursive: true })
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)
  assert.equal(components.packagedEngineDir(), '')
})

// ------------------------------------------------------------------ 就绪查询

test('inspectEngineRuntime：开发态恒就绪，一次探测都不跑', t => {
  const calls = installDeps(t)
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)

  const result = components.inspectEngineRuntime(withUserData(t))
  assert.equal(result.ready, true, '开发态的引擎由本机环境负责，壳不该去装它')
  assert.equal(result.reason, 'dev')
  assert.equal(calls.spawnSync, 0, '判就绪不该探测解释器')
  assert.equal(calls.spawn, 0)
})

test('inspectEngineRuntime：打包态但引擎 venv 从没建过时判未就绪', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })

  const result = components.inspectEngineRuntime(userData)
  assert.equal(result.ready, false)
  assert.equal(result.reason, 'venv-missing')
  assert.equal(result.target, 'engine', '结论要带上是哪一套环境，否则界面上没法说清在装什么')
})

/**
 * 这条是"进度窗该不该出现"的闸门。
 *
 * 只看 core 的老判据在"core 装好、引擎从没装过"时返回就绪 ⇒ 不弹进度窗 ⇒
 * 引擎的安装（几分钟）在无任何反馈的情况下进行，失败也无处显示。
 */
test('inspectLocalRuntime：core 就绪但引擎没装 ⇒ 整体未就绪', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, false, '引擎缺失时不许谎报就绪，否则安装无处显示')
  assert.equal(result.target, 'engine')
  assert.equal(result.targetLabel, 'AI 引擎')
})

test('inspectLocalRuntime：core 改绑了但整栈仍未就绪时，记录不能随早退丢失', t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  // 探测恒失败：core 改绑完照样判未就绪 ⇒ 走"先报 core"那条早退
  installDeps(t)
  components._setDeps({
    app: { getPath: () => fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-')) },
    spawnSync: () => ({ status: 1, stdout: '', stderr: 'ModuleNotFoundError: No module named "fastapi"' }),
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) }),
  })
  const bundled = withBundledRuntime(t, resources)
  const userData = withUserData(t)
  createVenvWithHome(userData, 'core', path.join(resources, 'gone', 'cpython-3.12.16-test'))

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, false, '探测不过就不许谎报就绪')
  assert.equal(result.target, 'core')
  assert.deepEqual(
    result.rebinds.map(b => b.target),
    ['core'],
    '早退那条分支也要带 rebinds：改绑发生过就得能查见',
  )
  assert.equal(result.rebinds[0].to, bundled.home)
})

test('inspectLocalRuntime：两套都就绪才算就绪', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })
  createPython(userData, 'engine')

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, true, `两套环境都该判就绪：${JSON.stringify(result)}`)
})

test('inspectLocalRuntime：core 没装时先报 core（一次只修一环）', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  const userData = withUserData(t)

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, false)
  assert.equal(result.target, 'core', '两套都缺时应先报 core：缺解释器/缺 uv 是共同原因')
})
/**
 * 下面两条钉的是**自愈的可见性**。
 *
 * 就地改绑是"零成本修好、但用户永远看不见"的那类动作：日志里少一行，下次现场就没法
 * 区分"环境从没装过"和"绑定还跟着上一个包跑"。真机验证时 core 那条就是这么丢的 ——
 * core 修好后函数继续往下判引擎，返回值只带引擎那份 binding，主进程照着 `binding`
 * 记日志就等于永远只记引擎。汇总口径因此落在 `rebinds` 上。
 */
test('inspectLocalRuntime：两套都需要改绑时，rebinds 两条都在', t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  installDeps(t)
  const bundled = withBundledRuntime(t, resources)
  const deadHome = path.join(resources, 'gone', 'cpython-3.12.16-test')
  const userData = withUserData(t)
  createVenvWithHome(userData, 'core', deadHome)
  createVenvWithHome(userData, 'engine', deadHome)

  const result = components.inspectLocalRuntime()
  assert.deepEqual(result.rebinds.map(b => b.target), ['core', 'engine'], JSON.stringify(result.rebinds))
  assert.ok(result.rebinds.every(b => b.rebound && b.to === bundled.home), '改绑记录要能看出绑到了哪')
  assert.equal(result.ready, true, `两套都该改绑了事：${JSON.stringify(result)}`)
})

test('inspectLocalRuntime：只有 core 改了绑也要报出来（返回值本身是引擎那套）', t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  installDeps(t)
  const bundled = withBundledRuntime(t, resources)
  const userData = withUserData(t)
  createVenvWithHome(userData, 'core', path.join(resources, 'gone', 'cpython-3.12.16-test'))
  createVenvWithHome(userData, 'engine', bundled.home)

  const result = components.inspectLocalRuntime()
  assert.equal(result.target, 'engine')
  assert.equal(result.binding.rebound, false, '引擎本来就好，binding 不该谎报改绑')
  assert.deepEqual(
    result.rebinds.map(b => b.target),
    ['core'],
    'core 那次不能因为"已经修好、继续判引擎"就从返回值里消失',
  )
})

// ------------------------------------------------------------------ 解释器选择

test('resolveEngineLaunch：打包态只认引擎自己那份 venv', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  withoutExplicitPython(t)
  const userData = withUserData(t, { coreInstalled: true })
  const enginePython = createPython(userData, 'engine')

  const launch = components.resolveEngineLaunch({ userDataPath: userData })
  assert.equal(launch.command, enginePython)
  assert.equal(launch.kind, 'packaged')
  assert.deepEqual(launch.args, ['-m', 'app.main'], '两个包都叫 app，只能靠 cwd 区分')
  assert.equal(path.basename(launch.cwd), 'python-engine')
})

/**
 * 引导器那份 venv 里从来就没有引擎的依赖（按 desktop-local/pyproject.toml 装的）。
 * 选中它唯一的结局是 ModuleNotFoundError，而那次失败过去会被吞掉、静默回落老 core。
 */
test('resolveEngineLaunch：绝不回退到引导器 venv 或 PATH 上的 python', t => {
  withResourcesPath(t, makeResources(t))
  const calls = installDeps(t)
  withoutExplicitPython(t)
  const userData = withUserData(t, { coreInstalled: true })

  assert.equal(components.resolveEngineLaunch({ userDataPath: userData }), null, '只有 core 那份 venv 时必须判"没法起"')

  // 反向确认：PATH 上的 python 恰好可用也不入选（本机 python 常装着引擎依赖，最容易蒙混过关）
  assert.equal(components.resolveEngineLaunch({ userDataPath: userData }), null)
  assert.ok(!calls.probed.includes('python'), 'PATH 上的 python 不该进候选链')
})

test('resolveEngineLaunch：CHIRON_PYTHON 是显式覆盖，优先于自建 venv', t => {
  withResourcesPath(t, makeResources(t))
  installDeps(t)
  const saved = process.env.CHIRON_PYTHON
  process.env.CHIRON_PYTHON = 'D:/tools/python.exe'
  t.after(() => {
    if (saved === undefined) delete process.env.CHIRON_PYTHON
    else process.env.CHIRON_PYTHON = saved
  })
  const userData = withUserData(t, { coreInstalled: true })

  const launch = components.resolveEngineLaunch({ userDataPath: userData })
  assert.equal(launch.command, 'D:/tools/python.exe', '开发者指定解释器时要跳过自动装的那份')
  assert.equal(launch.kind, 'packaged')
})

test('resolveEngineLaunch：开发态用本机环境，位置取仓库里的 python-engine', t => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)
  installDeps(t)
  withoutExplicitPython(t)

  const launch = components.resolveEngineLaunch({ userDataPath: withUserData(t) })
  assert.equal(launch.kind, 'dev', '开发态由本机环境负责，不该去 userData 里找 venv')
  assert.equal(launch.command, 'python')
  assert.deepEqual(launch.args, ['-m', 'app.main'])
  assert.equal(path.basename(launch.cwd), 'python-engine')
})

test('inspectEngineRuntime：环境坏了时，错误文案点名是引擎在失败', t => {
  withResourcesPath(t, makeResources(t))
  withoutExplicitPython(t)
  const userData = withUserData(t, { coreInstalled: true })
  createPython(userData, 'engine')

  const probed = []
  components._setDeps({
    app: { getPath: () => fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-')) },
    spawnSync: cmd => {
      probed.push(cmd)
      // 引擎的 venv 在、但导入不了：探测器报缺模块
      return {
        status: 1,
        stdout: '',
        stderr: "Traceback (most recent call last):\nModuleNotFoundError: No module named 'pymupdf'",
      }
    },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) }),
  })
  components._resetState()
  t.after(() => {
    components._resetDeps()
    components._resetState()
  })

  const result = components.inspectEngineRuntime(userData)
  assert.equal(result.ready, false)
  assert.equal(result.reason, 'missing-dependency')
  // core 与引擎共用同一份探测代码：不说清是哪一个在失败，用户就没法照着修
  assert.match(result.message, /AI 引擎/, '失败文案要点名目标')
  assert.match(result.message, /pymupdf/, '要带上缺失的模块')
  assert.deepEqual(probed, [installer.venvPython(userData, 'engine')], '判就绪必须真探测过引擎解释器')
})

// ------------------------------------------------------------------ 按需安装

test('ensureEngineRuntime：开发态直接放行，一条命令都不跑', async t => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)
  const calls = installDeps(t)

  const result = await components.ensureEngineRuntime({ userDataPath: withUserData(t) })
  assert.equal(result.ok, true)
  assert.equal(result.reason, 'dev')
  assert.equal(calls.spawn, 0)
})

test('ensureEngineRuntime：已就绪时不重装（正常开机走这条）', async t => {
  withResourcesPath(t, makeResources(t))
  const calls = installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })
  createPython(userData, 'engine')

  const result = await components.ensureEngineRuntime({ userDataPath: userData })
  assert.equal(result.ok, true)
  assert.equal(result.reason, 'ok')
  assert.equal(result.installed, false)
  assert.equal(calls.spawn, 0, '就绪就不该跑 uv')
})

/**
 * 装失败必须是**抛错**且点名引擎。
 *
 * 这条用例借"安装包没带 uv"来触发失败（不联网、不真跑 uv），但它钉的是接线：
 * 内层错误写的是「准备AI 引擎环境」而不是「准备本地核心环境」—— 一旦有人把
 * `target: 'engine'` 传丢，这里立刻变红；回退到 core 会让引擎永远装不上 yet 自检绿。
 */
test('ensureEngineRuntime：装不起来时抛错、带 reason，并给可照做的指引', async t => {
  withResourcesPath(t, makeResources(t, { withUv: false }))
  installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })

  const stages = []
  await assert.rejects(
    () =>
      components.ensureEngineRuntime({
        userDataPath: userData,
        onRuntimeStage: info => stages.push(info),
      }),
    err => {
      assert.match(err.message, /AI 引擎的依赖没能装好/)
      assert.match(err.message, /准备AI 引擎环境/, 'target 必须一路传到安装器')
      assert.match(err.message, /CHIRON_PIP_INDEX_URL/, '要给出换源这条出路')
      assert.match(err.message, /python-engine/, '要说清缺的是哪一套环境')
      assert.equal(err.reason, 'engine-deps-install-failed')
      return true
    },
  )
  assert.ok(
    stages.some(s => s.target === 'engine'),
    '失败前至少推过一条带 target 的进度：进度窗要能显示"正在装引擎"',
  )
})

test('ensureEngineRuntime：进度回调透传给安装器（引擎安装也要显示在进度窗里）', async t => {
  withResourcesPath(t, makeResources(t, { withUv: false }))
  installDeps(t)
  const userData = withUserData(t, { coreInstalled: true })
  const seen = []

  await components
    .ensureEngineRuntime({ userDataPath: userData, onRuntimeStage: info => seen.push(info) })
    .catch(() => {})

  assert.ok(seen.length > 0, '一个 stage 事件都没收到，说明回调没透传下去')
  for (const info of seen) {
    assert.equal(info.target, 'engine')
    assert.equal(info.targetLabel, 'AI 引擎')
    assert.ok(info.message, `阶段 ${info.stage} 缺少可读文案`)
  }
})

// ------------------------------------------------------------------ core 的按需安装

/**
 * 全新安装时 `resolveCoreLaunch()` 必然抛错（venv 还没建）。老代码据此直接放弃
 * Go 链路、回落 desktop-local core ⇒ 第一次启动的用户拿到的是半套界面，而且引擎依赖
 * 压根轮不到装。`prepareCoreLaunch` 负责把这一段补上。
 */
test('prepareCoreLaunch：开发态拿到启动信息就走，不碰安装', async t => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)
  const calls = installDeps(t)

  const launch = await components.prepareCoreLaunch({ userDataPath: withUserData(t) })
  assert.equal(launch.kind, 'dev')
  assert.deepEqual(launch.args, ['-m', 'app.main'])
  assert.equal(calls.spawn, 0, '开发态不该由壳去装运行时')
})

test('prepareCoreLaunch：打包态装不起来时把安装错误抛回，不静默改用别的解释器', async t => {
  withResourcesPath(t, makeResources(t, { withUv: false }))
  installDeps(t)
  withoutExplicitPython(t)

  await assert.rejects(
    () => components.prepareCoreLaunch({ userDataPath: withUserData(t) }),
    err => {
      assert.match(err.message, /本地运行时自动安装失败|安装包内缺少 uv/, '要报的是"装不上"，不是"找不到解释器"')
      return true
    },
  )
})

// ------------------------------------------------------------------ 主进程接线（源码守卫）
//
// main.cjs 一 require 就会起 Electron，行为只能端到端验证；这里用源码断言钉住
// "失败必须上抛 / 装完再起栈"这两条最容易在重构时被顺手改回去的规则。

const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8').replace(/\r\n/g, '\n')
const componentsSource = fs
  .readFileSync(path.join(__dirname, '..', 'components.cjs'), 'utf8')
  .replace(/\r\n/g, '\n')

/** 取某个顶层函数的源码体（从声明处到下一个顶格 `}`）；源文件是 CRLF，先归一化 */
function functionBody(source, declaration) {
  const text = source.replace(/\r\n/g, '\n')
  const start = text.indexOf(declaration)
  assert.ok(start > 0, `源码里找不到 ${declaration}`)
  const end = text.indexOf('\n}\n', start)
  assert.ok(end > start, `${declaration} 之后找不到函数结束`)
  return text.slice(start, end)
}

test('main.cjs：起 Go 栈之前先按需装好引擎依赖', () => {
  const body = functionBody(mainSource, 'async function tryStartGoStack')
  const prepare = body.indexOf('prepareEngine(components, options)')
  const start = body.indexOf('stack.startLocalStack(')
  assert.ok(prepare > 0, '没有调用 prepareEngine：引擎依赖又没人装了')
  assert.ok(start > prepare, '安装必须在起栈之前，否则引擎一定起不来')
  assert.ok(/await\s+prepareEngine/.test(body), 'prepareEngine 是异步的，漏掉 await 等于没装')
  assert.match(body, /onEngineExit:\s*notifyEngineExit/, '引擎运行中退出必须有通知通道')
})

/**
 * 反向断言：Go 栈"试过但失败"不许再退回老 core。
 *
 * 老代码是 `catch { return null }`，注释还写着"错误信息更贴近既有 UI"——实际效果是
 * 引擎失败被丢掉、界面落在只有 desktop-local 的半套功能上。
 */
test('main.cjs：Go 栈失败时上抛而不是回退 desktop-local core', () => {
  const body = functionBody(mainSource, 'async function tryStartGoStack')
  const catchAt = body.lastIndexOf('} catch (err) {')
  assert.ok(catchAt > 0, '找不到 catch 块')
  const catchBlock = body.slice(catchAt)
  assert.doesNotMatch(catchBlock, /return null/, '试过并失败了却回 null，等于让调用方回落老 core')
  assert.match(catchBlock, /throw\b/, '必须把失败抛给 startLocalBackend 转成可读状态')
  assert.match(catchBlock, /reason/, '要带上 reason：恢复对话框与错误页靠它区分事故种类')
})

test('main.cjs：不适用（无网关二进制 / 运行时装不起来）才允许走旧路径', () => {
  const body = functionBody(mainSource, 'async function tryStartGoStack')
  const head = body.slice(0, body.indexOf('const dataDir'))
  assert.match(head, /return null/, '模块缺失/无 chiron.exe 仍要返回 null，否则开发态起不来')
  // 断言到调用点而不是关键词：注释里也写着 prepareCoreLaunch，只匹配名字会把删掉调用的变异放过去
  assert.match(
    head,
    /await\s+components\.prepareCoreLaunch\(/,
    '打包态要在这里按需装好 core，否则首启落到只有 desktop-local 的半套界面，引擎依赖也永远轮不到装',
  )
})

test('main.cjs：从主界面重启本地后端也走带进度窗的那条路', () => {
  assert.match(mainSource, /localBackend = await startLocalBackendWithProgress\(\)/, '引擎依赖可能从没装过，不弹窗就是静默假死几分钟')
  const body = functionBody(mainSource, 'async function startLocalBackendWithProgress')
  assert.match(body, /openInstallWindow\(\)/)
  assert.match(body, /closeInstallWindow\(\)/)
  assert.match(body, /onRuntimeStage/, '进度要推给安装窗')
})

test('壳里不再有第二份引擎解释器回退链', () => {
  assert.doesNotMatch(mainSource, /function resolveEngineLaunch\(/, '解释器选择只能有一处（components.cjs）')
  assert.doesNotMatch(mainSource, /function resolveEngineDir\(/, '"随包 or 仓库"的判据不能两处漂移')
  const body = functionBody(componentsSource, 'function resolveEngineLaunch')
  assert.doesNotMatch(body, /['"]runtime['"]/, '不许把引导器 venv 当引擎解释器：那份没有引擎依赖')
})

/**
 * 改绑日志的取值口径。
 *
 * `components.inspectLocalRuntime()` 修完 core 会继续判引擎，返回值里的 `binding` 是**引擎那份**；
 * 只看它就等于 core 的自愈永远不进日志（真机验证时实测丢掉过一条）。
 * main.cjs 一 require 就起 Electron，只能这样按源码钉。
 */
test('main.cjs：改绑日志遍历汇总的 rebinds，而不是只看返回值那一份 binding', () => {
  const body = functionBody(mainSource, 'function inspectLocalRuntime')
  assert.match(body, /result?.rebinds/, '要遍历 rebinds：只看 result.binding 会漏掉 core 那次自愈')
  assert.doesNotMatch(body, /result.binding.rebound/, '旧的单份判据不能回来，它会随返回值指向哪套环境而漂移')
  assert.match(body, /runtime python binding rebound/, '改绑必须在 main.log 留一行，否则现场分不清「从没装过」与「绑定过期」')
})
