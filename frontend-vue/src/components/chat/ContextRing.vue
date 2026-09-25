<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

/**
 * 上下文占用环。
 *
 * 分子用「最近一轮请求的 input tokens」：每轮请求都会带完整历史，因此它就是
 * 当前上下文规模的真实口径（不是把各轮 input 累加，那样会重复计数）。
 * 没有已用数据（还没发过消息）或模型没配上 context_window 时只显示占位。
 */
const props = defineProps<{
  used?: number | null
  limit?: number | null
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

const title = computed(() => {
  if (percent.value === null) return t('上下文占用：暂无数据')
  return t('上下文占用 {label}：最近一轮请求 {used} tokens / 上限 {limit}', { label: label.value, used: props.used, limit: props.limit })
})
</script>

<template>
  <span
    class="ctx-ring"
    :data-tone="tone"
    :title="title"
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
    <span class="ctx-title">{{ title }}</span>
  </span>
</template>

<style scoped>
.ctx-ring { display: inline-flex; align-items: center; gap: 5px; color: var(--text-tertiary); }
.ctx-ring svg { transform: rotate(-90deg); }
.ctx-track, .ctx-bar { fill: none; stroke-width: 2.5; }
.ctx-track { stroke: var(--border-default); }
.ctx-bar { stroke: currentColor; stroke-linecap: round; transition: stroke-dasharray var(--dur-normal) var(--ease-out); }
.ctx-label { font-variant-numeric: tabular-nums; }
.ctx-ring[data-tone='ok'] { color: var(--success); }
.ctx-ring[data-tone='warn'] { color: var(--warning); }
.ctx-ring[data-tone='danger'] { color: var(--error); }
/* 文案仅供读屏与悬停，不占可见排版（title 属性已提供同样的信息） */
.ctx-title { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (prefers-reduced-motion: reduce) { .ctx-bar { transition: none; } }
</style>
