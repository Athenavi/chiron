#!/usr/bin/env node
/**
 * 主题 token 契约检查。
 *
 * 背景：`src/style.css` 定义 4 套主题（notion 极简功能 / paper 暖纸阅读 / terminal 终端硬核 /
 * aurora 玻璃柔光 × light/dark），切换时全靠 CSS 变量生效。但组件里散落着硬编码色值
 * （`#fff`、`rgba(0,0,0,.04)` …）—— 它们在深色主题下不会跟着变，表现为"某些区域白底
 * 黑字、某些不是"这类**只有切主题才发现**的视觉不一致。
 *
 * ratchet 约束：存量记录在 BASELINE 里容忍，**新增硬编码即失败**。换成 `var(--*)` 后
 * 把对应数字下调来锁定成果（脚本不接受上调）。
 *
 * `--xxx: <值>` 形式的变量声明行不计入 —— 那是 token 的定义处，本就该有原始色值。
 *
 * 用法：node scripts/check-theme-token-contract.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/** 存量基线：文件 → 允许的硬编码色值数量。只允许下调。 */
const BASELINE = {
  'src/components/AgentCollabPanel.vue': 1,
  'src/components/AppLayout.vue': 4,
  'src/components/CallChainTimeline.vue': 5,
  'src/components/CaptchaWidget.vue': 1,
  'src/components/CommandPalette.vue': 2,
  'src/components/KBSearchResults.vue': 8,
  'src/components/SsoLoginButtons.vue': 9,
  'src/components/WorkflowDAGEditor.vue': 19,
  'src/components/chat/AskCard.vue': 1,
  'src/components/chat/ChatEmptyHero.vue': 1,
  'src/components/chat/ChatInput.vue': 2,
  'src/components/chat/ChatSidePanel.vue': 1,
  'src/components/chat/MessageItem.vue': 5,
  'src/components/chat/MessageList.vue': 1,
  'src/components/common/ImageViewer.vue': 5,
  'src/components/home/HomeScene3D.vue': 5,
  'src/style.css': 1,
  'src/views/AgentsView.vue': 4,
  'src/views/BillingView.vue': 3,
  'src/views/ChatView.vue': 8,
  'src/views/HomeView.vue': 4,
  'src/views/KnowledgeDetailView.vue': 2,
  'src/views/LoginView.vue': 1,
  'src/views/MediaView.vue': 13,
  'src/views/MemoryView.vue': 1,
  'src/views/PluginsView.vue': 3,
  'src/views/ProfileView.vue': 6,
  'src/views/RegisterView.vue': 1,
  'src/views/ShareView.vue': 1,
  'src/views/WorkflowView.vue': 30,
  'src/views/admin/CacheView.vue': 3,
  'src/views/admin/DashboardView.vue': 13,
  'src/views/admin/EvalView.vue': 2,
  'src/views/admin/Layout.vue': 3,
  'src/views/admin/OAuthProvidersView.vue': 1,
}

/** 颜色字面量：十六进制、rgb(a)、hsl(a) */
const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/g
/** token 定义行（`--name: value`）—— 定义处本就该写原始色值 */
const TOKEN_DEFINITION = /^\s*--[a-z0-9-]+\s*:/
/** `var(--x, <fallback>)` 的兜底值合法 —— 整段摘掉再匹配，否则会误报（实测 63 处） */
const VAR_WITH_FALLBACK = /var\(--[^)]*\)/g

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...walk(full))
    else if (/\.(vue|css)$/.test(entry)) out.push(full)
  }
  return out
}

const counts = new Map()
for (const file of walk(join(ROOT, 'src'))) {
  let n = 0
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    if (TOKEN_DEFINITION.test(line)) continue
    n += (line.replace(VAR_WITH_FALLBACK, 'VAR').match(COLOR_LITERAL) || []).length
  }
  if (n > 0) counts.set(relative(ROOT, file).split('\\').join('/'), n)
}

const grown = []
for (const [file, n] of counts) {
  const allowed = BASELINE[file] ?? 0
  if (n > allowed) grown.push(`${file}: ${n} 处硬编码色值（基线 ${allowed}）`)
}

for (const file of Object.keys(BASELINE)) {
  if (!counts.has(file)) console.log(`基线可下调（该文件已清零）：${file}`)
}

if (grown.length) {
  console.error('主题 token 契约失败 —— 请改用 src/style.css 的 CSS 变量：')
  for (const line of grown) console.error(`  ${line}`)
  console.error('')
  console.error('常用：--text-primary/--text-secondary/--text-tertiary · --bg-page/--bg-card/--bg-hover')
  console.error('      --border · --primary · --shadow-* · --bubble-user')
  process.exit(1)
}

const total = [...counts.values()].reduce((a, b) => a + b, 0)
console.log(`主题 token 契约通过（存量硬编码 ${total} 处，分布在 ${counts.size} 个文件）`)
