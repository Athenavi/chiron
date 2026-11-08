<script setup lang="ts">
import { computed } from 'vue'
import { Popover } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

/**
 * 上下文占用环 + 构成明细。
 *
 * 分子用「最近一轮请求的 input tokens」：每轮请求都会带完整历史，因此它就是
 * 当前上下文规模的真实口径（不是把各轮 input 累加，那样会重复计数）。
 * 没有已用数据（还没发过消息）或模型没配上 context_window 时只显示占位。
 *
 * 构成来自引擎 `_context_breakdown`：系统提示 / 工具定义 / 你的输入 / 模型回复 /
 * 工具输出。它是**估算值**（同一套密度校准），与 `used`（服务商回报）不必逐 token 相等。
 */
const props = defineProps<{
  used?: number | null
  limit?: number | null
  /** 压缩触发比例（0~1，引擎下发）。>0 时在 title 里提示"到多少会被压缩" */
  compactRatio?: number | null
  /** 上下文分段（`system`/`tools`/`user`/`assistant`/`tool_results` 的估算 token） */
  breakdown?: Record<string, number> | null
  /** 占用最大的几条（只含类别/角色/大小，**不含消息内容**） */
  largest?: Array<{ class?: string; role?: string; tokens?: number }> | null
}>()

const R = 8
const CIRC = 2 * Math.PI * R

const percent = computed(() => {
  if (!props.used || !props.limit || props.limit <= 0) return null
  return Math.min(1, props.used / props.limit)
})

const dash = computed(() => (percent.value === null ? 0 : CIRC * percent.value))

const tone = computed(() => {
  if (percent.value === null) return 'idle'
  if (percent.value >= 0.9) return 'danger'
  if (percent.value >= 0.7) return 'warn'
  return 'ok'
})

const label = computed(() => (percent.value === null ? '—' : `${Math.round(percent.value * 100)}%`))

/** 剩余可用 token：比百分比更直接回答"还能聊多久"。`limit` 是模型窗口，`used` 是最近一轮 input。 */
const remaining = computed(() => {
  if (percent.value === null || props.limit == null || props.used == null) return null
  return Math.max(0, props.limit - props.used)
})

/** token 数的紧凑显示（`1.2k`）。`title` 里分段与最大项两处共用，避免抄两份。 */
const fmtK = (n?: number) => (n ? `${(n / 1000).toFixed(1)}k` : '0')

/** 五类构成：名称 + 估算 token + 占本段合计的比例（用于明细条宽度）。 */
const breakdownRows = computed(() => {
  const b = props.breakdown
  if (!b) return []
  const rows = [
    { key: 'system', labelKey: 'common.context_system_prompt', tokens: b.system || 0 },
    { key: 'tools', labelKey: 'common.context_tools_def', tokens: b.tools || 0 },
    { key: 'user', labelKey: 'common.context_user_input', tokens: b.user || 0 },
    { key: 'assistant', labelKey: 'common.context_assistant_reply', tokens: b.assistant || 0 },
    { key: 'tool_results', labelKey: 'common.context_tool_output', tokens: b.tool_results || 0 },
  ].filter(r => r.tokens > 0)
  const total = rows.reduce((a, r) => a + r.tokens, 0) || 1
  return rows.map(r => ({ ...r, pct: `${Math.round((r.tokens / total) * 100)}%` }))
})

const title = computed(() => {
  if (percent.value === null) return t('common.context_usage_no_data')
  const parts = [t('common.context_usage_label_last_request_used_tokens_limit_limit', { label: label.value, used: props.used, limit: props.limit })]
  if (remaining.value !== null) parts.push(t('common.context_remaining_tokens', { n: remaining.value }))
  // 压缩阈值由**引擎**下发（租户/模式可覆盖），前端不复制常量
  if (props.compactRatio && props.compactRatio > 0) {
    parts.push(t('common.context_compacts_at', { pct: Math.round(props.compactRatio * 100) }))
  }
  // 分段：回答"上下文被谁占满了"。一个带参数的键覆盖五类，避免五份易漂的独立文案。
  const b = props.breakdown
  if (b) {
    parts.push(t('common.context_breakdown', {
      system: fmtK(b.system),
      tools: fmtK(b.tools),
      user: fmtK(b.user),
      assistant: fmtK(b.assistant),
      toolResults: fmtK(b.tool_results),
    }))
  }
  // 最大的几项：引擎只给类别/角色/大小，**不含消息内容**（隐私）；"是谁占的"这就够了。
  const top = props.largest
  if (top?.length) {
    parts.push(t('common.context_largest_items', {
      items: top.map(x => `${x.role || x.class || '?'} ${fmtK(x.tokens)}`).join(' / '),
    }))
  }
  parts.push(t('common.context_click_for_breakdown'))
  return parts.join(' ')
})
</script>

<template>
  <Popover
    trigger="click"
    placement="topRight"
  >
    <template #content>
      <div class="ctx-detail">
        <div class="ctx-detail-title">
          {{ t('common.context_breakdown_title') }}
        </div>
        <div
          v-for="row in breakdownRows"
          :key="row.key"
          class="ctx-detail-row"
        >
          <span class="ctx-detail-name">{{ t(row.labelKey) }}</span>
          <span class="ctx-detail-bar">
            <span
              class="ctx-detail-fill"
              :style="{ width: row.pct }"
            />
          </span>
          <span class="ctx-detail-val">{{ fmtK(row.tokens) }}</span>
        </div>
        <div
          v-if="!breakdownRows.length"
          class="ctx-detail-empty"
        >
          {{ t('common.context_usage_no_data') }}
        </div>
        <div class="ctx-detail-note">
          {{ t('common.context_breakdown_estimated') }}
        </div>
      </div>
    </template>
    <button
      type="button"
      class="ctx-ring"
      :data-tone="tone"
      :title="title"
      :aria-label="title"
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 20 20"
        aria-hidden="true"
      >
        <circle
          class="ctx-track"
          cx="10"
          cy="10"
          :r="R"
        />
        <circle
          class="ctx-bar"
          cx="10"
          cy="10"
          :r="R"
          :stroke-dasharray="`${dash} ${CIRC}`"
        />
      </svg>
      <span class="ctx-label">{{ label }}</span>
    </button>
  </Popover>
</template>

<style scoped>
.ctx-ring {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0;
  color: var(--text-tertiary);
  background: none;
  border: none;
  cursor: pointer;
  font: inherit;
}
.ctx-ring:hover { color: var(--text-secondary); }
.ctx-ring svg { transform: rotate(-90deg); }
.ctx-track, .ctx-bar { fill: none; stroke-width: 2.5; }
.ctx-track { stroke: var(--border-default); }
.ctx-bar { stroke: currentColor; stroke-linecap: round; transition: stroke-dasharray var(--dur-normal) var(--ease-out); }
.ctx-label { font-variant-numeric: tabular-nums; }
.ctx-ring[data-tone='ok'] { color: var(--success); }
.ctx-ring[data-tone='warn'] { color: var(--warning); }
.ctx-ring[data-tone='danger'] { color: var(--error); }

/* 构成明细（Popover 内容） */
.ctx-detail { min-width: 240px; color: var(--text-primary); }
.ctx-detail-title { margin-bottom: 8px; font-size: var(--fs-xs); font-weight: var(--fw-semibold); }
.ctx-detail-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  font-size: var(--fs-xs);
}
.ctx-detail-name { flex: none; width: 72px; color: var(--text-secondary); }
.ctx-detail-bar {
  flex: 1;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  overflow: hidden;
}
.ctx-detail-fill {
  display: block;
  height: 100%;
  border-radius: var(--radius-full);
  background: var(--primary);
}
.ctx-detail-val { flex: none; width: 44px; text-align: end; font-variant-numeric: tabular-nums; }
.ctx-detail-empty { font-size: var(--fs-xs); color: var(--text-tertiary); }
.ctx-detail-note { margin-top: 8px; font-size: var(--fs-xs); color: var(--text-tertiary); }
@media (prefers-reduced-motion: reduce) { .ctx-bar { transition: none; } }
</style>
