<script setup lang="ts">
/**
 * 会话详情：消息流 + 过程记录（工具调用）+ 用量。
 *
 * 对等旧实现的详情页，但有两处刻意的不同：
 *
 *   1. **不再渲染 Markdown**。旧实现自己写了一套 `renderMarkdown`，输入来自用户与模型，
 *      却靠 40 处手写 `escapeHtml` 兜底 —— 漏一处就是 XSS。这里按纯文本渲染，
 *      需要富文本的用户回到对话页（那里有经过测试的渲染管线）。
 *   2. **工具输出只显示预览**。完整原文按 8KB 分段读（`getToolCallOutput`），
 *      在详情面板里做分页会把"读全文"这件事拆成两套实现；这里明确提示去对话页看。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { getChatSessionMessages } from '../../../api'
import { getSessionMetrics, type SessionMetrics } from '../../../api/sessionRuntime'

const props = defineProps<{
  sessionId: string
  title?: string
}>()

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()

interface RawMessage {
  id?: string
  role?: string
  content?: string
  created_at?: string
}

/** `internal/model/model.go:67-88` 的 `ToolCall` —— 列表接口给的是**预览**。 */
interface RawToolCall {
  id?: string
  tool_name?: string
  input?: string
  output?: string
  is_error?: boolean
  duration_ms?: number
  output_bytes?: number
  output_truncated?: boolean
}

const loading = ref(false)
const failed = ref(false)
const messages = ref<RawMessage[]>([])
const toolCalls = ref<RawToolCall[]>([])
const metrics = ref<SessionMetrics | null>(null)
/** 过程记录默认收起：它通常比正文长得多，展开会把正文挤走。 */
const showProcess = ref(false)

async function load() {
  const sid = props.sessionId
  if (!sid) return
  loading.value = true
  failed.value = false
  try {
    const res = await getChatSessionMessages(sid)
    // 两种包装形态并存：直接 `{messages}`，或 `{data:{messages}}`（同 ChatView.vue:1221）
    const payload = (res?.messages ? res : (res?.data ?? {})) as {
      messages?: unknown[]
      tool_calls?: unknown[]
    }
    messages.value = Array.isArray(payload.messages) ? (payload.messages as RawMessage[]) : []
    toolCalls.value = Array.isArray(payload.tool_calls) ? (payload.tool_calls as RawToolCall[]) : []
  } catch {
    failed.value = true
    messages.value = []
    toolCalls.value = []
  } finally {
    loading.value = false
  }

  // 用量单独取：它读的是 `turns` + `billing_records` 的归并结果，
  // 与消息条数不是同一个口径，失败也不该让正文读不出来。
  try {
    metrics.value = await getSessionMetrics(sid)
  } catch {
    metrics.value = null
  }
}

watch(() => props.sessionId, load, { immediate: true })

/** 只展示能读的正文；工具调用的 content 通常是空串，留在列表里只会是一堆空行。 */
const bubbles = computed(() =>
  messages.value
    .filter(m => (m.role === 'user' || m.role === 'assistant') && (m.content ?? '').trim() !== '')
    .map((m, index) => ({
      key: m.id || `m-${index}`,
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content ?? '',
    })),
)

const errorCount = computed(() => toolCalls.value.filter(tc => tc.is_error).length)

const totals = computed(() => metrics.value?.totals ?? {})

const fmtInt = (n?: number) => (n ?? 0).toLocaleString()

function dims(tc: RawToolCall): string {
  const parts: string[] = []
  if (typeof tc.duration_ms === 'number' && tc.duration_ms > 0) parts.push(`${Math.round(tc.duration_ms)} ms`)
  if (typeof tc.output_bytes === 'number' && tc.output_bytes > 0) parts.push(`${fmtInt(tc.output_bytes)} B`)
  return parts.join(' · ')
}
</script>

<template>
  <section
    class="map-detail"
    :aria-label="t('chat.sessionmap.detail')"
  >
    <header class="map-detail-head">
      <h3 class="map-detail-title">
        {{ title || t('chat.sessionmap.untitled') }}
      </h3>
      <button
        type="button"
        class="map-detail-btn"
        :aria-label="t('chat.sessionmap.cancel')"
        @click="emit('close')"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>

    <p
      v-if="loading"
      class="map-detail-note"
    >
      {{ t('chat.sessionmap.loading') }}
    </p>
    <p
      v-else-if="failed"
      class="map-detail-note is-error"
      role="alert"
    >
      {{ t('chat.sessionmap.serviceFailed') }}
    </p>
    <p
      v-else-if="bubbles.length === 0"
      class="map-detail-note"
    >
      {{ t('chat.sessionmap.detailEmpty') }}
    </p>

    <ol
      v-else
      class="map-detail-thread"
    >
      <li
        v-for="bubble in bubbles"
        :key="bubble.key"
        class="map-detail-bubble"
        :class="`is-${bubble.role}`"
      >
        <p class="map-detail-text">
          {{ bubble.content }}
        </p>
      </li>
    </ol>

    <!-- 过程记录：默认收起 -->
    <section
      v-if="toolCalls.length > 0"
      class="map-detail-process"
    >
      <button
        type="button"
        class="map-detail-toggle"
        :aria-expanded="showProcess"
        @click="showProcess = !showProcess"
      >
        <span aria-hidden="true">{{ showProcess ? '▾' : '▸' }}</span>
        {{ t('chat.sessionmap.process') }} ({{ toolCalls.length }})
        <span
          v-if="errorCount > 0"
          class="map-detail-errors"
        >· {{ t('chat.sessionmap.errors') }} {{ errorCount }}</span>
      </button>

      <ul
        v-if="showProcess"
        class="map-detail-tools"
      >
        <li
          v-for="(tc, index) in toolCalls"
          :key="tc.id || `tc-${index}`"
          class="map-detail-tool"
          :class="{ 'is-error': tc.is_error }"
        >
          <p class="map-detail-tool-head">
            <span class="map-detail-tool-name">{{ tc.tool_name || '—' }}</span>
            <span
              v-if="dims(tc)"
              class="map-detail-tool-dims"
            >{{ dims(tc) }}</span>
          </p>
          <pre
            v-if="tc.output"
            class="map-detail-tool-output"
          >{{ tc.output }}</pre>
          <p
            v-if="tc.output_truncated"
            class="map-detail-note"
          >
            {{ t('chat.sessionmap.outputTruncated') }}
          </p>
        </li>
      </ul>
    </section>

    <!-- 用量：来自 turns + billing_records 的归并结果 -->
    <dl
      v-if="metrics"
      class="map-detail-usage"
    >
      <div class="map-detail-usage-item">
        <dt>{{ t('chat.sessionmap.turns') }}</dt>
        <dd>{{ fmtInt(totals.turns) }}</dd>
      </div>
      <div class="map-detail-usage-item">
        <dt>{{ t('chat.stats.inputTokens') }}</dt>
        <dd>{{ fmtInt(totals.input_tokens) }}</dd>
      </div>
      <div class="map-detail-usage-item">
        <dt>{{ t('chat.stats.outputTokens') }}</dt>
        <dd>{{ fmtInt(totals.output_tokens) }}</dd>
      </div>
    </dl>
  </section>
</template>

<style scoped>
.map-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-block-size: 0;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-primary);
  text-align: start;
}

.map-detail-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.map-detail-title {
  flex: 1;
  min-inline-size: 0;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  overflow-wrap: anywhere;
}

.map-detail-btn {
  flex-shrink: 0;
  padding: 0 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-detail-btn:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.map-detail-btn:focus-visible,
.map-detail-toggle:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-detail-thread {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.map-detail-bubble {
  padding: 8px 10px;
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
}

/* 用户消息靠主色一档区分；颜色都走 token，RTL 下不需要换边 */
.map-detail-bubble.is-user {
  background: var(--accent-bg);
}

.map-detail-text {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-snug);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.map-detail-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-detail-note.is-error {
  color: var(--warning);
}

.map-detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.map-detail-toggle:hover {
  color: var(--primary);
  border-color: var(--primary);
}

.map-detail-errors {
  color: var(--warning);
}

.map-detail-tools {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.map-detail-tool {
  padding: 6px 8px;
  border: var(--hairline-width) solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
}

.map-detail-tool.is-error {
  border-color: var(--warning);
}

.map-detail-tool-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin: 0;
}

.map-detail-tool-name {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.map-detail-tool-dims {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* 预格式化输出：等宽 + 横向滚动，避免长单行撑破面板 */
.map-detail-tool-output {
  margin: 6px 0 0;
  padding: 6px;
  max-block-size: 200px;
  overflow: auto;
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: var(--lh-snug);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.map-detail-usage {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin: 0;
}

.map-detail-usage-item {
  padding: 6px 8px;
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
}

.map-detail-usage-item dt {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-detail-usage-item dd {
  margin: 2px 0 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  font-variant-numeric: tabular-nums;
}
</style>
