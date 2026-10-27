'use strict'
/**
 * components.cjs 纯 Node 单测（mock app.getPath + spawn + fetch，不启动真实进程）。
 *
 * 重点：
 *   - core 的启停行为不变（假 spawn + 假 fetch，走真实的 startCore/stopCore 代码路径）；
 *   - pageindex / langextract 的 list/install/start/stop/remove 都打到本地 core 的
 *     `/v1/components*`，方法/路径正确，core 的错误码被翻成人话；
 *   - core 没在跑时回退到磁盘探测并标 `error`，install 直接抛"本地 core 未运行"，
 *     绝不静默成功；
 *   - 未知组件名一律拒绝（防路径穿越）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')

const components = require('../components.cjs')

// --------------------------------------------------------------------- 夹具
function jsonResponse(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data }
}

/**
 * 一个"假 core"：spawn 返回可控的 child，fetch 记录请求并交给 handler 应答。
 * 用真实 startCore 起它，这样 state.core 与代码路径都是真的。
 */
function setup(t) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-components-'))
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-repo-'))
  fs.mkdirSync(path.join(repo, 'desktop-local', 'app'), { recursive: true })
  fs.writeFileSync(path.join(repo, 'desktop-local', 'app', 'main.py'), '# fake core\n')

  const requests = []
  const child = new EventEmitter()
  child.pid = 4242
  child.stdout = new EventEmitter()
  child.stderr = new EventEmitter()
  child.killed = false
  child.kill = () => {
    child.killed = true
    child.emit('exit', 0)
    return true
  }

  let handler = () => jsonResponse({})
  components._setDeps({
    app: { getPath: () => userData },
    spawn: () => child,
    fetch: async (url, options = {}) => {
      requests.push({ url: String(url), method: options.method || 'GET' })
      return handler(String(url), options)
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

  return {
    userData,
    repo,
    root: path.join(userData, 'components'),
    requests,
    /** 换一个 core 应答函数（默认 `{}`，足够让 /healthz 判活） */
    respond(next) {
      handler = next
    },
    /** 起假 core，返回 startCore 的结果 */
    async startCore() {
      return components.startCore({ repoRoot: repo })
    },
  }
}

function writeComponent(root, name, manifest, files = {}) {
  const dir = path.join(root, name)
  fs.mkdirSync(dir, { recursive: true })
  if (manifest !== undefined) {
    const body = typeof manifest === 'string' ? manifest : JSON.stringify(manifest)
    fs.writeFileSync(path.join(dir, 'manifest.json'), body, 'utf8')
  }
  for (const [file, content] of Object.entries(files)) {
    const full = path.join(dir, file)
    fs.mkdirSync(path.dirname(full), { recursive: true })
    fs.writeFileSync(full, content)
  }
  return dir
}

// --------------------------------------------------------------------- 磁盘探测
test('组件目录为空：三项都在，全部未安装、未运行、体积 0', async t => {
  setup(t)
  const list = await components.listComponents()

  assert.deepEqual(list.map(item => item.name), ['core', 'pageindex', 'langextract'])
  for (const item of list) {
    assert.equal(item.installed, false)
    assert.equal(item.running, false)
    assert.equal(item.sizeBytes, 0)
    assert.equal(item.version, '')
  }
  // core 没在跑：非 core 项明确标出来源不可用，而不是假装"没装"
  for (const item of list.filter(entry => entry.name !== 'core')) {
    assert.equal(item.error, 'core-not-running')
  }
})

test('目录存在但缺 manifest.json：仍算未安装', async t => {
  const { root } = setup(t)
  writeComponent(root, 'pageindex', undefined, { 'index.py': 'x'.repeat(16) })

  const info = await components.describeComponent('pageindex')
  assert.equal(info.installed, false)
  assert.equal(info.version, '')
  assert.ok(info.sizeBytes > 0, '体积按目录内容统计，不受 manifest 影响')
})

test('manifest.json 是坏 JSON / 非对象：视为未安装', async t => {
  const { root } = setup(t)
  writeComponent(root, 'langextract', '{ not json')
  assert.equal((await components.describeComponent('langextract')).installed, false)

  writeComponent(root, 'core', '[]')
  assert.equal((await components.describeComponent('core')).installed, false)
})

test('manifest 合法：installed=true，version 与 sizeBytes 来自实际目录', async t => {
  const { root } = setup(t)
  writeComponent(root, 'pageindex', { name: 'pageindex', version: '1.2.3' }, {
    'bin/run.js': 'y'.repeat(32),
    'docs/readme.md': 'z'.repeat(8),
  })

  const list = await components.listComponents()
  const pageindex = list.find(item => item.name === 'pageindex')
  assert.equal(pageindex.installed, true)
  assert.equal(pageindex.version, '1.2.3')
  assert.ok(pageindex.sizeBytes > 40, `sizeBytes 应包含附属文件，实际 ${pageindex.sizeBytes}`)
  assert.equal(pageindex.running, false)

  const core = list.find(item => item.name === 'core')
  assert.equal(core.installed, false)
})

// --------------------------------------------------------------------- core 请求
test('listComponents：core 在跑时以 /v1/components 的返回为准', async t => {
  const harness = setup(t)
  harness.respond(url => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (url.endsWith('/v1/components')) {
      return jsonResponse({
        data: [
          { name: 'pageindex', version: '2.0.0', installed: true, running: true, sizeBytes: 4096, pid: 777, baseUrl: 'http://127.0.0.1:5100' },
          { name: 'langextract', version: '', installed: false, running: false, sizeBytes: 0 },
        ],
        source: { configured: true, kind: 'dir', location: '/opt/components', available: true },
      })
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  const list = await components.listComponents()
  const pageindex = list.find(item => item.name === 'pageindex')
  assert.equal(pageindex.installed, true)
  assert.equal(pageindex.version, '2.0.0')
  assert.equal(pageindex.running, true)
  assert.equal(pageindex.pid, 777)
  assert.equal(pageindex.baseUrl, 'http://127.0.0.1:5100')
  assert.equal(pageindex.sizeBytes, 4096)

  const langextract = list.find(item => item.name === 'langextract')
  assert.equal(langextract.installed, false)
  assert.equal(langextract.error, '')
})

test('listComponents：core 的接口报错时回退磁盘探测并保留错误信息', async t => {
  const harness = setup(t)
  harness.respond(url => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    return jsonResponse({ error: 'boom', message: 'core 内部错误' }, 500)
  })
  await harness.startCore()
  writeComponent(harness.root, 'pageindex', { name: 'pageindex', version: '0.1.0' })

  const list = await components.listComponents()
  const pageindex = list.find(item => item.name === 'pageindex')
  assert.equal(pageindex.installed, true, '回退到磁盘探测')
  assert.equal(pageindex.version, '0.1.0')
  assert.match(pageindex.error, /core 内部错误/)
})

test('installComponent：core 未运行时报"本地 core 未运行"', async t => {
  setup(t)
  await assert.rejects(() => components.installComponent('pageindex'), /本地 core 未运行/)
})

test('installComponent：未配置组件源时把 409 翻成可读错误', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (url.endsWith('/v1/components/pageindex/install') && options.method === 'POST') {
      return jsonResponse(
        { error: 'component_source_unconfigured', message: '未配置组件源' },
        409,
      )
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  await assert.rejects(() => components.installComponent('pageindex'), /未配置组件源/)
})

test('installComponent：成功后返回版本与目录，请求打到 /v1/components/{name}/install', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (url.endsWith('/v1/components/pageindex/install') && options.method === 'POST') {
      return jsonResponse(
        {
          data: {
            name: 'pageindex',
            version: '1.4.0',
            installed: true,
            dir: '/data/components/pageindex/1.4.0',
            entry: 'app.py',
            source: '/opt/components',
          },
        },
        201,
      )
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  const result = await components.installComponent('pageindex')
  assert.equal(result.name, 'pageindex')
  assert.equal(result.version, '1.4.0')
  assert.equal(result.installed, true)
  assert.equal(result.requiresSource, false)
  assert.equal(result.entry, 'app.py')
  assert.ok(
    harness.requests.some(req => req.method === 'POST' && req.url.endsWith('/v1/components/pageindex/install')),
    '应调用 core 的安装端点',
  )
})

test('startComponent：pageindex 返回 {pid, baseUrl}，请求打到 start 端点', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (url.endsWith('/v1/components/pageindex/start') && options.method === 'POST') {
      return jsonResponse({
        data: { name: 'pageindex', version: '1.0.0', running: true, pid: 1234, baseUrl: 'http://127.0.0.1:5200', port: 5200 },
      })
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  const started = await components.startComponent('pageindex')
  assert.equal(started.running, true)
  assert.equal(started.pid, 1234)
  assert.equal(started.baseUrl, 'http://127.0.0.1:5200')
  assert.equal(started.version, '1.0.0')
  assert.ok(harness.requests.some(req => req.method === 'POST' && req.url.endsWith('/v1/components/pageindex/start')))
})

test('startComponent：未安装时把 core 的 404 翻成可读错误', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (options.method === 'POST' && url.includes('/start')) {
      return jsonResponse({ error: 'component_not_installed', message: '组件 pageindex 未安装' }, 404)
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  await assert.rejects(() => components.startComponent('langextract'), /组件未安装/)
})

test('stopComponent：返回 stopped，未运行时不报错', async t => {
  const harness = setup(t)
  let stopped = true
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (options.method === 'POST' && url.endsWith('/v1/components/pageindex/stop')) {
      return jsonResponse({ data: { name: 'pageindex', running: false, stopped, pid: 1234 } })
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  const first = await components.stopComponent('pageindex')
  assert.equal(first.stopped, true)
  assert.equal(first.pid, 1234)

  stopped = false
  const second = await components.stopComponent('pageindex')
  assert.equal(second.stopped, false, '幂等：没在跑就返回 stopped=false')
})

test('removeComponent：调用 DELETE /v1/components/{name}', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (options.method === 'DELETE' && url.endsWith('/v1/components/langextract')) {
      return jsonResponse({ data: { name: 'langextract', removed: true, dir: '/data/components/langextract' } })
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  const result = await components.removeComponent('langextract')
  assert.equal(result.removed, true)
  assert.ok(harness.requests.some(req => req.method === 'DELETE' && req.url.endsWith('/v1/components/langextract')))
})

test('removeComponent：正在运行时把 409 翻成可读错误', async t => {
  const harness = setup(t)
  harness.respond((url, options) => {
    if (url.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (options.method === 'DELETE') {
      return jsonResponse({ error: 'component_running', message: '组件正在运行' }, 409)
    }
    return jsonResponse({}, 404)
  })
  await harness.startCore()

  await assert.rejects(() => components.removeComponent('pageindex'), /正在运行/)
})

test('core 自身的安装/卸载不走组件源，给出明确说明', async t => {
  setup(t)
  await assert.rejects(() => components.installComponent('core'), /由桌面端自身提供/)
  await assert.rejects(() => components.removeComponent('core'), /随桌面端发布/)
})

// --------------------------------------------------------------------- core 启停（行为不变）
test('startCore：既无 core 二进制也无 desktop-local 时给可读错误', async t => {
  const { repo } = setup(t)
  fs.rmSync(path.join(repo, 'desktop-local'), { recursive: true, force: true })
  await assert.rejects(() => components.startCore({ repoRoot: repo }), /未找到本地 core/)
  assert.equal(components.getCoreState().running, false)
})

test('startCore + stopCore：假 core 能起能停，startComponent(core) 复用同一路径', async t => {
  const harness = setup(t)
  const started = await components.startComponent('core', { repoRoot: harness.repo })
  assert.equal(started.running, true)
  assert.match(started.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/)
  assert.equal(components.isCoreRunning(), true)

  const stopped = await components.stopComponent('core')
  assert.equal(stopped.stopped, true)
  assert.equal(components.isCoreRunning(), false)
})

test('stopCore：没有在跑的进程时返回 false', async t => {
  setup(t)
  assert.equal(await components.stopCore(), false)
  assert.equal(components.isCoreRunning(), false)
})

// --------------------------------------------------------------------- 错误翻译 / 入参
test('describeCoreError：已知错误码给人话，未知码保留 core 的 message', () => {
  assert.match(
    components.describeCoreError(409, { error: 'component_source_unconfigured', message: '未配置组件源' }),
    /CHIRON_COMPONENT_BASE_URL/,
  )
  assert.match(
    components.describeCoreError(400, { error: 'component_integrity_failed', message: 'sha256 不匹配' }),
    /sha256/,
  )
  assert.equal(components.describeCoreError(500, { error: 'weird', message: '内部错误' }), '内部错误')
  assert.equal(components.describeCoreError(503, null), '本地 core 返回 HTTP 503')
})

test('未知组件名一律拒绝（防路径穿越）', async t => {
  setup(t)
  await assert.rejects(() => components.startComponent('../etc'), /未知组件/)
  await assert.rejects(() => components.installComponent('../../evil'), /未知组件/)
  await assert.rejects(() => components.describeComponent('core/../..'), /未知组件/)
})
