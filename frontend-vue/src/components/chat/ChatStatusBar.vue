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
  /** 压缩触发比例（引擎下发，租户/模式可覆盖）。前端不写死 —— 常量写两处必漂移 */
  compactRatio?: number | null
  /** 上下文分段（各类别的估算 token）：回答"上下文被谁占满了" */
  breakdown?: Record<string, number> | null
  /** 占用最大的几条（只含类别/角色/大小，**不含消息内容**）：回答"是哪一条占的" */
  largest?: Array<{ class?: string; role?: string; tokens?: number }> | null
  /** 前缀缓存诊断：`prefix_changed` 非空说明稳定前缀被打断（命中率会掉） */
  cache?: { prefix_changed?: string[]; first_turn?: boolean } | null
  /** 最近一次自动压缩（问题 4）：让"上下文被压缩了"可见，而不是悄悄变短 */
  compaction?: { beforeTokens?: number; afterTokens?: number; savedTokens?: number } | null
  /** 任务预算快照（每轴 `{used, limit}`；`limit=0` = 该轴不限）。引擎五轴一直在跑，此前完全不外露 */
  budget?: Record<string, { used?: number; limit?: number }> | null
  /** 会话累计成本（分）。计价在网关侧，引擎不产生该值；null = 未取到，不显示 */
  costCents?: number | null
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
  return t('common.compressed_before_after', { before: k(c.beforeTokens), after: k(c.afterTokens) })
})

/** 预算速览：只挑**有上限**的轴（`limit=0` 是"不限"，摆出来只是噪音）。 */
const budgetText = computed(() => {
  const b = props.budget
  if (!b) return ''
  const parts: string[] = []
  if (b.steps?.limit) parts.push(`${b.steps.used ?? 0}/${b.steps.limit}`)
  if (b.tokens?.limit) parts.push(`${Math.round(((b.tokens.used ?? 0) / b.tokens.limit) * 100)}%`)
  return parts.join(' · ')
})

/** 完整快照进 title：不常看的轴（turns/wall/cost）不占版面，但查得到。 */
const budgetTitle = computed(() => {
  const b = props.budget
  if (!b) return ''
  return Object.entries(b)
    .map(([axis, v]) => `${axis}: ${v?.used ?? 0}${v?.limit ? ` / ${v.limit}` : ' (unlimited)'}`)
    .join('\n')
})

/** 会话累计成本（分 → 元）。此前只在需展开的 SessionStatsPanel 里能看到。 */
const costText = computed(() => {
  const c = props.costCents
  if (c == null) return ''
  return `¥${(c / 100).toFixed(2)}`
})

/**
 * 前缀缓存被谁打断。**只在真的变了时显示**（`prefix_changed` 非空），首轮不算——
 * 命中率掉下来时回答"为什么"，而不是把"没变"也摆出来占版面。
 */
const cacheWarnText = computed(() => {
  const parts = props.cache?.prefix_changed
  if (!parts || parts.length === 0) return ''
  return t('common.cache_prefix_changed', { parts: parts.join(' + ') })
})
</script>

<template>
  <div class="chat-status">
    <span class="cs-item cs-model">{{ model || $t('agent.default_model') }}</span>
    <!-- 任务预算：仅在引擎下发了快照时显示；五轴里只在有上限的轴占版面，其余进 title -->
    <span
      v-if="budgetText"
      class="cs-item cs-budget"
      :title="budgetTitle"
    >{{ budgetText }}</span>
    <span
      v-if="costText"
      class="cs-item cs-cost tnum"
    >{{ costText }}</span>
    <!-- 前缀缓存被打断：只在真的变了时出现 —— 回答"命中率为什么会掉" -->
    <span
      v-if="cacheWarnText"
      class="cs-item cs-cache-warn"
    >{{ cacheWarnText }}</span>
    <template v-if="hasUsage">
      <span
        class="cs-sep"
        aria-hidden
      />
      <span class="cs-item tnum">{{ stats?.inputTokens ?? 0 }} in / {{ stats?.outputTokens ?? 0 }} out</span>
      <span
        v-if="stats?.durationSec"
        class="cs-item"
      ><span class="tnum-fixed">{{ stats.durationSec }}s</span></span>
    </template>
    <span
      v-if="compactionText"
      class="cs-item cs-compaction"
    >{{ compactionText }}</span>
    <span class="cs-spacer" />
    <ContextRing
      :used="contextUsed ?? null"
      :limit="contextLimit ?? null"
      :compact-ratio="compactRatio ?? null"
      :breakdown="breakdown ?? null"
      :largest="largest ?? null"
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
