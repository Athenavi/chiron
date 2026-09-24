#!/usr/bin/env node
/**
 * i18n codemod（script 段）：把 `<script setup>` 里**明确 UI 语境**的中文字符串字面量
 * 换成 `t('原文')`，并自动注入 `useI18n`。
 *
 * 为什么单独一个脚本、且只做 `<script setup>`：
 *   - `<script setup>` 的顶层代码就是组件 setup 体，`t` 一定在作用域内 → 注入可靠；
 *   - 普通 `<script>`（模块作用域）与独立 .ts 需要 `i18n.global.t` 与相对路径 import，
 *     注入点因文件而异 → 留给人工批次。
 *
 * ── 保守策略（这是本脚本的核心，不是附属）──
 * 字符串字面量有两类截然不同的用途：**给用户看的文案**与**参与比较/分派的数据值**。
 * 把后者 i18n 化会造成"后端返回 '已激活'，前端比较 t('已激活')"这种永久失配的 bug。
 * 因此：
 *   1) **行级白名单**：只有命中 `label:` / `title:` / `placeholder:` / `description:` /
 *      `hint:` / `tooltip:` / `okText:` / `cancelText:` / `errorText:` / `emptyText:` /
 *      `text:` / `content:`，或 `message.success|error|warning|info(` 的行才处理；
 *   2) **行级黑名单**：含 `===` `!==` `case` `switch` `.includes(` `.startsWith(` /
 *      `.endsWith(` `indexOf(` 的行整行跳过（比较上下文）；
 *   3) 只处理**单引号**字面量，反引号（模板串/插值）与双引号一律不动；
 *   4) 复用与模板 codemod 相同的 isSafeText / esc（单一口径）。
 *
 * 用法：
 *   node scripts/i18n-codemod-script.mjs                 # dry-run
 *   node scripts/i18n-codemod-script.mjs --apply
 *   node scripts/i18n-codemod-script.mjs --apply --only src/views/admin/TenantManagementView.vue
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const APPLY = process.argv.includes('--apply')
const onlyIdx = process.argv.indexOf('--only')
const ONLY = onlyIdx >= 0 ? process.argv[onlyIdx + 1]?.split('\\').join('/') : null

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/
/** 只处理单引号字面量：反引号有插值、双引号在 HTML 属性里更常见，都放过 */
const STRING_RE = /'((?:[^'\\\n]|\\.)*)'/g
/**
 * 已经 i18n 化的字面量（`t('…')` / `tr('…')` / `$t('…')` / `i18n.global.t('…')`）。
 *
 * 本脚本按行内**所有**单引号字面量替换，若不排除已有调用，原有的 `t('微信')` 会被
 * 再包一层成为 `t(t('微信'))`（实测 790 处）—— 虽因内层先翻译而"巧合等价"，但语义
 * 冗余、且一旦某语言有真实译文就会二次查找。这里先把这些区间算出来，替换时跳过。
 */
const I18N_WRAPPED = /(?:\$t|(?<![\w.$])(?:t|tr)|i18n\.global\.t)\(\s*'(?:[^'\\]|\\.)*'\s*\)/g

const WHITE =
  /(?:\b(?:label|title|placeholder|description|hint|tooltip|okText|cancelText|errorText|emptyText|text|content)\s*:)|(?:message\.(?:success|error|warning|info|loading)\()/
const BLACK =
  /(?:===|!==|\bcase\b|\bswitch\b|\.includes\(|\.startsWith\(|\.endsWith\(|indexOf\(|\.match\()/

function isSafeText(s) {
  const text = s.trim()
  if (!text || text.length > 40) return false
  if (/[`{}()<>@:$\\%&]/.test(text)) return false
  if (/[\r\n\t]/.test(text)) return false
  if (/^(https?:|\/|#)/.test(text)) return false
  if (!CJK.test(text)) return false
  if (text.length < 2) return false
  return true
}

const esc = s =>
  s
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')
    .replace(/\t/g, '\\t')

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'locales') continue
      out.push(...walk(full))
    } else if (entry.endsWith('.vue')) {
      out.push(full)
    }
  }
  return out
}

const SETUP_RE = /<script\s+setup[^>]*>([\s\S]*?)<\/script>/
const phrases = new Map()
const report = []

for (const file of walk(join(ROOT, 'src')).sort()) {
  const rel = relative(ROOT, file).split('\\').join('/')
  if (ONLY && rel !== ONLY) continue
  const source = readFileSync(file, 'utf8')
  const m = SETUP_RE.exec(source)
  if (!m) continue
  const inner = m[1]
  const innerStart = m.index + m[0].indexOf(inner)

  // ── 决定调用名与是否需要注入 ──
  let call = 't'
  let needDecl = true
  let declLine = 'const { t } = useI18n()'
  const aliasMatch = /const\s*\{\s*t\s*:\s*(\w+)\s*\}\s*=\s*useI18n\(\)/.exec(inner)
  if (aliasMatch) {
    call = aliasMatch[1]
    needDecl = false
  } else if (/const\s*\{\s*t\s*\}\s*=\s*useI18n\(\)/.test(inner)) {
    call = 't'
    needDecl = false
  } else if (/(^|[^\w.$])t\s*=/.test(inner)) {
    // 已有名为 t 的变量（如 v-for 的循环变量同名的局部绑定）→ 用别名，避免遮蔽
    call = 'tr'
    declLine = 'const { t: tr } = useI18n()'
  }
  const needImport = !/from\s+['"]vue-i18n['"]/.test(inner)

  let hits = 0
  const lines = inner.split('\n')
  const outLines = lines.map(line => {
    if (!CJK.test(line)) return line
    if (BLACK.test(line)) return line
    if (!WHITE.test(line)) return line
    // 跳过已在 i18n 调用内的字面量（否则会包出 t(t('…'))）
    const wrapped = [...line.matchAll(I18N_WRAPPED)].map(m => [m.index, m.index + m[0].length])
    return line.replace(STRING_RE, (whole, body, offset) => {
      if (wrapped.some(([a, b]) => offset >= a && offset < b)) return whole
      if (!isSafeText(body)) return whole
      hits++
      phrases.set(body.trim(), (phrases.get(body.trim()) ?? 0) + 1)
      return `${call}('${esc(body.trim())}')`
    })
  })

  if (hits === 0) continue

  let newInner = outLines.join('\n')
  if (needImport || needDecl) {
    const decls = []
    if (needImport) decls.push("import { useI18n } from 'vue-i18n'")
    if (needDecl) decls.push(declLine)
    // 插到 import 区之后。**不能**用 `/^import .*$/` 取"最后一条 import"：多行 import
    // （`import {\n  a,\n} from 'x'`）的首行同样匹配，声明会被插进花括号里 —— 曾导致
    // vue/compiler-sfc 报 "Unexpected keyword 'import'"（整块 setup 编译失败，套件级挂掉）。
    // 改为按 import 语句的**结束位置**取最大值。
    let at = 0
    const IMPORT_END = [
      /^import[^\n]*from\s+['"][^'"]+['"]\s*$/gm, // 单行：import x from 'y'
      /^\}\s*from\s+['"][^'"]+['"]\s*$/gm, // 多行 import 的收尾行：} from 'y'
      /^import\s+['"][^'"]+['"]\s*$/gm, // 副作用导入：import 'y'
    ]
    for (const re of IMPORT_END) {
      for (const match of newInner.matchAll(re)) at = Math.max(at, match.index + match[0].length)
    }
    newInner =
      at > 0
        ? newInner.slice(0, at) + '\n' + decls.join('\n') + newInner.slice(at)
        : '\n' + decls.join('\n') + '\n' + newInner
  }

  report.push({ file: rel, hits, call, injected: needImport || needDecl })
  if (APPLY) {
    writeFileSync(file, source.slice(0, innerStart) + newInner + source.slice(innerStart + inner.length), 'utf8')
  }
}

const unique = [...phrases.keys()].sort()
console.log(`${APPLY ? '已写入' : 'dry-run'}：${report.length} 个文件 / ${report.reduce((a, r) => a + r.hits, 0)} 处替换 / ${unique.length} 条唯一原文`)
for (const r of report.sort((a, b) => b.hits - a.hits).slice(0, 15)) {
  console.log(`  ${r.hits.toString().padStart(3)}× ${r.file}${r.injected ? '  (+注入 useI18n)' : ''}`)
}
if (unique.length) {
  console.log('\n唯一原文（前 25）：')
  for (const p of unique.slice(0, 25)) console.log(`  ${p}`)
}

if (APPLY && unique.length) {
  const legacyPath = join(ROOT, 'src', 'locales', 'zh-CN', 'legacy.ts')
  const prev = readFileSync(legacyPath, 'utf8')
  const existing = new Set([...prev.matchAll(/^ {2}'((?:[^'\\]|\\.)*)':/gm)].map(x => x[1]))
  const added = unique.filter(p => !existing.has(esc(p)))
  if (added.length) {
    const lines = added.map(p => `  '${esc(p)}': '${esc(p)}',`).join('\n')
    writeFileSync(legacyPath, prev.replace(/\n\}\n$/, `\n${lines}\n}\n`), 'utf8')
  }
  console.log(`\nlegacy.ts 新增 ${added.length} 条（已存在 ${unique.length - added.length} 条）`)
}
