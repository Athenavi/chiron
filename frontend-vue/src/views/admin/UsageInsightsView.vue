<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Select, Table } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { getUsageCatalog, type UsageBucket, type UsageCatalog } from '../../api/admin'
import { errorMessageText } from '../../utils/apiError'
import { useI18n } from 'vue-i18n'

/**
 * 团队 / 租户用量（管理员视角）。
 *
 * 与「个人用量」是同一份数据源（billing_records 的真实扣费记录），区别只在聚合维度：
 *   - 趋势：按天看本租户的 token 与花费走势；
 *   - 成员：按用户看谁花得多（label 取 users.name，bucket 仍是 user_id）；
 *   - 租户：跨租户对比（仅平台 owner，后端对非 owner 直接 403）。
 *
 * 这是原来 /admin/usage-catalog 的替代页：那个页面只做"用量目录"，既不回答
 * "谁在用"，也无法跨租户看；现在按角色给出可用的视角。
 */
const { t } = useI18n()
const route = useRoute()
// /tenant/usage：本租户的按天/成员明细；/admin/usage：平台只能看租户级。
const isTenantArea = computed(() => route.path.startsWith('/tenant'))

type GroupBy = 'day' | 'user' | 'tenant'
type Range = '7d' | '30d' | '90d'

const group = ref<GroupBy>(isTenantArea.value ? 'day' : 'tenant')
const range = ref<Range>('30d')
const loading = ref(false)
const error = ref('')
const catalog = ref<UsageCatalog | null>(null)

const groupTabs = computed<{ value: GroupBy; label: string }[]>(() => {
  if (isTenantArea.value) {
    return [
      { value: 'day', label: t('admin.usage_group_day') },
      { value: 'user', label: t('admin.usage_group_user') },
    ]
  }
  return [
    { value: 'tenant', label: t('admin.usage_group_tenant') },
    { value: 'day', label: t('admin.usage_group_day') },
  ]
})

const rangeOptions = computed(() => [
  { value: '7d', label: t('billing.usage_range_days', { n: 7 }) },
  { value: '30d', label: t('billing.usage_range_days', { n: 30 }) },
  { value: '90d', label: t('billing.usage_range_days', { n: 90 }) },
])

const rangeDays: Record<Range, number> = { '7d': 7, '30d': 30, '90d': 90 }

const rows = computed(() =>
  (catalog.value?.buckets ?? []).map((b: UsageBucket) => ({
    key: b.bucket,
    name: b.label || b.bucket || t('billing.usage_unknown_bucket'),
    input_tokens: formatTokens(b.input_tokens),
    output_tokens: formatTokens(b.output_tokens),
    cost_cents: formatCost(b.cost_cents),
    records: b.records,
  })),
)

const columns = computed(() => [
  {
    title: {
      day: t('admin.usage_group_day'),
      user: t('admin.usage_group_user'),
      tenant: t('admin.usage_group_tenant'),
    }[group.value],
    dataIndex: 'name',
    key: 'name',
  },
  { title: t('admin.usage_input_tokens'), dataIndex: 'input_tokens', key: 'in', align: 'right' as const },
  { title: t('admin.usage_output_tokens'), dataIndex: 'output_tokens', key: 'out', align: 'right' as const },
  { title: t('admin.usage_cost'), dataIndex: 'cost_cents', key: 'cost', align: 'right' as const },
  { title: t('admin.usage_records'), dataIndex: 'records', key: 'records', align: 'right' as const },
])

const totals = computed(() => {
  const total = catalog.value?.total
  if (!total) return []
  return [
    { label: t('admin.usage_input_tokens'), value: formatTokens(total.input_tokens) },
    { label: t('admin.usage_output_tokens'), value: formatTokens(total.output_tokens) },
    { label: t('admin.usage_cost'), value: formatCost(total.cost_cents) },
    { label: t('admin.usage_records'), value: String(total.records) },
  ]
})

function formatTokens(n: number): string {
  if (!Number.isFinite(n)) return '-'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function formatCost(cents: number): string {
  if (!Number.isFinite(cents)) return '-'
  return `¥${(cents / 100).toFixed(2)}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const to = new Date()
    const from = new Date(to.getTime() - rangeDays[range.value] * 24 * 60 * 60 * 1000)
    catalog.value = await getUsageCatalog({
      groupBy: group.value,
      scope: isTenantArea.value ? 'tenant' : 'admin',
      from: from.toISOString(),
      to: to.toISOString(),
    })
  } catch (e) {
    error.value = errorMessageText(e, t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

watch(group, () => { void load() })
watch(range, () => { void load() })

onMounted(load)
</script>

<template>
  <div class="usage-insights-view">
    <div class="page-header">
      <div>
        <h2 class="page-title">
          {{ $t('admin.usage_insights') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.usage_insights_desc') }}
        </p>
      </div>
      <div class="controls">
        <Select
          v-model:value="range"
          :options="rangeOptions"
          size="small"
          style="width: 120px"
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

    <div
      class="tabs"
      role="tablist"
      :aria-label="$t('admin.usage_insights')"
    >
      <button
        v-for="tab in groupTabs"
        :key="tab.value"
        type="button"
        role="tab"
        class="tab"
        :class="{ active: group === tab.value }"
        :aria-selected="group === tab.value"
        @click="group = tab.value"
      >
        {{ tab.label }}
      </button>
    </div>

    <p
      v-if="error"
      class="error-line"
      role="alert"
    >
      {{ error }}
    </p>

    <div
      v-if="totals.length"
      class="summary"
    >
      <div
        v-for="item in totals"
        :key="item.label"
        class="summary-item"
      >
        <span class="summary-label">{{ item.label }}</span>
        <span class="summary-value">{{ item.value }}</span>
      </div>
    </div>

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, hideOnSinglePage: true }"
      size="small"
      row-key="key"
    />

    <p
      v-if="catalog?.note"
      class="hint"
    >
      {{ catalog.note }}
    </p>
  </div>
</template>

<style scoped>
.usage-insights-view { padding: 4px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.page-title { margin: 0; font-size: var(--fs-xl); font-weight: var(--fw-semibold); color: var(--text-primary); }
.page-desc { margin: 4px 0 0; font-size: var(--fs-xs); color: var(--text-secondary); }
.controls { display: flex; align-items: center; gap: 8px; }
.refresh-btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; font: inherit; font-size: var(--fs-xs);
  color: var(--text-secondary); background: var(--bg-card);
  border: var(--hairline-width) solid var(--border); border-radius: var(--radius-md);
  cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.refresh-btn:hover { color: var(--text-primary); }
.refresh-btn:disabled { opacity: 0.6; cursor: default; }
.tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: var(--hairline-width) solid var(--border); }
.tab {
  padding: 8px 14px; font: inherit; font-size: var(--fs-sm);
  color: var(--text-secondary); background: transparent;
  border: none; border-bottom: 2px solid transparent; cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--primary); border-bottom-color: var(--primary); }
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
