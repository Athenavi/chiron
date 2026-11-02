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
const components = require('../components.cjs')

const VALID_SECRET = 'x'.repeat(32)

/** 让 PassThrough 的 'data' 事件（stderr 落地等）跑完一个宏任务 */
const tick = () => new Promise(resolve => setImmediate(resolve))

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

test('buildGatewayEnv 打开本地会话引导：不打开就没有任何身份，本地模式只能停在未登录', () => {
  const env = stack.buildGatewayEnv(
    { pgDsn: 'postgresql://x', redisAddr: '' },
    { appSecret: VALID_SECRET, port: 9100, env: {} },
  )

  assert.equal(env[stack.LOCAL_BOOTSTRAP_ENV], '1')
})

test('buildGatewayEnv 保留调用方显式给出的 LOCAL_BOOTSTRAP（含关闭）', () => {
  const env = stack.buildGatewayEnv(
    { pgDsn: 'postgresql://x', redisAddr: '' },
    { appSecret: VALID_SECRET, port: 9100, env: { LOCAL_BOOTSTRAP: '0' } },
  )

  assert.equal(env[stack.LOCAL_BOOTSTRAP_ENV], '0')
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

/**
 * ★ 回归：树杀那条路**不能**报"已回收"。
 *
 * 真机现场：应用退出后日志写 `local backend stopped go-stack（gateway=true engine=true
 * launcher=true）`，可 postgres 还在监听 64762。PG 是 `pg_ctl` 起的**非子进程**，
 * `killProcessTree` 收不到它 —— 只有引导器自己走到 `run()` 的 `finally` 才会 `pg.stop()`。
 * 所以"我们是靠树杀收的场"这件事必须原样上报，别让它伪装成一次正常停机。
 */
test('★ 回归：引导器不响应 stdin 时，树杀之后返回 false', async t => {
  const child = fakeChild()
  withFakeSpawn(t, child)

  const promise = stack.startLauncher({ launcher: { command: 'python' } })
  child.stdout.write(JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u', redis_addr: '' }) + '\n')
  await promise

  // 关 stdin 不退出（原 bug 的形状：Windows 上 EOF 侦测失效就是这个表现）
  let treeKilled = 0
  components._setDeps({
    killTree: target => {
      treeKilled += 1
      assert.equal(target, child, '收的必须是引导器那一棵树')
      // exit 必须**异步**到达：真实 taskkill 是同步调用的，但子进程的 exit 事件要等
      // 下一个 tick。同步 emit 会让紧接着注册的 waitExit 监听器错过这次事件，只能
      // 一路等到超时——而那个超时定时器是 unref 的，于是测试的 Promise 永远悬着。
      setImmediate(() => child.emit('exit', null, 'SIGKILL'))
      return true
    },
  })
  t.after(() => components._resetDeps())

  assert.equal(await stack.stopLauncher(30), false, '绕过引导器的 finally 却报已回收 = 谎报 PG 已停')
  assert.equal(treeKilled, 1, '宽限期后仍要收树，Redis 那类子进程得带走')
})

test('宽限期要够引导器停 Redis + pg_ctl -m fast', () => {
  // 给短了就会落到树杀那条路，而 PG 不在树里：等于每次退出都赌一次孤儿
  assert.ok(stack.LAUNCHER_EXIT_GRACE_MS >= 15000, `宽限期只有 ${stack.LAUNCHER_EXIT_GRACE_MS}ms`)
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

// ------------------------------------------------------------ 本地模式身份密钥

/**
 * 事故形状（真机）：打包态网关的 cwd 在仓库树里，`config/config.go::loadDotEnv` 的
 * `findFileUpward` 会一路往上找到文件系统根，命中开发用 `.env` 里的
 * `JWT_SECRET` / `INTERNAL_TOKEN`（显式配置优先于派生）；而引擎的
 * `app/config.py::_find_env_file` 只从自己的包目录往上找 3 层，打包态找不到，
 * 于是按 APP_SECRET 老实派生。两侧各信一个 token：网关→引擎全线 401。
 *
 * 口径钉在这里：值来自**同一个表达式**，且不因为继承来的环境而改变。
 */

function envOfGateway() {
  return stack.buildGatewayEnv({ pgDsn: 'postgresql://x' }, { appSecret: VALID_SECRET, port: 9100, env: {} })
}

test('派生口径与 Go / Python 逐字一致（跨语言黄金向量）', () => {
  // 由 python 侧真实实现算出：base64.urlsafe_b64encode(HMAC_SHA256('x'*32, domain)).rstrip('=')
  // go 侧同口径见 config/derive_subsecret_test.go 的同一组向量
  assert.equal(
    stack.deriveSubsecret(VALID_SECRET, 'chiron-jwt'),
    'SYvVXpKr8n5cZ_vf3u_0XzzekZyu1aXr-v225-TQbTg',
  )
  assert.equal(
    stack.deriveSubsecret(VALID_SECRET, 'chiron-internal'),
    'BzcOjSRtC43_-5jEb9X-OwD3Degb-CMKy8RwVEyQ9ok',
  )
  // base64url 且**无 padding**：Go 用 RawURLEncoding、Python 把 '=' 剥掉，多一个 '=' 就是 401
  assert.match(stack.deriveSubsecret(VALID_SECRET, 'chiron-internal'), /^[A-Za-z0-9_-]{43}$/)
})

test('网关与引擎拿到同一个 INTERNAL_TOKEN / JWT_SECRET', () => {
  const gatewayEnv = envOfGateway()
  const engineEnv = stack.buildEngineEnv({ pgDsn: 'postgresql://x' }, { appSecret: VALID_SECRET, port: 9200, env: {} })

  assert.equal(gatewayEnv.INTERNAL_TOKEN, engineEnv.INTERNAL_TOKEN)
  assert.equal(gatewayEnv.JWT_SECRET, engineEnv.JWT_SECRET)
  assert.equal(gatewayEnv.INTERNAL_TOKEN, 'BzcOjSRtC43_-5jEb9X-OwD3Degb-CMKy8RwVEyQ9ok')
  assert.equal(engineEnv.JWT_SECRET, 'SYvVXpKr8n5cZ_vf3u_0XzzekZyu1aXr-v225-TQbTg')
})

test('继承来的同名变量不能把两侧推开：本地模式的密钥只由 APP_SECRET 决定', () => {
  // 开发机上 shell / .env 加载器留下的旧值必须被覆盖，否则又是一次"各信各的"
  const poisoned = { JWT_SECRET: 'stale-jwt-from-shell', INTERNAL_TOKEN: 'stale-internal-from-shell' }

  const gatewayEnv = stack.buildGatewayEnv(
    { pgDsn: 'postgresql://x' },
    { appSecret: VALID_SECRET, port: 9100, env: { ...poisoned } },
  )
  const engineEnv = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x' },
    { appSecret: VALID_SECRET, port: 9200, env: { ...poisoned } },
  )

  assert.notEqual(gatewayEnv.INTERNAL_TOKEN, poisoned.INTERNAL_TOKEN)
  assert.equal(gatewayEnv.INTERNAL_TOKEN, engineEnv.INTERNAL_TOKEN)
  assert.equal(gatewayEnv.JWT_SECRET, engineEnv.JWT_SECRET)
})

test('换一个 APP_SECRET，两个子进程的密钥一起变（不留旧值）', () => {
  const other = 'y'.repeat(48)
  const gatewayEnv = stack.buildGatewayEnv({ pgDsn: 'postgresql://x' }, { appSecret: other, port: 9100, env: {} })
  const engineEnv = stack.buildEngineEnv({ pgDsn: 'postgresql://x' }, { appSecret: other, port: 9200, env: {} })

  assert.equal(gatewayEnv.INTERNAL_TOKEN, stack.deriveSubsecret(other, 'chiron-internal'))
  assert.equal(gatewayEnv.INTERNAL_TOKEN, engineEnv.INTERNAL_TOKEN)
  assert.notEqual(engineEnv.JWT_SECRET, 'SYvVXpKr8n5cZ_vf3u_0XzzekZyu1aXr-v225-TQbTg')
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

test('引擎起不来时 stderr 尾部拼进错误：那是唯一能区分缺依赖与配置错的证据', async t => {
  const launcherChild = fakeChild()
  const engineChild = fakeChild()
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  withSpawnQueue(t, [launcherChild, engineChild, fakeChild()])
  mockFetch(t, async () => ({ ok: false }))

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: 9410,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', cwd: '/' },
    enginePort: 9411,
    intervalMs: 1,
    timeoutMs: 200,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )
  // 等引擎真的被 spawn 出来再喂退出：早一步 emit 时监听器还没挂上，
  // 那条 exit 会落到无人监听的子进程上，用例就变成"超时失败"而不是"退出失败"。
  for (let i = 0; i < 20 && !stack.getStackState().engine; i += 1) await tick()
  engineChild.stderr.write('ModuleNotFoundError: No module named "pymupdf"\n')
  await tick()
  engineChild.exitCode = 1
  engineChild.emit('exit', 1)

  await assert.rejects(promise, err => {
    assert.match(err.message, /python-engine 未能启动/)
    // "引擎未能启动"这种消息用户没法照着修；缺哪个模块必须直接出现在消息里
    assert.match(err.message, /pymupdf/, '错误消息要带 stderr 尾部')
    assert.equal(err.reason, 'exited')
    return true
  })
  // 失败结论落在状态里（这个字段以前是死的）：组件管理页与日志才有的可展示
  assert.equal(stack.getStackState().engineFailure.reason, 'exited')
})

// ---------------------------------------------------- 引擎回调网关的地址（GATEWAY_INTERNAL_URL）

test('buildEngineEnv 给定网关端口时写下真实回调地址，而不是让引擎用默认的 8080', () => {
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x', redisAddr: '' },
    { appSecret: VALID_SECRET, port: 9200, gatewayPort: 9100, env: {} },
  )

  assert.equal(env.GATEWAY_INTERNAL_URL, 'http://127.0.0.1:9100')
})

test('buildEngineEnv 不知道网关端口时不写这一项（写了就是替引擎编一个地址）', () => {
  const env = stack.buildEngineEnv(
    { pgDsn: 'postgresql://x', redisAddr: '' },
    { appSecret: VALID_SECRET, port: 9200, env: {} },
  )

  assert.equal(env.GATEWAY_INTERNAL_URL, undefined)
})

test('startLocalStack 把网关端口预先告诉引擎：引擎先起，URL 只能 spawn 时给全', async t => {
  const launcherChild = fakeChild()
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  const calls = withSpawnQueue(t, [launcherChild, fakeChild(), fakeChild()])
  mockFetch(t, async url => gatewayFetch(url))

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: 9460,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', args: ['-m', 'app.main'], cwd: '/' },
    enginePort: 9461,
    intervalMs: 1,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )
  await promise

  // calls[1] = 引擎，calls[2] = 网关：两边必须落在同一个端口上
  assert.equal(calls[1].options.env.GATEWAY_INTERNAL_URL, 'http://127.0.0.1:9460')
  assert.equal(calls[2].options.env.PORT, '9460')
})

test('startLocalStack 在没给定端口时先挑一个，并同时告诉引擎和网关', async t => {
  const launcherChild = fakeChild()
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  const original = components.pickFreePort
  components.pickFreePort = async () => 9777
  t.after(() => {
    components.pickFreePort = original
  })
  const calls = withSpawnQueue(t, [launcherChild, fakeChild(), fakeChild()])
  mockFetch(t, async url => gatewayFetch(url))

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', args: ['-m', 'app.main'], cwd: '/' },
    enginePort: 9778,
    intervalMs: 1,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )
  await promise

  assert.equal(calls[1].options.env.GATEWAY_INTERNAL_URL, 'http://127.0.0.1:9777')
  assert.equal(calls[2].options.env.PORT, '9777')
})

test('网关没能落在预定端口就失败：引擎已经按前者配置，将就是静默半残', async t => {
  const launcherChild = fakeChild()
  const gatewayFirst = fakeChild()
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  const original = components.pickFreePort
  components.pickFreePort = async () => 9555
  t.after(() => {
    components.pickFreePort = original
  })
  const calls = withSpawnQueue(t, [launcherChild, fakeChild(), gatewayFirst, fakeChild()])
  // 预定的 9500 怎么都不就绪（像被别的进程占着），换到 9555 才通
  mockFetch(t, async url => (String(url).includes('127.0.0.1:9500/') ? { ok: false } : gatewayFetch(url)))

  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: 9500,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', args: ['-m', 'app.main'], cwd: '/' },
    enginePort: 9501,
    intervalMs: 1,
    timeoutMs: 40,
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )

  for (let i = 0; i < 200 && calls.length < 3; i += 1) await tick()
  gatewayFirst.stderr.write('2026/11/01 listen tcp 127.0.0.1:9500: bind: address in use\n')

  await assert.rejects(promise, err => {
    assert.match(err.message, /没能落在预定端口 9500/)
    assert.match(err.message, /9555/)
    assert.equal(err.reason, 'gateway-port-drift')
    return true
  })
})

// ------------------------------------------------------------------ 引擎运行中退出
//
// 网关对引擎是**按请求解析地址**、不探活也不熔断：引擎崩了之后网关还是照常应答，
// 用户看到的只是"发消息就报服务暂时不可用"。真正的原因只有子进程的退出码知道，
// 所以这条通知通道必须有；同时主动关停不能报故障，否则每次正常退出应用都会弹崩溃窗。

/** 起一个"引导器 + 引擎 + 网关都就绪"的假栈，返回引擎子进程与退出通知记录 */
async function startAndReady(t, { portBase = 9420 } = {}) {
  const launcherChild = fakeChild()
  const engineChild = fakeChild()
  const gatewayChild = fakeChild()
  // 真实引导器收到 stdin EOF 后自行退出；回滚时靠这条不等满超时
  launcherChild.stdin.on('finish', () => {
    launcherChild.exitCode = 0
    launcherChild.emit('exit', 0)
  })
  withSpawnQueue(t, [launcherChild, engineChild, gatewayChild])
  mockFetch(t, async url => gatewayFetch(url))

  const notices = []
  const promise = stack.startLocalStack({
    launcher: { command: 'py', cwd: '/' },
    exe: 'gateway.exe',
    port: portBase,
    appSecret: VALID_SECRET,
    engine: { command: 'py-engine', args: ['-m', 'app.main'], cwd: '/' },
    enginePort: portBase + 1,
    intervalMs: 1,
    onEngineExit: info => notices.push(info),
  })
  launcherChild.stdout.write(
    JSON.stringify({ event: 'launcher-ready', pg_dsn: 'postgresql://u@127.0.0.1:1/c', redis_addr: '127.0.0.1:2' }) + '\n',
  )
  const result = await promise
  return { engineChild, notices, result }
}

test('引擎就绪后自己退出 ⇒ 立刻通知，带退出码与 stderr 尾部', async t => {
  const { engineChild, notices } = await startAndReady(t)

  engineChild.stderr.write('RuntimeError: boom\n')
  await tick()
  engineChild.emit('exit', 3)

  assert.equal(notices.length, 1, '引擎崩了必须有一次通知')
  assert.equal(notices[0].code, 3)
  assert.match(notices[0].stderrTail, /boom/)
  assert.equal(notices[0].address, '127.0.0.1:9421')
  assert.ok(notices[0].at, '要带时间戳，日志里才排得出顺序')

  // 只通知一次：反复崩不该弹一串窗
  engineChild.emit('exit', 4)
  assert.equal(notices.length, 1, '退出通知只能有一条')
})

test('信号杀死引擎也要报：退出码为 null 时不能当成正常退出', async t => {
  const { engineChild, notices } = await startAndReady(t, { portBase: 9430 })

  engineChild.emit('exit', null, 'SIGKILL')

  assert.equal(notices.length, 1)
  assert.equal(notices[0].code, null)
  assert.equal(notices[0].signal, 'SIGKILL', 'OOM 杀手只有信号可查，丢了就没法定因')
})

test('主动关停引擎不通知（正常退出应用不该弹"引擎已停止"）', async t => {
  const { engineChild, notices } = await startAndReady(t, { portBase: 9440 })

  await stack.stopEngine()
  engineChild.stderr.write('shutting down\n')
  await tick()

  assert.equal(notices.length, 0, '我们让它退的，不算故障')
})



