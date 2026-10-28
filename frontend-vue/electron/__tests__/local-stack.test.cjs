'use strict'
/**
 * `local-stack.cjs` 的契约测试：引导器 → 网关这条交接链路。
 *
 * 这条链路有两处"错了也很难看出来"的地方，都用用例钉住：
 *
 * 1. **引导器 stdout 是混合流**。它混着 alembic 的 `[Alembic] ...` 日志。若解析时
 *    用"包含某字符串"之类的模糊判断，日志里出现 `launcher-ready` 字样就会误判就绪，
 *    于是拿着空 DSN 去起网关 —— 用户看到的是网关报 PG 不可用，真因被埋掉。
 * 2. **APP_SECRET 不足 32 字符网关会直接拒绝启动**。在交接处校验，错误才指向真正原因；
 *    放行到网关侧，用户看到的是网关日志里一句 FATAL。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const { PassThrough } = require('node:stream')

const stack = require('../local-stack.cjs')

const VALID_SECRET = 'x'.repeat(32)

/** 假子进程：stdout/stderr 用 PassThrough，便于按行喂数据 */
function fakeChild() {
  const child = new EventEmitter()
  child.stdout = new PassThrough()
  child.stderr = new PassThrough()
  child.stdin = new PassThrough()
  child.pid = 4242
  child.exitCode = null
  child.kill = () => {
    child.exitCode = 0
    child.emit('exit', 0)
    return true
  }
  return child
}

function withFakeSpawn(t, child) {
  const calls = []
  stack._setDeps({
    spawn: (command, args, options) => {
      calls.push({ command, args, options })
      return child
    },
  })
  t.after(() => {
    stack._resetDeps()
    stack._resetState()
  })
  return calls
}

// ------------------------------------------------------------------ 行解析

test('parseLauncherLine 忽略 alembic 等非 JSON 日志行', () => {
  for (const line of [
    '[Alembic] Loaded .env from X:\\project\\Chiron\\.env',
    '[Alembic] Database URL: postgresql+psycopg2://postgres:***@127.0.0.1:1/chiron',
    'WARNING: something',
    '',
    '{ not json',
  ]) {
    assert.equal(stack.parseLauncherLine(line), null, `不该把 ${JSON.stringify(line)} 当成事件`)
  }
})

test('parseLauncherLine 认不出的事件返回 null（不误判就绪）', () => {
  assert.equal(stack.parseLauncherLine('{"event":"listening","port":123}'), null)
  assert.equal(stack.parseLauncherLine('{"event":"something-else"}'), null)
})

test('parseLauncherLine 解析 launcher-ready 的全部交接字段', () => {
  const line = JSON.stringify({
    event: 'launcher-ready',
    pg_dsn: 'postgresql://postgres:@127.0.0.1:50165/chiron',
    redis_addr: '127.0.0.1:50404',
    data_dir: 'C:\\data',
    pg_backend: 'embedded-pack',
    pg_port: 50165,
    redis_backend: 'bundled',
    redis_port: 50404,
    migration: { ok: true, revision: 'abc' },
    startup_ms: 5658.1,
  })

  const parsed = stack.parseLauncherLine(line)

  assert.equal(parsed.event, stack.LAUNCHER_READY_EVENT)
  assert.equal(parsed.pgDsn, 'postgresql://postgres:@127.0.0.1:50165/chiron')
  assert.equal(parsed.redisAddr, '127.0.0.1:50404')
  assert.equal(parsed.pgPort, 50165)
  assert.equal(parsed.redisPort, 50404)
  assert.equal(parsed.pgBackend, 'embedded-pack')
  assert.deepEqual(parsed.migration, { ok: true, revision: 'abc' })
})

test('parseLauncherLine 解析 launcher-error', () => {
  const parsed = stack.parseLauncherLine('{"event":"launcher-error","error":"RedisUnavailable: 找不到 redis-server"}')

  assert.equal(parsed.event, stack.LAUNCHER_ERROR_EVENT)
  assert.match(parsed.error, /找不到 redis-server/)
})

// ------------------------------------------------------------------ 网关环境

test('buildGatewayEnv 缺 DSN 时在交接处就报错', () => {
  assert.throws(
    () => stack.buildGatewayEnv({ pgDsn: '', redisAddr: '127.0.0.1:1' }, { appSecret: VALID_SECRET, port: 9000 }),
    /未提供 PostgreSQL DSN/,
  )
})

test('buildGatewayEnv 拒绝过短的 APP_SECRET', () => {
  assert.throws(
    () => stack.buildGatewayEnv({ pgDsn: 'postgresql://x' }, { appSecret: 'short', port: 9000 }),
    /至少需要 32 个字符/,
  )
})

test('buildGatewayEnv 拒绝非法端口', () => {
  assert.throws(
    () => stack.buildGatewayEnv({ pgDsn: 'postgresql://x' }, { appSecret: VALID_SECRET, port: 0 }),
    /端口非法/,
  )
})

test('buildGatewayEnv 把 DSN / 端口 / 密钥 / Redis 地址写成网关认的名字', () => {
  const env = stack.buildGatewayEnv(
    { pgDsn: 'postgresql://postgres:@127.0.0.1:50165/chiron', redisAddr: '127.0.0.1:50404' },
    { appSecret: VALID_SECRET, port: 9100 },
  )

  assert.equal(env.POSTGRES_DSN, 'postgresql://postgres:@127.0.0.1:50165/chiron')
  assert.equal(env.PORT, '9100')
  assert.equal(env.APP_SECRET, VALID_SECRET)
  assert.equal(env.REDIS_ADDR, '127.0.0.1:50404')
})

test('buildGatewayEnv 无 Redis 地址时不写 REDIS_ADDR（交给网关自己的默认与报错）', () => {
  const env = stack.buildGatewayEnv(
    { pgDsn: 'postgresql://x', redisAddr: '' },
    { appSecret: VALID_SECRET, port: 9100, env: {} },
  )

  // 环境里可能继承到用户设置的 REDIS_ADDR，这里只断言我们没主动覆盖成空串
  assert.notEqual(env.REDIS_ADDR, '')
})

// ------------------------------------------------------------------ 引导器握手

test('startLauncher 用 -m app.launcher 起进程，并在 ready 行后返回连接信息', async t => {
  const child = fakeChild()
  const calls = withFakeSpawn(t, child)

  const promise = stack.startLauncher({ launcher: { command: 'python', cwd: '/tmp' }, dataDir: '/tmp/data' })

  // 先喂一条 alembic 噪声行，确认它不会让人抢跑
  child.stdout.write('[Alembic] Loaded .env\n')
  child.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )

  const result = await promise

  assert.equal(calls.length, 1)
  assert.equal(calls[0].command, 'python')
  assert.deepEqual(calls[0].args, ['-m', 'app.launcher', '--data-dir', '/tmp/data'])
  // stdin 必须是 pipe：引导器靠 stdin EOF 判断父进程是否退出
  assert.equal(calls[0].options.stdio[0], 'pipe')
  assert.equal(result.info.pgDsn, 'postgresql://u@127.0.0.1:1/c')
  assert.equal(result.info.redisAddr, '127.0.0.1:2')
})

test('startLauncher 收到 launcher-error 事件时拒绝，并带上原因', async t => {
  const child = fakeChild()
  withFakeSpawn(t, child)

  const promise = stack.startLauncher({ launcher: { command: 'python' } })
  child.stdout.write(JSON.stringify({ event: 'launcher-error', error: 'RedisUnavailable: 找不到 redis-server' }) + '\n')

  await assert.rejects(promise, /找不到 redis-server/)
})

test('startLauncher 在未就绪就退出时拒绝（不假装成功）', async t => {
  const child = fakeChild()
  withFakeSpawn(t, child)

  const promise = stack.startLauncher({ launcher: { command: 'python' } })
  child.exitCode = 1
  child.emit('exit', 1)

  await assert.rejects(promise, /未就绪即退出/)
})

test('stopLauncher 通过关闭 stdin 请求正常退出', async t => {
  const child = fakeChild()
  withFakeSpawn(t, child)

  const promise = stack.startLauncher({ launcher: { command: 'python' } })
  child.stdout.write(JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u', redis_addr: '' }) + '\n')
  await promise

  // 引导器收到 EOF 后自行退出
  child.stdin.on('finish', () => {
    child.exitCode = 0
    child.emit('exit', 0)
  })

  assert.equal(await stack.stopLauncher(), true)
})

// ------------------------------------------------------------------ 引擎环境
//
// 引擎与网关的环境变量名**几乎全不一样**，写错不会有明显报错，只会让引擎连不上
// 库或压根起不来。这组用例把每个键钉住。

test('buildEngineEnv 缺 DSN 时在交接处就报错', () => {
  assert.throws(
    () => stack.buildEngineEnv({ pgDsn: '' }, { appSecret: VALID_SECRET, port: 9000 }),
    /未提供 PostgreSQL DSN/,
  )
})

test('buildEngineEnv 拒绝过短的 APP_SECRET', () => {
  assert.throws(
    () => stack.buildEngineEnv({ pgDsn: 'postgresql://x' }, { appSecret: 'short', port: 9000 }),
    /至少需要 32 个字符/,
  )
})

test('buildEngineEnv 用引擎认的变量名，并显式开降级模式', () => {
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://postgres:@127.0.0.1:50165/chiron', redisAddr: '127.0.0.1:50404' },
    { appSecret: VALID_SECRET, port: 9100, env: {} },
  )

  assert.equal(env.POSTGRES_DSN, 'postgresql://postgres:@127.0.0.1:50165/chiron')
  assert.equal(env.APP_SECRET, VALID_SECRET)
  // 引擎走 uvicorn，认 HTTP_HOST/HTTP_PORT；网关认的是 PORT
  assert.equal(env.HTTP_HOST, '127.0.0.1')
  assert.equal(env.HTTP_PORT, '9100')
  // **单机必须显式降级**：否则 verify_sandbox_backend 会在缺 SANDBOX_SERVICE_URL 时拒绝启动
  assert.equal(env.DEGRADED_MODE, 'true')
  // 引擎要 URI 形态；引导器给的是 host:port
  assert.equal(env.REDIS_URL, 'redis://127.0.0.1:50404')
})

test('buildEngineEnv 不把已带 scheme 的 Redis 地址拼成两遍', () => {
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x', redisAddr: 'redis://127.0.0.1:50404' },
    { appSecret: VALID_SECRET, port: 9100, env: {} },
  )

  assert.equal(env.REDIS_URL, 'redis://127.0.0.1:50404')
})

// ------------------------------------------------------------------ 引擎进程

/** 替换全局 fetch：本模块的探针都走 fetch，不打真实网络 */
function mockFetch(t, handler) {
  const original = globalThis.fetch
  globalThis.fetch = handler
  t.after(() => {
    globalThis.fetch = original
  })
}

test('startEngine 没有启动信息时返回 ok:false，且不 spawn', async t => {
  const calls = withFakeSpawn(t, fakeChild())

  const result = await stack.startEngine({ pgDsn: 'postgresql://x' }, { appSecret: VALID_SECRET })

  assert.equal(result.ok, false)
  assert.equal(result.reason, 'no-launch')
  assert.equal(calls.length, 0)
})

test('startEngine 探测到 /healthz 后返回可交给网关的地址', async t => {
  const child = fakeChild()
  const calls = withFakeSpawn(t, child)
  const probed = []
  mockFetch(t, async url => {
    probed.push(String(url))
    return { ok: true, json: async () => ({}) }
  })

  const result = await stack.startEngine(
    { pgDsn: 'postgresql://x', redisAddr: '127.0.0.1:2' },
    { appSecret: VALID_SECRET, port: 9300, engine: { command: 'py', args: ['-m', 'app.main'], cwd: '/' }, intervalMs: 1 },
  )

  assert.equal(result.ok, true)
  // 网关要的是 host:port（不含 scheme），这里给了带 scheme 的就会连不上
  assert.equal(result.address, '127.0.0.1:9300')
  assert.equal(calls[0].command, 'py')
  assert.deepEqual(calls[0].args, ['-m', 'app.main'])
  // 判"活着"只认 /healthz：降级模式下 /readyz 的语义与网关的 /ready 不同
  assert.ok(probed.some(u => u.endsWith('/healthz')), `探针应打 /healthz，实际：${probed.join(',')}`)
})

test('startEngine 进程立即退出时返回 ok:false 并带上 stderr', async t => {
  const child = fakeChild()
  withFakeSpawn(t, child)
  mockFetch(t, async () => ({ ok: false }))

  const promise = stack.startEngine(
    { pgDsn: 'postgresql://x' },
    { appSecret: VALID_SECRET, port: 9301, engine: { command: 'py', cwd: '/' }, intervalMs: 1 },
  )
  child.stderr.write('ModuleNotFoundError: No module named "pydantic_ai"\n')
  child.exitCode = 1
  child.emit('exit', 1)

  const result = await promise

  assert.equal(result.ok, false)
  assert.equal(result.reason, 'exited')
  // stderr 必须带回去：缺依赖与端口占用都只能从这里看出来
  assert.match(result.stderrTail, /pydantic_ai/)
})

// ------------------------------------------------------------------ 整栈编排

/** 让 spawn 按调用顺序返回不同假子进程（引导器 / 引擎 / 网关） */
function withSpawnQueue(t, children) {
  const calls = []
  stack._setDeps({
    spawn: (command, args, options) => {
      calls.push({ command, args, options })
      const child = children[calls.length - 1]
      assert.ok(child, `意外的第 ${calls.length} 次 spawn：${command}`)
      return child
    },
  })
  t.after(() => {
    stack._resetDeps()
    stack._resetState()
  })
  return calls
}

/** 网关的就绪探针：/health 只证明活着，/ready 要求 PG 与 Redis 都 up */
function gatewayFetch(url) {
  const u = String(url)
  if (u.endsWith('/ready')) {
    return { ok: true, json: async () => ({ success: true, data: { postgres: 'up', redis: 'up' } }) }
  }
  return { ok: true, json: async () => ({}) }
}

test('startLocalStack 按 引导器 → 引擎 → 网关 的顺序起，并把引擎地址交给网关', async t => {
  const launcherChild = fakeChild()
  const engineChild = fakeChild()
  const gatewayChild = fakeChild()
  const calls = withSpawnQueue(t, [launcherChild, engineChild, gatewayChild])
  mockFetch(t, async url => gatewayFetch(url))

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: 9400,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', args: ['-m', 'app.main'], cwd: '/' },
    enginePort: 9401,
    intervalMs: 1,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )

  const result = await promise

  assert.equal(result.engine.ok, true)
  // 顺序不变量：引擎必须先于网关，否则网关起来时拿不到地址
  assert.deepEqual(calls[0].args.slice(0, 2), ['-m', 'app.launcher'])
  assert.deepEqual(calls[1].args, ['-m', 'app.main'])
  assert.equal(calls[2].command, 'gateway.exe')
  // 全仓唯一写 PYTHON_ENGINE_ADDRESS 的地方，错了网关就建不出客户端
  assert.equal(calls[2].options.env.PYTHON_ENGINE_ADDRESS, '127.0.0.1:9401')
})

test('引擎起不来就是整栈失败：不悄悄降级成"没有引擎的本地模式"', async t => {
  const launcherChild = fakeChild()
  const engineChild = fakeChild()
  const gatewayChild = fakeChild()
  // 真实引导器收到 stdin EOF 后自行退出（见 launcher.py::_watch_stdin_eof）。
  // 不模拟这一条，回滚就要在 waitExit 里干等满 8s。
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  const calls = withSpawnQueue(t, [launcherChild, engineChild, gatewayChild])
  mockFetch(t, async () => ({ ok: false })) // 引擎永远不健康

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: 9400,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', cwd: '/' },
    enginePort: 9401,
    intervalMs: 1,
    timeoutMs: 60,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )

  // PG、Redis、引擎三者同级：引擎缺席不是"少几个页面"，而是本地模式不可用
  await assert.rejects(promise, /python-engine 未能启动/)
  // 网关不该被拉起：起了它就会用一个不存在的引擎地址对外提供服务
  assert.equal(calls.length, 2, `只该 spawn 引导器与引擎，实际 ${calls.length} 次`)
  // 已启动的部分必须回滚，否则下次启动会撞上残留的 postmaster
  const state = stack.getStackState()
  assert.equal(state.launcher, null, '引导器应已被停掉')
  assert.equal(state.engine, null, '引擎应已被停掉')
})

