#!/usr/bin/env node
/**
 * z-index token 契约检查。
 *
 * 背景：`src/style.css` 的 `:root` 已定义 `--z-*` 层级 token，但历史上 z-index 一直是裸
 * 数字 —— 15 个不同取值散落在 18 个文件里，含 `9999` 这种"冲突了就拍更大的数字"的产物，
 * 层叠关系没人能说清。
 *
 * 本脚本用 ratchet（棘轮）约束：存量记录在 BASELINE 里予以容忍，**任何新增裸数字即失败**。
 * 把裸数字换成 `var(--z-*)` 后可以下调对应数字来锁定成果（无法上调，脚本会拒绝）。
 *
 * 用法：node scripts/check-z-index-tokens.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/** 存量基线：文件 → 允许的裸 z-index 数量。只允许下调。
 *
 * 已清零：全部裸 z-index 换成 src/style.css 的语义 token（含保持 HomeView 的
 * hero>grid、OAuthProviders 固定列表头>表体、admin/Layout 的 drawer>mask>sider
 * 这些原有相对关系）。此后任何新增裸数字都会直接失败。 */
const BASELINE = {}

/** 裸数字 z-index（`z-index: 10`）；`z-index: var(--z-*)` 不算命中 */
const BARE_Z_INDEX = /z-index:\s*-?\d+/g

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...walk(full))
    else if (/\.(vue|css)$/.test(entry)) out.push(full)
  }
  return out
}

const counts = new Map()
for (const file of walk(join(ROOT, 'src'))) {
  const rel = relative(ROOT, file).split('\\').join('/')
  const hits = readFileSync(file, 'utf8').match(BARE_Z_INDEX)
  if (hits) counts.set(rel, hits.length)
}

const grown = []
for (const [file, n] of counts) {
  const allowed = BASELINE[file] ?? 0
  if (n > allowed) grown.push(`${file}: ${n} 处裸 z-index（基线 ${allowed}）`)
}

const shrinkable = Object.keys(BASELINE).filter(file => !counts.has(file))

for (const file of shrinkable) {
  console.log(`基线可下调（该文件已无裸 z-index）：${file}`)
}

if (grown.length) {
  console.error('z-index token 契约失败 —— 新增浮层请用 src/style.css 的 var(--z-*)：')
  for (const line of grown) console.error(`  ${line}`)
  console.error('')
  console.error('可用 token：--z-content / --z-local / --z-sticky / --z-dropdown / --z-dock')
  console.error('           --z-drawer / --z-overlay / --z-modal / --z-page-bar')
  console.error('           --z-page-overlay / --z-page-panel / --z-palette / --z-viewer / --z-progress')
  process.exit(1)
}

const total = [...counts.values()].reduce((a, b) => a + b, 0)
console.log(`z-index token 契约通过（存量裸数字 ${total} 处，分布在 ${counts.size} 个文件）`)
