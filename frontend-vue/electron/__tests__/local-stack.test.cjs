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
