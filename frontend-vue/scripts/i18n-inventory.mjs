#!/usr/bin/env node
/**
 * i18n 存量清单（迁移工作台）。
 *
 * 作用：把 scripts/check-i18n.mjs 只用来"计数"的同一口径，升级成**可执行的清单**：
 *   - 逐文件列出待迁移的每一行（行号 + 原文），供分批抽取；
 *   - 汇总**高频短语**（如「确定」「取消」「加载失败」），它们是公共术语表与
 *     common.* 的候选 —— 先把高频短语收敛到 common，后续批次能显著少写 key；
 *   - 输出 JSON 便于脚本/人工处理（例如生成翻译工作单交给译者）。
 *
 * 只读源码，只写 scripts/i18n-inventory.json，不改任何业务文件。
 *
 * 用法：node scripts/i18n-inventory.mjs
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const OUT = join(ROOT, 'scripts', 'i18n-inventory.json')

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/

function stripComments(source) {
  // ⚠ 与 check-i18n.mjs 的实现**有意不同**：那里把整段注释替换成单个空格（它只需要计数），
  // 而本脚本要把行号交给人工定位，所以必须**保留行结构** —— 把注释内容替换为等量空格但
  // 保留其中的换行。此前沿用「压成一行」的写法，跨行 HTML 注释会让其后所有行号前移
  // （CaptchaWidget.vue 实测错位 10 行），清单直接不可用。
  //
  // `<style>` 块同样剔除（口径与 check-i18n.mjs 一致）：CSS 的 `content: '…'` 无法走 i18n。
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

/**
 * 已 i18n 化的调用（`$t('…')` / `t('…')` / `tr('…')` / `i18n.global.t('…')`）内的中文
 * **不算待迁移文案** —— 与 scripts/check-i18n.mjs 必须用**同一口径**。
 *
 * 历史缺陷：本脚本此前只剥离注释、不剥离 i18n 调用，于是「$t('取消')」这类**已经迁移完**的行
 * 被计入待迁移清单，高频短语表里甚至出现 `$t('取消')` 28 次、`$t('删除')` 等。照这份清单干活
 * 会把已完成的文件再做一遍，且「存量总额」会与护栏（check-i18n.mjs）长期对不上。
 */
const I18N_CALL = /(?:\$t|(?<![\w.$])(?:t|tr)|i18n\.global\.t)\(\s*'((?:[^'\\]|\\.)*)'(?:\s*,[^)]*)?\)/g

function stripI18nCalls(source) {
  // ⚠ 替换时必须**补回被吃掉的换行**：`I18N_CALL` 的 `(?:,\s*[^)]*)?` 能跨行，
  // `t('key', { …多行对象… })` 会被整体匹配成一段，直接替换成 `$t('')` 会让其后
  // 所有行号前移（ChatView.vue 实测偏移 2 行）。行号是本清单交付给人工的核心信息。
  return source.replace(I18N_CALL, (m) => "$t('')" + '\n'.repeat(m.split('\n').length - 1))
}

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
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

/** 从一行里抽出"引号内的中文串"，用于高频短语统计（模板文本单独处理） */
function quotedPhrases(line) {
  const out = []
  for (const re of [/'([^'\\]*[\u3400-\u4dbf\u4e00-\u9fff][^'\\]*)'/g, /"([^"\\]*[\u3400-\u4dbf\u4e00-\u9fff][^"\\]*)"/g, /`([^`\\]*[\u3400-\u4dbf\u4e00-\u9fff][^`\\]*)`/g]) {
    for (const m of line.matchAll(re)) out.push(m[1].trim())
  }
  return out
}

const files = []
const phraseCount = new Map()

for (const file of walk(join(ROOT, 'src')).sort()) {
  const rel = relative(ROOT, file).split('\\').join('/')
  const lines = stripI18nCalls(stripComments(readFileSync(file, 'utf8'))).split(/\r?\n/)
  const entries = []
  lines.forEach((line, i) => {
    if (!CJK.test(line)) return
    entries.push({ line: i + 1, text: line.trim().slice(0, 200) })
    for (const p of quotedPhrases(line)) {
      if (p && p.length <= 24) phraseCount.set(p, (phraseCount.get(p) ?? 0) + 1)
    }
  })
  if (entries.length) files.push({ file: rel, count: entries.length, entries })
}

const frequent = [...phraseCount.entries()]
  .filter(([, n]) => n >= 3)
  .sort((a, b) => b[1] - a[1])
  .slice(0, 60)
  .map(([phrase, n]) => ({ phrase, occurrences: n }))

const inventory = {
  generated_by: 'scripts/i18n-inventory.mjs',
  totals: {
    files: files.length,
    lines: files.reduce((a, f) => a + f.count, 0),
  },
  /** 高频短语：优先收敛到 src/locales/<lang>/common.ts（或已有 key），减少后续 key 数量 */
  frequent_phrases: frequent,
  files,
}

writeFileSync(OUT, JSON.stringify(inventory, null, 2) + '\n', 'utf8')
console.log(`清单已写入 scripts/i18n-inventory.json：${inventory.totals.files} 个文件 / ${inventory.totals.lines} 行`)
console.log(`高频短语（≥3 次）${frequent.length} 条，前 10：`)
for (const { phrase, occurrences } of frequent.slice(0, 10)) {
  console.log(`  ${occurrences}× ${phrase}`)
}
