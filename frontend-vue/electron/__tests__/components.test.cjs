'use strict'
/**
 * components.cjs 纯 Node 单测：mock app.getPath 到临时目录。
 * 重点：manifest 缺失/损坏一律视为未安装；install/remove 明确报“需 P3 组件源”；
 * 未找到 core 二进制与 desktop-local 脚本时给可读错误。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const components = require('../components.cjs')

function setup(t) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-components-'))
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-repo-'))
  components._setDeps({ app: { getPath: () => userData } })
  t.after(() => {
    components._resetDeps()
    fs.rmSync(userData, { recursive: true, force: true })
    fs.rmSync(repo, { recursive: true, force: true })
  })
  return { userData, repo, root: path.join(userData, 'components') }
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
  // 32 + 8 字节的附属文件，再加 manifest.json 本身
  assert.ok(pageindex.sizeBytes > 40, `sizeBytes 应包含附属文件，实际 ${pageindex.sizeBytes}`)
  assert.equal(pageindex.running, false)

  const core = list.find(item => item.name === 'core')
  assert.equal(core.installed, false)
})

test('installComponent：本地没有 manifest 时报“需要 P3 组件源”，不创建目录', async t => {
  const { root } = setup(t)
  await assert.rejects(() => components.installComponent('pageindex'), /P3 组件源/)
  assert.equal(fs.existsSync(path.join(root, 'pageindex')), false)
})

test('installComponent：目录在但缺 manifest 时报错里点明 manifest', async t => {
  const { root } = setup(t)
  writeComponent(root, 'langextract', undefined)
  await assert.rejects(() => components.installComponent('langextract'), /manifest\.json/)
})

test('installComponent：已有合法 manifest 时返回现状，不下载', async t => {
  const { root } = setup(t)
  writeComponent(root, 'pageindex', { name: 'pageindex', version: '0.9.0' })
  const result = await components.installComponent('pageindex')
  assert.deepEqual(result, {
    name: 'pageindex',
    version: '0.9.0',
    installed: true,
    requiresSource: false,
  })
})

test('removeComponent：未安装与已安装都给出明确说明，且不删目录', async t => {
  const { root } = setup(t)
  await assert.rejects(() => components.removeComponent('core'), /未安装/)

  const dir = writeComponent(root, 'pageindex', { name: 'pageindex', version: '1.0.0' })
  await assert.rejects(() => components.removeComponent('pageindex'), /P3 组件源/)
  assert.equal(fs.existsSync(dir), true, '占位实现不得删除用户目录')
})

test('未知组件名一律拒绝（防路径穿越）', async t => {
  setup(t)
  await assert.rejects(() => components.startComponent('../etc'), /未知组件/)
  await assert.rejects(() => components.installComponent('../../evil'), /未知组件/)
  await assert.rejects(() => components.describeComponent('core/../..'), /未知组件/)
})

test('startComponent：pageindex 未安装报错，已安装报 P3 占位', async t => {
  const { root } = setup(t)
  await assert.rejects(() => components.startComponent('pageindex'), /未安装/)

  writeComponent(root, 'pageindex', { name: 'pageindex', version: '1.0.0' })
  await assert.rejects(() => components.startComponent('pageindex'), /P3/)
  await assert.rejects(() => components.stopComponent('pageindex'), /P3/)
})

test('startCore：既无 core 二进制也无 desktop-local 时给可读错误', async t => {
  const { repo } = setup(t)
  await assert.rejects(() => components.startCore({ repoRoot: repo }), /未找到本地 core/)
  assert.equal(components.getCoreState().running, false)
})

test('stopCore：没有在跑的进程时返回 false', async t => {
  setup(t)
  assert.equal(await components.stopCore(), false)
  assert.equal(components.isCoreRunning(), false)
})
