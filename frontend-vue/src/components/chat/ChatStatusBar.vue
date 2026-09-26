<script setup lang="ts">
import { computed } from 'vue'
import { CloudServerOutlined, DisconnectOutlined, ApartmentOutlined, BarChartOutlined } from '@ant-design/icons-vue'
import ContextRing from './ContextRing.vue'
import type { TurnStatsItem } from './chat-types'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

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
  /**
   * 运行中的子 Agent 数。>0 时「子 Agent」触发器显示角标 ——
   * 解决"有子 Agent 在跑，界面上却看不出来"这类感知缺失（子 Agent 与统计已从侧栏移至悬浮窗）。
   */
  subagentActiveCount?: number
}>()

const emit = defineEmits<{
  (e: 'toggle-subagents'): void
  (e: 'toggle-stats'): void
}>()

const hasUsage = computed(() => Boolean(props.stats && (props.stats.inputTokens || props.stats.outputTokens)))

/** 压缩提示文案：仅在有实际节省时显示 */
const compactionText = computed(() => {
  const c = props.compaction
  if (!c || !c.savedTokens) return ''
  const k = (n?: number) => (n ? `${(n / 1000).toFixed(1)}k` : '0')
  return t('已压缩 {before} → {after}', { before: k(c.beforeTokens), after: k(c.afterTokens) })
})
</script>

<template>
  <div class="chat-status">
    <span class="cs-item cs-model">{{ model || $t('agent.default_model') }}</span>
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
    <!-- 观测面板入口：子 Agent 与统计已从侧栏移出（侧栏只留"导航"：轨迹 / 会话历史）。
         复用 .cs-item 的行内样式，不引入新的尺寸体系；角标只在真有运行时出现。 -->
    <button
      type="button"
      class="cs-item cs-trigger"
      :title="$t('agent.sub_agent_run_popover')"
      @click="emit('toggle-subagents')"
    >
      <ApartmentOutlined />
      <span>{{ $t('agent.sub_agent') }}</span>
      <span
        v-if="subagentActiveCount"
        class="cs-badge"
      >{{ subagentActiveCount }}</span>
    </button>
    <button
      type="button"
      class="cs-item cs-trigger"
      :title="$t('billing.session_stats_tokens_cost_cache_hits_throughput_popover')"
      @click="emit('toggle-stats')"
    >
      <BarChartOutlined />
      <span>{{ $t('common.statistics') }}</span>
    </button>
    <span
      class="cs-item cs-conn"
      :class="{ offline: !online }"
    >
      <CloudServerOutlined v-if="online" />
      <DisconnectOutlined v-else />
      <span>{{ online ? $t('chat.status.online') : $t('chat.status.offline') }}</span>
    </span>
  </div>
</template>

<!-- 状态栏：除两个悬浮窗触发器外均为只读展示；字号与颜色都走 token，切主题自动跟随 -->
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
/* 观测面板触发器：本行唯一的交互入口。button 需显式重置默认样式，
   才能与旁边的只读项看起来完全一致（不引入新的尺寸/字色体系）。 */
.cs-trigger {
  padding: 2px 6px; margin: 0 -4px;
  border: none; border-radius: var(--radius-sm);
  background: transparent; color: inherit;
  font: inherit; cursor: pointer;
}
.cs-trigger:hover { background: var(--bg-hover); color: var(--text-primary); }
.cs-trigger:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
/* 运行中的子 Agent 数量角标（仅 >0 时渲染 —— 无信息就不显示） */
.cs-badge {
  min-width: 14px; height: 14px; padding: 0 4px;
  border-radius: var(--radius-full);
  background: var(--primary); color: var(--on-solid);
  font-size: 10px; line-height: 14px; text-align: center;
  font-variant-numeric: tabular-nums;
}
@media (max-width: 576px) { .chat-status { padding: 0 12px; } .cs-model { max-width: 30%; } }
</style>
