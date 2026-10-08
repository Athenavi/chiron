#!/usr/bin/env node
/**
 * transcript 滚动**单写者**守卫（契约：docs/transcript-contract.md §1.1）。
 *
 * 契约原文：分页 prepend、流式追加、折叠、跳转**都只提交"意图"**，由 `transcriptViewport`
 * 写滚动位置 —— **"别处直接写 `scrollTop` 一律视为缺陷"**。
 *
 * **为什么需要机械门禁**：这条契约此前**已经被破坏过 3 处**（`ChatView.vue` 里直接写
 * `.message-list` 的 `scrollTop`），而没有任何检查能发现 —— 破坏方式很隐蔽：它绕过的不是
 * 渲染，而是 `transcriptViewport` 的**输入租约**（用户正读历史时被抢走位置）与
 * **writer provenance**（程序写入没登记为 pending，下一个 scroll 事件会被误判成用户滚动）。
 * 靠"看代码"发现不了，只能靠**枚举**。
 *
 * 口径（与仓库既有清单式守卫同构）：枚举 `src/**` 下所有 `scrollTop` **赋值**，逐条归类：
 *   - `SANCTIONED`     —— 单写者本人（`transcriptViewport.ts`）；
 *   - `NOT_TRANSCRIPT` —— 写的是**别的**滚动容器（自带理由）；
 *   - `EXEMPT`         —— 测试等不参与运行时；
 * 未归类即失败。**注释里的字样先剥掉**，避免把说明文字判成代码。
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, posix } from 'node:path'

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')
const SRC = join(ROOT, 'src')

/** 单写者：契约里唯一允许写**消息列表** scrollTop 的模块。 */
const SANCTIONED = {
  'components/chat/transcriptViewport.ts': '契约 §1.1 的唯一写者',
}

/** 写的是别的滚动容器（不是 transcript）—— 每条都必须写明是哪个容器。 */
const NOT_TRANSCRIPT = {
  'components/AgentCollabPanel.vue': 'Agent 协作日志自己的 logContainer',
  'components/chat/ReasoningBlock.vue': '思考块自己的滚动框',
  'composables/useVirtualList.ts': '通用虚拟列表（非消息列表）',
  'views/ChatView.vue':
    '统一任务模式的 .unified-list（**第二条渲染路径**，不经 transcriptViewport；' +
    '属已知问题，见 docs/reasonix-ui-ux-gap-analysis.md §4.1，修法是把该模式也复用 MessageList）',
}

const EXEMPT = {
  '__tests__': '测试文件',
}

const ASSIGN = /\.scrollTop\s*=(?!=)/

function walk(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) out.push(...walk(full))
    else if (/\.(ts|vue|mts)$/.test(name)) out.push(full)
  }
  return out
}

/** 去掉行注释与块注释（只用于**判定**，不改变源码）。 */
function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

const findings = []
for (const file of walk(SRC)) {
  const rel = posix.join(...relative(SRC, file).split(/[\\/]/))
  const code = stripComments(readFileSync(file, 'utf8'))
  if (!ASSIGN.test(code)) continue
  findings.push(rel)
}

const classified = { ...SANCTIONED, ...NOT_TRANSCRIPT }
const isExempt = rel => Object.keys(EXEMPT).some(k => rel.includes(k))
const unclassified = findings.filter(rel => !(rel in classified) && !isExempt(rel))

if (unclassified.length) {
  console.error('FAIL: 以下文件直接写 scrollTop，但未归类（契约 §1.1：别处直写一律视为缺陷）：')
  for (const rel of unclassified) console.error('  ' + rel)
  console.error(
    '\n要么改成经 transcriptViewport 提交"意图"，要么在 scripts/check-transcript-scroll.mjs ' +
      '的 NOT_TRANSCRIPT 里写明"写的是哪个别的容器"。'
  )
  process.exit(1)
}

const stale = Object.keys(classified).filter(rel => !findings.includes(rel))
if (stale.length) {
  console.error('FAIL: 下列文件已归类但已不再写 scrollTop，请清理清单：' + stale.join(', '))
  process.exit(1)
}

console.log(
  `transcript 单写者守卫通过（${findings.length} 处 scrollTop 赋值，全部已归类：` +
    `单写者 ${Object.keys(SANCTIONED).length} · 非 transcript ${Object.keys(NOT_TRANSCRIPT).length}）`
)
