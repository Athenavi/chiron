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
  // `<style>` 块整体剔除：CSS 的 `content: '…'` 确实面向用户，但**没有 t() 可用**，
  // 走 i18n 需要把文案搬进模板并用 attr()/data 属性渲染。把它计入存量会得到一个
  // 永远清不掉的数字，最终逼迫上调基线（基线一被上调，护栏就失去公信力）。
  // 用"等量空格 + 保留换行"替换，保持行结构（inventory 依赖行号）。
  const blank = (m) => m.replace(/[^\n]/g, ' ')
  let out = source.replace(/<style[\s\S]*?<\/style>/g, blank)
  out = out.replace(/<!--[\s\S]*?-->/g, blank)
  out = out.replace(/\/\*[\s\S]*?\*\//g, blank)
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
      // `i18n`：i18n 基础设施自身（languages.ts 的 nativeName 是**母语名**，切换器里必须
      // 始终用母语显示 —— 翻译它反而会让中文用户看到「Simplified Chinese」）。
      if (entry === '__tests__' || entry === 'locales' || entry === 'i18n') continue
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

// ── 消息编译检查 ─────────────────────────────────────────────────────────────
// 键就是消息本身（gettext 风格），因此键里出现**字面** `{` 会被 vue-i18n 当作插值解析，
// 运行时抛 `Message compilation error` —— 实测 `{{.SiteName}}`（Go 模板变量）与
// `{"name":…}`（JSON 示例）都会直接把组件渲染打挂。静态正则只能猜"哪个 { 非法"，
// 所以这里直接用 vue-i18n 的编译器过一遍：漏掉一处就是线上白屏。
const { createI18n } = await import('vue-i18n')
const legacySrc = readFileSync(join(ROOT, 'src/locales/zh-CN/legacy.ts'), 'utf8')
const legacyKeys = [...legacySrc.matchAll(/^ {2}'((?:[^'\\]|\\.)*)': /gm)].map(m => m[1])
const probe = createI18n({
  legacy: false,
  locale: 'zh-CN',
  messages: { 'zh-CN': Object.fromEntries(legacyKeys.map(k => [k, k])) },
  missingWarn: false,
  fallbackWarn: false,
})
const brokenKeys = []
const suspicious = []
// 剔除插值 `{…}` 后仍出现 `|`（复数分隔）或 `@`（linked message）→ 消息会被**静默改写**：
// t() 只返回其中一个分支，或把剩余文本当 key 再去查。这类比抛错更难发现。
//
// 注意不能用「t(k) !== k」来判断：实测 vue-i18n 对**未提供的命名参数**是替换成空串
// （不是保留 `{n}`），合法插值会被全部误报。
for (const k of legacyKeys) {
  try {
    probe.global.t(k)
  } catch (e) {
    brokenKeys.push(`${k} —— ${String(e).split('\n')[0]}`)
    continue
  }
  if (/[|@]/.test(k.replace(/\{[^}]*\}/g, ''))) suspicious.push(k)
}
if (brokenKeys.length || suspicious.length) {
  if (brokenKeys.length) {
    console.error('i18n 消息编译失败 —— 键里含**字面** `{` / `}`（vue-i18n 会当插值解析，运行时直接抛错）：')
    for (const line of brokenKeys) console.error(`  ${line}`)
    console.error('')
    console.error("修复：把字面花括号当**参数**传入 —— t('…{ph}…', { ph: '{{.SiteName}}' })，不要写进消息本身；")
    console.error("      若正文本身就是含花括号的技术示例，用 t('{jsonExample}', { jsonExample: '…' })。")
  }
  if (suspicious.length) {
    console.error('i18n 消息含**歧义符号**（插值之外出现 `|` 复数分隔 或 `@` linked message）：')
    for (const line of suspicious) console.error(`  ${line}`)
    console.error('')
    console.error("修复：把这类符号当**参数**传入 —— t('启用 {sep} 停用', { sep: '|' })，或改用不含歧义符号的表达。")
  }
  process.exit(1)
}

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
