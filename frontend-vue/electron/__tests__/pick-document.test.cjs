'use strict'
/**
 * `pickDocument`（契约 §2）的结果翻译。
 *
 * 为什么要单独测：`main.cjs` 一 require 就会起 Electron，`dialog.showOpenDialog`
 * 的返回没法在单测里真跑。所以把"对话框结果 → 契约返回值"抽成 `shared.pickDocumentResult`，
 * 在这里钉住三条约定：取消是 `null` 不是错误、多选只取第一个、只回路径与文件名不读内容。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')

const { pickDocumentResult } = require('../shared.cjs')

test('正常选中一个文件：返回 path 与 basename，不读内容', () => {
  const file = path.join(path.sep, 'tmp', 'docs', 'report.pdf')
  const picked = pickDocumentResult({ canceled: false, filePaths: [file] })
  assert.deepEqual(picked, { path: file, name: 'report.pdf' })
  // 契约只允许这两个键：多带一个字段就等于把文件信息漏给渲染进程
  assert.deepEqual(Object.keys(picked).sort(), ['name', 'path'])
})

test('用户取消：返回 null（不是错误，调用方按"没选"处理）', () => {
  assert.equal(pickDocumentResult({ canceled: true, filePaths: [] }), null)
})

test('取消时即使带着路径也返回 null（以 canceled 为准）', () => {
  assert.equal(pickDocumentResult({ canceled: true, filePaths: ['/tmp/a.md'] }), null)
})

test('多选只取第一个：契约返回类型是单个对象', () => {
  const picked = pickDocumentResult({ canceled: false, filePaths: ['/tmp/a.md', '/tmp/b.md'] })
  assert.deepEqual(picked, { path: '/tmp/a.md', name: 'a.md' })
})

test('空 filePaths / 缺字段 / 空结果都不伪装成"选中了"', () => {
  assert.equal(pickDocumentResult({ canceled: false, filePaths: [] }), null)
  assert.equal(pickDocumentResult({ canceled: false }), null)
  assert.equal(pickDocumentResult({ canceled: false, filePaths: [''] }), null)
  assert.equal(pickDocumentResult(null), null)
  assert.equal(pickDocumentResult(undefined), null)
})

test('名字取 basename：不把用户目录结构带回渲染进程', () => {
  const picked = pickDocumentResult({
    canceled: false,
    filePaths: [path.join('C:', 'Users', 'someone', 'private', 'notes.txt')],
  })
  assert.equal(picked.name, 'notes.txt')
  assert.ok(!picked.name.includes(path.sep))
})

test('不加扩展名过滤：任意后缀都照原样返回', () => {
  for (const name of ['a.md', 'b.pdf', 'c.docx', 'noext', 'd.tar.gz']) {
    const file = path.join(path.sep, 'tmp', name)
    assert.deepEqual(pickDocumentResult({ canceled: false, filePaths: [file] }), {
      path: file,
      name,
    })
  }
})
