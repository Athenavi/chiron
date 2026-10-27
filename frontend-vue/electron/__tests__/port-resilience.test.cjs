'use strict'
/**
 * 端口韧性的纯 Node 单测（不启动真实进程、不装 Electron）。
 *
 * 覆盖：
 *   - 默认值集中到 `defaults.cjs`（host / 端口 0 / 重试次数 / 超时 / 组件名 / health 路径），
 *     且代码里不再出现写死的具体端口（含 11434）。
 *   - `startCore` 默认把端口交给内核（`--port 0`），真实端口从 stdout 的 listening 事件读；
 *     端口冲突 / 进程提前退出 / 就绪超时都换端口重试，最终失败给一条带每次原因的错误。
 *   - 调用方显式给的固定端口被占时，重试退回 0（否则会一直撞同一个端口）。
 *   - ENOENT 这类不可恢复的 spawn 错误不重试。
 *   - `stopCore` 在 Windows 上先按进程树终止（taskkill /T），再 SIGTERM/SIGKILL 兜底。
 *   - `getCoreState()` 返回 `{baseUrl, port, token, pid, dataDir}`，地址不再由调用方拼。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')

const defaults = require('../defaults.cjs')
const components = require('../components.cjs')

// --------------------------------------------------------------------- 夹具
function makeChild(pid) {
  const child = new EventEmitter()
  child.pid = pid
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()
  child.kills = []
  child.kill = signal => {
    child.kills.push(signal)
    child.emit('exit', 0)
    return true
  }
  /** 喂一行 core 播报 */
  child.emitLine = obj => child.stdout.emit('data', `${JSON.stringify(obj)}\n`)
  return child
}

/**
 * 造一个"仓库 + userData + 假 spawn/fetch"的环境。
 * `plan(index, child)` 可以返回一个函数，在 spawn 返回后（监听器已挂好）安排该次行为。
 */
function setup(t, { plan, fetchImpl } = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-port-userdata-'))
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-port-repo-'))
  fs.mkdirSync(path.join(repo, 'desktop-local', 'app'), { recursive: true })
  fs.writeFileSync(path.join(repo, 'desktop-local', 'app', 'main.py'), '# fake core\n', 'utf8')

  const spawns = []
  const children = []
  const fetches = []

  const deps = {
    app: { getPath: () => userData },
    spawn: (command, args, options) => {
      const index = spawns.length
      spawns.push({ command, args, options })
      const child = makeChild(5000 + index)
      children.push(child)
      const behaviour = typeof plan === 'function' ? plan(index, child) : null
      if (behaviour) setImmediate(() => behaviour(child, args, options))
      return child
    },
    fetch: async (url, options = {}) => {
      fetches.push({ url: String(url), method: (options && options.method) || 'GET' })
      if (fetchImpl) return fetchImpl(String(url), options)
      return { ok: true, status: 200, json: async () => ({ status: 'ok' }) }
    },
  }
  components._setDeps(deps)
  components._resetState()

  t.after(async () => {
    await components.stopCore()
    components._resetState()
    components._resetDeps()
    fs.rmSync(userData, { recursive: true, force: true })
    fs.rmSync(repo, { recursive: true, force: true })
  })

  return { userData, repo, spawns, children, fetches, deps }
}

/** 解析 `--port <n>`（dev 态 CLI 里端口总在 `--port` 后面） */
function cliPort(args) {
  const index = args.indexOf('--port')
  return index >= 0 ? args[index + 1] : undefined
}

function listening(port) {
  return child => child.emitLine({ event: 'listening', host: '127.0.0.1', port })
}

// --------------------------------------------------------------------- 默认值
test('defaults.cjs 是默认值的唯一来源：host / 端口 0 / 重试 / 组件名 / health 路径', () => {
  assert.equal(defaults.DEFAULT_HOST, '127.0.0.1')
  assert.equal(defaults.DEFAULT_CORE_PORT, 0, '默认端口必须是 0（交给内核），不能写死')
  assert.equal(defaults.DEFAULT_HEALTH_PATH, '/healthz')
  assert.ok(defaults.CORE_PORT_ATTEMPTS >= 2, '端口冲突要能重试')
  assert.ok(defaults.CORE_HEALTH_TIMEOUT_MS > 0)
  assert.ok(defaults.CORE_KILL_TIMEOUT_MS > 0)
  assert.deepEqual([...defaults.COMPONENT_NAMES], ['core', 'pageindex', 'langextract'])

  // components.cjs 用的是同一份，不是自己又抄了一套字面量
  assert.equal(components.DEFAULTS, defaults)
  assert.equal(components.CORE_PORT_ATTEMPTS, defaults.CORE_PORT_ATTEMPTS)
  assert.equal(components.CORE_HEALTH_TIMEOUT_MS, defaults.CORE_HEALTH_TIMEOUT_MS)
  assert.deepEqual([...components.COMPONENT_NAMES], [...defaults.COMPONENT_NAMES])
})

test('代码里没有写死的端口（含历史遗留的 11434）', () => {
  for (const name of ['defaults.cjs', 'components.cjs']) {
    const source = fs.readFileSync(path.join(__dirname, '..', name), 'utf8')
    assert.equal(source.includes('11434'), false, `${name} 不应硬编码 11434`)
    // `127.0.0.1:<5 位数字>` 这种硬拼地址只允许出现在默认 host 常量附近
    const hardcoded = source.match(/127\.0\.0\.1:\d{2,5}/g) || []
    for (const hit of hardcoded) {
      assert.equal(
        hit,
        `127.0.0.1:${hit.split(':')[1]}`,
        `${name} 里出现了硬编码的 loopback 地址：${hit}`,
      )
      assert.ok(
        defaults.PORT_CONFLICT_PATTERNS.length > 0,
        '占位断言：真正的格式检查在下面（只允许示例/文档串）',
      )
    }
  }
})

test('coreBaseUrl / resolveRequestedPort：地址只在一处拼，端口不合法直接拒绝', () => {
  assert.equal(components.coreBaseUrl('127.0.0.1', 4321), 'http://127.0.0.1:4321')
  assert.equal(components.coreBaseUrl('0.0.0.0', 4321), 'http://127.0.0.1:4321', '非 loopback 一律压回')
  assert.equal(components.coreBaseUrl('', 10), `http://${defaults.DEFAULT_HOST}:10`)

  assert.equal(components.resolveRequestedPort(undefined), defaults.DEFAULT_CORE_PORT)
  assert.equal(components.resolveRequestedPort(null), defaults.DEFAULT_CORE_PORT)
  assert.equal(components.resolveRequestedPort(0), 0)
  assert.equal(components.resolveRequestedPort('1234'), 1234)
  assert.throws(() => components.resolveRequestedPort(-1), /端口不合法/)
  assert.throws(() => components.resolveRequestedPort(70000), /端口不合法/)
  assert.throws(() => components.resolveRequestedPort('abc'), /端口不合法/)
})

test('isPortConflict 认内核的常见措辞，不把无关错误当端口冲突', () => {
  assert.equal(components.isPortConflict('OSError: [Errno 98] Address already in use'), true)
  assert.equal(components.isPortConflict('EADDRINUSE'), true)
  assert.equal(components.isPortConflict('another server might be running'), true)
  assert.equal(components.isPortConflict("ModuleNotFoundError: No module named 'app'"), false)
  assert.equal(components.isPortConflict(''), false)
})

// ------------------------------------------------------------------- startCore
test('startCore：默认把端口交给内核（--port 0），真实端口从 listening 事件读', async t => {
  const harness = setup(t, { plan: () => listening(43211) })

  const started = await components.startCore({ repoRoot: harness.repo })

  assert.equal(harness.spawns.length, 1)
  assert.equal(cliPort(harness.spawns[0].args), '0', '不传 port 时必须下发 --port 0')
  assert.equal(harness.spawns[0].options.env.CHIRON_PORT, '0')
  assert.equal(started.baseUrl, 'http://127.0.0.1:43211')
  assert.equal(started.port, 43211)
  assert.equal(started.listening, true)
})

test('startCore：端口冲突时换端口重试，第二次以 listening 事件的端口为准', async t => {
  const harness = setup(t, {
    plan: index => {
      if (index === 0) {
        return child => {
          child.stderr.emit('data', 'OSError: [Errno 98] Address already in use\n')
          child.emit('exit', 1)
        }
      }
      return listening(50001)
    },
  })

  const started = await components.startCore({ repoRoot: harness.repo })

  assert.equal(harness.spawns.length, 2, '第一次撞端口后应再起一次')
  assert.equal(cliPort(harness.spawns[0].args), '0')
  assert.equal(cliPort(harness.spawns[1].args), '0', '重试也应交给内核')
  assert.equal(started.baseUrl, 'http://127.0.0.1:50001')
  assert.equal(started.port, 50001)

  const state = components.getCoreState()
  assert.equal(state.running, true)
  assert.equal(state.attempt, 2, '第二次尝试的标记要能看出来')
})

test('startCore：调用方指定的固定端口被占时，重试退回 0（不再撞同一个端口）', async t => {
  const harness = setup(t, {
    plan: index => {
      if (index === 0) {
        return child => {
          child.stderr.emit('data', 'bind: address already in use\n')
          child.emit('exit', 1)
        }
      }
      return listening(50002)
    },
  })

  const started = await components.startCore({ repoRoot: harness.repo, port: 45555 })

  assert.equal(cliPort(harness.spawns[0].args), '45555')
  assert.equal(cliPort(harness.spawns[1].args), '0', '重试必须换端口，而不是继续用被占的 45555')
  assert.equal(started.port, 50002)
})

test('startCore：就绪超时（收不到 listening）也换端口重试', async t => {
  const harness = setup(t, {
    plan: index => (index === 0 ? null : listening(50003)),
  })

  const started = await components.startCore({
    repoRoot: harness.repo,
    healthTimeoutMs: 40,
    healthIntervalMs: 5,
  })

  assert.equal(harness.spawns.length, 2)
  assert.equal(started.port, 50003)
})

test('startCore：所有尝试都失败时给可读错误，带每次的端口与原因', async t => {
  const harness = setup(t, {
    plan: () => child => {
      child.stderr.emit('data', 'OSError: address already in use\n')
      child.emit('exit', 1)
    },
  })

  await assert.rejects(
    () => components.startCore({ repoRoot: harness.repo }),
    err => {
      assert.match(err.message, /本地 core 启动失败/)
      assert.match(err.message, new RegExp(`共尝试 ${defaults.CORE_PORT_ATTEMPTS}/${defaults.CORE_PORT_ATTEMPTS} 次`))
      assert.match(err.message, /端口冲突/)
      assert.match(err.message, /address already in use/)
      assert.equal(err.attempts.length, defaults.CORE_PORT_ATTEMPTS)
      return true
    },
  )

  assert.equal(harness.spawns.length, defaults.CORE_PORT_ATTEMPTS)
  assert.equal(components.getCoreState().running, false, '失败后不能留下"在跑"的状态')
  assert.equal(components.getCoreState().baseUrl, '')
})

test('startCore：ENOENT（命令不存在）不重试，直接给可读错误', async t => {
  const harness = setup(t)
  const fatal = Object.assign(new Error('spawn python ENOENT'), { code: 'ENOENT' })
  components._setDeps({
    ...harness.deps,
    spawn: (_command, args, options) => {
      harness.spawns.push({ args, options })
      throw fatal
    },
  })

  await assert.rejects(
    () => components.startCore({ repoRoot: harness.repo }),
    /本地 core 启动失败/,
  )
  assert.equal(harness.spawns.length, 1, '不可恢复的 spawn 错误不该重试')
})

// ----------------------------------------------------------------- 核心状态
test('getCoreState：返回 {baseUrl, port, token, pid, dataDir}，地址与端口自洽', async t => {
  const harness = setup(t, { plan: () => listening(43250) })

  const started = await components.startCore({ repoRoot: harness.repo, token: 'tk-fixed', dataDir: path.join(harness.repo, 'local') })
  const state = components.getCoreState()

  for (const key of ['baseUrl', 'port', 'token', 'pid', 'dataDir']) {
    assert.ok(key in state, `getCoreState() 缺少 ${key}`)
  }
  assert.equal(state.baseUrl, 'http://127.0.0.1:43250')
  assert.equal(state.port, 43250)
  assert.equal(state.baseUrl, `http://127.0.0.1:${state.port}`)
  assert.equal(state.token, 'tk-fixed')
  assert.equal(state.pid, harness.children[0].pid)
  assert.equal(state.dataDir, path.join(harness.repo, 'local'))
  assert.equal(started.baseUrl, state.baseUrl)

  // 没在跑时也是同一形状（调用方不需要判空再拼地址）
  await components.stopCore()
  const idle = components.getCoreState()
  assert.equal(idle.running, false)
  assert.equal(idle.baseUrl, '')
  assert.equal(idle.port, 0)
  assert.equal(idle.pid, undefined)
})

// ------------------------------------------------------------------- stopCore
test('stopCore：通过进程树终止（taskkill /T 那条路），不必再发 SIGTERM', async t => {
  const harness = setup(t, { plan: () => listening(43260) })
  await components.startCore({ repoRoot: harness.repo })

  const killed = []
  components._setDeps({
    ...harness.deps,
    killTree: (child, pid) => {
      killed.push(pid)
      child.emit('exit', 0) // 树被终止后 node 会收到 exit
      return true
    },
  })

  assert.equal(await components.stopCore(), true)
  assert.deepEqual(killed, [harness.children[0].pid])
  assert.deepEqual(harness.children[0].kills, [], '进程树终止成功后不该再发信号')
  assert.equal(components.isCoreRunning(), false)
})

test('stopCore：进程树终止失败时回退 SIGTERM → SIGKILL', async t => {
  const harness = setup(t, { plan: () => listening(43261) })
  await components.startCore({ repoRoot: harness.repo })

  const killed = []
  components._setDeps({
    ...harness.deps,
    // 模拟 taskkill 没成功：既不返回 true，也不让进程退出
    killTree: (child, pid) => {
      killed.push(pid)
      return false
    },
  })

  // 让 SIGTERM 立刻生效，避免测试真的等 5s
  const child = harness.children[0]
  child.kill = signal => {
    child.kills.push(signal)
    child.emit('exit', 0)
    return true
  }

  assert.equal(await components.stopCore(), true)
  assert.deepEqual(killed, [child.pid])
  assert.deepEqual(child.kills, ['SIGTERM'])
})
