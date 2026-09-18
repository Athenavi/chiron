<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { getPublicShare } from '../api'
import type { PublicShare } from '../api'
import { splitThinking, stripUserInputTag } from '../components/chat/chat-types'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'

const route = useRoute()

const share = ref<PublicShare | null>(null)
const loading = ref(true)
const error = ref('')

// 轻量 markdown 渲染（公开页不引入 KaTeX/mermaid，代码块带复制按钮）
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.renderer.rules.fence = (tokens: any[], idx: number) => {
  const token = tokens[idx]
  const lang = md.utils.escapeHtml((token.info || '').trim().toLowerCase() || 'code')
  const code = md.utils.escapeHtml(token.content)
  const encoded = encodeURIComponent(token.content)
  return `<div class="code-block"><div class="code-block-head"><span class="code-lang">${lang}</span><button type="button" class="code-copy" data-code="${encoded}">复制</button></div><pre><code>${code}</code></pre></div>`
}

function renderMarkdown(src: string): string {
  try {
    return DOMPurify.sanitize(md.render(src), {
      // button 必须放行：代码块的「复制」是真按钮才能键盘可达（<span> 无 tabindex 不可聚焦）；
      // type 同理需在 ALLOWED_ATTR 里，否则被 DOMPurify 剥掉。
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'del', 'sup', 'sub', 'button'],
      ALLOWED_ATTR: ['href', 'target', 'rel', 'class', 'data-code', 'type'],
      ALLOW_DATA_ATTR: true,
      FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'select', 'marquee', 'link', 'meta'],
      FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur', 'onchange', 'onsubmit', 'onreset', 'onkeydown', 'onkeyup', 'onkeypress'],
    })
  } catch {
    return md.utils.escapeHtml(src)
  }
}

/**
 * 渲染用的消息：把落库原文拆成「思考」与「正文」两段。
 *
 * `<user_input>` 包装由后端剥离（internal/api/share.go 的 stripUserInputTag），
 * 这里再剥一次只是兜底（老数据、缓存等）。
 *
 * `[thinking]` 块**保留**，拆到 reasoning 里由模板折叠展示 —— 公开页不该裸露标签，
 * 但也不该把"这次回答想了什么"整个丢掉。顺序与 ChatView 一致。
 */
const messages = computed(() =>
  (share.value?.messages ?? []).map(m => {
    const { reasoning, body } = splitThinking(
      stripUserInputTag(String(m.content ?? '')),
      { loose: true },
    )
    return { ...m, reasoning, body }
  }),
)

function handleClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement).closest('.code-copy') as HTMLElement | null
  if (!btn) return
  const code = decodeURIComponent(btn.dataset.code || '')
  if (!code) return
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '已复制！'
    setTimeout(() => { btn.textContent = '复制' }, 2000)
  }).catch(() => { /* clipboard unavailable */ })
}

onMounted(async () => {
  const id = String(route.params.id || '')
  try {
    share.value = await getPublicShare(id)
  } catch (e: any) {
    const status = e?.response?.status
    if (status === 410) error.value = '此分享已被创建者删除'
    else if (status === 404) error.value = '分享不存在或已失效'
    else error.value = '加载失败，请稍后重试'
  } finally {
    loading.value = false
  }
})

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <div class="share-page">
    <header class="share-header">
      <div class="share-brand">
        <span class="share-brand-mark">MC</span>
        <span>{{ $t('Chiron · 对话分享') }}</span>
      </div>
    </header>

    <main class="share-main">
      <PageSkeleton
        v-if="loading"
        variant="detail"
        :rows="4"
      />
      <EmptyState
        v-else-if="error"
        size="page"
        :description="error"
      />
      <template v-else-if="share">
        <div class="share-head">
          <h1 class="share-title">
            {{ share.title || '新对话' }}
          </h1>
          <div class="share-meta">
            {{ formatDate(share.created_at) }} · {{ share.messages.length }} 条消息
          </div>
        </div>

        <div
          class="share-thread"
          @click="handleClick"
        >
          <div
            v-for="(m, i) in messages"
            :key="i"
            class="share-msg"
            :class="m.role"
          >
            <div class="share-msg-bubble">
              <!-- 思考过程折叠展示：不裸露 [thinking] 标签，也不丢弃内容 -->
              <details
                v-if="m.reasoning"
                class="share-msg-reasoning"
              >
                <summary>{{ $t('思考过程') }}</summary>
                <div class="share-msg-reasoning-body">
                  {{ m.reasoning }}
                </div>
              </details>
              <div
                v-if="m.role === 'assistant'"
                class="share-msg-text"
                v-html="renderMarkdown(m.body)"
              />
              <div
                v-else
                class="share-msg-text"
              >
                {{ m.body }}
              </div>
            </div>
          </div>
        </div>
      </template>
    </main>

    <footer class="share-footer">
      {{ $t('© Chiron 生成 · 本页可被任何获得链接的人查看') }}
    </footer>
  </div>
</template>

<style scoped>
.share-page { min-height: 100vh; display: flex; flex-direction: column; background: var(--bg-page); color: var(--text-primary); }
.share-header { height: 52px; display: flex; align-items: center; padding: 0 24px; border-bottom: 1px solid var(--border); background: var(--bg-card); }
.share-brand { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; color: var(--text-primary); }
.share-brand-mark { width: 22px; height: 22px; border-radius: 6px; background: linear-gradient(135deg, var(--primary), var(--primary-dark)); color: #fff; font-weight: 700; font-size: 10px; display: inline-flex; align-items: center; justify-content: center; }
.share-main { flex: 1; width: 100%; max-width: 748px; margin: 0 auto; padding: 32px 24px 60px; min-width: 0; }
.share-head { margin-bottom: 24px; }
.share-title { font-size: 22px; line-height: 30px; font-weight: 700; margin: 0 0 6px; word-break: break-word; }
.share-meta { font-size: 12px; color: var(--text-tertiary); }
.share-thread { display: flex; flex-direction: column; gap: 18px; }
.share-msg { display: flex; }
.share-msg.user { justify-content: flex-end; }
.share-msg-bubble { min-width: 0; max-width: 100%; }
.share-msg.user .share-msg-bubble {
  max-width: min(525px, 100%);
  padding: 10px 16px;
  background: var(--bubble-user); color: var(--bubble-user-text);
  border-radius: 22px; border-bottom-right-radius: 6px;
  font-size: 15px; line-height: 24px; white-space: pre-wrap; word-break: break-word;
}
.share-msg-text { font-size: 16px; line-height: 28px; color: var(--text-primary); word-break: break-word; }
.share-msg-text :deep(p) { margin: 16px 0; }
.share-msg-text :deep(p:first-child) { margin-top: 0; }
.share-msg-text :deep(h1) { font-size: 24px; line-height: 34px; font-weight: 700; margin: 32px 0 16px; }
.share-msg-text :deep(h2) { font-size: 22px; line-height: 32px; font-weight: 700; margin: 32px 0 16px; }
.share-msg-text :deep(h3) { font-size: 20px; line-height: 30px; font-weight: 700; margin: 32px 0 16px; }
.share-msg-text :deep(ul), .share-msg-text :deep(ol) { padding-left: 24px; margin: 16px 0; }
.share-msg-text :deep(li) { margin: 4px 0; }
.share-msg-text :deep(blockquote) { margin: 16px 0; padding: 6px 12px; border-left: 3px solid var(--primary); background: var(--bg-hover); border-radius: 6px; }
.share-msg-text :deep(a) { color: var(--primary); text-decoration: none; }
.share-msg-text :deep(a:hover) { text-decoration: underline; }
.share-msg-text :deep(code) { font-family: var(--font-mono); font-size: 0.9em; background: var(--bg-secondary); padding: 2px 6px; border-radius: 6px; }
.share-msg-text :deep(.code-block) { margin: 16px 0; background: var(--bg-code); border-radius: 12px; overflow: hidden; max-width: 100%; }
.share-msg-text :deep(.code-block-head) { display: flex; justify-content: space-between; align-items: center; padding: 8px 14px; background: var(--bg-secondary); }
.share-msg-text :deep(.code-lang) { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); }
.share-msg-text :deep(.code-copy) { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-family: inherit; font-size: 12px; padding: 4px 10px; min-height: 32px; border-radius: 6px; transition: color 0.2s, background-color 0.2s; }
.share-msg-text :deep(.code-copy:hover) { color: var(--primary); background: var(--bg-hover); }
/* 代码块：横向滚动而非折行，长行不撑破页面 */
.share-msg-text :deep(pre) { margin: 0 !important; padding: 16px; overflow-x: auto; white-space: pre; word-break: normal; -webkit-overflow-scrolling: touch; }
.share-msg-text :deep(pre code) { background: none; padding: 0; font-size: 0.9em; color: var(--text-code); }
.share-msg-text :deep(table) { border-collapse: collapse; margin: 16px 0; width: 100%; }
.share-msg-text :deep(th), .share-msg-text :deep(td) { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
.share-msg-text :deep(th) { background: var(--bg-secondary); font-weight: 600; }
.share-footer { padding: 18px 24px; text-align: center; font-size: 12px; color: var(--text-muted); border-top: 1px solid var(--border); }

/* 移动端：小屏收紧 padding 和字号 */
@media (max-width: 640px) {
  .share-header { height: 48px; padding: 0 16px; }
  .share-main { padding: 20px 16px 40px; }
  .share-title { font-size: 18px; line-height: 26px; }
  .share-msg.user .share-msg-bubble { font-size: 14px; line-height: 22px; padding: 8px 12px; }
  .share-msg-text { font-size: 15px; line-height: 26px; }
  .share-msg-text :deep(h1) { font-size: 20px; line-height: 28px; margin: 24px 0 12px; }
  .share-msg-text :deep(h2) { font-size: 18px; line-height: 26px; margin: 24px 0 12px; }
  .share-msg-text :deep(h3) { font-size: 16px; line-height: 24px; margin: 24px 0 12px; }
  .share-msg-text :deep(pre) { padding: 12px; font-size: 12px; }
  /* 宽表格窄屏横向滚动，避免撑破页面 */
  .share-msg-text :deep(table) { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .share-footer { padding: 14px 16px; font-size: 11px; }
}

/* 窄屏（≤576px）：复制按钮触控目标 ≥ 40px */
@media (max-width: 576px) {
  .share-msg-text :deep(.code-copy) { min-height: 40px; padding: 8px 14px; font-size: 13px; }
}

@media (prefers-reduced-motion: reduce) {
  .share-page { animation: none; transition: none; }
}
</style>
