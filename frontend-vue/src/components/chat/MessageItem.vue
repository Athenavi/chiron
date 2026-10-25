<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github.css'
// P3-A: 暗色模式下的代码高亮配色覆盖（避免引入两个冲突的 hljs 主题）
import texmath from 'markdown-it-texmath'
import katex from 'katex'
import { computed, ref, watch, onMounted, onUpdated, onBeforeUnmount } from 'vue'
import { Input } from 'ant-design-vue'
import { notify } from '../../utils/notify'
import { fileTypeBadge, splitFileName, type SplitName } from '../../utils/fileName'
import { CopyOutlined, CheckOutlined, EditOutlined, ReloadOutlined, LikeOutlined, DislikeOutlined, LikeFilled, DislikeFilled, CommentOutlined } from '@ant-design/icons-vue'
import ReasoningBlock from './ReasoningBlock.vue'
import { inkCharsFor, inkIndexFor } from './ink'
import ToolCallCard from './ToolCallCard.vue'
import ToolResultBlock from './ToolResultBlock.vue'
import { renderMermaidBlocks } from './mermaidRenderer'
import { highlightCodeBlocks } from './codeHighlighter'
import { openLightboxFrom, openLightboxFromList } from '../common/lightboxTransition'
import type { ChatItem, ToolCallItem, ToolResultItem, TextItem, ChatAttachment } from './chat-types'
import { formatSize } from './chat-types'
import { resolveMediaUrl } from '../../api'
import { useRouter } from 'vue-router'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const props = defineProps<{
  item: ChatItem
  anchorKey?: number
  highlighted?: boolean
  /** 是否播放一次入场动效。由父组件按「新到的 id」门控 ——
   *  transcript 是窗口化的，滚出再滚回会重新挂载行，不门控就会反复重播。 */
  entering?: boolean
}>()

/** 正文容器：mermaid 渲染需要拿到已挂载的 DOM */
const msgTextRef = ref<HTMLElement | null>(null)

/**
 * 水墨逐字：流式期把正文拆成单字，让每个新字符"从模糊到对焦"。
 *
 * 两条硬约束：
 *  1. **≤ 2000 字才拆** —— 逐字 DOM 数量与字符数线性相关，长回答会拖慢滚动；
 *     超过就退回整条插值（长回答本就在快速滚动，逐字反而不易读）。
 *  2. **只为本批新增字符计时** —— `--i = min(i - batchStart, 12)`：
 *     否则第 500 个字要等 14 秒才出现；上限 12 档让"一次返回一大段"也在 ~340ms 内铺开。
 */
const inkChars = computed(() => {
  if (!('streaming' in props.item) || !props.item.streaming) return null
  return inkCharsFor(('content' in props.item ? props.item.content : '') || '')
})
const inkBatchStart = ref(0)
watch(
  () => ('content' in props.item ? props.item.content : '') || '',
  (now, before) => {
    const prev = (before || '').length
    inkBatchStart.value = (now || '').length > prev ? prev : 0
  },
)
function inkIndex(i: number): number {
  return inkIndexFor(i, inkBatchStart.value)
}

/**
 * "约 N 分钟后自动恢复"里的 N（分钟）。
 *
 * 秒数来自后端的 `params.retry_after_seconds` —— 它是**算出来的**（stale 阈值 + 一次
 * 巡检间隔），不是猜的。只对 `session_busy` 显示：别的失败原因不存在"等一会儿就好"。
 */
const repairWaitSeconds = computed(() => {
  const it = props.item
  if (!('errorCode' in it) || it.errorCode !== 'session_busy') return 0
  const sec = 'errorRetryAfterSec' in it ? Number(it.errorRetryAfterSec || 0) : 0
  return sec > 0 ? Math.ceil(sec) : 0
})

/**
 * 等待倒计时（秒）。
 *
 * 为什么要有它：`session_busy` 的等待秒数是**后端算出来的**（stale 阈值 + 一次巡检间隔），
 * 但只显示"约 N 分钟"时用户无法判断"现在点了会不会又是白点一下"。做成秒级倒计时后，
 * 归零那一刻他立刻知道可以重试了 —— 这也是这条提示存在的全部意义。
 * 手动重试按钮**始终可用**（我们不做静默自动重发）：倒计时只是解释，不是闸门。
 */
const waitLeft = ref(0)
let waitTimer: ReturnType<typeof setInterval> | null = null
function stopWaitTimer() {
  if (waitTimer) { clearInterval(waitTimer); waitTimer = null }
}
watch(
  repairWaitSeconds,
  (sec) => {
    stopWaitTimer()
    waitLeft.value = sec
    if (sec <= 0) return
    waitTimer = setInterval(() => {
      waitLeft.value = Math.max(0, waitLeft.value - 1)
      if (waitLeft.value === 0) stopWaitTimer()
    }, 1000)
  },
  { immediate: true },
)
/** 倒计时文案 m:ss；归零后返回空串（此时只留下"重试"按钮，不再啰嗦）。 */
const waitLabel = computed(() => {
  const s = waitLeft.value
  if (s <= 0) return ''
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
})

const emit = defineEmits<{
  /** 用户消息编辑后重发：删除该消息及其后所有消息，用新文本重发 */
  (e: 'retry-from', itemId: string, text: string): void
  /** P1-1 助手消息重新生成：删除该消息及其后所有消息，用上一条用户消息重发 */
  (e: 'regenerate', itemId: string): void
  /** P2-F 停止后继续生成 */
  (e: 'continue', itemId: string): void
  /** 失败消息重试：用原文本重发 */
  (e: 'retry-failed', itemId: string): void
  /** 引用整条消息到输入框（与选中文本引用同一出口） */
  (e: 'quote', text: string): void
  /** 手动修复卡住的会话（同会话还有一轮没收口时，"重试"必然再撞同一道闸门） */
  (e: 'repair-session', itemId: string): void
}>()

// ── 反向定位：消息 → 来源工作台 chips（assistant metadata 驱动）──
const router = useRouter()

interface SourceChip {
  key: string
  kind: 'kb' | 'workflow' | 'agent' | 'trace'
  label: string
  title: string
  go: () => void
}

const sourceChips = computed<SourceChip[]>(() => {
  if (props.item.kind !== 'text' || props.item.role !== 'assistant') return []
  // metadata 已收进 TextItem（此前这里靠 `as any` 绕过，接口缺字段）—— 只有 python
  // 引擎链路会带，Go 网关路径无此字段，故仍要兜底空对象
  const meta: Record<string, unknown> = props.item.metadata || {}
  const chips: SourceChip[] = []
  const kbId = typeof meta.kb_id === 'string' && meta.kb_id ? meta.kb_id : ''
  if (kbId) {
    chips.push({
      key: 'kb', kind: 'kb', label: t('knowledge.knowledge_base'),
      title: t('knowledge.source_knowledge_base_id_click_to_open', { id: kbId }),
      go: () => router.push(`/knowledge/${encodeURIComponent(kbId)}`),
    })
  }
  const wfId = typeof meta.workflow_id === 'string' && meta.workflow_id ? meta.workflow_id : ''
  if (wfId) {
    chips.push({
      key: 'workflow', kind: 'workflow', label: t('workflow.workflow'),
      title: t('workflow.source_workflow_id_click_to_open', { id: wfId }),
      go: () => router.push({ path: '/workflow', query: { id: wfId } }),
    })
  }
  const agentId = typeof meta.agent_id === 'string' && meta.agent_id ? meta.agent_id : ''
  if (agentId) {
    chips.push({
      key: 'agent', kind: 'agent', label: 'Agent',
      title: t('agent.source_agent_id_click_to_open', { id: agentId }),
      go: () => router.push('/agents'),
    })
  }
  // 追踪：metadata.trace_id 存在时显示「追踪」chip，点击复制 trace_id
  // （/admin/traces 为占位目标，暂不跳转，仅复制）
  const traceId = typeof meta.trace_id === 'string' && meta.trace_id ? meta.trace_id : ''
  if (traceId) {
    chips.push({
      key: 'trace', kind: 'trace', label: t('common.trace_2'),
      title: t('common.trace_id_click_to_copy', { id: traceId }),
      go: () => {
        navigator.clipboard.writeText(traceId)
          .then(() => notify.success(t('common.trace_id_copied')))
          .catch(() => notify.error(t('errors.copy_failed')))
      },
    })
  }
  return chips
})

// ── 安全改造：附件签名 URL 解析 ──
// 附件若为 /media/ 公开路径，渲染前异步解析为短时效签名 URL（12 分钟本地缓存）；
// 非 /media/ 前缀（绝对/签名/data:）原样；解析失败或加载失败回退原 url。
const attachmentResolved = ref<Record<string, string>>({})

/**
 * 附件名的双向中间截断（CDS v1/02 §2.5.3）：预先按 id 拆好，
 * 免得模板里对同一个名字调两次纯函数。
 */
const attachmentNameParts = computed<Record<string, SplitName>>(() => {
  const out: Record<string, SplitName> = {}
  if (props.item.kind !== 'text') return out
  for (const att of props.item.attachments ?? []) out[att.id] = splitFileName(att.name)
  return out
})

function attName(id: string): SplitName {
  return attachmentNameParts.value[id] ?? { head: '', tail: '' }
}

function attachmentUrl(att: ChatAttachment): string {
  return attachmentResolved.value[att.id] || att.url
}

async function resolveAttachmentUrls() {
  const item = props.item
  if (item.kind !== 'text' || !item.attachments?.length) return
  const targets = item.attachments.filter(a => a.url?.startsWith('/media/') && !attachmentResolved.value[a.id])
  if (!targets.length) return
  const results = await Promise.all(targets.map(async a => {
    const url = await resolveMediaUrl({ id: a.id, file_url: a.url })
    return [a.id, url] as const
  }))
  for (const [id, url] of results) {
    if (url) attachmentResolved.value[id] = url
  }
}

const attachmentUnwatch = watch(() => props.item, () => { void resolveAttachmentUrls() }, { immediate: true, deep: true })

onBeforeUnmount(() => {
  attachmentUnwatch()
  if (copiedTimer) clearTimeout(copiedTimer)
  stopWaitTimer()
})

function onAttachmentImgError(att: ChatAttachment) {
  // 签名 URL 加载失败/过期 → 回退原始公开路径（旧路径仍保留，仅不再作为新渲染首选）
  if (attachmentResolved.value[att.id] && attachmentResolved.value[att.id] !== att.url) {
    attachmentResolved.value[att.id] = ''
  }
}

// 消息时间（hover 淡入显示，deepseek MessageIconActions timeEnd）
// S 修复：历史消息用落库 created_at（item.time），实时消息缺省时取当前时间
const timeLabel = computed(() => {
  if (props.item.time) return props.item.time
  const now = new Date()
  return `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}`
})

/** 复制成功的**就地**反馈（CDS v1 §4.1.1 / §4.2.4）：图标画成对勾并驻留 2.5s，
 *  再按 `saved-check-decay` 缩一点淡出。刻意不再弹 toast —— 光标就在按钮上，
 *  弹窗反而是二次打扰（代码块里的「复制」有自己的文案反馈，不受影响）。 */
const copied = ref(false)
let copiedTimer: ReturnType<typeof setTimeout> | null = null

async function copyMessage() {
  if (props.item.kind !== 'text') return
  try {
    await navigator.clipboard.writeText(props.item.content)
    copied.value = true
    if (copiedTimer) clearTimeout(copiedTimer)
    // 2500ms = style.css 的 --dur-check-decay（TS 侧读不到 CSS 变量，故此处保持一致的值）
    copiedTimer = setTimeout(() => { copied.value = false }, 2500)
  } catch {
    notify.error(t('errors.copy_failed'))
  }
}

// ── P1-1 消息重试/重新生成 ──
// 用户消息：进入编辑模式，修改后"重发"会删除该消息及其后所有消息，用新文本重发
const editing = ref(false)
const editDraft = ref('')
const editInputRef = ref()

function startEdit() {
  if (props.item.kind !== 'text' || props.item.role !== 'user') return
  editDraft.value = props.item.content
  editing.value = true
  // 下一帧聚焦 textarea
  requestAnimationFrame(() => editInputRef.value?.focus?.())
}

function cancelEdit() {
  editing.value = false
  editDraft.value = ''
}

function confirmEdit() {
  if (props.item.kind !== 'text') return
  const text = editDraft.value.trim()
  if (!text) return
  editing.value = false
  emit('retry-from', props.item.id!, text)
}

// 助手消息：重新生成（删除该消息及其后所有，用上一条用户消息重发）
function regenerate() {
  if (props.item.kind !== 'text' || props.item.role !== 'assistant') return
  emit('regenerate', props.item.id!)
}

// 失败消息：一键重试（用原文本重发）
function retryFailed() {
  if (props.item.kind !== 'text') return
  emit('retry-failed', props.item.id!)
}

// ── P3-B 长消息折叠 ──
// 助手消息超过阈值时默认折叠，显示前 N 行 + "展开全部"按钮
const COLLAPSE_THRESHOLD = 2000  // 字符数阈值：只用来决定"要不要显示折叠按钮"
/**
 * 折叠态改用**纯 CSS 裁切 + 底边羽化**（CDS v1 §3.1.1），不再 `slice()` 伪造截断。
 *
 * 旧做法把正文截到 600 字符、再在末尾追加一句"已折叠，点击展开"，有三个问题：
 * ① 用户看到的不是正文（复制、页面搜索都对不上）；② Markdown 结构会断在半句上
 * （代码块/表格直接坏掉）；③ 高度随内容浮动，对 transcript 的窗口化测量不友好。
 * 现在整段照常渲染，折叠只由 `.msg-text.collapsed` 的 `max-height: 300px` + `mask-image`
 * 承担 —— 折叠高度**恒定为 300px**，边缘是羽化而不是硬切。
 */
const collapsed = ref(true)
const isLongMessage = computed(() => {
  if (props.item.kind !== 'text' || props.item.role !== 'assistant') return false
  return props.item.content.length > COLLAPSE_THRESHOLD
})
const displayContent = computed(() => (props.item.kind === 'text' ? props.item.content : ''))

// ── P2-D 消息反馈（👍/👎） ──
// 'up' | 'down' | null；切换反馈，再次点击同方向取消
const feedback = ref<'up' | 'down' | null>(null)
function setFeedback(dir: 'up' | 'down') {
  feedback.value = feedback.value === dir ? null : dir
  // 设计说明：反馈上报留待模型评估链路接入后端 API
  if (feedback.value) notify.success(feedback.value === 'up' ? t('common.thanks_for_the_upvote') : t('common.your_feedback_has_been_recorded'))
}

// ── Markdown 引擎（迁移自原 ChatView） ──
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.use(texmath, { engine: katex, delimiters: 'dollars', katexOptions: { throwOnError: false, output: 'html' } })
/**
 * 外链一律新窗口打开：SPA 内直接跳走会丢掉当前对话（消息状态、滚动位置、
 * 正在流的回合）。站内相对链接保持原行为，交给应用自身处理。
 */
// 签名沿用 markdown-it 的 RenderRule（去掉注解即可由 rules.<name> 推导），不再写 any[]
/** 代码块复制按钮的两个图标。
 *  代码块是 `md.renderer` 生成的字符串（v-html），挂不上 antd 图标组件，只能内联 SVG；
 *  12px 与按钮字号一致，垂直对齐交给 flex（不走 baseline，避免与文字错位）。 */
const ICON_COPY = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M6 2.75h5.25A1.75 1.75 0 0 1 13 4.5V10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><rect x="3" y="5.5" width="7.5" height="9" rx="1.75" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'
const ICON_CHECK = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M3.2 8.6 6.4 11.8 12.8 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
/** 每个复制按钮的恢复定时器：连点两次时旧定时器不能提前把新状态收回去。 */
const copyTimers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>()

md.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
  const token = tokens[idx]
  const href = (token?.attrGet?.('href') as string | null) || ''
  if (/^https?:\/\//i.test(href)) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return self.renderToken(tokens, idx, options)
}
md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx]
  const lang = (token.info || '').trim().toLowerCase()
  const code = token.content
  if (lang === 'mermaid') {
    // 先输出原始源码（未渲染/渲染失败时可读），data-code 供渲染器取用
    return `<div class="mermaid" data-code="${encodeURIComponent(code)}">${md.utils.escapeHtml(code)}</div>`
  }
  // 代码块先输出纯转义文本（带语言标记），高亮由 codeHighlighter 在挂载后按需增强：
  // 首屏不必为用不到的 highlight.js 付体积，高亮失败也不影响代码可读可复制
  const safeLang = md.utils.escapeHtml(lang || 'code')
  const encoded = encodeURIComponent(code)
  const lineCount = code.replace(/\n$/, '').split('\n').length
  const collapsible = lineCount > CODE_COLLAPSE_LINES
  const header = `<div class="code-block-header"><span class="code-lang">${safeLang}</span><span class="code-lines">${t('common.n_lines', { n: lineCount })}</span><button class="code-copy-btn" type="button" data-code="${encoded}">${ICON_COPY}<span class="code-copy-label">${t('common.copy')}</span></button></div>`
  const body = `<pre><code class="language-${safeLang}" data-lang="${safeLang}">${md.utils.escapeHtml(code)}</code></pre>`
  const toggle = collapsible
    ? `<button class="code-expand-btn" type="button" data-lines="${lineCount}">${t('common.expand_all_n_lines_total', { n: lineCount })}</button>`
    : ''
  return `<div class="code-block-wrapper"${collapsible ? ' data-collapsed="1"' : ''}>${header}${body}${toggle}</div>`
}

/** 超过该行数默认折叠：长代码会挤掉整屏消息，先给一段可读的预览 */
const CODE_COLLAPSE_LINES = 28

// P 性能：图片懒加载（长列表/历史中大量图片不阻塞首屏，滚动到才加载）
md.renderer.rules.image = (tokens, idx) => {
  const token = tokens[idx]
  const src = md.utils.escapeHtml(token.attrGet('src') || '')
  const alt = md.utils.escapeHtml(token.content || '')
  return `<img src="${src}" alt="${alt}" loading="lazy" decoding="async" />`
}

function renderMarkdown(src: string): string {
  // P 性能：LRU 缓存渲染结果（同内容消息不重复 markdown + 高亮）
  const hit = mdCache.get(src)
  if (hit !== undefined) return hit
  let out: string
  try {
    out = DOMPurify.sanitize(md.render(src), {
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'del', 'sup', 'sub', 'img', 'button'],
      ALLOWED_ATTR: ['href', 'target', 'rel', 'class', 'src', 'alt', 'loading', 'decoding', 'data-code'],
      ALLOW_DATA_ATTR: true,
      ADD_ATTR: ['target'],  // 允许 a 标签的 target 属性
      FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'select', 'marquee', 'link', 'meta'],
      FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur', 'onchange', 'onsubmit', 'onreset', 'onkeydown', 'onkeyup', 'onkeypress'],
    })
  } catch {
    out = md.utils.escapeHtml(src)
  }
  if (mdCache.size >= MD_CACHE_MAX) {
    const firstKey = mdCache.keys().next().value
    if (firstKey !== undefined) mdCache.delete(firstKey)
  }
  mdCache.set(src, out)
  return out
}

const mdCache = new Map<string, string>()
const MD_CACHE_MAX = 300

// ── 长内容的渲染让位 ─────────────────────────────────────────
// markdown + sanitize 对超长正文是重活：同步做会占住主线程，输入与滚动都卡一下。
// 这里只在超长时让位（先显示纯文本，空闲后再渲染替换）；普通长度仍同步渲染，
// 以免每条消息都"先纯文本后格式"地闪一下。
const LARGE_MESSAGE_CHARS = 30000
const renderedHtml = ref('')
const pendingLargeRender = ref(false)
let renderToken = 0

/** 是否正在流式（只有 text 条目有这个概念）。 */
const isStreaming = computed(
  () => props.item.kind === 'text' && Boolean((props.item as TextItem).streaming),
)

function scheduleRender() {
  const src = displayContent.value
  const token = ++renderToken

  // ── 流式期早退（2026-10-09）─────────────────────────────────
  // 模板在 `item.streaming` 时显示的是**纯文本**（`{{ displayContent }}`），
  // `renderedHtml` 只在回合结束、`streaming` 变 false 之后才被 `v-html` 用上
  // ⇒ 流式期每个 delta 都跑一遍 `md.render` + `DOMPurify.sanitize` 是**纯浪费**
  // （还会往 `mdCache` 里塞一堆用不到的半成品，把真正有用的条目挤出去）。
  //
  // ⚠ 注意：`displayContent` **不依赖 `streaming`**（它只看 `content` 与折叠状态），
  // 所以 watcher 必须**同时监听 `isStreaming`** —— 否则"流式结束"那一刻不会重算，
  // 消息会**一直停在纯文本**（这正是本次改动最容易引入的回归）。
  if (isStreaming.value) {
    pendingLargeRender.value = false
    return
  }

  if (!src) {
    renderedHtml.value = ''
    pendingLargeRender.value = false
    return
  }

  const cached = mdCache.get(src)
  if (cached !== undefined) {
    renderedHtml.value = cached
    pendingLargeRender.value = false
    return
  }

  if (src.length <= LARGE_MESSAGE_CHARS) {
    renderedHtml.value = renderMarkdown(src)
    pendingLargeRender.value = false
    return
  }

  pendingLargeRender.value = true
  const run = () => {
    if (token !== renderToken) return   // 让位期间内容又变了：本次作废
    renderedHtml.value = renderMarkdown(src)
    pendingLargeRender.value = false
  }
  if (typeof requestIdleCallback === 'function') requestIdleCallback(run, { timeout: 400 })
  else setTimeout(run, 0)
}

watch([displayContent, isStreaming], scheduleRender, { immediate: true })

function handleMsgClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const toggle = target.closest('.code-expand-btn') as HTMLElement | null
  if (toggle) {
    const box = toggle.closest('.code-block-wrapper') as HTMLElement | null
    if (box) {
      const collapsed = box.dataset.collapsed === '1'
      box.dataset.collapsed = collapsed ? '0' : '1'
      const lines = toggle.dataset.lines || ''
      toggle.textContent = collapsed ? t('common.collapse_n_lines_total', { n: lines }) : t('common.expand_all_n_lines_total', { n: lines })
    }
    return
  }
  const btn = target.closest('.code-copy-btn') as HTMLElement | null
  if (btn) {
    const code = decodeURIComponent(btn.dataset.code || '')
    if (!code) return
    navigator.clipboard.writeText(code).then(() => {
      // 与消息级复制同一套状态机（CDS v1 §5.5.4）：图标画成对勾、文案改"已复制"，
      // 2.5s 后由 CSS 的 saved-check-decay 淡出，再恢复原状。
      const prev = copyTimers.get(btn)
      if (prev) clearTimeout(prev)
      btn.classList.add('copied')
      btn.innerHTML = `${ICON_CHECK}<span class="code-copy-label">${t('common.copied_2')}</span>`
      copyTimers.set(btn, setTimeout(() => {
        btn.classList.remove('copied')
        btn.innerHTML = `${ICON_COPY}<span class="code-copy-label">${t('common.copy')}</span>`
        copyTimers.delete(btn)
      }, 2500))
    }).catch(() => { /* clipboard not available */ })
    return
  }
  // 正文里的图片（markdown 图片由 v-html 生成，挂不上 Vue 事件）交查看器放大。
  // 把被点的 <img> 一起传进去：灯箱的共享元素形变要从它长出来（CDS v1 §5.2.2）。
  const img = target.closest('img') as HTMLImageElement | null
  if (img?.src) {
    // 同一条消息里的图片构成一个图集：正文有多张时可直接左右切换，
    // 而不是"关掉再看下一张"。作用域收在正文块内，避免把相邻消息的图片也卷进来。
    const scope = img.closest('.msg-text') ?? img.closest('.msg-row')
    const all = scope ? Array.from(scope.querySelectorAll('img')).filter(i => !!i.src) : []
    const at = all.indexOf(img)
    void openLightboxFromList(
      img,
      all.length > 1 ? all.map(i => ({ src: i.src, alt: i.alt })) : [{ src: img.src, alt: img.alt }],
      at > 0 ? at : 0,
    )
  }
}

/** 附件缩略图 → 灯箱：图片附件构成图集，音频/文件附件不参与切换。 */
function openAttachment(att: ChatAttachment, e: MouseEvent) {
  const el = e.currentTarget as HTMLElement
  const all = (props.item.kind === 'text' ? props.item.attachments : undefined)?.filter(a => a.isImage) ?? []
  const images = all.map(a => ({ src: attachmentUrl(a), alt: a.name }))
  const at = all.findIndex(a => a.id === att.id)
  if (images.length > 1) void openLightboxFromList(el, images, at > 0 ? at : 0)
  else void openLightboxFrom(el, { src: attachmentUrl(att), alt: att.name })
}

/**
 * 正文增强（图表渲染 + 代码高亮）：只在 DOM 完成 patch 后触发，内容变化由 onUpdated 覆盖。
 * 两者都是纯增强 —— 加载或渲染失败时正文保持可读，不影响消息本身。
 */
function enhanceContent() {
  const host = msgTextRef.value
  if (!host) return
  if (props.item.kind !== 'text' || (props.item as TextItem).streaming) return
  const dark = document.documentElement.classList.contains('dark')
  void renderMermaidBlocks(host, { dark })
  void highlightCodeBlocks(host)
}

onMounted(enhanceContent)
onUpdated(enhanceContent)
</script>

<template>
  <div
    v-if="item.kind === 'text'"
    v-bind="$attrs"
    class="msg-row"
    :class="[item.role, { streaming: item.streaming, highlighted: highlighted, 'msg-error': (item as TextItem).error, 'msg-enter': entering }]"
    :data-chat-anchor-key="anchorKey"
    data-time-hover-root
    data-click-delegate
    @click="handleMsgClick"
  >
    <div
      class="msg-content"
      :aria-live="item.streaming ? 'polite' : 'off'"
      :aria-busy="item.streaming"
    >
      <!-- 编辑模式（用户消息）：textarea + 保存/取消 -->
      <div
        v-if="editing"
        class="msg-edit"
      >
        <Input.TextArea
          ref="editInputRef"
          v-model:value="editDraft"
          :auto-size="{ minRows: 1, maxRows: 8 }"
          class="edit-textarea"
          @keydown.enter.exact.prevent="confirmEdit"
          @keydown.esc.prevent="cancelEdit"
        />
        <div class="edit-actions">
          <button
            class="edit-btn cancel"
            type="button"
            @click="cancelEdit"
          >
            {{ $t('common.cancel') }}
          </button>
          <button
            class="edit-btn save"
            type="button"
            @click="confirmEdit"
          >
            {{ $t('common.save_and_send') }}
          </button>
        </div>
      </div>
      <template v-else>
        <!-- P 性能：流式期间纯文本渲染（零 markdown 开销），完成后一次性 markdown + 缓存 -->
        <div
          v-if="item.streaming"
          class="msg-text streaming-text"
        >
          <!-- 水墨逐字：≤2000 字才拆；超过退回整条插值（见 script 里的注释） -->
          <template v-if="inkChars">
            <span
              v-for="(ch, i) in inkChars"
              :key="i"
              class="ink"
              :style="{ '--i': inkIndex(i) }"
            >{{ ch }}</span>
          </template>
          <template v-else>{{ item.content }}</template>
        </div>
        <!-- 超长正文让位期间先给纯文本，避免空白；渲染完成后由下方 v-html 分支接管 -->
        <div
          v-else-if="pendingLargeRender"
          class="msg-text streaming-text"
          :class="{ collapsed: isLongMessage && collapsed }"
        >
          {{ displayContent }}
        </div>
        <div
          v-else
          ref="msgTextRef"
          class="msg-text"
          :class="{ collapsed: isLongMessage && collapsed }"
          v-html="renderedHtml"
        />
        <!-- P3-B: 长消息展开/折叠按钮 -->
        <button
          v-if="isLongMessage"
          class="collapse-toggle btn-sm"
          type="button"
          @click.stop="collapsed = !collapsed"
        >
          {{ collapsed ? $t('common.expand_all') : $t('common.collapse') }}
        </button>
        <!-- 附件展示：图片内联，文件显示卡片 -->
        <div
          v-if="(item as TextItem).attachments?.length"
          class="msg-attachments"
        >
          <template
            v-for="att in (item as TextItem).attachments"
            :key="att.id"
          >
            <img
              v-if="att.isImage"
              :src="attachmentUrl(att)"
              :alt="att.name"
              class="msg-attachment-img"
              loading="lazy"
              @error="onAttachmentImgError(att)"
              @click.stop="openAttachment(att, $event)"
            >
            <div
              v-else-if="att.isAudio"
              class="msg-attachment-audio"
            >
              <audio
                :src="attachmentUrl(att)"
                controls
                preload="metadata"
                class="msg-audio-player"
              >
                {{ $t('media.your_browser_does_not_support_audio_playback') }}
              </audio>
              <span class="att-audio-name">{{ att.name }}</span>
            </div>
            <a
              v-else
              :href="attachmentUrl(att)"
              :download="att.name"
              :title="att.name"
              class="msg-attachment-file"
            >
              <!-- 类型徽标取代通用文件图标：TS / PY / PDF 一眼可辨（CDS v1/02 §2.5.4） -->
              <span class="file-type-badge">{{ fileTypeBadge(att.name) }}</span>
              <!-- 双向中间截断：让位只发生在中间，扩展名与尾部词干永远可见（§2.5.3）。
                   两段紧挨着写在同一行，视觉上仍是一个连续的文件名。 -->
              <span class="att-name">
                <span class="att-name-head">{{ attName(att.id).head }}</span><span class="att-name-tail">{{ attName(att.id).tail }}</span>
              </span>
              <span class="att-size">{{ formatSize(att.size) }}</span>
            </a>
          </template>
        </div>
        <!-- 子 Agent 自动轮注入的消息：标出来源，别让用户以为是自己发的提问
             （后端 messages.source === 'subagent_followup'） -->
        <div
          v-if="item.role === 'user' && (item as TextItem).source === 'subagent_followup'"
          class="followup-badge"
        >
          {{ $t('workflow.subtask_complete_main_agent_auto_summarizes') }}
        </div>
        <!-- 反向定位：来源工作台 chips（kb_id / workflow_id / agent_id，metadata 驱动） -->
        <div
          v-if="item.role === 'assistant' && sourceChips.length"
          class="source-chips"
        >
          <a
            v-for="chip in sourceChips"
            :key="chip.key"
            class="source-chip"
            :class="chip.kind"
            :title="chip.title"
            href="#"
            @click.prevent="chip.go()"
          >
            {{ chip.label }}
          </a>
        </div>
        <!-- 失败消息错误提示 -->
        <div
          v-if="(item as TextItem).error"
          class="msg-error-banner"
        >
          <span class="error-text">{{ $t('errors.send_failed_msg', { msg: (item as TextItem).errorMsg || $t('errors.network_error_2') }) }}</span>
          <!-- 会话忙：给“修复”而不只是“重试” —— 重试必然再撞同一道闸门 -->
          <button
            v-if="(item as TextItem).errorCode === 'session_busy'"
            class="retry-btn"
            type="button"
            @click.stop="emit('repair-session', String((item as TextItem).id || ''))"
          >
            {{ $t('errors.session_busy_repair') }}
          </button>
          <span
            v-if="waitLabel"
            class="repair-hint"
          >{{ $t('errors.session_busy_auto_recover_in', { time: waitLabel }) }}</span>
          <button
            class="retry-btn"
            type="button"
            @click.stop="retryFailed"
          >
            <ReloadOutlined /> {{ $t('common.retry') }}
          </button>
        </div>
        <div
          class="msg-actions"
          :class="item.role"
        >
          <span class="msg-time tnum">{{ timeLabel }}</span>
          <button
            class="msg-action"
            :class="{ copied }"
            type="button"
            :title="copied ? $t('common.copied_2') : $t('common.copy')"
            @click.stop="copyMessage"
          >
            <CheckOutlined v-if="copied" />
            <CopyOutlined v-else />
          </button>
          <!-- 引用整条消息：与「选中文本引用」走同一出口，省去手动划选长回复 -->
          <button
            v-if="!item.streaming"
            class="msg-action"
            type="button"
            :title="$t('common.quote_to_input_box')"
            @click.stop="emit('quote', (item as TextItem).content || '')"
          >
            <CommentOutlined />
          </button>
          <!-- 用户消息：编辑重发（非流式、非错误态） -->
          <button
            v-if="item.role === 'user' && !item.streaming && !(item as TextItem).error"
            class="msg-action"
            type="button"
            :title="$t('common.edit_and_resend')"
            @click.stop="startEdit"
          >
            <EditOutlined />
          </button>
          <!-- 助手消息：重新生成（非流式、非错误态） -->
          <button
            v-if="item.role === 'assistant' && !item.streaming && !(item as TextItem).error && !(item as TextItem).stopped"
            class="msg-action"
            type="button"
            :title="$t('common.regenerate')"
            @click.stop="regenerate"
          >
            <ReloadOutlined />
          </button>
          <!-- P2-F: 停止后显示"继续生成"按钮 -->
          <button
            v-if="item.role === 'assistant' && (item as TextItem).stopped"
            class="msg-action continue-btn"
            type="button"
            :title="$t('common.continue_generating')"
            @click.stop="emit('continue', item.id!)"
          >
            <ReloadOutlined /> {{ $t('common.continue') }}
          </button>
          <!-- P2-D: 助手消息反馈 👍/👎 -->
          <template v-if="item.role === 'assistant' && !item.streaming && !(item as TextItem).error">
            <button
              class="msg-action"
              type="button"
              :class="{ active: feedback === 'up' }"
              :title="feedback === 'up' ? $t('common.cancel_upvote') : $t('common.upvote')"
              @click.stop="setFeedback('up')"
            >
              <LikeFilled
                v-if="feedback === 'up'"
                class="action-pop"
              />
              <LikeOutlined v-else />
            </button>
            <button
              class="msg-action"
              type="button"
              :class="{ active: feedback === 'down' }"
              :title="feedback === 'down' ? $t('common.cancel_downvote') : $t('common.downvote')"
              @click.stop="setFeedback('down')"
            >
              <DislikeFilled
                v-if="feedback === 'down'"
                class="action-pop action-pop-down"
              />
              <DislikeOutlined v-else />
            </button>
          </template>
        </div>
      </template>
    </div>
  </div>

  <ReasoningBlock
    v-else-if="item.kind === 'reasoning'"
    :content="item.content"
    :streaming="item.streaming"
  />
  <ToolCallCard
    v-else-if="item.kind === 'tool_call'"
    :item="item as ToolCallItem"
  />
  <ToolResultBlock
    v-else-if="item.kind === 'tool_result'"
    :item="item as ToolResultItem"
  />
  <!-- UI/UX：日期分隔线（deepseek DateDivider） -->
  <div
    v-else-if="item.kind === 'date_divider'"
    v-bind="$attrs"
    class="date-divider"
  >
    <span class="date-divider-line" /><span class="date-divider-text">{{ item.content }}</span><span class="date-divider-line" />
  </div>

  <div
    v-else-if="item.kind === 'turn_stats'"
    v-bind="$attrs"
    class="turn-stats"
  >
    <span
      v-if="item.inputTokens || item.outputTokens || item.durationSec"
      class="tnum"
    >
      <template v-if="item.durationSec">{{ $t('common.elapsed_n_s', { n: item.durationSec }) }} </template>
      tokens: {{ item.inputTokens }} in / {{ item.outputTokens }} out
    </span>
  </div>

  <div
    v-else-if="item.kind === 'notice'"
    v-bind="$attrs"
    class="notice-row"
    :class="item.tone"
    role="status"
  >
    <span class="notice-icon" aria-hidden="true">{{ item.tone === 'warning' ? '⚠' : 'ℹ' }}</span>
    <span class="notice-text">{{ item.content }}</span>
  </div>
</template>

<style scoped>
/* 消息列：748px 内容宽（deepseek --dsh-chat-content-width），16px 节奏。
   注意：虚拟列表 item 为 absolute（left:0 right:0），此处不能设 width:100%，
   否则 left+width+right 超约束会让 right 失效、margin auto 退化为 0（消息列贴左） */
.msg-row { padding: var(--chat-msg-gap, 6px) 0; max-width: min(var(--chat-content-width), 92%); margin: 0 auto; }
/* 系统通知行（护栏拦截等）：不是工具卡，也不占用户/助手气泡的位。
   `role="status"` 让读屏软件把它播报出来（拦截发生时用户可能正不看屏幕）。 */
.notice-row {
  display: flex; align-items: flex-start; gap: 6px;
  max-width: min(var(--chat-content-width), 92%); margin: 6px auto;
  padding: 8px 12px; border-radius: var(--sig-radius-card, 8px);
  font-size: var(--font-size-sm, 13px); line-height: 1.5;
  border: var(--hairline-width) solid var(--warning-border, var(--border-color));
  background: var(--warning-bg, var(--bg-secondary));
  color: var(--text-secondary, var(--text-primary));
}
.notice-row.info {
  border-color: var(--primary-border, var(--border-color));
  background: var(--primary-bg, var(--bg-secondary));
}
.notice-icon { flex: none; }
.msg-row.user { display: flex; justify-content: flex-end; }
/* 纵向律动（CDS v1 §3.1.2）：同一回合内的行距 = 用户选的「消息间距」；
   用户消息开启新回合，所以它上方按 1.5 倍放大 —— 手册给的 turn gap / item gap
   正是 0.75 : 0.50 的比值。这样既保留用户那个旋钮，又让"换人说话"真正分段。 */
/* 轮次之间的间距要比轮次内更松（CDS 的 turn-gap : item-gap = 0.75 : 0.5 = 1.5）。
   本仓的 --chat-msg-gap 是**半间距**（上下 padding 各算一次），所以这里取 ×2：
   实际轮次间距 = 本条 top(2g) + 上一条 bottom(g) = 3g，而轮次内是 2g → 比例正好 1.5。 */
.msg-row.user { padding-top: calc(var(--chat-msg-gap, 6px) * 2); }
/* 新消息入场（CDS v1 §4.1.1 的 `message-fade-up`）：只用 transform/opacity，
   不碰布局，所以不影响行高测量（transcript 窗口化的前提）。 */
.msg-row.msg-enter { animation: cdsMsgEnter var(--dur-slow) var(--ease-out); }
/* 用户自己发的那条走单独的入场（CDS v1 §4.1.1 的 `code-user-bubble-enter`）：
   从 0.92 倍 + 下移 6px 长回来，比通用的 fade-up 更像"发出去了"。
   再叠一层 4px blur 的"对焦"（CDS block entry = ease-enter 曲线 + 4px blur fade；
   本仓 --ease-out 的数值与 CDS 的 ease-enter 逐位相同，所以曲线本来就对，只缺 blur）。
   **只给用户气泡**：助手消息是流式的，整屏 blur 与 80~120 tokens/s 的 DOM 更新叠加
   是真实的掉帧源；用户气泡短小、一次性、不与流式并发，代价可忽略。 */
.msg-row.user.msg-enter {
  animation-name: cdsUserBubbleEnter;
  will-change: filter, transform, opacity;      /* .msg-enter 会在 420ms 后摘掉，不常驻图层 */
}
@keyframes cdsMsgEnter {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: none; }
}
@keyframes cdsUserBubbleEnter {
  from { opacity: 0; transform: scale(0.92) translateY(6px); filter: blur(4px); }
  to { opacity: 1; transform: none; filter: blur(0); }
}
/* 轨迹跳转高亮闪烁（deepseek data-current 聚焦反馈） */
.msg-row.highlighted { background: var(--primary-bg); border-radius: var(--sig-radius-card); animation: trajectoryFlash var(--dur-slow) ease-out; }
@keyframes trajectoryFlash { 0% { background: var(--primary-bg); } 100% { background: transparent; } }
.msg-content { min-width: 0; }
.msg-text { font-size: var(--chat-text-size, 16px); line-height: var(--chat-text-leading, 1.75); color: var(--text-primary);  /* 行末单字保护：浏览器会在最后一行避免孤字（不支持时无副作用） */
  text-wrap: pretty;
  /* 长 URL / 英文串不撑破容器；代码块有自己的横向滚动，不受影响 */
  overflow-wrap: anywhere;
}
.streaming-text { white-space: pre-wrap; word-break: break-word; }
/* 折叠态（CDS v1 §3.1.1）：真实内容由 max-height 裁切，底边用 3rem 渐变蒙版羽化。
   高度恒定为 300px —— 这对 transcript 的窗口化测量比"随内容浮动"友好得多。 */
.msg-text.collapsed {
  max-height: 300px;
  overflow: hidden;
  mask-image: linear-gradient(to bottom, black calc(100% - 3rem), transparent);
  -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 3rem), transparent);
}
/* 水墨晕染：极快高斯模糊 → 对焦；相邻字错开（步长走 token，见 style.css :root） */
.ink {
  animation: inkIn var(--dur-ink) var(--ease-out) both;
  animation-delay: calc(var(--i, 0) * var(--dur-ink-stagger));
}
@keyframes inkIn {
  from { filter: blur(6px); opacity: 0.35; transform: translateY(1px); }
  to { filter: blur(0); opacity: 1; transform: none; }
}
/* 行内图标下移 1/8 字高（与正文基线咬合） */
.msg-text :is(svg, img) { vertical-align: -0.125em; }
@media (prefers-reduced-motion: reduce) {
  .ink { animation: none; }
  .action-pop { animation: none; }
}
/* 会话修复提示：显示自动恢复的最坏等待（后端算的 stale 阈值 + 一次巡检间隔） */
/* 等待倒计时：从"灰字备注"升级为 warning 药丸 —— 它是当前唯一能解释
   "为什么现在点了也没用 / 还要等多久"的信息，不该和普通说明文字同色同权。 */
.repair-hint {
  display: inline-flex; align-items: center; padding: 2px 8px;
  border-radius: var(--radius-full);
  background: var(--warning-bg); color: var(--warning);
  font-size: var(--fs-xs); font-variant-numeric: tabular-nums;
}
/* UI/UX：流式生成光标（deepseek 打字效果） */
.streaming-text::after {
  content: '▍';
  display: inline-block;
  margin-inline-start: 2px;
  color: var(--primary);
  animation: streamCursor var(--dur-pulse) ease-in-out infinite;
}
@keyframes streamCursor { 0%, 100% { opacity: 1; } 50% { opacity: 0.2; } }
/* UI/UX：日期分隔线（deepseek DateDivider） */
.date-divider { display: flex; align-items: center; gap: 12px; max-width: min(var(--chat-content-width), 92%); margin: 18px auto; padding: 0 20px; }
.date-divider-line { flex: 1; height: 1px; background: var(--border); opacity: 0.6; }
.date-divider-text { font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }
/* 流式光标：assistant 正在输出时末尾闪烁（打字感） */
.msg-row.streaming.assistant .msg-text::after {
  content: ''; display: inline-block; width: 2px; height: 1em;
  margin-inline-start: 2px; vertical-align: -0.15em;
  background: var(--primary);
  animation: streamCursor var(--dur-pulse) step-end infinite;
}
@keyframes streamCursor { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .msg-row.streaming.assistant .msg-text::after { animation: none; } }
.msg-row.assistant .msg-text { padding: 2px 0; }
/* 用户气泡：deepseek User_Bubble — deepseek-50 淡蓝底 + 深色文字，22px 圆角，525px 上限 */
.msg-row.user .msg-text {
  display: inline-block; padding: 10px 16px;
  background: var(--bubble-user); color: var(--bubble-user-text);
  border-radius: var(--sig-radius-bubble); border-bottom-right-radius: var(--sig-radius-bubble-assistant); max-width: min(525px, 88%);
  /* 气泡行高比正文紧一档，但随字号缩放（16px 字号下仍是 24px） */
  line-height: calc(var(--chat-text-size, 16px) * 1.5);
  transition: box-shadow var(--dur-normal) ease;
}
.msg-row.user .msg-text:hover { box-shadow: var(--sig-shadow-hover); }
.turn-stats { max-width: min(var(--chat-content-width), 92%); margin: 0 auto; padding: 4px 0 10px; font-size: var(--fs-xs); color: var(--text-muted); text-align: end; }
/* markdown 正文（deepseek MarkdownText：16/28、标题层级、块 gap 16） */
.msg-text :deep(p) { margin: 16px 0; }
.msg-text :deep(p:first-child) { margin-top: 0; }
.msg-text :deep(h1) { font-size: 24px; line-height: 34px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.msg-text :deep(h2) { font-size: 22px; line-height: 32px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.msg-text :deep(h3) { font-size: 20px; line-height: 30px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.msg-text :deep(h4) { font-size: 16px; line-height: 28px; font-weight: var(--fw-semibold); margin: 16px 0; }
.msg-text :deep(strong) { font-weight: var(--fw-semibold); }
.msg-text :deep(ul), .msg-text :deep(ol) { padding-inline-start: 24px; margin: 16px 0; }
.msg-text :deep(li) { margin: 4px 0; }
.msg-text :deep(blockquote) { margin: 16px 0; padding: 6px 12px; border-inline-start: 3px solid var(--primary); background: var(--bg-hover); border-radius: var(--sig-radius-button); }
.msg-text :deep(a) { color: var(--primary); text-decoration: none; }
.msg-text :deep(a:hover) { text-decoration: underline; }
.msg-text :deep(table) { border-collapse: collapse; margin: 16px 0; display: block; width: max-content; min-width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.msg-text :deep(th), .msg-text :deep(td) { border: var(--hairline-width) solid var(--border); padding: 8px 12px; text-align: start; }
.msg-text :deep(th) { background: var(--bg-secondary); font-weight: var(--fw-semibold); }
.msg-text :deep(img) { max-width: 100%; border-radius: var(--sig-radius-code); }/* 行内代码（deepseek markdown-inline-code：bluish-100 底） */
.msg-text :deep(code) { font-family: var(--font-mono); font-size: 0.9em; background: var(--bg-secondary); padding: 2px 6px; border-radius: var(--sig-radius-button); }
/* 代码块：12px 圆角 + banner（deepseek CodeBlock） */
.msg-text :deep(.code-block-wrapper) { margin: 16px 0; background: var(--bg-code); border-radius: var(--sig-radius-code); overflow: hidden; }
.msg-text :deep(.code-block-header) { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 9px 14px; background: var(--bg-secondary); }
.msg-text :deep(.code-lang) { font-family: var(--font-mono); font-size: 12px; line-height: 18px; color: var(--text-primary); }
.msg-text :deep(.code-lines) { flex: 1; min-width: 0; font-size: var(--fs-xs); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.msg-text :deep(.code-copy-btn) { display: inline-flex; align-items: center; gap: 4px; background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: 12px; padding: 0; }
.msg-text :deep(.code-copy-btn:hover) { color: var(--primary); }
/* 复制成功：对勾先画出来、文案改"已复制"，2.5s 后按 saved-check-decay 淡出恢复 ——
   与消息级复制是同一套状态机（CDS v1 §5.5.4），不再只换一行文案。 */
.msg-text :deep(.code-copy-btn.copied) { color: var(--success); animation: cdsCheckDecay var(--dur-check-decay) linear; }
.msg-text :deep(.code-copy-btn svg) { flex: none; }
/* 长代码默认折叠：给一段可读预览 + 底部渐隐提示，展开按钮写明总行数 */
.msg-text :deep(.code-block-wrapper[data-collapsed='1'] pre) { max-height: 460px; overflow: hidden; }
.msg-text :deep(.code-block-wrapper[data-collapsed='1']) { position: relative; }
.msg-text :deep(.code-block-wrapper[data-collapsed='1'])::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: 31px; height: 56px;
  background: linear-gradient(to bottom, transparent, var(--bg-code));
  pointer-events: none;
}
.msg-text :deep(.code-expand-btn) {
  display: block; width: 100%; padding: 6px;
  border: none; border-top: var(--hairline-width) solid var(--border-subtle);
  background: var(--bg-secondary); color: var(--text-secondary);
  font-size: 12px; cursor: pointer;
}
.msg-text :deep(.code-expand-btn:hover) { color: var(--primary); }
.msg-text :deep(.code-expand-btn:focus-visible) { outline: 2px solid var(--primary); outline-offset: -2px; }
.msg-text :deep(pre) { margin: 0 !important; padding: 16px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
.msg-text :deep(pre code) { background: none; padding: 0; font-size: 0.9em; color: var(--text-code); }
/* 行号：两列布局（行号列不可选中，复制代码时不会带上行号） */
.msg-text :deep(.code-line) { display: flex; }
.msg-text :deep(.code-line-no) {
  flex: none; width: 2.6em; padding-inline-end: 12px;
  text-align: end; color: var(--text-tertiary); user-select: none;
}
.msg-text :deep(.code-line-body) { flex: 1; min-width: 0; white-space: pre-wrap; word-break: break-all; }
/* mermaid 图表：未渲染/渲染失败时按源码块展示（可读、可复制），成功后居中撑满内容列宽 */
.msg-text :deep(.mermaid) {
  margin: 16px 0; padding: 12px;
  background: var(--bg-code); border-radius: var(--sig-radius-code);
  font-family: var(--font-mono); font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-all; color: var(--text-secondary);
}
.msg-text :deep(.mermaid-diagram) { display: flex; justify-content: center; overflow-x: auto; background: none; padding: 8px; }
.msg-text :deep(.mermaid-diagram svg) { max-width: 100%; height: auto; }
.msg-text :deep(.mermaid-error) { border-inline-start: 3px solid var(--error); }
.msg-text :deep(.mermaid-error)::before {
  content: attr(data-error); display: block; margin-bottom: 6px;
  color: var(--error); font-family: var(--font-sans); font-size: 12px;
}
/* 消息操作行（deepseek MessageIconActions：28px 高、hover 淡入、80ms） */
.msg-actions { display: flex; align-items: center; gap: 10px; height: 28px; margin-top: 4px; }
.msg-actions.user { justify-content: flex-end; }
.msg-time { padding-inline-end: 6px; font-size: 12px; color: var(--text-tertiary); opacity: 0; transition: opacity var(--dur-fast) ease; }
[data-time-hover-root]:hover .msg-time, [data-time-hover-root]:focus-within .msg-time { opacity: 1; }
.msg-action { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border: none; border-radius: 50%; background: transparent; color: var(--text-tertiary); cursor: pointer; opacity: 0; transition: opacity var(--dur-fast) ease, background var(--dur-fast) ease, transform var(--spring-bouncy); }
.msg-action:hover { background: var(--bg-hover); color: var(--text-primary); }
.msg-action.active { color: var(--primary); opacity: 1; }
.msg-action.continue-btn { width: auto; padding: 0 10px; border-radius: var(--sig-radius-button); background: var(--primary); color: var(--on-solid); font-size: 12px; gap: 4px; opacity: 1; }
.msg-action.continue-btn:hover { opacity: 0.9; background: var(--primary); color: var(--on-solid); }
/* 复制成功：对勾先"画"出来，再驻留 2.5s 衰减（CDS v1 §4.2.4 的 check-draw + saved-check-decay） */
.msg-action.copied { color: var(--success); animation: cdsCheckDecay var(--dur-check-decay) linear; }
.msg-action.copied :deep(svg) { animation: cdsCheckDraw var(--dur-normal) var(--ease-out); }

/* P3-A: 用 CSS 变量覆盖 hljs token 颜色，暗色模式自动跟随 .dark class */
.hljs { color: var(--hljs-fg); background: var(--hljs-bg); }
.hljs-keyword, .hljs-selector-tag, .hljs-deletion { color: var(--hljs-keyword); }
.hljs-string, .hljs-attr, .hljs-template-string, .hljs-addition { color: var(--hljs-string); }
.hljs-number, .hljs-literal, .hljs-variable, .hljs-template-variable, .hljs-type { color: var(--hljs-number); }
.hljs-comment, .hljs-quote { color: var(--hljs-comment); }
.hljs-title, .hljs-section, .hljs-function .hljs-title { color: var(--hljs-title); }
.hljs-built_in, .hljs-class .hljs-title, .hljs-name { color: var(--hljs-built-in); }
.hljs-meta, .hljs-symbol, .hljs-bullet, .hljs-link { color: var(--hljs-meta); }

/* P3-B: 长消息折叠 */
/* 发丝环改用 inset 阴影而不是 border：不参与盒模型，所以"未选中 ↔ 选中"切换
   不会带来 1px 重排与尺寸抖动（CDS §1.4.1，与本文档「单闭合边框原则」同源）。 */
.collapse-toggle { margin-top: 8px; background: var(--bg-card); color: var(--text-secondary); box-shadow: inset 0 0 0 var(--hairline-width) var(--border); transition: box-shadow var(--dur-fast) ease, color var(--dur-fast) ease; }
.collapse-toggle:hover { box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); color: var(--primary); }
.collapse-toggle:active { transform: scale(0.97); }
/* 点赞/点踩：按下走"过阻尼"（60ms 跟手），松开由上面的 --ease-spring 回弹一下 */
.msg-action:active {
  transform: scale(0.86);
  transition-duration: var(--dur-instant);
  transition-timing-function: var(--ease-snap);
}
/* 选中瞬间图标弹一下：1 → 1.18 → 1；点踩幅度减半（1.09）——
   避免"负面操作也很欢快"的语义错位。v-if 挂载即播，不需要额外 JS。 */
@keyframes action-pop {
  0% { transform: scale(1); }
  45% { transform: scale(1.18); }
  100% { transform: scale(1); }
}
@keyframes action-pop-down {
  0% { transform: scale(1); }
  45% { transform: scale(1.09); }
  100% { transform: scale(1); }
}
.action-pop { animation: action-pop var(--spring-bouncy); }
.action-pop-down { animation-name: action-pop-down; }
.retry-btn:active { transform: scale(0.96); }
.edit-btn:active { transform: scale(0.97); }
[data-time-hover-root]:hover .msg-action, [data-time-hover-root]:focus-within .msg-action { opacity: 1; }
@media (hover: none) { .msg-time, .msg-action { opacity: 1; } }
/* ── 移动端：间距压缩 + 操作按钮常驻（触控目标 ≥40px）+ 代码块横向滚动 ── */
@media (max-width: 768px) {
  .msg-row { padding: 4px 0; }
  .source-chip { min-height: 28px; } /* 反向定位 chip 触控目标微放大 */
  .msg-actions { gap: 4px; height: 40px; }
  .msg-time { opacity: 1; font-size: var(--fs-xs); padding-inline-end: 4px; }
  .msg-action { width: 40px; height: 40px; opacity: 1; }
  .msg-action.continue-btn { width: auto; height: 40px; padding: 0 14px; border-radius: var(--sig-radius-bubble); }
  .msg-text :deep(pre) { white-space: pre; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .msg-text :deep(p) { margin: 10px 0; }
  .msg-text :deep(ul), .msg-text :deep(ol) { padding-inline-start: 20px; margin: 10px 0; }
  .msg-text :deep(h1) { font-size: 20px; line-height: 28px; margin: 20px 0 10px; }
  .msg-text :deep(h2) { font-size: 18px; line-height: 26px; margin: 18px 0 10px; }
  .msg-text :deep(h3) { font-size: 16px; line-height: 24px; margin: 14px 0 8px; }
  .msg-text :deep(blockquote) { margin: 10px 0; }
  .msg-attachment-img { max-width: min(200px, 70vw); }
  .retry-btn { min-height: 40px; } /* 失败重试触控目标 ≥40px */
}
@media (max-width: 576px) {
  .msg-text { font-size: 15px; line-height: 25px; }
  .msg-row.user .msg-text { padding: 8px 12px; line-height: 22px; }
  .msg-error-banner { flex-wrap: wrap; }
}

/* ── P1-1 编辑重发 ── */
.msg-edit { display: flex; flex-direction: column; gap: 8px; max-width: min(525px, 100%); margin-inline-start: auto; }
.edit-textarea { border-radius: var(--sig-radius-card) !important; border-color: var(--primary) !important; }
.edit-textarea :deep(textarea) { font-size: 16px !important; line-height: 24px !important; }
.edit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.edit-btn { padding: 4px 12px; border-radius: var(--sig-radius-button); font-size: 12px; cursor: pointer; border: var(--hairline-width) solid var(--border); background: var(--bg-card); color: var(--text-secondary); transition: transform var(--dur-fast), opacity var(--dur-fast), color var(--dur-fast), background-color var(--dur-fast); }
.edit-btn.save { background: var(--primary); color: var(--on-solid); border-color: var(--primary); }
.edit-btn.save:hover { opacity: 0.9; }
.edit-btn.cancel:hover { color: var(--text-primary); background: var(--bg-hover); }

/* ── P1-2 附件展示 ── */
.msg-attachments { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.msg-attachment-img { max-width: 200px; max-height: 200px; border-radius: var(--sig-radius-card); border: var(--hairline-width) solid var(--border); object-fit: cover; }
.msg-attachment-file { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: var(--sig-radius-card); box-shadow: inset 0 0 0 var(--hairline-width) var(--border); background: var(--bg-card); color: var(--text-secondary); text-decoration: none; font-size: 13px; transition: box-shadow var(--dur-fast) ease, color var(--dur-fast) ease; }
.msg-attachment-file:hover { box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); color: var(--primary); }
/* 双向中间截断（CDS v1/02 §2.5.3）：head 允许被省略，tail（尾部词干 + 扩展名）
   锁成不收缩。两段之间不留空隙，看起来仍是一个连续的文件名。 */
.msg-attachment-file .att-name { display: flex; min-width: 0; max-width: 180px; }
.msg-attachment-file .att-name-head { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.msg-attachment-file .att-name-tail { flex: none; white-space: nowrap; }
/* 类型徽标的规格已提到全局 `.file-type-badge`（消息附件行与 composer 磁贴共用同一套） */
.msg-attachment-file .att-size { color: var(--text-tertiary); font-size: 12px; }
/* 音频播放器 */
.msg-attachment-audio { display: flex; flex-direction: column; gap: 4px; width: 100%; max-width: 300px; padding: 8px; border-radius: var(--sig-radius-card); border: var(--hairline-width) solid var(--border); background: var(--bg-card); }
.msg-audio-player { width: 100%; height: 40px; border-radius: var(--radius-sm); }
.msg-audio-player::-webkit-media-controls-panel { background: var(--bg-card); }
.att-audio-name { font-size: 12px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── 反向定位：来源工作台 chips（与 kb-hits 标签同设计语言：胶囊 + CSS 变量色）── */
.source-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
/* 子 Agent 自动轮注入的消息标记（见 internal/api/agent_followup.go） */
.followup-badge {
  display: inline-flex; align-items: center; gap: 4px;
  margin-top: 6px; padding: 2px 8px; font-size: var(--fs-xs);
  color: var(--text-tertiary); background: var(--bg-code);
  border: var(--hairline-width) solid var(--border-subtle); border-radius: var(--sig-radius-button);
}
.source-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 10px; border-radius: var(--sig-radius-button);
  font-size: 12px; line-height: 18px; text-decoration: none; cursor: pointer;
  background: var(--bg-secondary); color: var(--text-secondary);
  box-shadow: inset 0 0 0 var(--hairline-width) var(--border);
  transition: box-shadow var(--dur-fast) ease, color var(--dur-fast) ease, background var(--dur-fast) ease;
}
.source-chip:hover { box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); color: var(--primary); background: var(--primary-bg); }
.source-chip.kb { background: var(--primary-bg); color: var(--primary); box-shadow: none; }
.source-chip.kb:hover { background: var(--primary); color: var(--on-solid); box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); }
.source-chip.agent { background: var(--bg-hover); color: var(--text-primary); }
.source-chip.trace { background: var(--bg-card); color: var(--text-secondary); box-shadow: inset 0 0 0 var(--hairline-width) var(--border); font-family: var(--font-mono); font-size: var(--fs-xs); }
.source-chip.trace:hover { box-shadow: inset 0 0 0 var(--hairline-width) var(--primary); color: var(--primary); background: var(--primary-bg); }

/* ── P1-3 错误状态 ── */
.msg-row.msg-error .msg-text { opacity: 0.6; }
.msg-error-banner { display: flex; align-items: center; gap: 12px; margin-top: 8px; padding: 8px 12px; border-radius: var(--sig-radius-card); background: var(--error-bg); border: var(--hairline-width) solid var(--error); }
.msg-error-banner .error-text { font-size: 13px; color: var(--error); flex: 1; }
.retry-btn { display: inline-flex; align-items: center; gap: 4px; padding: 4px 12px; border-radius: var(--sig-radius-button); border: var(--hairline-width) solid var(--error); background: transparent; color: var(--error); font-size: 12px; cursor: pointer; transition: background var(--dur-fast) ease, color var(--dur-fast) ease; }
.retry-btn:hover { background: var(--error); color: var(--on-solid); }
</style>
