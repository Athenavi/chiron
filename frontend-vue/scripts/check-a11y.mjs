#!/usr/bin/env node
/**
 * 可访问性（a11y）契约检查（棘轮 / ratchet）。
 *
 * 背景：项目已有四个 token/契约护栏（z-index、主题、动效、i18n），但**没有可访问性护栏**。
 * 键盘可达性此前靠"写的时候记得"，而"记得"是不可靠的 —— 与 i18n 迁移同理，只能靠棘轮
 * 把存量冻结住、阻止新增，再分批清理。
 *
 * 检查项只收录**能无歧义机械判定**的规则。假阳性会让基线失去公信力，最终被人为上调
 * （护栏失效），因此宁可少收：
 *
 *   R1 clickable-nonsemantic —— 非交互元素（div/span/li/td…）上绑了 @click 却不带 role。
 *      后果：键盘无法聚焦、屏幕阅读器不认为它是控件。正确做法是改用 <button>，或补
 *      role="button" + tabindex="0" + 键盘事件（参见 src/components/chat/ChatEmptyHero.vue）。
 *      带 href 的 <a> 天然可聚焦，不算违规。
 *
 *   R2 img-missing-alt —— <img> 缺 alt 属性。alt="" 表示装饰图，合法（不报）。
 *
 * 统计口径：先整体剥离注释（<!-- -->、块注释、行注释）再扫描 —— 注释不面向用户。
 * 标签匹配是**引号感知**的（`(?:[^>"']|"[^"]*"|'[^']*')*`）。两个必须这么做的理由：
 *   1) `@click="x => y()"` 里箭头函数的 `>` 会截断朴素正则，导致漏报；
 *   2) 朴素正则的 `<a\b` 会把 `<a-button>` 的前缀当成标签 a，把 Ant Design 组件误判为
 *      违规（40 处假阳性）—— 基线一旦掺假就会被上调，护栏随之失效。
 * 测试文件与 locales 不计入。
 *
 * 已知例外：事件委托容器。
 * src/components/chat/MessageItem.vue 的 .msg-row 与 src/views/ShareView.vue 的 .share-thread
 * 上的 @click 只是委托入口 —— handler 内部用 `e.target.closest(...)` 定位真正的交互元素
 * （那些元素本身是 <button>，键盘可达）。容器不是交互元素，给它加 role="button" 会让屏幕
 * 阅读器把整块消息内容朗读成一个按钮，反而更糟。
 *
 * 这类容器由**显式属性 `data-click-delegate`** 标记（而不是记在基线里）：规则本身无法从
 * 属性机械推断「这个 handler 是不是委托」（那需要跟随 JS 调用），所以要求作者**当场声明**。
 * 相比留在 a11y-baseline.json，标记贴着代码、评审时可见，也让基线能真正清零 —— 基线是一条
 * 单调下降的棘轮，掺杂"不该修的例外"会让它失去意义。
 *
 * 用法：
 *   node scripts/check-a11y.mjs                  # 校验（CI / check:ui）
 *   node scripts/check-a11y.mjs --list           # 列出全部存量位置（file:line）
 *   node scripts/check-a11y.mjs --write-baseline # 生成/下调基线（清理一批后执行）
 */
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const BASELINE_PATH = join(ROOT, 'scripts', 'a11y-baseline.json')
const WRITE = process.argv.includes('--write-baseline')
const LIST = process.argv.includes('--list')

/** 非交互标签：绑 @click 时必须自带 role 才进入无障碍树 */
const NON_INTERACTIVE = new Set([
  'div', 'span', 'li', 'td', 'tr', 'th', 'section', 'article', 'p',
  'header', 'footer', 'ul', 'ol', 'dl', 'dt', 'dd',
])

/** 开始标签：引号感知，属性值里的 > 与 => 不会截断匹配 */
const TAG_RE = /<([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g

const CLICK_RE = /(?:@click(?:\.\w+)*|v-on:click(?:\.\w+)*)\s*=/
const ROLE_RE = /\brole\s*=/
const HREF_RE = /\bhref\s*=/
const ALT_RE = /\balt\s*=/
/**
 * 豁免一：`@click.self` 是遮罩/背景层的典型写法（只有点在自己身上才触发），键盘用户有
 * 等价路径（Esc 或关闭按钮），不应要求遮罩本身可聚焦。
 * 豁免二：显式 `aria-hidden` 表示该元素对辅助技术不可见（纯装饰遮罩），即已声明语义 ——
 * 强行给它加 role="button" 反而更糟。
 * 豁免三：显式 `data-click-delegate` 声明「这里的 @click 只是事件委托入口」—— 容器自身
 * 不可交互，真正可聚焦的是内部的 <button>（handler 用 e.target.closest 定位它们）。
 * 需要作者当场声明：从属性无法机械推断 handler 是否委托（那要跟随 JS 调用）。
 */
const CLICK_SELF_RE = /@click\.self\s*=/
const DECORATIVE_RE = /\baria-hidden\s*=/
const CLICK_DELEGATE_RE = /\bdata-click-delegate\b/
/**
 * 剥离注释：HTML/Vue 模板注释、块注释、行注释（行注释避开 http:// 这类字符串）。
 * 注释内容用空格替换但**保留其中的换行** —— 行号必须与原文件一致，否则失败信息里的
 * 位置无法定位（跨行注释整体塌缩会让后面所有行号前移）。
 */
function stripComments(source) {
  const blank = text => text.replace(/[^\n]/g, ' ')
  let out = source.replace(/<!--[\s\S]*?-->/g, blank)
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
      if (entry === '__tests__' || entry === 'node_modules') continue
      out.push(...walk(full))
    } else if (/\.vue$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

/** 扫描单个 .vue，返回 { rule: [行号...] }（只含非零项） */
function scanVue(source) {
  const hits = {}
  const clean = stripComments(source)
  const add = (rule, index) => {
    ;(hits[rule] ??= []).push(clean.slice(0, index).split('\n').length)
  }
  TAG_RE.lastIndex = 0
  let m
  while ((m = TAG_RE.exec(clean)) !== null) {
    const tag = m[1].toLowerCase()
    const attrs = m[2]

    if (tag === 'img') {
      if (!ALT_RE.test(attrs)) add('img-missing-alt', m.index)
      continue
    }

    if (!CLICK_RE.test(attrs)) continue
    if (CLICK_SELF_RE.test(attrs)) continue
    if (ROLE_RE.test(attrs) || DECORATIVE_RE.test(attrs)) continue
    if (CLICK_DELEGATE_RE.test(attrs)) continue
    if (tag === 'a') {
      // <a> 有 href 才可聚焦；无 href 的 <a @click> 是常见误用
      if (!HREF_RE.test(attrs)) add('clickable-nonsemantic', m.index)
      continue
    }
    if (NON_INTERACTIVE.has(tag)) add('clickable-nonsemantic', m.index)
  }
  return hits
}

const RULES = {
  'clickable-nonsemantic':
    '非交互元素绑了 @click 却不带 role —— 键盘无法聚焦。请改用 <button>，或补 role="button" + tabindex="0" + 键盘事件',
  'img-missing-alt': '<img> 缺 alt —— 装饰图请显式写 alt=""，否则屏幕阅读器会朗读文件名',
}

const current = {}
for (const file of walk(join(ROOT, 'src'))) {
  const rel = relative(ROOT, file).split('\\').join('/')
  const hits = scanVue(readFileSync(file, 'utf8'))
  if (Object.keys(hits).length > 0) current[rel] = hits
}
const sorted = Object.fromEntries(Object.entries(current).sort(([a], [b]) => a.localeCompare(b)))

if (WRITE) {
  const out = {}
  for (const [file, hits] of Object.entries(sorted)) {
    out[file] = Object.fromEntries(Object.entries(hits).map(([rule, lines]) => [rule, lines.length]))
  }
  writeFileSync(BASELINE_PATH, JSON.stringify(out, null, 2) + '\n', 'utf8')
  let total = 0
  for (const hits of Object.values(out)) for (const n of Object.values(hits)) total += n
  console.log(
    `基线已写入 ${relative(ROOT, BASELINE_PATH)}：${Object.keys(out).length} 个文件、${total} 处待修复`,
  )
  process.exit(0)
}

if (LIST) {
  let total = 0
  for (const [file, hits] of Object.entries(sorted)) {
    for (const [rule, lines] of Object.entries(hits)) {
      total += lines.length
      console.log(`${file}:${lines.join(',')}  [${rule}]`)
    }
  }
  console.log(`共 ${total} 处`)
  process.exit(0)
}

if (!existsSync(BASELINE_PATH)) {
  console.error('缺少 scripts/a11y-baseline.json —— 首次使用请先执行：node scripts/check-a11y.mjs --write-baseline')
  process.exit(1)
}
const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))

const grown = []
const shrinkable = []

for (const [file, hits] of Object.entries(sorted)) {
  for (const [rule, lines] of Object.entries(hits)) {
    const allowed = baseline[file]?.[rule] ?? 0
    if (lines.length > allowed) grown.push({ file, rule, lines, allowed })
  }
}
for (const [file, hits] of Object.entries(baseline)) {
  for (const [rule, allowed] of Object.entries(hits)) {
    const n = sorted[file]?.[rule]?.length ?? 0
    if (n < allowed) shrinkable.push(`${file} [${rule}]: ${allowed} → ${n}`)
  }
}

if (shrinkable.length) {
  console.log('基线可下调（修复成果可锁定，执行 --write-baseline）：')
  for (const line of shrinkable) console.log(`  ${line}`)
}

if (grown.length) {
  console.error('可访问性契约失败 —— 新增代码请保证键盘可达：')
  for (const g of grown) {
    console.error(`  ${g.file} [${g.rule}]: ${g.lines.length} 处（基线 ${g.allowed}）`)
    for (const ln of g.lines) console.error(`      L${ln}`)
  }
  console.error('')
  for (const [rule, hint] of Object.entries(RULES)) console.error(`  ${rule}：${hint}`)
  process.exit(1)
}

let total = 0
for (const hits of Object.values(sorted)) for (const lines of Object.values(hits)) total += lines.length
console.log(`可访问性契约通过（存量待修复 ${total} 处，分布在 ${Object.keys(sorted).length} 个文件）`)
