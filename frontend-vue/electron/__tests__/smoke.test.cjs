'use strict'
/**
 * 桌面 smoke 的纯 Node 单测（**不起真实进程、不装 Electron**）。
 *
 * 覆盖 desktop-local/scripts/smoke_desktop.py 与壳层之间真正容易漂移的三件事：
 *
 *   1. **启动参数** —— `python -m app.main` 而不是 `python desktop-local/app/main.py`
 *      （后者必然 ImportError），dev 态必须带 `--data-dir/--port/--inference-proxy`，
 *      且契约名 `CHIRON_*` 与 config.py 真名 `CHIRON_LOCAL_*` 两套都得下发；
 *   2. **退出回收** —— stopCore 先 SIGTERM，进程不退才 SIGKILL，然后状态清零、可以重启；
 *   3. **local 模式的 baseUrl 注入** —— 端口以 stdout 的 listening 事件为准（`--port 0`
 *      时只有 core 知道真实端口），非 loopback 的 host 一律压回 127.0.0.1，
 *      组件管理拿到的 `{baseUrl, token, pid}` 就是 main.cjs 注入给 preload 的那份。
 *
 * 所有 Electron / spawn / fetch 都由 `components.cjs::_setDeps` 注入，走的仍是真实的
 * startCore/stopCore 代码路径 —— mock 的只是外部世界，不是被测逻辑。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')

const components = require('../components.cjs')
const shared = require('../shared.cjs')

// --------------------------------------------------------------------- 夹具
/** 假 core 进程：kill 可控（默认 SIGTERM 就退出），stdout/stderr 可喂 */
function makeChild({ exitOn } = {}) {
  const exitSignals = exitOn || ['SIGTERM']
  const child = new EventEmitter()
  child.pid = 9001
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()
  child.kills = []
  child.kill = signal => {
    child.kills.push(signal)
    if (exitSignals.includes(signal)) child.emit('exit', 0)
    return true
  }
  /** 喂一行 core 播报（startCore 按行切分 stdout） */
  child.emitLine = obj => child.stdout.emit('data', `${JSON.stringify(obj)}\n`)
  return child
}

/**
 * 造一个"仓库 + userData + 假 spawn/fetch"的环境。
 * `withSource` 决定 resolveCoreLaunch 走 dev（有 main.py）还是装好的二进制。
 */
function setup(t, { withSource = true, child = makeChild(), healthOk = true } = {}) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-smoke-userdata-'))
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-smoke-repo-'))
  if (withSource) {
    fs.mkdirSync(path.join(repo, 'desktop-local', 'app'), { recursive: true })
    fs.writeFileSync(path.join(repo, 'desktop-local', 'app', 'main.py'), '# fake core\n', 'utf8')
  }
  if (!withSource) {
    fs.mkdirSync(path.join(userData, 'components', 'core'), { recursive: true })
    const exe = process.platform === 'win32' ? 'chiron-local.exe' : 'chiron-local'
    fs.writeFileSync(path.join(userData, 'components', 'core', exe), '', 'utf8')
  }

  const spawns = []
  const fetched = []
  components._setDeps({
    app: { getPath: () => userData },
    spawn: (command, args, options) => {
      spawns.push({ command, args, options })
      return child
    },
    fetch: async url => {
      fetched.push(String(url))
      return { ok: healthOk, status: healthOk ? 200 : 503, json: async () => ({ status: 'ok' }) }
    },
  })
  components._resetState()

  t.after(async () => {
    await components.stopCore()
    components._resetState()
    components._resetDeps()
    fs.rmSync(userData, { recursive: true, force: true })
    fs.rmSync(repo, { recursive: true, force: true })
  })

  return { userData, repo, spawns, fetched, child }
}

// --------------------------------------------------------------------- 1. 启动参数
test('启动参数：dev 态用 `-m app.main` + cwd=desktop-local（不能按脚本路径启动）', async t => {
  const { repo, spawns, child } = setup(t)

  const launch = components.resolveCoreLaunch(repo)
  assert.equal(launch.kind, 'dev')
  assert.deepEqual(launch.args, ['-m', 'app.main'])
  assert.equal(launch.cwd, path.join(repo, 'desktop-local'))
  assert.equal(launch.command, process.env.CHIRON_PYTHON || 'python')

  // 真正 spawn 出去的命令必须是同一份（按脚本路径启动必然 ImportError）
  setImmediate(() => child.emitLine({ event: 'listening', host: '127.0.0.1', port: 45120 }))
  await components.startCore({ repoRoot: repo, port: 0 })
  assert.deepEqual(spawns[0].args.slice(0, 3), ['-m', 'app.main', '--data-dir'])
  assert.equal(spawns[0].options.cwd, path.join(repo, 'desktop-local'))
})

test('启动参数：装好的二进制只走 env，绝不追加 CLI 参数', async t => {
  const { userData, repo } = setup(t, { withSource: false })
  const launch = components.resolveCoreLaunch(repo)
  assert.equal(launch.kind, 'binary')
  assert.deepEqual(launch.args, [])
  assert.equal(launch.cwd, repo)
  const exe = process.platform === 'win32' ? 'chiron-local.exe' : 'chiron-local'
  assert.equal(launch.command, path.join(userData, 'components', 'core', exe))
})

test('启动参数：data-dir / port / inference-proxy 同时进 CLI 与两套 env 名', async t => {
  const { repo, spawns, child } = setup(t)
  const dataDir = path.join(repo, 'local-data')
  // 监听器在 spawn 返回后才挂上，所以要在下一个 tick 才喂 stdout
  setImmediate(() => child.emitLine({ event: 'listening', host: '127.0.0.1', port: 4567 }))

  await components.startCore({
    repoRoot: repo,
    dataDir,
    port: 0,
    token: 'fixed-token',
    inferenceProxyUrl: 'http://127.0.0.1:9/infer',
  })

  const { args, options } = spawns[0]
  // CLI：argparse 的显式参数优先级最高，所以 smoke 与壳层都靠它
  assert.deepEqual(args.slice(2), [
    '--data-dir',
    dataDir,
    '--port',
    '0',
    '--inference-proxy',
    'http://127.0.0.1:9/infer',
  ])
  // env：契约名 + config.py 真名（pydantic 的 env_prefix 是 CHIRON_LOCAL_）
  assert.equal(options.env.CHIRON_DATA_DIR, dataDir)
  assert.equal(options.env.CHIRON_LOCAL_DATA_DIR, dataDir)
  assert.equal(options.env.CHIRON_PORT, '0')
  assert.equal(options.env.CHIRON_LOCAL_PORT, '0')
  assert.equal(options.env.CHIRON_INFERENCE_PROXY, 'http://127.0.0.1:9/infer')
  assert.equal(options.env.CHIRON_LOCAL_INFERENCE_PROXY, 'http://127.0.0.1:9/infer')
  // 设备 token：桌面端默认开鉴权，token 只进 env，不进 argv（argv 会被别的进程看到）
  assert.equal(options.env.CHIRON_DEVICE_TOKEN, 'fixed-token')
  assert.equal(options.env.CHIRON_LOCAL_AUTH_ENABLED, '1')
  assert.equal(args.includes('fixed-token'), false)
  // Windows 上不弹控制台窗口
  assert.equal(options.windowsHide, true)
})

test('启动参数：authEnabled:false 下发 0，但已有环境变量不被覆盖', () => {
  const off = components.buildCoreEnv({ authEnabled: false })
  assert.equal(off.CHIRON_LOCAL_AUTH_ENABLED, '0')

  const preset = components.buildCoreEnv({ env: { CHIRON_LOCAL_AUTH_ENABLED: '1' }, authEnabled: false })
  assert.equal(preset.CHIRON_LOCAL_AUTH_ENABLED, '1')

  // 空值不下发（pydantic 会把 "" 当成显式值，反而盖掉合理默认）
  assert.equal(components.buildCoreEnv({ dataDir: '' }).CHIRON_DATA_DIR, undefined)
})

// --------------------------------------------------------------------- 2. local 模式 baseUrl 注入
test('baseUrl 注入：`--port 0` 时以 stdout 的 listening 事件为准', async t => {
  const { repo, child } = setup(t)
  // 不要在这个 tick 内发：监听器是在 spawn 返回后才挂上的
  setImmediate(() => child.emitLine({ event: 'listening', host: '127.0.0.1', port: 45123, version: '0.1.0' }))

  const started = await components.startCore({ repoRoot: repo, port: 0, token: 'tk' })

  assert.equal(started.baseUrl, 'http://127.0.0.1:45123')
  assert.equal(started.token, 'tk')
  assert.equal(started.pid, 9001)
  assert.equal(started.listening, true)
  // 形状就是 main.cjs 注入给 preload 的那份（baseUrl/token/pid 是壳层唯一依赖的三个）
  for (const key of ['baseUrl', 'token', 'pid']) assert.ok(key in started, `缺少 ${key}`)
})

test('baseUrl 注入：非 loopback 的 host 一律压回 127.0.0.1', async t => {
  const { repo, child } = setup(t)
  setImmediate(() => child.emitLine({ event: 'listening', host: '0.0.0.0', port: 45124 }))

  const started = await components.startCore({ repoRoot: repo, port: 0 })
  assert.equal(started.baseUrl, 'http://127.0.0.1:45124')
})

test('baseUrl 注入：没有 listening 事件时回退到「借来的端口 + /healthz 探测」', async t => {
  const { repo, fetched } = setup(t)

  const started = await components.startCore({ repoRoot: repo }) // 不传 port → 先借一个空闲端口

  assert.match(started.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/)
  assert.equal(started.listening, false)
  assert.ok(fetched.some(url => url.endsWith('/healthz')), '没有探测过 /healthz')
})

test('baseUrl 注入：组件管理从同一个 baseUrl 取数（local 模式下 preload 注入的就是它）', async t => {
  const { repo, child, fetched } = setup(t)
  setImmediate(() => child.emitLine({ event: 'listening', host: '127.0.0.1', port: 45125 }))
  await components.startCore({ repoRoot: repo, port: 0, token: 'tk' })

  components._setDeps({
    app: { getPath: () => repo },
    fetch: async (url, options = {}) => {
      fetched.push({ url: String(url), method: options.method || 'GET' })
      return { ok: true, status: 200, json: async () => ({ data: [] }) }
    },
  })
  const started = await components.startComponent('core')
  assert.equal(started.running, true)
  assert.equal(started.baseUrl, 'http://127.0.0.1:45125')
  assert.equal(components.isCoreRunning(), true)
})

test('baseUrl 注入：参数前缀是共享常量（main.cjs 拼 additionalArguments 时用的就是它们）', () => {
  assert.equal(shared.MODE_ARG_PREFIX, '--chiron-mode=')
  assert.equal(shared.API_URL_ARG_PREFIX, '--chiron-api-url=')
  assert.equal(shared.DEVICE_TOKEN_ARG_PREFIX, '--chiron-device-token=')
  // 与 main.cjs::createWindow 的拼法一致：local 模式只注入 baseUrl 与 core 给的 token
  const baseUrl = 'http://127.0.0.1:45126'
  assert.equal(`${shared.API_URL_ARG_PREFIX}${baseUrl}`, '--chiron-api-url=http://127.0.0.1:45126')
  assert.equal(`${shared.DEVICE_TOKEN_ARG_PREFIX}tk`, '--chiron-device-token=tk')
  assert.equal(shared.partitionFor('local'), 'persist:chiron-local')
})

// --------------------------------------------------------------------- 3. 退出回收
test('退出回收：stopCore 发 SIGTERM，进程退出后状态清零、可以重新拉起', async t => {
  const { repo, spawns, child } = setup(t)
  setImmediate(() => child.emitLine({ event: 'listening', host: '127.0.0.1', port: 45127 }))
  await components.startCore({ repoRoot: repo, port: 0 })
  assert.equal(components.isCoreRunning(), true)
  assert.equal(spawns.length, 1)

  const stopped = await components.stopCore()
  assert.equal(stopped, true)
  assert.deepEqual(child.kills, ['SIGTERM'])
  assert.equal(components.isCoreRunning(), false)
  const state = components.getCoreState()
  assert.equal(state.running, false)
  assert.equal(state.baseUrl, '')

  // 再停一次：没有进程可停，返回 false（不能让调用方以为又杀了一次）
  assert.equal(await components.stopCore(), false)

  // 重启：真的又 spawn 了一次，而不是复用已死的记录
  const next = makeChild()
  components._setDeps({
    app: { getPath: () => path.dirname(repo) },
    spawn: (_c, a, o) => (spawns.push({ args: a, options: o }), next),
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'ok' }) }),
  })
  setImmediate(() => next.emitLine({ event: 'listening', host: '127.0.0.1', port: 45128 }))
  const again = await components.startCore({ repoRoot: repo, port: 0 })
  assert.equal(spawns.length, 2)
  assert.equal(again.baseUrl, 'http://127.0.0.1:45128')
})

test('退出回收：SIGTERM 不生效时退回 SIGKILL（core 的超时是 5s）', async t => {
  const stubborn = makeChild({ exitOn: ['SIGKILL'] })
  const { repo } = setup(t, { child: stubborn })
  setImmediate(() => stubborn.emitLine({ event: 'listening', host: '127.0.0.1', port: 45129 }))
  await components.startCore({ repoRoot: repo, port: 0 })

  const startedAt = Date.now()
  assert.equal(await components.stopCore(), true)
  const elapsed = Date.now() - startedAt
  assert.deepEqual(stubborn.kills, ['SIGTERM', 'SIGKILL'])
  // 证明它等过了 SIGTERM 的宽限期（5s，给 uvicorn 关 PG 留时间），不是立刻强杀
  assert.ok(elapsed >= 4000, `SIGKILL 来得太早（${elapsed}ms），没有等 SIGTERM 宽限期`)
  assert.equal(components.isCoreRunning(), false)
})

test('退出回收：core 自己挂掉时 startCore 抛错并带上真实 stderr（不伪装成功）', async t => {
  const { repo } = setup(t)
  const dying = makeChild({ exitOn: [] })
  components._setDeps({
    app: { getPath: () => path.dirname(repo) },
    spawn: () => dying,
    fetch: async () => ({ ok: false, status: 503, json: async () => ({}) }),
  })
  setImmediate(() => {
    dying.stderr.emit('data', 'Traceback (most recent call last):\nModuleNotFoundError: No module named app\n')
    dying.emit('exit', 1)
  })

  await assert.rejects(
    () => components.startCore({ repoRoot: repo, port: 0 }),
    err => {
      assert.match(err.message, /本地 core 启动失败/)
      assert.match(err.message, /No module named app/)
      return true
    },
  )
  assert.equal(components.isCoreRunning(), false)
})
