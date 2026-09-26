<script setup lang="ts">
/**
 * 会话统计（问题 5）：tokens / 费用 / **缓存命中率** / 吞吐 / 压缩记录。
 *
 * 数据来自 `GET /v1/sessions/{id}/metrics`（internal/api/session_runtime.go）：
 * 后端把「实时层」（Redis 里以 turn_id 为 field 的每轮明细，**读取时聚合**，天然幂等）
 * 与「权威层」（`turns` + `billing_records`）归一到同一份 `totals`，并用 `source`
 * 字段标明本次数据来自哪一层 —— 排查"数字对不上"时先看它。
 */
import { computed, ref, watch } from 'vue'
import { getSessionMetrics, type SessionMetrics } from '../../api/sessionRuntime'

const props = defineProps<{ sessionId?: string }>()

const metrics = ref<SessionMetrics | null>(null)
const loading = ref(false)
const failed = ref(false)

async function load() {
  if (!props.sessionId) {
    metrics.value = null
    return
  }
  loading.value = true
  failed.value = false
  try {
    metrics.value = await getSessionMetrics(props.sessionId)
  } catch {
    // 指标不可用不该打断阅读：面板自己降级成一行提示
    failed.value = true
    metrics.value = null
  } finally {
    loading.value = false
  }
}
watch(() => props.sessionId, load, { immediate: true })

const fmtInt = (n?: number) => (n ?? 0).toLocaleString()
const fmtCost = (cents?: number) => `$${((cents ?? 0) / 100).toFixed(4)}`
const fmtRate = (r?: number) => (r === undefined || r === null ? '—' : `${(r * 100).toFixed(1)}%`)
const fmtMs = (ms?: number) => (ms ? `${Math.round(ms)} ms` : '—')
const fmtTps = (tps?: number) => (tps ? `${tps.toFixed(1)} tok/s` : '—')

const totals = computed(() => metrics.value?.totals || {})
const throughput = computed(() => metrics.value?.throughput)

const rows = computed(() => [
  { label: 'chat.stats.turns', value: fmtInt(totals.value.turns) },
  { label: 'chat.stats.inputTokens', value: fmtInt(totals.value.input_tokens) },
  { label: 'chat.stats.outputTokens', value: fmtInt(totals.value.output_tokens) },
  { label: 'chat.stats.cachedTokens', value: fmtInt(totals.value.cached_tokens) },
  { label: 'chat.stats.cacheHitRate', value: fmtRate(totals.value.cache_hit_rate) },
  { label: 'chat.stats.cost', value: fmtCost(totals.value.cost_cents) },
])

const speedRows = computed(() => {
  const t = throughput.value
  if (!t) return []
  return [
    { label: 'chat.stats.ttftP50', value: fmtMs(t.ttft_ms_p50) },
    { label: 'chat.stats.outputTpsP50', value: fmtTps(t.output_tps_p50) },
    { label: 'chat.stats.outputTpsP95', value: fmtTps(t.output_tps_p95) },
  ]
})
</script>

<template>
  <div class="stats-panel">
    <div
      v-if="!sessionId"
      class="stats-empty"
    >
      {{ $t('chat.select_a_session_first_or_send_a_message_to_view_stats') }}
    </div>
    <div
      v-else-if="loading"
      class="stats-empty"
    >
      {{ $t('common.loading') }}
    </div>
    <div
      v-else-if="failed"
      class="stats-empty"
    >
      {{ $t('admin.statistics_unavailable_engine_or_database_temporarily_unreachable') }}
    </div>
    <template v-else>
      <dl class="stats-grid">
        <template
          v-for="row in rows"
          :key="row.label"
        >
          <dt>{{ $t(row.label) }}</dt>
          <dd>{{ row.value }}</dd>
        </template>
      </dl>
      <template v-if="speedRows.length">
        <div class="stats-sub">
          {{ $t('common.throughput') }}
        </div>
        <dl class="stats-grid">
          <template
            v-for="row in speedRows"
            :key="row.label"
          >
            <dt>{{ $t(row.label) }}</dt>
            <dd>{{ row.value }}</dd>
          </template>
        </dl>
      </template>
      <div
        v-if="metrics?.source"
        class="stats-source"
      >
        {{ $t('common.data_source') }}: {{ metrics.source === 'redis' ? $t('admin.realtime_layer_redis') : $t('admin.authoritative_layer_database') }}
      </div>
    </template>
  </div>
</template>

<style scoped>
.stats-panel {
  padding: 10px 12px;
  font-size: 12px;
  color: var(--text-secondary);
}
.stats-empty {
  padding: 12px 4px;
  color: var(--text-tertiary);
  font-size: 12px;
}
.stats-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 4px 12px;
  margin: 0;
}
.stats-grid dt {
  color: var(--text-tertiary);
}
.stats-grid dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.stats-sub {
  margin: 10px 0 4px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.stats-source {
  margin-top: 10px;
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
