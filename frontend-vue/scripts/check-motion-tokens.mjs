#!/usr/bin/env node
/**
 * 动效 token 契约检查。
 *
 * 背景：`src/style.css` 早已定义 `--dur-fast: 100ms` / `--dur-normal: 200ms` /
 * `--dur-slow: 350ms` 与 `--ease-*`，但组件里仍散落着 127 处硬编码时长
 * （`transition: opacity 0.2s`）。后果是**动效节奏无法统一调整** —— 想放慢整体动效、
 * 或按 `prefers-reduced-motion` 做降级，都得逐处去改。
 *
 * ratchet 约束：存量记录在 BASELINE 里容忍，**新增硬编码时长即失败**。换成
 * `var(--dur-*)` 后下调对应数字（脚本不接受上调）。
 *
 * 口径说明：按**行**计数 —— 一行里出现至少一个非零硬编码时长即算一处，
 * 因为这一行就是需要改的地方。
 *
 * 用法：node scripts/check-motion-tokens.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))

/** 存量基线：文件 → 允许的硬编码动效行数。只允许下调。 */
const BASELINE = {
  'src/App.vue': 2,
  'src/components/AgentCollabPanel.vue': 4,
  'src/components/AppLayout.vue': 10,
  'src/components/CallChainTimeline.vue': 3,
  'src/components/CaptchaWidget.vue': 1,
  'src/components/CollapseTransition.vue': 1,
  'src/components/CommandPalette.vue': 5,
  'src/components/KBSearchResults.vue': 4,
  'src/components/SkillMarketCard.vue': 1,
  'src/components/SsoLoginButtons.vue': 1,
  'src/components/ThemeSwitcher.vue': 3,
  'src/components/WorkflowDAGEditor.vue': 4,
  'src/components/chat/AskCard.vue': 1,
  'src/components/chat/ChatEmptyHero.vue': 2,
  'src/components/chat/ChatSidePanel.vue': 14,
  'src/components/chat/MessageItem.vue': 11,
  'src/components/chat/MessageList.vue': 3,
  'src/components/chat/ToolCallCard.vue': 1,
  'src/components/common/ImageViewer.vue': 1,
  'src/components/common/RouteProgressBar.vue': 1,
  'src/style.css': 5,
  'src/views/AgentsView.vue': 1,
  'src/views/ChatView.vue': 9,
  'src/views/HomeView.vue': 13,
  'src/views/KnowledgeDetailView.vue': 1,
  'src/views/KnowledgeView.vue': 1,
  'src/views/LoginView.vue': 1,
  'src/views/MediaView.vue': 1,
  'src/views/PluginsView.vue': 1,
  'src/views/ProfileView.vue': 1,
  'src/views/RegisterView.vue': 1,
  'src/views/ShareView.vue': 1,
  'src/views/SkillsView.vue': 1,
  'src/views/WorkflowView.vue': 4,
  'src/views/admin/DashboardView.vue': 2,
  'src/views/admin/GroupsView.vue': 1,
  'src/views/admin/Layout.vue': 3,
}

/** transition / animation 声明里的时长字面量 */
const DURATION = /(\d+(?:\.\d+)?)(m?s)/g
/** 已经走 token 的行不算违规 */
const USES_TOKEN = /var\(--dur/

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
    if (!/transition:|animation:/.test(line) || USES_TOKEN.test(line)) continue
    // 纯 0s / 0ms 表示"不动画"，是合法写法
    const positive = [...line.matchAll(DURATION)].some(m => parseFloat(m[1]) > 0)
    if (positive) n++
  }
  if (n > 0) counts.set(relative(ROOT, file).split('\\').join('/'), n)
}

const grown = []
for (const [file, n] of counts) {
  const allowed = BASELINE[file] ?? 0
  if (n > allowed) grown.push(`${file}: ${n} 处硬编码动效时长（基线 ${allowed}）`)
}

for (const file of Object.keys(BASELINE)) {
  if (!counts.has(file)) console.log(`基线可下调（该文件已清零）：${file}`)
}

if (grown.length) {
  console.error('动效 token 契约失败 —— 请改用 src/style.css 的时长 token：')
  for (const line of grown) console.error(`  ${line}`)
  console.error('')
  console.error('--dur-fast: 100ms（颜色/边框 hover、tooltip）')
  console.error('--dur-normal: 200ms（popover、菜单、小入场）')
  console.error('--dur-slow: 350ms（抽屉、模态、面板滑动）')
  console.error('缓动：--ease-out / --ease-in-out')
  process.exit(1)
}

const total = [...counts.values()].reduce((a, b) => a + b, 0)
console.log(`动效 token 契约通过（存量硬编码 ${total} 处，分布在 ${counts.size} 个文件）`)
