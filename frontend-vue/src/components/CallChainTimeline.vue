<!-- Unified CallChain Timeline — 聊天界面底部折叠面板 -->
<template>
  <div
    v-if="isVisible"
    class="callchain-timeline"
  >
    <CollapseTransition>
      <div
        class="timeline-header"
        role="button"
        tabindex="0"
        :aria-expanded="isExpanded"
        @click="toggle"
        @keydown.enter.prevent="toggle"
        @keydown.space.prevent="toggle"
      >
        <span class="header-title">
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M8 1v14M1 8h14"
              stroke="currentColor"
              stroke-width="2"
            />
          </svg>
          {{ $t('工具调用链 ({n} 个工具, 总耗时 {ms}ms)', { n: spanCount, ms: totalDurationMs }) }}
        </span>
        <span class="header-icon">{{ isExpanded ? '▼' : '▶' }}</span>
      </div>

      <div
        v-if="isExpanded"
        class="timeline-body"
      >
        <!-- 时间线渲染 -->
        <div
          v-for="(span, idx) in displaySpans"
          :key="idx"
          class="timeline-node"
        >
          <div
            class="node-line"
            :class="getNodeClass(span)"
          >
            <span class="node-icon">{{ getNodeIcon(span) }}</span>
            <div class="node-content">
              <div class="node-header">
                <span class="node-name">{{ getSpanDisplayName(span) }}</span>
                <span class="node-duration">{{ span.duration_ms }}ms</span>
              </div>
              
              <!-- 展开详情 -->
              <div
                v-if="span.showDetails"
                class="node-details"
              >
                <pre class="metadata-json">{{ formatMetadata(span.metadata) }}</pre>
              </div>
            </div>
            
            <button
              class="details-toggle"
              @click.stop="span.showDetails = !span.showDetails"
            >
              {{ span.showDetails ? $t('收起') : $t('详情') }}
            </button>
          </div>
        </div>

        <!-- 完成事件 -->
        <div class="timeline-node complete">
          <CheckCircleOutlined style="color: var(--success)" />
          <span>{{ $t('推理完成 (总耗时 {ms}ms)', { ms: totalDurationMs }) }}</span>
        </div>
      </div>
    </CollapseTransition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { CheckCircleOutlined } from '@ant-design/icons-vue'
import CollapseTransition from '@/components/CollapseTransition.vue'
import { api } from '../api'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

interface TraceSpan {
  trace_id: string
  span_name: string
  duration_ms: number
  timestamp: string
  tenant_id: string
  metadata?: Record<string, unknown>
  showDetails?: boolean
}

const props = defineProps<{
  /** 兼容旧用法：直接传入 spans */
  visible?: boolean
  spans?: TraceSpan[]
  /** 新用法：传 traceId 由组件自行拉取 GET /v1/traces/{trace_id} */
  traceId?: string
  tenantId?: string
}>()

const emit = defineEmits<{
  (e: 'select', span: TraceSpan): void
}>()

const fetchedSpans = ref<TraceSpan[]>([])
const fetching = ref(false)

// traceId 模式：拉取该 trace 的全部 span
watch(() => props.traceId, async (id) => {
  if (!id) {
    fetchedSpans.value = []
    return
  }
  fetching.value = true
  try {
    const response = await api.get(`/v1/traces/${encodeURIComponent(id)}`)
    const payload = response.data?.data ?? response.data
    fetchedSpans.value = (payload?.spans || []).map((s: TraceSpan) => ({ ...s, showDetails: false }))
  } catch {
    fetchedSpans.value = []
  } finally {
    fetching.value = false
  }
}, { immediate: true })

// 展示用的 spans：traceId 模式用拉取结果，否则用传入的 spans
const displaySpans = computed<TraceSpan[]>(() =>
  props.traceId ? fetchedSpans.value : (props.spans || [])
)

const isVisible = computed(() =>
  props.traceId ? displaySpans.value.length > 0 : !!props.visible
)

const isExpanded = ref(false)

function toggle() {
  isExpanded.value = !isExpanded.value
}

const spanCount = computed(() => displaySpans.value.filter(s => s.span_name.startsWith('tool:')).length)
const totalDurationMs = computed(() => displaySpans.value.reduce((sum, s) => sum + s.duration_ms, 0))

function getNodeClass(span: TraceSpan): string {
  if (span.span_name === 'llm_call') return 'type-llm'
  if (span.span_name.startsWith('tool:')) return 'type-tool'
  if (span.span_name.includes('workflow')) return 'type-workflow'
  return 'type-default'
}

function getNodeIcon(span: TraceSpan): string {
  if (span.span_name === 'llm_call') return '🧠'
  if (span.span_name.startsWith('tool:')) {
    const toolName = span.span_name.replace('tool:', '')
    const iconMap: Record<string, string> = {
      read_file: '📄',
      write_file: '✏️',
      shell_exec: '💻',
      grep_files: '🔍',
      workflow_run: '⚙️',
    }
    return iconMap[toolName] || '🔧'
  }
  return '📊'
}

function getSpanDisplayName(span: TraceSpan): string {
  if (span.span_name === 'llm_call') {
    const model = span.metadata?.model || 'unknown'
    const inputTokens = span.metadata?.input_tokens || 0
    return t('LLM 调用 ({model}, {tokens} tokens)', { model, tokens: inputTokens })
  }
  if (span.span_name.startsWith('tool:')) {
    const toolName = span.span_name.replace('tool:', '')
    return capitalize(toolName.replace(/_/g, ' '))
  }
  return span.span_name
}

function formatMetadata(metadata?: Record<string, unknown>): string {
  if (!metadata) return '{}'
  return JSON.stringify(metadata, null, 2)
}

function capitalize(str: string): string {
  return str.charAt(0).toUpperCase() + str.slice(1)
}
</script>

<style scoped>
.callchain-timeline {
  margin-top: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--bg-card);
}

.timeline-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  background: var(--bg-secondary);
  cursor: pointer;
  user-select: none;
  transition: background var(--dur-fast) ease;
}

.timeline-header:hover {
  background: var(--bg-hover);
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.header-icon {
  font-size: 12px;
  color: var(--text-tertiary);
}

.timeline-body {
  padding: 12px;
  max-height: 400px;
  overflow-y: auto;
}

.timeline-node {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
}

.complete {
  color: var(--success);
  font-weight: 500;
}

.node-line {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--border);
  border-radius: 8px;
  transition: background var(--dur-fast) ease, border-color var(--dur-fast) ease;
}

.node-line:hover {
  background: var(--bg-hover);
}

.node-line.type-llm {
  border-left-color: var(--node-violet);
}

.node-line.type-tool {
  border-left-color: var(--info);
}

.node-line.type-workflow {
  border-left-color: var(--node-pink);
}

.node-icon {
  font-size: 18px;
}

.node-content {
  flex: 1;
  min-width: 0;
}

.node-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.node-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-duration {
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.node-details {
  margin-top: 8px;
}

.metadata-json {
  background: var(--bg-secondary);
  padding: 8px 12px;
  border-radius: 6px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
  color: var(--text-secondary);
  word-break: break-all;
}

.details-toggle {
  padding: 4px 10px;
  font-size: 12px;
  color: var(--primary);
  background: none;
  border: 1px solid var(--primary);
  border-radius: 6px;
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
  flex-shrink: 0;
}

.details-toggle:hover {
  background: var(--primary);
  color: var(--on-solid);
}

.details-toggle:focus-visible,
.timeline-header:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

/* 移动端：行内元素压缩，详情按钮触控目标放大 */
@media (max-width: 768px) {
  .timeline-body { padding: 10px; }
  .node-line { gap: 8px; padding: 8px 10px; }
  .details-toggle { min-height: 32px; }
}

@media (prefers-reduced-motion: reduce) {
  .timeline-header,
  .node-line,
  .details-toggle {
    transition: none;
  }
}
</style>
