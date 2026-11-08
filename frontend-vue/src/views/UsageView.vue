<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Select, Table } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { api } from '../api'
import { errorMessageText } from '../utils/apiError'
import { useI18n } from 'vue-i18n'

/**
 * 个人用量（普通用户自助）。
 *
 * 数据来自 GET /v1/billing/usage：按天分组时是记账口径（credit_transactions，
 * 含充值），按模型/会话分组时是真实用量（usage_events）—— 两者不可混用，否则
 * "按模型看花费"会拿到空数组。接口与账单页（BillingView）用的是同一个，
 * 这里只是把它从账单页里独立出来，让用户不必先找计费页。
 */
const { t } = useI18n()

type UsageGroup = 'day' | 'model' | 'session'
type UsageRange = '7d' | '30d' | '90d'

interface DailyRow {
  day: string
  tx_count: number
  credits_spent: number
  credits_added: number
}

interface BreakdownRow {
  bucket: string
  label?: string
  event_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_cents: number
}

interface UsageData {
  daily: DailyRow[]
  period_days: number
  group_by?: UsageGroup
  breakdown?: BreakdownRow[]
  total_input?: number
  total_output?: number
  total_cached?: number
  total_cost?: number
  truncated?: boolean
  total_buckets?: number
}

const group = ref<UsageGroup>('day')
const range = ref<UsageRange>('30d')
const loading = ref(false)
const error = ref('')
const usage = ref<UsageData | null>(null)

const groupOptions = computed(() => [
  { value: 'day', label: t('billing.usage_group_day') },
  { value: 'model', label: t('billing.usage_group_model') },
  { value: 'session', label: t('billing.usage_group_session') },
])

const rangeOptions = computed(() => [
  { value: '7d', label: t('billing.usage_range_days', { n: 7 }) },
  { value: '30d', label: t('billing.usage_range_days', { n: 30 }) },
  { value: '90d', label: t('billing.usage_range_days', { n: 90 }) },
])

const isDayGroup = computed(() => group.value === 'day')

const dayRows = computed(() =>
  [...(usage.value?.daily ?? [])]
    .reverse()
    .map(r => ({
      key: r.day,
      day: r.day,
      tx_count: r.tx_count,
      credits_spent: r.credits_spent,
      credits_added: r.credits_added,
    })),
)

const breakdownRows = computed(() =>
  (usage.value?.breakdown ?? []).map(r => ({
    key: r.bucket,
    name: r.label || (r.bucket === 'unknown' ? t('billing.usage_unknown_bucket') : r.bucket),
    event_count: r.event_count,
    input_tokens: r.input_tokens,
    output_tokens: r.output_tokens,
    cached_tokens: r.cached_tokens,
    cost_cents: r.cost_cents,
  })),
)

/** 汇总：按天口径只有记账流入/流出，其余维度是真实 token 与花费。 */
const summary = computed(() => {
  const u = usage.value
  if (!u) return null
  if (isDayGroup.value) {
    const spent = u.daily.reduce((a, r) => a + (r.credits_spent || 0), 0)
    const added = u.daily.reduce((a, r) => a + (r.credits_added || 0), 0)
    const count = u.daily.reduce((a, r) => a + (r.tx_count || 0), 0)
    return [
      { label: t('billing.usage_col_cost'), value: String(spent) },
      { label: t('billing.top_up_credits'), value: String(added) },
      { label: t('billing.usage_col_events'), value: String(count) },
    ]
  }
  return [
    { label: t('billing.usage_col_input_tokens'), value: formatTokens(u.total_input) },
    { label: t('billing.usage_col_output_tokens'), value: formatTokens(u.total_output) },
    { label: t('billing.usage_col_cached_tokens'), value: formatTokens(u.total_cached) },
    { label: t('billing.usage_col_cost'), value: String(u.total_cost ?? 0) },
  ]
})

const dayColumns = computed(() => [
  { title: t('billing.usage_col_day'), dataIndex: 'day', key: 'day' },
  { title: t('billing.usage_col_events'), dataIndex: 'tx_count', key: 'tx_count', align: 'right' as const },
  { title: t('billing.usage_col_cost'), dataIndex: 'credits_spent', key: 'credits_spent', align: 'right' as const },
  { title: t('billing.top_up_credits'), dataIndex: 'credits_added', key: 'credits_added', align: 'right' as const },
])

const breakdownColumns = computed(() => [
  {
    title: {
      day: t('billing.usage_col_day'),
      model: t('billing.usage_col_model'),
      session: t('billing.usage_col_session'),
    }[group.value],
    dataIndex: 'name',
    key: 'name',
  },
  { title: t('billing.usage_col_events'), dataIndex: 'event_count', key: 'event_count', align: 'right' as const },
  { title: t('billing.usage_col_input_tokens'), dataIndex: 'input_tokens', key: 'input_tokens', align: 'right' as const },
  { title: t('billing.usage_col_output_tokens'), dataIndex: 'output_tokens', key: 'output_tokens', align: 'right' as const },
  { title: t('billing.usage_col_cached_tokens'), dataIndex: 'cached_tokens', key: 'cached_tokens', align: 'right' as const },
  { title: t('billing.usage_col_cost'), dataIndex: 'cost_cents', key: 'cost_cents', align: 'right' as const },
])

function formatTokens(n?: number): string {
  if (n == null || !Number.isFinite(n)) return '-'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const resp = await api.get('/v1/billing/usage', {
      params: { groupBy: group.value, range: range.value },
    })
    usage.value = (resp.data?.data as UsageData | undefined) ?? null
  } catch (e) {
    error.value = errorMessageText(e, t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function onQueryChange() {
  void load()
}

onMounted(load)
</script>

<template>
  <div class="usage-view">
    <div class="page-header">
      <div>
        <h2 class="page-title">
          {{ $t('billing.my_usage') }}
        </h2>
        <p class="page-desc">
          {{ $t('billing.my_usage_desc') }}
        </p>
      </div>
      <div class="controls">
        <Select
          v-model:value="group"
          :options="groupOptions"
          size="small"
          style="width: 130px"
          @change="onQueryChange"
        />
        <Select
          v-model:value="range"
          :options="rangeOptions"
          size="small"
          style="width: 120px"
          @change="onQueryChange"
        />
        <button
          type="button"
          class="refresh-btn"
          :disabled="loading"
          :aria-label="$t('common.refresh')"
          @click="load"
        >
          <ReloadOutlined />
          <span>{{ $t('common.refresh') }}</span>
        </button>
      </div>
    </div>

    <p
      v-if="error"
      class="error-line"
      role="alert"
    >
      {{ error }}
    </p>

    <div
      v-if="summary"
      class="summary"
    >
      <div
        v-for="item in summary"
        :key="item.label"
        class="summary-item"
      >
        <span class="summary-label">{{ item.label }}</span>
        <span class="summary-value">{{ item.value }}</span>
      </div>
    </div>

    <Table
      v-if="isDayGroup"
      :columns="dayColumns"
      :data-source="dayRows"
      :loading="loading"
      :pagination="{ pageSize: 20, hideOnSinglePage: true }"
      size="small"
      row-key="key"
    />

    <template v-else>
      <Table
        :columns="breakdownColumns"
        :data-source="breakdownRows"
        :loading="loading"
        :pagination="{ pageSize: 20, hideOnSinglePage: true }"
        size="small"
        row-key="key"
      />
      <p class="hint">
        {{ $t('billing.usage_breakdown_hint') }}
      </p>
      <p
        v-if="usage?.truncated"
        class="hint"
      >
        {{ $t('billing.usage_breakdown_truncated', {
          shown: breakdownRows.length,
          total: usage.total_buckets ?? breakdownRows.length,
        }) }}
      </p>
    </template>
  </div>
</template>

<style scoped>
.usage-view { padding: 4px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.page-title { margin: 0; font-size: var(--fs-xl); font-weight: var(--fw-semibold); color: var(--text-primary); }
.page-desc { margin: 4px 0 0; font-size: var(--fs-xs); color: var(--text-secondary); }
.controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.refresh-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; font: inherit; font-size: var(--fs-xs);
  color: var(--text-secondary); background: var(--bg-card);
  border: var(--hairline-width) solid var(--border); border-radius: var(--radius-md);
  cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.refresh-btn:hover { color: var(--text-primary); border-color: var(--border-strong, var(--border)); }
.refresh-btn:disabled { opacity: 0.6; cursor: default; }
.summary { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }
.summary-item {
  display: flex; flex-direction: column; gap: 4px;
  min-width: 130px; padding: 10px 14px;
  background: var(--bg-card); border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
}
.summary-label { font-size: var(--fs-xs); color: var(--text-secondary); }
.summary-value { font-size: var(--fs-lg); font-weight: var(--fw-semibold); color: var(--text-primary); }
.error-line { margin: 0 0 12px; font-size: var(--fs-xs); color: var(--danger, var(--error)); }
.hint { margin: 8px 0 0; font-size: var(--fs-xs); color: var(--text-tertiary); }
</style>
