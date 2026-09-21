<script setup lang="ts">
import { computed } from 'vue'
import { CloudServerOutlined, DisconnectOutlined } from '@ant-design/icons-vue'
import ContextRing from './ContextRing.vue'
import type { TurnStatsItem } from './chat-types'

const props = defineProps<{
  model?: string
  /** 最近一轮的用量（后端在回合结束时下发 turn_stats；没有则整段隐藏） */
  stats?: TurnStatsItem | null
  /** 上下文占用：分子 = 最近一轮 input tokens，分母 = 模型 context_window */
  contextUsed?: number | null
  contextLimit?: number | null
  /** 最近一次自动压缩（问题 4）：让"上下文被压缩了"可见，而不是悄悄变短 */
  compaction?: { beforeTokens?: number; afterTokens?: number; savedTokens?: number } | null
  online: boolean
}>()

const hasUsage = computed(() => Boolean(props.stats && (props.stats.inputTokens || props.stats.outputTokens)))

/** 压缩提示文案：仅在有实际节省时显示 */
const compactionText = computed(() => {
  const c = props.compaction
  if (!c || !c.savedTokens) return ''
  const k = (n?: number) => (n ? `${(n / 1000).toFixed(1)}k` : '0')
  return `已压缩 ${k(c.beforeTokens)} → ${k(c.afterTokens)}`
})
</script>

<template>
  <div class="chat-status">
    <span class="cs-item cs-model">{{ model || '默认模型' }}</span>
    <template v-if="hasUsage">
      <span
        class="cs-sep"
        aria-hidden
      />
      <span class="cs-item">{{ stats?.inputTokens ?? 0 }} in / {{ stats?.outputTokens ?? 0 }} out</span>
      <span
        v-if="stats?.durationSec"
        class="cs-item"
      >{{ stats.durationSec }}s</span>
    </template>
    <span
      v-if="compactionText"
      class="cs-item cs-compaction"
    >{{ compactionText }}</span>
    <span class="cs-spacer" />
    <ContextRing
      :used="contextUsed ?? null"
      :limit="contextLimit ?? null"
    />
    <span
      class="cs-item cs-conn"
      :class="{ offline: !online }"
    >
      <CloudServerOutlined v-if="online" />
      <DisconnectOutlined v-else />
      <span>{{ online ? '已连接' : '离线' }}</span>
    </span>
  </div>
</template>

<!-- 状态栏只读展示：字号与颜色都走 token，切主题自动跟随 -->
<style scoped>
.chat-status {
  flex: none;
  display: flex; align-items: center; gap: 8px;
  height: 26px; padding: 0 16px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-page);
  font-size: 11px; color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.cs-item { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.cs-model { max-width: 38%; overflow: hidden; text-overflow: ellipsis; font-family: var(--font-mono); }
.cs-sep { flex: none; width: 2px; height: 2px; border-radius: 1px; background: var(--text-muted); }
.cs-spacer { flex: 1; }
.cs-conn.offline { color: var(--warning); }
@media (max-width: 576px) { .chat-status { padding: 0 12px; } .cs-model { max-width: 30%; } }
</style>
