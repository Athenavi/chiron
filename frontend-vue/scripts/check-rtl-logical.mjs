#!/usr/bin/env node
/**
 * RTL 逻辑属性契约。
 *
 * 背景：本仓**支持阿拉伯语**（`locales/ar` 14 个域文件 + `LanguageSwitcher` +
 * `i18n/index.ts:96` 真的写 `root.setAttribute('dir', meta.dir)`，连测试都断言了
 * `dir === 'rtl'`）。但样式层曾 **100% 使用物理属性**（`margin-left` / `text-align: left` …）
 * —— 物理属性不随书写方向镜像，于是阿语界面处于"文字 RTL、间距与对齐仍 LTR"的
 * **半翻转**状态：比完全不支持 RTL 更别扭，因为功能承诺了而布局没跟上。
 *
 * 2026-10-25 把 77 处改成逻辑属性（`margin-inline-*` / `padding-inline-*` /
 * `text-align: start|end`）。逻辑属性在 LTR 下与原物理属性**计算值完全相同**，
 * 只在 RTL 下镜像 —— 所以这次改动零视觉回归（已用浏览器两侧实测验证）。
 *
 * ratchet 约束：`border-left` / `border-right` 这 17 处存量记在 BASELINE 里容忍 ——
 * 它们是"强调竖条"（如审批卡左侧的 clay 边），是否该镜像需要逐处判断（有些是刻意的
 * 左侧标记，不是方向性的），不能机械替换。**新增即失败**；改成 `border-inline-*` 后
 * 把对应数字下调来锁定成果（脚本不接受上调）。
 *
 * 刻意**不检查** `left:` / `right:` —— 绝对定位里的 left/right 大量是"相对容器的
 * 物理位置"（`left: 50%` 居中、`left: 0` 贴边、装饰层的固定偏移），机械判成 RTL 违规
 * 会产生大量假阳性；而假阳性会让基线失去公信力、最终被人为上调，护栏随之失效
 * （同 `check-a11y.mjs` 与 `strip-comments.mjs` 里写下的是同一条纪律）。
 *
 * 只扫描 **CSS 声明**（属性名 + 冒号），不会误伤：
 *   · i18n 文案里的同名英文单词（`text-align` 不会出现在文案里，但 `left` 会）；
 *   · JS 里的驼峰写法（`:style="{ marginLeft: 8 }"` 是 `marginLeft`，不匹配）；
 *   · 注释（统一走 stripComments）。
 *
 * 用法：node scripts/check-rtl-logical.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripComments } from './strip-comments.mjs'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/**
 * 存量基线：文件 → 允许的物理属性数量。只允许下调。
 *
 * **已归零**（2026-10-25）：原先记的 17 处 `border-left` / `border-right` 已全部改成
 * `border-inline-start` / `border-inline-end` —— 逐处核对后确认它们全是"边界"：
 * 强调竖线（blockquote 引用线、`.mermaid-error`、审批卡与 AskCard 的色条、`.kb-card.public`）
 * 与面板分隔界（`.property-panel` 左界、`.node-palette` 右界、侧栏右界、时间轴标签右界、
 * 树形导线）。没有一处是"刻意的左侧标记"。所以基线清空，此后**任何**方向性物理属性都会失败。
 */
const BASELINE = {}

/**
 * 违规写法：方向性的物理属性。
 *
 * 每条的 `hint` 是对应的逻辑属性写法 —— 失败信息要能直接照着改。
 * 用 `(^|[\s;{])` 前缀锁定"声明开头"，避免匹配到 `border-inline-start` 这类
 * 已经正确的逻辑属性（它们以 `border-inline-` 开头，不会被 `border-left` 命中）。
 */
const RULES = [
  { re: /(^|[\s;{])margin-left\s*:/g, hint: 'margin-inline-start' },
  { re: /(^|[\s;{])margin-right\s*:/g, hint: 'margin-inline-end' },
  { re: /(^|[\s;{])padding-left\s*:/g, hint: 'padding-inline-start' },
  { re: /(^|[\s;{])padding-right\s*:/g, hint: 'padding-inline-end' },
  { re: /(^|[\s;{])border-left[a-z-]*\s*:/g, hint: 'border-inline-start' },
  { re: /(^|[\s;{])border-right[a-z-]*\s*:/g, hint: 'border-inline-end' },
  { re: /\btext-align\s*:\s*left\b/g, hint: 'text-align: start' },
  { re: /\btext-align\s*:\s*right\b/g, hint: 'text-align: end' },
]

/** 跳过目录：测试夹具与脚本自身 */
const SKIP_DIRS = new Set(['__tests__', 'node_modules'])

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (entry.endsWith('.vue') || entry.endsWith('.css')) out.push(full)
  }
  return out
}

const srcDir = join(ROOT, 'src')
const problems = []
const counts = new Map()

for (const file of walk(srcDir)) {
  const rel = relative(srcDir, file).replace(/\\/g, '/')
  const source = stripComments(readFileSync(file, 'utf8'))
  const lines = source.split('\n')

  lines.forEach((line, idx) => {
    for (const { re, hint } of RULES) {
      re.lastIndex = 0
      if (!re.test(line)) continue
      counts.set(rel, (counts.get(rel) || 0) + 1)
      problems.push({ rel, line: idx + 1, hint, text: line.trim() })
    }
  })
}

const offenders = []
for (const [rel, count] of counts) {
  const allowed = BASELINE[rel] ?? 0
  if (count > allowed) offenders.push({ rel, count, allowed })
}

if (offenders.length) {
  console.error('RTL 逻辑属性契约失败 —— 新增了方向性的物理属性：')
  for (const { rel, count, allowed } of offenders) {
    console.error(`\n  ${rel}：${count} 处（基线 ${allowed}）`)
    for (const p of problems.filter(p => p.rel === rel)) {
      console.error(`    L${p.line}  ${p.text.slice(0, 96)}`)
      console.error(`           → 用 ${p.hint}`)
    }
  }
  console.error(
    '\n物理属性（margin-left / padding-right / border-left / text-align: left|right）' +
    '不随 dir="rtl" 镜像。阿拉伯语界面会变成"文字 RTL、布局 LTR"的半翻转状态。',
  )
  console.error('改法要点：LTR 下逻辑属性与原写法计算值完全相同，零视觉回归。')
  process.exit(1)
}

const total = [...counts.values()].reduce((a, b) => a + b, 0)
const files = counts.size
console.log(
  total === 0
    ? 'RTL 逻辑属性契约通过（零物理属性：全仓已改用逻辑属性）'
    : `RTL 逻辑属性契约通过（存量 ${total} 处物理属性，分布在 ${files} 个文件）`,
)
