#!/usr/bin/env node
/**
 * 主题 token 一致性守卫（UI/UX §4.10 的 #8）。
 *
 * **先说清判据为什么是这样**：§4.10 原本提议加一条「**CSS token ↔ antd token 键集同构**」的守卫。
 * 2026-10-09 实测后**否掉了这个判据**：两侧词表**结构不同** ——
 *   · CSS（`src/style.css`）：**语义**词表，每块 **96** 个变量（`--primary` / `--brand-500` / `--success` …）
 *   · antd（`src/stores/theme.ts`）：**组件面**词表，每对象 **13** 个键（`colorPrimary` / `colorBgLayout` …）
 * 按 `camelCase → kebab` 约定去对，**0/13 命中** ⇒ 两侧是「**投影**」关系（antd 取色板的一个子集），
 * **不是同名同构**。硬写"键集相等"会得到一个**永远红或永远假绿**的护栏。
 *
 * **所以本守卫钉的是**「**每一侧内部必须齐整**」——这是"双源漂移"里**真正会出事**的那种：
 *   ① `style.css` 的 **8** 个 `[data-theme='<id>-<light|dark>']` 块必须共享**同一套 token 名**；
 *   ② `stores/theme.ts` 的 **8** 个 `*Tokens` 对象必须共享**同一套键**。
 * 任何一处少写一个（改主题时最容易漏）⇒ 该主题下某个组件拿到**未定义变量 / 回退色**，而界面上
 * 往往只是"某个角落颜色不对"，没人会立刻发现 —— 这正是要用守卫钉住的那类。
 *
 * **跨侧关系**（antd 取哪些色板值）**刻意不查**：那是**语义映射**，需要人给映射表；
 * 实测 0/13 可由约定推导 ⇒ 现在写只会是伪判据。已在 `docs/reasonix-ui-ux-gap-analysis.md` §4.10 记明。
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const CSS = join(ROOT, 'src', 'style.css')
const THEME_TS = join(ROOT, 'src', 'stores', 'theme.ts')

/** 取从 `{` 起配平的块体（不处理字符串里的花括号 —— 这两处都没有）。 */
function balanced(text, openIndex) {
  let depth = 0
  for (let i = openIndex; i < text.length; i++) {
    if (text[i] === '{') depth++
    else if (text[i] === '}') {
      depth--
      if (depth === 0) return text.slice(openIndex + 1, i)
    }
  }
  return ''
}

/** 去掉 CSS 注释（`/* … *\/`）—— 注释掉的 token 不该被算进去。 */
function stripCssComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** 去掉 TS 的块注释与行注释（够用即可：这两处不含字符串里的 `//`）。 */
function stripTsComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

const problems = []

// ① CSS：8 个主题块共享同一套 token 名
const css = stripCssComments(readFileSync(CSS, 'utf8'))
const cssBlocks = new Map()
const blockRe = /\[data-theme=['"]([a-z]+-(?:light|dark))['"]\]\s*\{/g
for (const m of css.matchAll(blockRe)) {
  const open = m.index + m[0].length - 1
  const names = new Set([...balanced(css, open).matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map(x => x[1]))
  cssBlocks.set(m[1], names)
}

if (cssBlocks.size === 0) {
  problems.push('[X] 守卫自身失效：style.css 里没找到任何 [data-theme=...] 块（判据已与写法脱节？）')
} else {
  const [baseName, base] = [...cssBlocks.entries()][0]
  for (const [name, names] of cssBlocks) {
    const missing = [...base].filter(n => !names.has(n)).sort()
    const extra = [...names].filter(n => !base.has(n)).sort()
    if (missing.length || extra.length) {
      problems.push(
        `[X] style.css：${name} 与 ${baseName} 的 token 名不一致` +
        `（缺 ${missing.slice(0, 8).join(', ') || '无'} · 多 ${extra.slice(0, 8).join(', ') || '无'}）`
      )
    }
  }
}

// ② theme.ts：8 个 *Tokens 对象共享同一套键
const ts = stripTsComments(readFileSync(THEME_TS, 'utf8'))
const tsObjects = []
const objRe = /(?:lightTokens|darkTokens)\s*:\s*\{/g
for (const m of ts.matchAll(objRe)) {
  const open = m.index + m[0].length - 1
  const keys = new Set(
    [...balanced(ts, open).matchAll(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/gm)].map(x => x[1])
  )
  tsObjects.push(keys)
}

if (tsObjects.length === 0) {
  problems.push('[X] 守卫自身失效：stores/theme.ts 里没找到任何 *Tokens 对象（判据已与写法脱节？）')
} else {
  const base = tsObjects[0]
  tsObjects.forEach((keys, idx) => {
    const missing = [...base].filter(k => !keys.has(k)).sort()
    const extra = [...keys].filter(k => !base.has(k)).sort()
    if (missing.length || extra.length) {
      problems.push(
        `[X] stores/theme.ts：第 ${idx + 1} 个 *Tokens 与第 1 个的键不一致` +
        `（缺 ${missing.slice(0, 8).join(', ') || '无'} · 多 ${extra.slice(0, 8).join(', ') || '无'}）`
      )
    }
  })
}

if (problems.length) {
  console.error(`主题 token 一致性检查失败：${problems.length} 处`)
  for (const p of problems) console.error(`  ${p}`)
  console.error('\n修法：把缺的 token/键补齐到**同一套**里（8 个主题块 / 8 个 token 对象必须齐整）')
  process.exit(1)
}

const n = [...cssBlocks.values()][0]?.size ?? 0
const k = tsObjects[0]?.size ?? 0
console.log(
  `主题 token 一致性通过：style.css ${cssBlocks.size} 个主题块各 ${n} 个 token（名集合一致）· ` +
  `stores/theme.ts ${tsObjects.length} 个 token 对象各 ${k} 个键（键集合一致）`
)
