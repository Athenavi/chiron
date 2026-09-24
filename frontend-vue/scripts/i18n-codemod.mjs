#!/usr/bin/env node
/**
 * i18n codemod：把 Vue **模板**里的中文静态文案替换为 `$t('原文')`。
 *
 * ── 为什么用"原文作 key" ──
 * 批量抽取无法自动生成语义化 key，而为 2695 行逐条人工命名不现实。原文作 key
 * （gettext 的 msgid 模式）保证：抽取零语义损失、同一短语跨文件**自动去重**、
 * zh-CN 原文即译文源。代价是 key 是中文；语义化 key 改造可作为后续独立任务
 * （只改 key，不动文案）。
 *
 * ── 为什么只动模板 ──
 * 模板可直接用 `$t`（main.ts 里 globalInjection: true），因此**无需注入任何
 * import / useI18n**，改动是纯文本替换；而 script 内的文案需要 t 在作用域，
 * 注入点因文件而异（setup / composable / store / 纯工具），风险高 → 留给人工批次。
 *
 * ── 保守过滤（命中任一条即跳过该处）──
 *   含 `{ } ( ) < > = @ : $ \`` 、含模板插值、以 http/ / /# 开头、长度 > 60
 *   另外：`<script>` / `<style>` 段永不处理。
 *
 * 用法：
 *   node scripts/i18n-codemod.mjs                  # dry-run（默认，不写文件）
 *   node scripts/i18n-codemod.mjs --apply          # 实际写入
 *   node scripts/i18n-codemod.mjs --apply --only src/views/admin/TenancyView.vue
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const APPLY = process.argv.includes('--apply')
const onlyIdx = process.argv.indexOf('--only')
const ONLY = onlyIdx >= 0 ? process.argv[onlyIdx + 1]?.split('\\').join('/') : null

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/
/** 静态属性白名单：只翻译这些语义明确的属性，避免误伤 key/dataIndex/class 等 */
/**
 * 会被 i18n 化的**静态**属性名。
 *
 * `aria-label` 是无障碍读屏用的界面文案（"消息输入框"/"工作台停靠坞"），与 title /
 * placeholder 同属必须翻译的文本 —— 原先漏掉它，导致模板里这类文案一处未迁（实测
 * 它是模板侧剩余存量的主要来源之一）。
 * 注意 `attrRe` 用 `\s(name)="`，因此 `aria-label="x"` 不会被 `label` 项误配
 * （`label` 前是 `-` 而非空白），两项互不干扰。
 */
const ATTRS = ['aria-label', 'label', 'title', 'placeholder', 'description', 'hint', 'ok-text', 'cancel-text', 'empty-text', 'alt']

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__') continue
      out.push(...walk(full))
    } else if (entry.endsWith('.vue')) {
      out.push(full)
    }
  }
  return out
}

/** 是否适合自动替换（保守：宁漏不误） */
function isSafeText(s) {
  if (!s || s.length > 60) return false
  if (/[`{}()<>@:$\\]/.test(s)) return false
  if (/^(https?:|\/|#)/.test(s)) return false
  if (!CJK.test(s)) return false
  // HTML 实体（&nbsp; / &amp; …）：$t 的输出是文本插值、不会解析实体，会字面显示成
  // "&nbsp;"，因此含 & 的文本一律跳过，交人工处理
  if (s.includes('&')) return false
  // 跨行文本（模板里换行书写的长句）：转义后键会带 \n，译文与语序几乎无法维护，
  // 而且历史上正是它把生成的 TS 字符串撑破（Unterminated string）→ 一律跳过
  if (/[\r\n\t]/.test(s)) return false
  // 单字（如 "删"）多为图标/符号语境，交给人工
  if (s.trim().length < 2) return false
  return true
}

/** 转义：源码里的 key 与 locales 的键必须用同一套转义（含换行/制表符兜底） */
const esc = s =>
  s
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')
    .replace(/\t/g, '\\t')

/** 只取 SFC 的顶层模板区间（第一个 <template> 到最后一个 </template>），script/style 一律不动 */
function templateSpan(source) {
  const start = source.indexOf('<template')
  const end = source.lastIndexOf('</template>')
  if (start < 0 || end < start) return null
  return [start, end]
}

const phrases = new Map() // 原文 → 出现次数
const report = []

for (const file of walk(join(ROOT, 'src')).sort()) {
  const rel = relative(ROOT, file).split('\\').join('/')
  if (ONLY && rel !== ONLY) continue
  const source = readFileSync(file, 'utf8')
  const span = templateSpan(source)
  if (!span) continue
  const [s, e] = span
  let head = source.slice(0, s)
  let body = source.slice(s, e)
  let tail = source.slice(e)
  let hits = 0

  // 1) 属性值 → :attr="$t('原文')"
  const attrRe = new RegExp(`\\s(${ATTRS.join('|')})="([^"]*)"`, 'g')
  body = body.replace(attrRe, (m, attr, val) => {
    const text = val.trim()
    if (!isSafeText(text)) return m
    if (val !== text) return m // 前后有空白说明是动态拼接，放过
    hits++
    phrases.set(text, (phrases.get(text) ?? 0) + 1)
    return ` :${attr}="$t('${esc(text)}')"`
  })

  // 2) 文本节点 → {{ $t('原文') }}（保留前后空白，避免可读性/渲染差异）
  body = body.replace(/>([^<>]*)</g, (m, inner) => {
    if (!CJK.test(inner)) return m
    const text = inner.trim()
    if (!isSafeText(text)) return m
    const lead = inner.slice(0, inner.indexOf(text))
    const trail = inner.slice(inner.indexOf(text) + text.length)
    hits++
    phrases.set(text, (phrases.get(text) ?? 0) + 1)
    return `>${lead}{{ $t('${esc(text)}') }}${trail}<`
  })

  if (hits > 0) {
    report.push({ file: rel, hits })
    if (APPLY) writeFileSync(file, head + body + tail, 'utf8')
  }
}

const unique = [...phrases.keys()].sort()
const totalHits = report.reduce((a, r) => a + r.hits, 0)
console.log(`${APPLY ? '已写入' : 'dry-run'}：${report.length} 个文件 / ${totalHits} 处替换 / ${unique.length} 条唯一原文`)
for (const r of report.sort((a, b) => b.hits - a.hits).slice(0, 20)) {
  console.log(`  ${r.hits.toString().padStart(3)}× ${r.file}`)
}
if (unique.length) {
  console.log('\n唯一原文（前 30，将作为 locales/zh-CN/legacy.ts 的键）：')
  for (const p of unique.slice(0, 30)) console.log(`  ${p}`)
}

if (APPLY && unique.length) {
  const lines = unique.map(p => `  '${esc(p)}': '${esc(p)}',`).join('\n')
  const content = `/**
 * legacy 域：**存量自动抽取**的模板文案（原文即 key）。
 *
 * 来源：scripts/i18n-codemod.mjs 对 Vue 模板中静态中文文案的替换（$t('原文')）。
 * 约定：
 *   - 这里是 gettext 风格的 msgid —— 键就是 zh-CN 原文，因此本文件即源语言译文；
 *   - en-US / ar 的对应文件可留空（vue-i18n 会回退到本文件），
 *     翻译是可并行的独立任务，不阻塞代码；
 *   - 后续「语义化 key」改造只需改键与调用点，不动文案。
 */
export default {
${lines}
}
`
  writeFileSync(join(ROOT, 'src', 'locales', 'zh-CN', 'legacy.ts'), content, 'utf8')
  console.log(`\n已写入 src/locales/zh-CN/legacy.ts（${unique.length} 条）`)
  console.log('记得把 legacy 域接入 src/locales/*/index.ts 的 messages。')
}
