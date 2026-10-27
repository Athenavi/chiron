'use strict'
/**
 * 随包运行时缺失时的行为（回归防护）。
 *
 * 背景：`resolveCoreLaunch` 原先在找不到 `resources/python` 时会**静默**回退到 PATH 上的
 * `'python'`。用户机上这表现为"所有页面报服务繁忙"，而用户没有任何线索可循：
 *   - 没有 python → ENOENT；
 *   - 有 Store 占位符 → spawn 成功但立刻退出；
 *   - 有真 python 但没装 fastapi/asyncpg → ModuleNotFoundError。
 *
 * 现在的行为是**有意识地回退**：resources/python → CHIRON_PYTHON → PATH，
 * 每个候选都必须真的 import 四个关键模块才被采用；全都不行才报错并指明怎么修。
 *
 * 本文件锁定四条：
 *   1. 全部候选都不可用时：不 spawn，错误里逐个列出候选与原因，并给出修复指引；
 *   2. 随包那份缺依赖、PATH 上另有可用解释器时：自动回退并正常启动；
 *   3. 随包那份可用时：直接用随包，不多探候补；
 *   4. 探测结论按解释器缓存（同一解释器不重复 import）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')

const components = require('../components.cjs')

/** 造一个"打包态"资源目录：有 desktop-local，但没有/有 python 由调用方决定 */
function makeResources(t, { withPython }) {
  const resources = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-res-'))
  fs.mkdirSync(path.join(resources, 'desktop-local', 'app'), { recursive: true })
  fs.writeFileSync(path.join(resources, 'desktop-local', 'app', 'main.py'), '# fake\n')
  if (withPython) {
    fs.mkdirSync(path.join(resources, 'python'), { recursive: true })
    fs.writeFileSync(path.join(resources, 'python', 'python.exe'), '')
  }
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
function installDeps(t, { probeFor } = {}) {
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
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) }),
  })
  components._resetState()
  t.after(() => {
    components._resetDeps()
    components._resetState()
  })
  return calls
}

test('全部候选都不可用：不 spawn，逐个列出原因并给出修复指引', async t => {
  const resources = makeResources(t, { withPython: false })
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const calls = installDeps(t, { probeFor: () => MISSING_DEPS })

  await assert.rejects(
    () => components.startCore({ repoRoot: resources }),
    err => {
      assert.match(err.message, /本地运行时不可用/, '要明确说是运行时的问题')
      assert.match(err.message, /resources\/python/, '要点出随包那份缺的是什么')
      assert.match(err.message, /fastapi/, '要指名缺的模块')
      assert.match(err.message, /重新安装|desktop:pack/, '要给可执行的修复路径')
      assert.equal(err.retryable, false, '换端口救不回来')
      assert.ok(Array.isArray(err.runtimeAttempts) && err.runtimeAttempts.length >= 2, '要留下每个候选的证据')
      return true
    },
  )
  assert.equal(calls.spawn, 0, '不应该 spawn 任何进程')
})

test('随包缺依赖但 PATH 上有可用解释器：自动回退并启动', async t => {
  const resources = makeResources(t, { withPython: true })
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const bundled = path.join(resources, 'python', 'python.exe')
  const calls = installDeps(t, {
    // 只有随包那份坏；PATH 上的 'python' 判定可用
    probeFor: cmd => (cmd === bundled ? MISSING_DEPS : OK_PROBE),
  })

  const started = await components.startCore({ repoRoot: resources })
  assert.equal(started.pid, 999, 'core 应当真的起来了')
  assert.equal(started.listening, true)
  assert.equal(calls.spawn, 1)
  assert.ok(calls.probed.includes(bundled), '必须先探随包那份')
  assert.ok(calls.probed.includes('python'), '随包不可用后要探 PATH')
})

test('随包那份可用时：直接用随包，不探候补', async t => {
  const resources = makeResources(t, { withPython: true })
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const bundled = path.join(resources, 'python', 'python.exe')
  const calls = installDeps(t, { probeFor: () => OK_PROBE })

  const started = await components.startCore({ repoRoot: resources })
  assert.equal(started.pid, 999)
  assert.deepEqual(calls.probed, [bundled], '只应探测随包那一份')
  assert.equal(calls.spawn, 1)
})

test('探测结论按解释器缓存：同一进程内第二次启动不重复 import', async t => {
  const resources = makeResources(t, { withPython: true })
  withResourcesPath(t, resources)
  withoutExplicitPython(t)
  const calls = installDeps(t, { probeFor: () => OK_PROBE })

  await components.startCore({ repoRoot: resources })
  await components.stopCore()
  assert.equal(calls.spawnSync, 1, '第一次启动探测一次')

  await components.startCore({ repoRoot: resources })
  assert.equal(calls.spawnSync, 1, '第二次应命中缓存，不再探测')
})
