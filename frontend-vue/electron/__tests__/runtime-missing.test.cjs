'use strict'
/**
 * 本地运行时的解析与安装行为（回归防护）。
 *
 * 背景：`resolveCoreLaunch` 原先在找不到解释器时会**静默**回退到 PATH 上的
 * `'python'`。用户机上这表现为"所有页面报服务繁忙"，而用户没有任何线索可循：
 *   - 没有 python → ENOENT；
 *   - 有 Store 占位符 → spawn 成功但立刻退出；
 *   - 有真 python 但没装 fastapi/asyncpg → ModuleNotFoundError。
 *
 * 现在的行为：**解释器与 uv 都随包**（`resources/python-runtime/`、`resources/uv/`），
 * 首次启动只做"建 venv + 按 `pyproject.toml` 装依赖"。候选链只剩两条：
 * 自建 venv（`<userData>/local/runtime/venv`）与显式设置的 CHIRON_PYTHON。
 * 刻意不兜底到 PATH，也刻意不再找 `resources/python`（解释器 + 已装依赖的旧整包）。
 *
 * 本文件锁定四条：
 *   1. 安装包缺 uv 时：不 spawn，直接判安装包残缺并让用户重装（不提供换源误导）；
 *   2. 缺运行时就走安装，绝不因 PATH 上恰好有个可用解释器而蒙混启动；
 *   3. 自建 venv 可用时：直接用它，不多探候补；
 *   4. 探测结论按解释器缓存（同一解释器不重复 import）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')

const components = require('../components.cjs')

/**
 * 造一个"打包态"资源目录：只有 desktop-local 源码。
 *
 * **刻意不再构造 `resources/python`**：那是"解释器 + 已装依赖"的旧整包，候选链里也没有
 * 这个位置。以前这个 helper 能靠"包里放个解释器"让用例通过，等于把已被否决的形态
 * 固化进测试。
 */
function makeResources(t) {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-res-'))
  fs.mkdirSync(path.join(resources, 'desktop-local', 'app'), { recursive: true })
  fs.writeFileSync(path.join(resources, 'desktop-local', 'app', 'main.py'), '# fake\n')
  t.after(() => fs.rmSync(resources, { recursive: true, force: true }))
  return resources
}

/**
 * 把 process.resourcesPath 指向临时目录。它是 Electron 注入的只读属性，
 * 纯 Node 下用 defineProperty 覆盖，测试结束必须还原（否则污染同进程其它用例）。
 */
function withResourcesPath(t, dir) {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'resourcesPath')
  Object.defineProperty(process, 'resourcesPath', { value: dir, configurable: true, writable: true })
  t.after(() => {
    if (descriptor) Object.defineProperty(process, 'resourcesPath', descriptor)
    else delete process.resourcesPath
  })
}

/** CHIRON_PYTHON 会参与候选链，测完必须还原，否则串味到别的用例 */
function withoutExplicitPython(t) {
  const saved = process.env.CHIRON_PYTHON
  delete process.env.CHIRON_PYTHON
  t.after(() => {
    if (saved === undefined) delete process.env.CHIRON_PYTHON
    else process.env.CHIRON_PYTHON = saved
  })
}

/**
 * 造一个"自建运行时已就绪"的 userData：`local/runtime/venv/` 里有解释器。
 *
 * 这是安装包**唯一**会走的那条路（随包的是裸解释器，依赖必须现装进 venv）。用环境变量
 * `CHIRON_DESKTOP_USER_DATA` 固定 userData，避免 `app.getPath` 每次返回新目录
 * 导致 `installedRuntimePython()` 和安装器看到两个不同的根。
 */
function withInstalledRuntime(t) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-'))
  const venv = path.join(userData, 'local', 'runtime', 'venv')
  const exe =
    process.platform === 'win32'
      ? path.join(venv, 'Scripts', 'python.exe')
      : path.join(venv, 'bin', 'python3')
  fs.mkdirSync(path.dirname(exe), { recursive: true })
  fs.writeFileSync(exe, '')

  const saved = process.env.CHIRON_DESKTOP_USER_DATA
  process.env.CHIRON_DESKTOP_USER_DATA = userData
  t.after(() => {
    if (saved === undefined) delete process.env.CHIRON_DESKTOP_USER_DATA
    else process.env.CHIRON_DESKTOP_USER_DATA = saved
    fs.rmSync(userData, { recursive: true, force: true })
  })
  return exe
}

const OK_PROBE = { status: 0, stdout: '', stderr: '' }
const MISSING_DEPS = {
  status: 1,
  stdout: '',
  stderr: "Traceback (most recent call last):\nModuleNotFoundError: No module named 'fastapi'",
}

/**
 * 装一个假的 spawn/spawnSync：记录是否真的尝试起进程。
 * `probeFor(cmd)` 可按候选解释器返回不同结果，用来模拟"随包坏了但 PATH 上有好的"。
 */
function installDeps(t, { probeFor, fetch: fetchImpl } = {}) {
  const calls = { spawn: 0, spawnSync: 0, probed: [] }
  const child = new EventEmitter()
  child.pid = 999
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()
  child.kill = () => {
    child.emit('exit', 0)
    return true
  }
  components._setDeps({
    app: { getPath: () => fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-ud-')) },
    spawn: () => {
      calls.spawn += 1
      // 与 components.test.cjs 同款：startCore 收到 listening 播报后才会探 /healthz
      setImmediate(() =>
        child.stdout.emit('data', `${JSON.stringify({ event: 'listening', host: '127.0.0.1', port: 43299 })}\n`),
      )
      return child
    },
    spawnSync: (cmd) => {
      calls.spawnSync += 1
      calls.probed.push(cmd)
      return probeFor ? probeFor(cmd) : OK_PROBE
    },
    fetch: fetchImpl || (async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) })),
  })
  components._resetState()
  t.after(() => {
    components._resetDeps()
    components._resetState()
  })
  return calls
}

test('运行时缺失且安装包没带 uv：不 spawn，直接判安装包残缺并让用户重装', async t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const calls = installDeps(t, { probeFor: () => MISSING_DEPS })

  await assert.rejects(
    () => components.startCore({ repoRoot: resources }),
    err => {
      // 运行时缺失 → 自动去装。uv 现在随包分发，缺了只能是打包出错：
      // 说清"是安装包残缺、重装能修"，而不是让用户去调镜像变量白折腾。
      assert.match(err.message, /本地运行时自动安装失败/, '要明确是安装环节的问题')
      assert.match(err.message, /uv/, '要指名缺的组件')
      assert.match(err.message, /重新安装/, '安装包残缺的唯一出路就是重装')
      assert.doesNotMatch(err.message, /CHIRON_UV_MIRROR/, '换镜像解决不了包内缺文件')
      assert.equal(err.retryable, false, '换端口救不回来')
      return true
    },
  )
  assert.equal(calls.spawn, 0, '不应该 spawn 任何进程')
})

test('不再回退到 PATH 上的 python：缺运行时就走安装，而不是蒙混启动', async t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  // PATH 上的 'python' 一律判"可用"——正好用来验证它不会入选
  const calls = installDeps(t, { probeFor: () => OK_PROBE })

  await assert.rejects(() => components.startCore({ repoRoot: resources }))
  // 这条是关键回归防线：一旦 PATH 上的 python 重回候选链，
  // "运行时没装"就会被一个碰巧装了依赖的解释器掩盖，安装流程永不触发。
  assert.ok(!calls.probed.includes('python'), 'PATH 上的 python 不该进候选链')
  assert.equal(calls.spawn, 0)
})

test('自建 venv 可用时：直接用它，不再探其它候选', async t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const venvPython = withInstalledRuntime(t)
  const calls = installDeps(t, { probeFor: () => OK_PROBE })

  const started = await components.startCore({ repoRoot: resources })
  assert.equal(started.pid, 999)
  assert.deepEqual(calls.probed, [venvPython], '只应探测自建 venv 那一份')
  assert.equal(calls.spawn, 1)
})

test('探测结论按解释器缓存：同一进程内第二次启动不重复 import', async t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  withInstalledRuntime(t)
  const calls = installDeps(t, { probeFor: () => OK_PROBE })

  await components.startCore({ repoRoot: resources })
  await components.stopCore()
  assert.equal(calls.spawnSync, 1, '第一次启动探测一次')

  await components.startCore({ repoRoot: resources })
  assert.equal(calls.spawnSync, 1, '第二次应命中缓存，不再探测')
})

/**
 * `inspectLocalRuntime` 决定主进程「要不要在建主窗口前弹安装进度窗」。
 * 它一旦误判成就绪，用户会落在"所有页面报服务繁忙"；误判成未就绪，
 * 则每次正常启动都会闪一个不该出现的进度窗。两头都得钉住。
 */
test('inspectLocalRuntime：开发态没有随包源码时判为就绪，不闪进度窗', t => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-empty-'))
  t.after(() => fs.rmSync(empty, { recursive: true, force: true }))
  withResourcesPath(t, empty)
  installDeps(t)

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, true, '开发态的运行时由开发者本机负责，界面不该提示安装')
  assert.equal(result.reason, 'dev')
})

test('inspectLocalRuntime：打包态但 venv 还没建时判为未就绪', t => {
  const resources = makeResources(t)
  withResourcesPath(t, resources)
  installDeps(t)

  const result = components.inspectLocalRuntime()
  assert.equal(result.ready, false, '没建 venv 就必须走安装流程，不能谎报就绪')
  assert.equal(result.reason, 'venv-missing')
})
