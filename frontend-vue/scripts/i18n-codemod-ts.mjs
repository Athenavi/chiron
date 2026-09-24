#!/usr/bin/env node
/**
 * i18n codemod（模块作用域）：处理独立 `.ts` 与 `.vue` 的**非 setup** `<script>`。
 *
 * 与 i18n-codemod-script.mjs 的区别：
 *   - 那里是组件 setup 体，可注入 `useI18n()`（响应式）；
 *   - 这里是**模块作用域**（工具函数、常量表、API 层），只能注入
 *     `import { t } from '<相对路径>/i18n'` 并使用**非响应式** `t()`。
 *
 * **非响应式的后果**：模块级常量在求值时翻译一次，语言切换后**不会重新求值**。
 * 对错误消息、工具函数文案（求值时即用）没有影响；对模块级表格列定义之类会影响
 * 列头语言 —— 若真需要响应式，应把定义移到组件的 `computed`（那属于 setup codemod 的范围）。
 *
 * 安全性沿用同一套保守策略：行级白名单（`label:` / `title:` / `message.*(` …）+
 * 行级黑名单（`===` / `case` / `.includes(` … 比较上下文整行跳过）+ 只动单引号字面量。
 *
 * 用法：
 *   node scripts/i18n-codemod-ts.mjs                # dry-run
 *   node scripts/i18n-codemod-ts.mjs --apply
 *   node scripts/i18n-codemod-ts.mjs --apply --only src/api/memory.ts
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const APPLY = process.argv.includes('--apply')
const onlyIdx = process.argv.indexOf('--only')
const ONLY = onlyIdx >= 0 ? process.argv[onlyIdx + 1]?.split('\\').join('/') : null

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/
const STRING_RE = /'((?:[^'\\\n]|\\.)*)'/g
/**
 * 已经 i18n 化的字面量（`t('…')` / `tr('…')` / `$t('…')` / `i18n.global.t('…')`）。
 *
 * 本脚本按行内**所有**单引号字面量替换，若不排除已有调用，原有的 `t('微信')` 会被
 * 再包一层成为 `t(t('微信'))`（实测 790 处，与 i18n-codemod-script.mjs 同因）。
 * 这里先把这些区间算出来，替换时跳过。
 */
const I18N_WRAPPED = /(?:\$t|(?<![\w.$])(?:t|tr)|i18n\.global\.t)\(\s*'(?:[^'\\]|\\.)*'\s*\)/g
const WHITE =
  /(?:\b(?:label|title|placeholder|description|hint|tooltip|okText|cancelText|errorText|emptyText|text|content|message)\s*:)|(?:message\.(?:success|error|warning|info|loading)\()/
const BLACK = /(?:===|!==|\bcase\b|\bswitch\b|\.includes\(|\.startsWith\(|\.endsWith\(|indexOf\(|\.match\()/

/** 跳过非业务文件：语言包本身就是译文、i18n 模块是基础设施 */
const SKIP_DIRS = new Set(['__tests__', 'locales', 'i18n'])

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

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (SKIP_DIRS.has(entry)) continue
      walk(full, out)
    } else if (
      (entry.endsWith('.ts') || entry.endsWith('.vue')) &&
      !entry.endsWith('.spec.ts') &&
      !entry.endsWith('.d.ts')
    ) {
      out.push(full)
    }
  }
  return out
}

/** 从 file 到 src/i18n 的相对 import 路径 */
function i18nImport(file) {
  let rel = relative(dirname(file), join(ROOT, 'src', 'i18n')).split('\\').join('/')
  if (!rel.startsWith('.')) rel = './' + rel
  return `import { t } from '${rel}'`
}

/** 取"非 setup"的 script 段（.vue 用）；.ts 文件整文件即模块作用域 */
const PLAIN_SCRIPT_RE = /<script(?![^>]*\bsetup\b)[^>]*>([\s\S]*?)<\/script>/

const phrases = new Map()
const report = []

for (const file of walk(join(ROOT, 'src')).sort()) {
  const rel = relative(ROOT, file).split('\\').join('/')
  if (ONLY && rel !== ONLY) continue
  const source = readFileSync(file, 'utf8')

  let inner = null
  let innerStart = 0
  if (file.endsWith('.ts')) {
    inner = source
    innerStart = 0
  } else {
    const m = PLAIN_SCRIPT_RE.exec(source)
    if (!m) continue // 只有 <script setup> 的组件已由上一个 codemod 处理
    inner = m[1]
    innerStart = m.index + m[0].indexOf(inner)
  }
  if (!inner || !CJK.test(inner)) continue

  const call = 't'
  const needImport = !/import\s*\{\s*t\s*\}\s*from\s+['"][^'"]*i18n['"]/.test(inner)

  let hits = 0
  let newInner = inner
    .split('\n')
    .map(line => {
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
    .join('\n')

  if (hits === 0) continue

  if (needImport) {
    // 注入点：import 区的结束位置（同 setup codemod —— 多行 import 的首行也匹配 ^import）
    let at = 0
    for (const re of [
      /^import[^\n]*from\s+['"][^'"]+['"]\s*$/gm,
      /^\}\s*from\s+['"][^'"]+['"]\s*$/gm,
      /^import\s+['"][^'"]+['"]\s*$/gm,
    ]) {
      for (const match of newInner.matchAll(re)) at = Math.max(at, match.index + match[0].length)
    }
    const decl = i18nImport(file)
    newInner = at > 0 ? newInner.slice(0, at) + '\n' + decl + newInner.slice(at) : decl + '\n' + newInner
  }

  report.push({ file: rel, hits, injected: needImport })
  if (APPLY) {
    writeFileSync(file, source.slice(0, innerStart) + newInner + source.slice(innerStart + inner.length), 'utf8')
  }
}

const unique = [...phrases.keys()].sort()
console.log(`${APPLY ? '已写入' : 'dry-run'}：${report.length} 个文件 / ${report.reduce((a, r) => a + r.hits, 0)} 处替换 / ${unique.length} 条唯一原文`)
for (const r of report.sort((a, b) => b.hits - a.hits).slice(0, 15)) {
  console.log(`  ${r.hits.toString().padStart(3)}× ${r.file}${r.injected ? '  (+注入 t)' : ''}`)
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
