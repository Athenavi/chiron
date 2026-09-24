#!/usr/bin/env node
/**
 * 主题 token 契约检查。
 *
 * 背景：`src/style.css` 定义 4 套主题（notion 极简功能 / paper 暖纸阅读 / terminal 终端硬核 /
 * aurora 玻璃柔光 × light/dark），切换时全靠 CSS 变量生效。但组件里散落着硬编码色值
 * （`#fff`、`rgba(0,0,0,.04)` …）—— 它们在深色主题下不会跟着变，表现为"某些区域白底
 * 黑字、某些不是"这类**只有切主题才发现**的视觉不一致。
 *
 * ratchet 约束：存量记录在 BASELINE 里容忍，**新增硬编码即失败**。换成 `var(--*)` 后
 * 把对应数字下调来锁定成果（脚本不接受上调）。
 *
 * `--xxx: <值>` 形式的变量声明行不计入 —— 那是 token 的定义处，本就该有原始色值。
 *
 * 用法：node scripts/check-theme-token-contract.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/** 存量基线：文件 → 允许的硬编码色值数量。只允许下调。 */
const BASELINE = {}

/** 颜色字面量：十六进制、rgb(a)、hsl(a) */
const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g
/** token 定义行（`--name: value`）—— 定义处本就该写原始色值 */
const TOKEN_DEFINITION = /^\s*--[a-z0-9-]+\s*:/
/** `var(--x, <fallback>)` 的兜底值合法 —— 整段摘掉再匹配，否则会误报（实测 63 处） */
const VAR_WITH_FALLBACK = /var\(--[^)]*\)/g

/**
 * `<script>` 块整体剥离后再扫描。
 *
 * 理由：CSS 变量只在**样式声明**里生效，`<script>` 内的颜色字面量（ECharts 主题配置、
 * canvas 的 `THREE.Color` / `addColorStop`、组件的 `color` prop 传值）**无法**写成
 * `var(--x)` —— 那不是"没上 token"，而是这套机制在那里不适用。把处死记在基线里只会让
 * 这条棘轮失去意义（它应当只统计"改得动却没改"的地方）。
 *
 * 这类色值若要跟随主题，需要**另一套机制**（用 `getComputedStyle` 读回变量再传给
 * ECharts / canvas），属独立议题，不在本契约范围内。
 */
const SCRIPT_BLOCK = /<script[\s\S]*?<\/script>/g
function stripScriptBlocks(source) {
  // 用等量换行替换，保持行号与原文件一致（失败信息要能定位）
  return source.replace(SCRIPT_BLOCK, m => m.replace(/[^\n]/g, ' '))
}

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
  let n = 0
  for (const line of stripScriptBlocks(readFileSync(file, 'utf8')).split('\n')) {
    if (TOKEN_DEFINITION.test(line)) continue
    n += (line.replace(VAR_WITH_FALLBACK, 'VAR').match(COLOR_LITERAL) || []).length
  }
  if (n > 0) counts.set(relative(ROOT, file).split('\\').join('/'), n)
}

const grown = []
for (const [file, n] of counts) {
  const allowed = BASELINE[file] ?? 0
  if (n > allowed) grown.push(`${file}: ${n} 处硬编码色值（基线 ${allowed}）`)
}

for (const file of Object.keys(BASELINE)) {
  if (!counts.has(file)) console.log(`基线可下调（该文件已清零）：${file}`)
}

if (grown.length) {
  console.error('主题 token 契约失败 —— 请改用 src/style.css 的 CSS 变量：')
  for (const line of grown) console.error(`  ${line}`)
  console.error('')
  console.error('常用：--text-primary/--text-secondary/--text-tertiary · --bg-page/--bg-card/--bg-hover')
  console.error('      --border · --primary · --shadow-* · --bubble-user')
  process.exit(1)
}

const total = [...counts.values()].reduce((a, b) => a + b, 0)
console.log(`主题 token 契约通过（存量硬编码 ${total} 处，分布在 ${counts.size} 个文件）`)
