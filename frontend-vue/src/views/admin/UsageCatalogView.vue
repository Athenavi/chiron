<template>
  <div class="usage-catalog-view">
    <div class="page-header">
      <div>
        <h2 class="page-title">
          {{ $t('admin.usage_catalog') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.usage_catalog_desc') }}
        </p>
      </div>
      <div class="controls">
        <Select
          :model-value="groupBy"
          :options="groupOptions"
          size="small"
          style="width: 140px"
          @update:value="onGroupChange"
        />
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('common.refresh') }}
        </Button>
      </div>
    </div>

    <div
      v-if="catalog"
      class="totals"
    >
      <div class="totals-row">
        <span class="totals-label">{{ $t('admin.usage_total') }}</span>
        <span>{{ formatTokens(catalog.total.input_tokens) }} in ·
          {{ formatTokens(catalog.total.output_tokens) }} out ·</span>
        <span>{{ formatCost(catalog.total.cost_cents) }} ·</span>
        <span>{{ $t('admin.usage_records_n', { n: catalog.total.records }) }}</span>
      </div>
      <div class="totals-note">
        {{ catalog.note }}
      </div>
    </div>

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, hideOnSinglePage: true }"
      size="small"
      row-key="bucket"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button, Select, Table } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { getUsageCatalog, type UsageBucket, type UsageCatalog } from '../../api/admin'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(false)
const groupBy = ref<'day' | 'model' | 'user'>('day')
const catalog = ref<UsageCatalog | null>(null)

const groupOptions = computed(() => [
  { value: 'day', label: t('admin.usage_group_day') },
  { value: 'model', label: t('admin.usage_group_model') },
  { value: 'user', label: t('admin.usage_group_user') },
])

const columns = computed(() => [
  {
    title: t(`admin.usage_group_${groupBy.value}`),
    dataIndex: 'bucket',
    key: 'bucket',
  },
  { title: t('admin.usage_input_tokens'), dataIndex: 'input_tokens', key: 'in', align: 'right' as const },
  { title: t('admin.usage_output_tokens'), dataIndex: 'output_tokens', key: 'out', align: 'right' as const },
  { title: t('admin.usage_cost'), dataIndex: 'cost_cents', key: 'cost', align: 'right' as const },
  { title: t('admin.usage_records'), dataIndex: 'records', key: 'records', align: 'right' as const },
])

// 表格里把 token 与金额**格式化后**再展示（原始列仍参与排序）。
const rows = computed(() =>
  (catalog.value?.buckets ?? []).map((b: UsageBucket) => ({
    key: b.bucket,
    bucket: b.bucket,
    input_tokens: formatTokens(b.input_tokens),
    output_tokens: formatTokens(b.output_tokens),
    cost_cents: formatCost(b.cost_cents),
    records: b.records,
  })),
)

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
  try {
    catalog.value = await getUsageCatalog({ groupBy: groupBy.value })
  } finally {
    loading.value = false
  }
}

function onGroupChange(v: unknown) {
  const g = String(v) as 'day' | 'model' | 'user'
  groupBy.value = g
  void load()
}

onMounted(load)
</script>

<style scoped>
.usage-catalog-view { padding: 4px; }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.page-title { margin: 0; font-size: 18px; font-weight: 600; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.controls { display: flex; gap: 8px; align-items: center; }
.totals { margin-bottom: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-card); }
.totals-row { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; font-size: 13px; }
.totals-label { font-weight: 600; }
.totals-note { margin-top: 4px; font-size: 12px; color: var(--text-secondary); }
</style>
