<script setup lang="ts">
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github.css'
// P3-A: 暗色模式下的代码高亮配色覆盖（避免引入两个冲突的 hljs 主题）
import texmath from 'markdown-it-texmath'
import katex from 'katex'
import { computed, ref, watch, onMounted, onUpdated, onBeforeUnmount } from 'vue'
import { message, Input } from 'ant-design-vue'
import { CopyOutlined, EditOutlined, ReloadOutlined, FileOutlined, LikeOutlined, DislikeOutlined, LikeFilled, DislikeFilled, CommentOutlined } from '@ant-design/icons-vue'
import ReasoningBlock from './ReasoningBlock.vue'
import ToolCallCard from './ToolCallCard.vue'
import ToolResultBlock from './ToolResultBlock.vue'
import { renderMermaidBlocks } from './mermaidRenderer'
import { highlightCodeBlocks } from './codeHighlighter'
import { openImageViewer } from '../common/imageViewerState'
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
}>()

/** 正文容器：mermaid 渲染需要拿到已挂载的 DOM */
const msgTextRef = ref<HTMLElement | null>(null)

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
  const meta: Record<string, any> = (props.item as any).metadata || {}
  const chips: SourceChip[] = []
  const kbId = typeof meta.kb_id === 'string' && meta.kb_id ? meta.kb_id : ''
  if (kbId) {
    chips.push({
      key: 'kb', kind: 'kb', label: t('知识库'),
      title: `来源知识库 #${kbId}，点击打开`,
      go: () => router.push(`/knowledge/${encodeURIComponent(kbId)}`),
    })
  }
  const wfId = typeof meta.workflow_id === 'string' && meta.workflow_id ? meta.workflow_id : ''
  if (wfId) {
    chips.push({
      key: 'workflow', kind: 'workflow', label: t('工作流'),
      title: `来源工作流 ${wfId}，点击打开`,
      go: () => router.push({ path: '/workflow', query: { id: wfId } }),
    })
  }
  const agentId = typeof meta.agent_id === 'string' && meta.agent_id ? meta.agent_id : ''
  if (agentId) {
    chips.push({
      key: 'agent', kind: 'agent', label: 'Agent',
      title: `来源 Agent #${agentId}，点击打开`,
      go: () => router.push('/agents'),
    })
  }
  // 追踪：metadata.trace_id 存在时显示「追踪」chip，点击复制 trace_id
  // （/admin/traces 为占位目标，暂不跳转，仅复制）
  const traceId = typeof meta.trace_id === 'string' && meta.trace_id ? meta.trace_id : ''
  if (traceId) {
    chips.push({
      key: 'trace', kind: 'trace', label: t('追踪'),
      title: `Trace ${traceId}，点击复制`,
      go: () => {
        navigator.clipboard.writeText(traceId)
          .then(() => message.success(t('Trace ID 已复制')))
          .catch(() => message.error(t('复制失败')))
      },
    })
  }
  return chips
})

// ── 安全改造：附件签名 URL 解析 ──
// 附件若为 /media/ 公开路径，渲染前异步解析为短时效签名 URL（12 分钟本地缓存）；
// 非 /media/ 前缀（绝对/签名/data:）原样；解析失败或加载失败回退原 url。
const attachmentResolved = ref<Record<string, string>>({})

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

async function copyMessage() {
  if (props.item.kind !== 'text') return
  try {
    await navigator.clipboard.writeText(props.item.content)
    message.success(t('已复制'))
  } catch {
    message.error(t('复制失败'))
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
const COLLAPSE_THRESHOLD = 2000  // 字符数阈值
const COLLAPSE_PREVIEW = 600     // 折叠时显示的字符数
const collapsed = ref(true)
const isLongMessage = computed(() => {
  if (props.item.kind !== 'text' || props.item.role !== 'assistant') return false
  return props.item.content.length > COLLAPSE_THRESHOLD
})
const displayContent = computed(() => {
  if (props.item.kind !== 'text') return ''
  if (isLongMessage.value && collapsed.value) {
    return props.item.content.slice(0, COLLAPSE_PREVIEW) + '\n\n... (已折叠，点击展开全部)'
  }
  return props.item.content
})

// ── P2-D 消息反馈（👍/👎） ──
// 'up' | 'down' | null；切换反馈，再次点击同方向取消
const feedback = ref<'up' | 'down' | null>(null)
function setFeedback(dir: 'up' | 'down') {
  feedback.value = feedback.value === dir ? null : dir
  // 设计说明：反馈上报留待模型评估链路接入后端 API
  if (feedback.value) message.success(feedback.value === 'up' ? '感谢好评' : '已记录您的反馈')
}

// ── Markdown 引擎（迁移自原 ChatView） ──
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.use(texmath, { engine: katex, delimiters: 'dollars', katexOptions: { throwOnError: false, output: 'html' } })
/**
 * 外链一律新窗口打开：SPA 内直接跳走会丢掉当前对话（消息状态、滚动位置、
 * 正在流的回合）。站内相对链接保持原行为，交给应用自身处理。
 */
md.renderer.rules.link_open = (tokens: any[], idx: number, options: any, _env: any, self: any) => {
  const token = tokens[idx]
  const href = (token?.attrGet?.('href') as string | null) || ''
  if (/^https?:\/\//i.test(href)) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return self.renderToken(tokens, idx, options)
}
md.renderer.rules.fence = (tokens: any[], idx: number) => {
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
  const header = `<div class="code-block-header"><span class="code-lang">${safeLang}</span><span class="code-lines">${lineCount} 行</span><button class="code-copy-btn" data-code="${encoded}">复制</button></div>`
  const body = `<pre><code class="language-${safeLang}" data-lang="${safeLang}">${md.utils.escapeHtml(code)}</code></pre>`
  const toggle = collapsible
    ? `<button class="code-expand-btn" type="button" data-lines="${lineCount}">展开全部（共 ${lineCount} 行）</button>`
    : ''
  return `<div class="code-block-wrapper"${collapsible ? ' data-collapsed="1"' : ''}>${header}${body}${toggle}</div>`
}

/** 超过该行数默认折叠：长代码会挤掉整屏消息，先给一段可读的预览 */
const CODE_COLLAPSE_LINES = 28

// P 性能：图片懒加载（长列表/历史中大量图片不阻塞首屏，滚动到才加载）
md.renderer.rules.image = (tokens: any[], idx: number) => {
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

function scheduleRender() {
  const src = displayContent.value
  const token = ++renderToken
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

watch(displayContent, scheduleRender, { immediate: true })

function handleMsgClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const toggle = target.closest('.code-expand-btn') as HTMLElement | null
  if (toggle) {
    const box = toggle.closest('.code-block-wrapper') as HTMLElement | null
    if (box) {
      const collapsed = box.dataset.collapsed === '1'
      box.dataset.collapsed = collapsed ? '0' : '1'
      const lines = toggle.dataset.lines || ''
      toggle.textContent = collapsed ? `收起（共 ${lines} 行）` : `展开全部（共 ${lines} 行）`
    }
    return
  }
  const btn = target.closest('.code-copy-btn') as HTMLElement | null
  if (btn) {
    const code = decodeURIComponent(btn.dataset.code || '')
    if (!code) return
    navigator.clipboard.writeText(code).then(() => {
      btn.textContent = '已复制'
      setTimeout(() => { btn.textContent = '复制' }, 2000)
    }).catch(() => { /* clipboard not available */ })
    return
  }
  // 正文里的图片（markdown 图片由 v-html 生成，挂不上 Vue 事件）交查看器放大
  const img = target.closest('img') as HTMLImageElement | null
  if (img?.src) openImageViewer({ src: img.src, alt: img.alt })
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
    :class="[item.role, { streaming: item.streaming, highlighted: highlighted, 'msg-error': (item as TextItem).error }]"
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
            {{ $t('取消') }}
          </button>
          <button
            class="edit-btn save"
            type="button"
            @click="confirmEdit"
          >
            {{ $t('保存并发送') }}
          </button>
        </div>
      </div>
      <template v-else>
        <!-- P 性能：流式期间纯文本渲染（零 markdown 开销），完成后一次性 markdown + 缓存 -->
        <div
          v-if="item.streaming"
          class="msg-text streaming-text"
        >
          {{ item.content }}
        </div>
        <!-- 超长正文让位期间先给纯文本，避免空白；渲染完成后由下方 v-html 分支接管 -->
        <div
          v-else-if="pendingLargeRender"
          class="msg-text streaming-text"
        >
          {{ displayContent }}
        </div>
        <div
          v-else
          ref="msgTextRef"
          class="msg-text"
          v-html="renderedHtml"
        />
        <!-- P3-B: 长消息展开/折叠按钮 -->
        <button
          v-if="isLongMessage"
          class="collapse-toggle"
          type="button"
          @click.stop="collapsed = !collapsed"
        >
          {{ collapsed ? '展开全部' : '收起' }}
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
              @click.stop="openImageViewer({ src: attachmentUrl(att), alt: att.name })"
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
                {{ $t('您的浏览器不支持音频播放') }}
              </audio>
              <span class="att-audio-name">{{ att.name }}</span>
            </div>
            <a
              v-else
              :href="attachmentUrl(att)"
              :download="att.name"
              class="msg-attachment-file"
            >
              <FileOutlined />
              <span class="att-name">{{ att.name }}</span>
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
          {{ $t('子任务完成 · 主 Agent 自动汇总') }}
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
          <span class="error-text">发送失败：{{ (item as TextItem).errorMsg || '网络错误' }}</span>
          <button
            class="retry-btn"
            type="button"
            @click.stop="retryFailed"
          >
            <ReloadOutlined /> {{ $t('重试') }}
          </button>
        </div>
        <div
          class="msg-actions"
          :class="item.role"
        >
          <span class="msg-time">{{ timeLabel }}</span>
          <button
            class="msg-action"
            type="button"
            :title="$t('复制')"
            @click.stop="copyMessage"
          >
            <CopyOutlined />
          </button>
          <!-- 引用整条消息：与「选中文本引用」走同一出口，省去手动划选长回复 -->
          <button
            v-if="!item.streaming"
            class="msg-action"
            type="button"
            :title="$t('引用到输入框')"
            @click.stop="emit('quote', (item as TextItem).content || '')"
          >
            <CommentOutlined />
          </button>
          <!-- 用户消息：编辑重发（非流式、非错误态） -->
          <button
            v-if="item.role === 'user' && !item.streaming && !(item as TextItem).error"
            class="msg-action"
            type="button"
            :title="$t('编辑并重发')"
            @click.stop="startEdit"
          >
            <EditOutlined />
          </button>
          <!-- 助手消息：重新生成（非流式、非错误态） -->
          <button
            v-if="item.role === 'assistant' && !item.streaming && !(item as TextItem).error && !(item as TextItem).stopped"
            class="msg-action"
            type="button"
            :title="$t('重新生成')"
            @click.stop="regenerate"
          >
            <ReloadOutlined />
          </button>
          <!-- P2-F: 停止后显示"继续生成"按钮 -->
          <button
            v-if="item.role === 'assistant' && (item as TextItem).stopped"
            class="msg-action continue-btn"
            type="button"
            :title="$t('继续生成')"
            @click.stop="emit('continue', item.id!)"
          >
            <ReloadOutlined /> {{ $t('继续') }}
          </button>
          <!-- P2-D: 助手消息反馈 👍/👎 -->
          <template v-if="item.role === 'assistant' && !item.streaming && !(item as TextItem).error">
            <button
              class="msg-action"
              type="button"
              :class="{ active: feedback === 'up' }"
              :title="feedback === 'up' ? '取消好评' : '好评'"
              @click.stop="setFeedback('up')"
            >
              <LikeFilled v-if="feedback === 'up'" />
              <LikeOutlined v-else />
            </button>
            <button
              class="msg-action"
              type="button"
              :class="{ active: feedback === 'down' }"
              :title="feedback === 'down' ? '取消差评' : '差评'"
              @click.stop="setFeedback('down')"
            >
              <DislikeFilled v-if="feedback === 'down'" />
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
    <span v-if="item.inputTokens || item.outputTokens || item.durationSec">
      <template v-if="item.durationSec">耗时 {{ item.durationSec }}s · </template>
      tokens: {{ item.inputTokens }} in / {{ item.outputTokens }} out
    </span>
  </div>
</template>

<style scoped>
/* 消息列：748px 内容宽（deepseek --dsh-chat-content-width），16px 节奏。
   注意：虚拟列表 item 为 absolute（left:0 right:0），此处不能设 width:100%，
   否则 left+width+right 超约束会让 right 失效、margin auto 退化为 0（消息列贴左） */
.msg-row { padding: var(--chat-msg-gap, 6px) 0; max-width: min(var(--chat-content-width), 92%); margin: 0 auto; }
.msg-row.user { display: flex; justify-content: flex-end; }
/* 轨迹跳转高亮闪烁（deepseek data-current 聚焦反馈） */
.msg-row.highlighted { background: var(--primary-bg); border-radius: var(--sig-radius-card); animation: trajectoryFlash var(--dur-slow) ease-out; }
@keyframes trajectoryFlash { 0% { background: var(--primary-bg); } 100% { background: transparent; } }
.msg-content { min-width: 0; }
.msg-text { font-size: var(--chat-text-size, 16px); line-height: var(--chat-text-leading, 1.75); color: var(--text-primary); }
.streaming-text { white-space: pre-wrap; word-break: break-word; }
/* UI/UX：流式生成光标（deepseek 打字效果） */
.streaming-text::after {
  content: '▍';
  display: inline-block;
  margin-left: 2px;
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
  margin-left: 2px; vertical-align: -0.15em;
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
.turn-stats { max-width: min(var(--chat-content-width), 92%); margin: 0 auto; padding: 4px 0 10px; font-size: 11px; color: var(--text-muted); text-align: right; }
/* markdown 正文（deepseek MarkdownText：16/28、标题层级、块 gap 16） */
.msg-text :deep(p) { margin: 16px 0; }
.msg-text :deep(p:first-child) { margin-top: 0; }
.msg-text :deep(h1) { font-size: 24px; line-height: 34px; font-weight: 700; margin: 32px 0 16px; }
.msg-text :deep(h2) { font-size: 22px; line-height: 32px; font-weight: 700; margin: 32px 0 16px; }
.msg-text :deep(h3) { font-size: 20px; line-height: 30px; font-weight: 700; margin: 32px 0 16px; }
.msg-text :deep(h4) { font-size: 16px; line-height: 28px; font-weight: 600; margin: 16px 0; }
.msg-text :deep(strong) { font-weight: 600; }
.msg-text :deep(ul), .msg-text :deep(ol) { padding-left: 24px; margin: 16px 0; }
.msg-text :deep(li) { margin: 4px 0; }
.msg-text :deep(blockquote) { margin: 16px 0; padding: 6px 12px; border-left: 3px solid var(--primary); background: var(--bg-hover); border-radius: var(--sig-radius-button); }
.msg-text :deep(a) { color: var(--primary); text-decoration: none; }
.msg-text :deep(a:hover) { text-decoration: underline; }
.msg-text :deep(table) { border-collapse: collapse; margin: 16px 0; display: block; width: max-content; min-width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.msg-text :deep(th), .msg-text :deep(td) { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
.msg-text :deep(th) { background: var(--bg-secondary); font-weight: 600; }
.msg-text :deep(img) { max-width: 100%; border-radius: var(--sig-radius-code); }/* 行内代码（deepseek markdown-inline-code：bluish-100 底） */
.msg-text :deep(code) { font-family: var(--font-mono); font-size: 0.9em; background: var(--bg-secondary); padding: 2px 6px; border-radius: var(--sig-radius-button); }
/* 代码块：12px 圆角 + banner（deepseek CodeBlock） */
.msg-text :deep(.code-block-wrapper) { margin: 16px 0; background: var(--bg-code); border-radius: var(--sig-radius-code); overflow: hidden; }
.msg-text :deep(.code-block-header) { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 9px 14px; background: var(--bg-secondary); }
.msg-text :deep(.code-lang) { font-family: var(--font-mono); font-size: 12px; line-height: 18px; color: var(--text-primary); }
.msg-text :deep(.code-lines) { flex: 1; min-width: 0; font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.msg-text :deep(.code-copy-btn) { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: 12px; padding: 0; }
.msg-text :deep(.code-copy-btn:hover) { color: var(--primary); }
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
  border: none; border-top: 1px solid var(--border-subtle);
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
  flex: none; width: 2.6em; padding-right: 12px;
  text-align: right; color: var(--text-tertiary); user-select: none;
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
.msg-text :deep(.mermaid-error) { border-left: 3px solid var(--error); }
.msg-text :deep(.mermaid-error)::before {
  content: attr(data-error); display: block; margin-bottom: 6px;
  color: var(--error); font-family: var(--font-sans); font-size: 12px;
}
/* 消息操作行（deepseek MessageIconActions：28px 高、hover 淡入、80ms） */
.msg-actions { display: flex; align-items: center; gap: 10px; height: 28px; margin-top: 4px; }
.msg-actions.user { justify-content: flex-end; }
.msg-time { padding-right: 6px; font-size: 12px; color: var(--text-tertiary); opacity: 0; transition: opacity var(--dur-fast) ease; }
[data-time-hover-root]:hover .msg-time, [data-time-hover-root]:focus-within .msg-time { opacity: 1; }
.msg-action { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border: none; border-radius: 50%; background: transparent; color: var(--text-tertiary); cursor: pointer; opacity: 0; transition: opacity var(--dur-fast) ease, background var(--dur-fast) ease, transform var(--dur-fast) ease; }
.msg-action:hover { background: var(--bg-hover); color: var(--text-primary); }
.msg-action.active { color: var(--primary); opacity: 1; }
.msg-action.continue-btn { width: auto; padding: 0 10px; border-radius: var(--sig-radius-button); background: var(--primary); color: var(--on-solid); font-size: 12px; gap: 4px; opacity: 1; }
.msg-action.continue-btn:hover { opacity: 0.9; background: var(--primary); color: var(--on-solid); }

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
.collapse-toggle { margin-top: 8px; padding: 4px 12px; border: 1px solid var(--border); border-radius: var(--sig-radius-button); background: var(--bg-card); color: var(--text-secondary); font-size: 12px; cursor: pointer; transition: border-color var(--dur-fast) ease, color var(--dur-fast) ease; }
.collapse-toggle:hover { border-color: var(--primary); color: var(--primary); }
.collapse-toggle:active { transform: scale(0.97); }
.msg-action:active { transform: scale(0.9); }
.retry-btn:active { transform: scale(0.96); }
.edit-btn:active { transform: scale(0.97); }
[data-time-hover-root]:hover .msg-action, [data-time-hover-root]:focus-within .msg-action { opacity: 1; }
@media (hover: none) { .msg-time, .msg-action { opacity: 1; } }
/* ── 移动端：间距压缩 + 操作按钮常驻（触控目标 ≥40px）+ 代码块横向滚动 ── */
@media (max-width: 768px) {
  .msg-row { padding: 4px 0; }
  .source-chip { min-height: 28px; } /* 反向定位 chip 触控目标微放大 */
  .msg-actions { gap: 4px; height: 40px; }
  .msg-time { opacity: 1; font-size: 11px; padding-right: 4px; }
  .msg-action { width: 40px; height: 40px; opacity: 1; }
  .msg-action.continue-btn { width: auto; height: 40px; padding: 0 14px; border-radius: var(--sig-radius-bubble); }
  .msg-text :deep(pre) { white-space: pre; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .msg-text :deep(p) { margin: 10px 0; }
  .msg-text :deep(ul), .msg-text :deep(ol) { padding-left: 20px; margin: 10px 0; }
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
.msg-edit { display: flex; flex-direction: column; gap: 8px; max-width: min(525px, 100%); margin-left: auto; }
.edit-textarea { border-radius: var(--sig-radius-card) !important; border-color: var(--primary) !important; }
.edit-textarea :deep(textarea) { font-size: 16px !important; line-height: 24px !important; }
.edit-actions { display: flex; justify-content: flex-end; gap: 8px; }
.edit-btn { padding: 4px 12px; border-radius: var(--sig-radius-button); font-size: 12px; cursor: pointer; border: 1px solid var(--border); background: var(--bg-card); color: var(--text-secondary); transition: all var(--dur-fast) ease; }
.edit-btn.save { background: var(--primary); color: var(--on-solid); border-color: var(--primary); }
.edit-btn.save:hover { opacity: 0.9; }
.edit-btn.cancel:hover { color: var(--text-primary); background: var(--bg-hover); }

/* ── P1-2 附件展示 ── */
.msg-attachments { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.msg-attachment-img { max-width: 200px; max-height: 200px; border-radius: var(--sig-radius-card); border: 1px solid var(--border); object-fit: cover; }
.msg-attachment-file { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: var(--sig-radius-card); border: 1px solid var(--border); background: var(--bg-card); color: var(--text-secondary); text-decoration: none; font-size: 13px; transition: border-color var(--dur-fast) ease, color var(--dur-fast) ease; }
.msg-attachment-file:hover { border-color: var(--primary); color: var(--primary); }
.msg-attachment-file .att-name { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.msg-attachment-file .att-size { color: var(--text-tertiary); font-size: 12px; }
/* 音频播放器 */
.msg-attachment-audio { display: flex; flex-direction: column; gap: 4px; width: 100%; max-width: 300px; padding: 8px; border-radius: var(--sig-radius-card); border: 1px solid var(--border); background: var(--bg-card); }
.msg-audio-player { width: 100%; height: 40px; border-radius: 4px; }
.msg-audio-player::-webkit-media-controls-panel { background: var(--bg-card); }
.att-audio-name { font-size: 12px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── 反向定位：来源工作台 chips（与 kb-hits 标签同设计语言：胶囊 + CSS 变量色）── */
.source-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
/* 子 Agent 自动轮注入的消息标记（见 internal/api/agent_followup.go） */
.followup-badge {
  display: inline-flex; align-items: center; gap: 4px;
  margin-top: 6px; padding: 2px 8px; font-size: 11px;
  color: var(--text-tertiary); background: var(--bg-code);
  border: 1px solid var(--border-subtle); border-radius: var(--sig-radius-button);
}
.source-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 10px; border-radius: 10px;
  font-size: 12px; line-height: 18px; text-decoration: none; cursor: pointer;
  background: var(--bg-secondary); color: var(--text-secondary);
  border: 1px solid var(--border);
  transition: border-color var(--dur-fast) ease, color var(--dur-fast) ease, background var(--dur-fast) ease;
}
.source-chip:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-bg); }
.source-chip.kb { background: var(--primary-bg); color: var(--primary); border-color: transparent; }
.source-chip.kb:hover { background: var(--primary); color: var(--on-solid); border-color: var(--primary); }
.source-chip.agent { background: var(--bg-hover); color: var(--text-primary); }
.source-chip.trace { background: var(--bg-card); color: var(--text-secondary); border-color: var(--border); font-family: var(--font-mono); font-size: 11px; }
.source-chip.trace:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-bg); }

/* ── P1-3 错误状态 ── */
.msg-row.msg-error .msg-text { opacity: 0.6; }
.msg-error-banner { display: flex; align-items: center; gap: 12px; margin-top: 8px; padding: 8px 12px; border-radius: var(--sig-radius-card); background: var(--error-bg); border: 1px solid var(--error); }
.msg-error-banner .error-text { font-size: 13px; color: var(--error); flex: 1; }
.retry-btn { display: inline-flex; align-items: center; gap: 4px; padding: 4px 12px; border-radius: var(--sig-radius-button); border: 1px solid var(--error); background: transparent; color: var(--error); font-size: 12px; cursor: pointer; transition: background var(--dur-fast) ease, color var(--dur-fast) ease; }
.retry-btn:hover { background: var(--error); color: var(--on-solid); }
</style>
