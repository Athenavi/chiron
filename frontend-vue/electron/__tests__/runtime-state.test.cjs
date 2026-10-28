'use strict'
/**
 * runtime-state.cjs：状态文件读写，以及"能否接管已有实例"的判定。
 *
 * 这些用例的重点都在**失败模式**上：真实故障几乎都出在"文件还在但进程已死"
 * 和"端口上有东西但不是我们的服务"，而不是正常路径。
 */

const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const rs = require('../runtime-state.cjs')

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-rs-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

// ------------------------------------------------------------------ 读写

test('写入后能原样读回，且带上 schema 与 pid', t => {
  const dir = tempDir(t)
  const written = rs.writeRuntimeState(dir, { gatewayPort: 18900, enginePort: 18901 })

  assert.equal(written.schema, rs.STATE_SCHEMA)
  assert.equal(written.pid, process.pid)

  const read = rs.readRuntimeState(dir)
  assert.equal(read.gatewayPort, 18900)
  assert.equal(read.enginePort, 18901)
})

test('没有状态文件时返回 null，不抛', t => {
  const dir = tempDir(t)
  assert.equal(rs.readRuntimeState(dir), null)
})

test('JSON 损坏时返回 null：坏文件不该让本地模式起不来', t => {
  const dir = tempDir(t)
  fs.writeFileSync(rs.statePathFor(dir), '{ 这不是 json', 'utf8')
  assert.equal(rs.readRuntimeState(dir), null)
})

test('schema 版本不匹配时返回 null：不猜旧格式的字段含义', t => {
  const dir = tempDir(t)
  fs.writeFileSync(rs.statePathFor(dir), JSON.stringify({ schema: 999, gatewayPort: 1 }), 'utf8')
  assert.equal(rs.readRuntimeState(dir), null)
})

test('写入是原子的：不留 .tmp 残file', t => {
  const dir = tempDir(t)
  rs.writeRuntimeState(dir, { gatewayPort: 1 })
  const leftovers = fs.readdirSync(dir).filter(name => name.endsWith('.tmp'))
  assert.deepEqual(leftovers, [])
})

test('clear 删掉文件；对不存在的文件也不抛', t => {
  const dir = tempDir(t)
  rs.writeRuntimeState(dir, { gatewayPort: 1 })
  assert.equal(rs.clearRuntimeState(dir), true)
  assert.equal(rs.readRuntimeState(dir), null)
  assert.equal(rs.clearRuntimeState(dir), true)
})

test('dataDir 为空时不写任何东西', () => {
  assert.equal(rs.writeRuntimeState('', { gatewayPort: 1 }), null)
  assert.equal(rs.readRuntimeState(''), null)
})

// ------------------------------------------------------------------ 判活

test('没有状态时判定为不可接管', async () => {
  const result = await rs.probeExistingStack(null)
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'no-state')
})

test('状态里没有网关端口时不可接管', async () => {
  const result = await rs.probeExistingStack({ schema: rs.STATE_SCHEMA })
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'no-gateway-port')
})

test('网关 /ready 说 PG 与 Redis 都 up 才算活着', async () => {
  const result = await rs.probeExistingStack(
    { schema: rs.STATE_SCHEMA, gatewayPort: 18900 },
    {
      fetch: async () => ({
        ok: true,
        json: async () => ({ success: true, data: { postgres: 'up', redis: 'up' } }),
      }),
    },
  )
  assert.equal(result.alive, true)
  assert.equal(result.port, 18900)
})

test('端口上有东西应答、但依赖没起来 → 不可接管', async () => {
  // 这是最危险的一种误判：端口被别的程序占着，它恰好返回 200
  const result = await rs.probeExistingStack(
    { schema: rs.STATE_SCHEMA, gatewayPort: 18900 },
    { fetch: async () => ({ ok: true, json: async () => ({ data: { postgres: 'down', redis: 'up' } }) }) },
  )
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'dependencies-down')
})

test('/ready 不 ok（比如 503）→ 不可接管', async () => {
  const result = await rs.probeExistingStack(
    { schema: rs.STATE_SCHEMA, gatewayPort: 18900 },
    { fetch: async () => ({ ok: false, json: async () => ({}) }) },
  )
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'gateway-unhealthy')
})

test('连不上（进程已被强杀）→ 不可接管，且不抛', async () => {
  const result = await rs.probeExistingStack(
    { schema: rs.STATE_SCHEMA, gatewayPort: 18900 },
    {
      fetch: async () => {
        throw new Error('ECONNREFUSED')
      },
    },
  )
  assert.equal(result.alive, false)
  assert.equal(result.reason, 'unreachable')
})
