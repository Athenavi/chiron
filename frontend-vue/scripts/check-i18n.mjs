#!/usr/bin/env node
/**
 * i18n 契约检查（棘轮 / ratchet）。
 *
 * 背景：前端 110 个文件里散落着约 2800 行中文 UI 文案。i18n 迁移只能分批推进，而
 * "记得别写硬编码"是不可靠的 —— 因此沿用 scripts/check-z-index-tokens.mjs 的棘轮策略：
 *   - scripts/i18n-baseline.json 记录每个文件的存量中文 UI 文案数（存量容忍）；
 *   - **任何文件超过基线即失败**（新增文案必须走 $t / t()）；
 *   - 迁移后基线只允许下调（脚本会提示可下调项），不允许上调。
 *
 * 统计口径：**只算疑似 UI 文案**。注释（`//`、`/* *​/`、`<!-- -->`，含跨行）与测试文件
 * 不计入 —— 注释不面向用户、不需要翻译。
 *
 * 实现要点：先整体剥离注释、再按行统计，而不是逐行正则匹配注释前缀。多行注释（HTML
 * 注释块、块注释）的第二行并不以注释符号开头，逐行判断会把它们当成文案 —— 假阳性会
 * 让基线失去公信力，最终被人为上调（护栏失效）。
 *
 * 用法：
 *   node scripts/check-i18n.mjs                  # 校验（CI / check:ui）
 *   node scripts/check-i18n.mjs --write-baseline # 生成/下调基线（迁移一批后执行）
 */
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const BASELINE_PATH = join(ROOT, 'scripts', 'i18n-baseline.json')
const WRITE = process.argv.includes('--write-baseline')

/** CJK 统一表意文字 */
const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/

/**
 * 剥离注释：
 *  1) HTML/Vue 模板注释（可跨行）：<!-- ... -->
 *  2) 块注释（可跨行）：斜杠星号 ... 星号斜杠
 *  3) 行注释：//（用 `(^|[^:'"`\\])` 前缀，避免吃掉 http:// 这类字符串里的双斜杠）
 * 统一替换为空格，保持行结构基本不变（计数口径确定即可）。
 */
function stripComments(source) {
  let out = source.replace(/<!--[\s\S]*?-->/g, ' ')
  out = out.replace(/\/\*[\s\S]*?\*\//g, ' ')
  out = out
    .split(/\r?\n/)
    .map(line => line.replace(/(^|[^:'"`\\])\/\/.*$/, '$1'))
    .join('\n')
  return out
}

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      // __tests__：断言里的中文随批次一起迁移，但不属于"界面文案"统计口径
      // locales：译文存放地 —— 那里的中文就是目标产物，不能算硬编码
      if (entry === '__tests__' || entry === 'locales') continue
      out.push(...walk(full))
    } else if (/\.(vue|ts)$/.test(entry) && !/\.spec\.ts$/.test(entry) && !/\.d\.ts$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

/**
 * 已 i18n 化的调用（`$t('…')` / `t('…')` / `tr('…')` / `i18n.global.t('…')`）内的中文**不算硬编码**：
 * 那正是迁移目标 —— 文案已集中到 locales。若不剥离，`$t('登录')` 会被误报为新增硬编码，
 * 护栏会把正确的做法判成违规（公信力一旦崩就会被人上调基线）。
 *
 * `tr` 是 `const { t: tr } = useI18n()` 的别名 —— 组件里已有 `v-for` 变量叫 `t` 时必须换名
 * （见 src/i18n/README.md 的"模板变量遮蔽"）。此前正则漏了它，把 ChatSidePanel 里
 * 6 处**已迁移**的文案误报成硬编码。`tr` 前的字符类排除了 `\w.$`，因此 `str(`/`other.tr(` 不会误配。
 */
const I18N_CALL = /(?:\$t|(?<![\w.$])(?:t|tr)|i18n\.global\.t)\(\s*'((?:[^'\\]|\\.)*)'(?:\s*,[^)]*)?\)/g

function stripI18nCalls(source) {
  return source.replace(I18N_CALL, "$t('')")
}

/** 统计单个文件里"疑似 UI 文案"的行数（注释与已 i18n 化调用已剥离） */
function countUserFacingCjk(source) {
  let n = 0
  for (const line of stripI18nCalls(stripComments(source)).split(/\r?\n/)) {
    if (CJK.test(line)) n++
  }
  return n
}

const counts = new Map()
for (const file of walk(join(ROOT, 'src'))) {
  const rel = relative(ROOT, file).split('\\').join('/')
  const n = countUserFacingCjk(readFileSync(file, 'utf8'))
  if (n > 0) counts.set(rel, n)
}

const current = Object.fromEntries([...counts.entries()].sort(([a], [b]) => a.localeCompare(b)))

if (WRITE) {
  writeFileSync(BASELINE_PATH, JSON.stringify(current, null, 2) + '\n', 'utf8')
  const total = Object.values(current).reduce((a, b) => a + b, 0)
  console.log(`基线已写入 ${relative(ROOT, BASELINE_PATH)}：${Object.keys(current).length} 个文件、${total} 行待迁移`)
  process.exit(0)
}

if (!existsSync(BASELINE_PATH)) {
  console.error('缺少 scripts/i18n-baseline.json —— 首次使用请先执行：node scripts/check-i18n.mjs --write-baseline')
  process.exit(1)
}
const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))

const grown = []
for (const [file, n] of Object.entries(current)) {
  const allowed = baseline[file] ?? 0
  if (n > allowed) grown.push(`${file}: ${n} 行含中文（基线 ${allowed}）`)
}

// 已清零或已下降的文件：提示下调基线，锁定迁移成果
const shrinkable = []
for (const [file, allowed] of Object.entries(baseline)) {
  const n = current[file] ?? 0
  if (n < allowed) shrinkable.push(`${file}: ${allowed} → ${n}`)
}

if (shrinkable.length) {
  console.log('基线可下调（迁移成果可锁定，执行 --write-baseline）：')
  for (const line of shrinkable) console.log(`  ${line}`)
}

if (grown.length) {
  console.error('i18n 契约失败 —— 新增用户可见文案请走 i18n（$t / t()），不要硬编码中文：')
  for (const line of grown) console.error(`  ${line}`)
  console.error('')
  console.error('参考：src/i18n/index.ts（t 入口）、src/locales/zh-CN/（源语言）')
  process.exit(1)
}

const total = Object.values(current).reduce((a, b) => a + b, 0)
console.log(`i18n 契约通过（存量待迁移 ${total} 行，分布在 ${Object.keys(current).length} 个文件）`)
