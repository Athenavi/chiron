#!/usr/bin/env node
/**
 * 译文键集检查（i18n 的第二道棘轮）。
 *
 * 与 scripts/check-i18n.mjs 分工不同：
 *   - check-i18n.mjs 管**源码** —— 禁止新增硬编码中文（存量已归零）；
 *   - 本脚本管**译文** —— zh-CN 是源语言，其它语言的键集必须逐步追平。
 *
 * 分两级口径，因为 legacy 有 2150 条、不可能一次翻完：
 *  1) **语义域（common / chat / errors / auth / admin）严格对齐** —— 键集必须与 zh-CN
 *     一一对应，缺一个就红。这些域是人工写的、量小（<100 条），没有"待翻译"的借口；
 *     而且它们含 `{占位符}`，缺键时 vue-i18n 会**原样返回 key 并跳过插值**，
 *     界面会直接显示 `{s}s 后重发` 这种半成品。
 *  2) **legacy 域（原文即 key 的存量抽取）走棘轮** —— 缺失数只允许下降、不允许上升
 *     （基线 scripts/i18n-keys-baseline.json）。这样每翻一批就锁一批，不会回退。
 *
 * 为什么用 esbuild 真编译而不是正则扫源码：域文件是**嵌套对象**（chat.time.justNow），
 * 扁平化嵌套结构的正则一旦写错就会漏键，而漏键正是本脚本要防的事 —— 宁可多一道构建。
 *
 * 用法：
 *   node scripts/check-i18n-keys.mjs                      # 校验（CI / check:ui）
 *   node scripts/check-i18n-keys.mjs --write-baseline     # 下调 legacy 基线
 *   node scripts/check-i18n-keys.mjs --list-missing en-US # 打印待翻译键（翻译流程的输入）
 */
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'esbuild'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const LOCALES_DIR = join(ROOT, 'src', 'locales')
const BASELINE_PATH = join(ROOT, 'scripts', 'i18n-keys-baseline.json')
const SOURCE = 'zh-CN'
const TARGETS = ['en-US', 'ar']
/** 语义域前缀：必须严格对齐（legacy 的键是中文原文，不会以这些前缀开头） */
const DOMAIN_PREFIXES = ['common.', 'chat.', 'errors.', 'auth.', 'admin.']

const WRITE = process.argv.includes('--write-baseline')
const listIdx = process.argv.indexOf('--list-missing')
const LIST_MISSING = listIdx === -1 ? null : process.argv[listIdx + 1]

/** 展平成 `a.b.c` → 译文 */
function flatten(obj, prefix = '', out = new Map()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') flatten(v, key, out)
    else out.set(key, String(v))
  }
  return out
}

/** 用 esbuild 把 locales/<lang>/index.ts 编成可 import 的 ESM（含相对导入的子域文件） */
async function loadLocale(lang) {
  const tmp = mkdtempSync(join(tmpdir(), 'chiron-i18n-'))
  await build({
    entryPoints: [join(LOCALES_DIR, lang, 'index.ts')],
    bundle: true,
    format: 'esm',
    platform: 'node',
    // .mjs 扩展名：否则 node 会按 "typeless package.json" 警告后重新解析为 ESM（噪音 + 开销）
    outExtension: { '.js': '.mjs' },
    outbase: LOCALES_DIR,
    outdir: tmp,
    logLevel: 'silent',
  })
  const mod = await import(pathToFileURL(join(tmp, lang, 'index.mjs')).href)
  return flatten(mod.default)
}

const source = await loadLocale(SOURCE)
const sourceKeys = [...source.keys()]

if (LIST_MISSING) {
  if (!TARGETS.includes(LIST_MISSING)) {
    console.error(`--list-missing 只支持 ${TARGETS.join(' / ')}，收到：${LIST_MISSING}`)
    process.exit(1)
  }
  const target = await loadLocale(LIST_MISSING)
  const missing = sourceKeys.filter(k => !target.has(k))
  console.log(missing.join('\n'))
  console.error(`\n${LIST_MISSING}：待翻译 ${missing.length} / ${sourceKeys.length} 条`)
  process.exit(0)
}

const errors = []
const summary = []

for (const lang of TARGETS) {
  const target = await loadLocale(lang)

  const missingDomain = []
  const missingLegacy = []
  for (const key of sourceKeys) {
    if (target.has(key)) continue
    if (DOMAIN_PREFIXES.some(p => key.startsWith(p))) missingDomain.push(key)
    else missingLegacy.push(key)
  }
  // 反向：目标语言有、源语言没有 —— 通常是 key 改名后忘了清旧译文，同样算漂移
  const extra = [...target.keys()].filter(k => !source.has(k))

  if (missingDomain.length) {
    errors.push(
      `${lang} 语义域缺 ${missingDomain.length} 个键（必须与 zh-CN 一一对应；` +
        `缺键会让含 {占位符} 的文案显示成未插值的 key）：`,
    )
    for (const k of missingDomain) errors.push(`  ${k}`)
  }
  if (extra.length) {
    errors.push(`${lang} 有 ${extra.length} 个 zh-CN 不存在的键（key 改名后请同步清理）：`)
    for (const k of extra) errors.push(`  ${k}`)
  }
  summary.push({ lang, missingDomain: missingDomain.length, missingLegacy: missingLegacy.length, extra: extra.length })
}

const currentBaseline = Object.fromEntries(summary.map(s => [s.lang, s.missingLegacy]))

if (WRITE) {
  writeFileSync(BASELINE_PATH, JSON.stringify(currentBaseline, null, 2) + '\n', 'utf8')
  console.log(`legacy 待翻译基线已写入 ${relative(ROOT, BASELINE_PATH)}：` +
    summary.map(s => `${s.lang} ${s.missingLegacy}`).join('、'))
  process.exit(0)
}

if (!existsSync(BASELINE_PATH)) {
  console.error('缺少 scripts/i18n-keys-baseline.json —— 请先执行：node scripts/check-i18n-keys.mjs --write-baseline')
  process.exit(1)
}
const baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))

for (const s of summary) {
  const allowed = baseline[s.lang] ?? 0
  if (s.missingLegacy > allowed) {
    errors.push(
      `${s.lang} 的 legacy 待翻译数上升 —— ${s.missingLegacy}（基线 ${allowed}）。` +
        `新增文案请同时补 en-US / ar，或走语义域 key。`,
    )
  }
}

if (errors.length) {
  console.error('i18n 键集检查失败：')
  for (const line of errors) console.error(`  ${line}`)
  process.exit(1)
}

for (const s of summary) {
  const allowed = baseline[s.lang] ?? 0
  const note = s.missingLegacy < allowed ? `（本批新增译文 ${allowed - s.missingLegacy} 条，可执行 --write-baseline 锁定）` : ''
  console.log(`  ${s.lang}：语义域对齐 ✓，legacy 待翻译 ${s.missingLegacy} 条${note}`)
}
console.log('i18n 键集检查通过')
