<script setup lang="ts">
/**
 * 参考价格目录：展示经**多源交叉验证**的大模型价格。
 *
 * 这张页面回答的是账单页回答不了的问题：「这个模型的市场价大概是多少」。
 * 两个必须讲清楚的口径（页面上也直接写出来）：
 *   1. 数据由引擎侧 `app/pricing/sync.py` 拉取多个公开价格源后写入，
 *      仅当 **≥2 个源在 5% 容差内一致**才入库 —— 每行都带 `sources`。
 *   2. ⚠ **不参与计费**：实际扣费以 `BillingConfig` 为准，这里只是选型参考。
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Alert, Empty, Input, Select, Space, Table, Tag, Tooltip } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { listModelPrices, type ModelPrice } from '../api'

const { t } = useI18n()

const loading = ref(false)
const rows = ref<ModelPrice[]>([])
const providers = ref<string[]>([])
const keyword = ref('')
const provider = ref<string>('')
const sort = ref('model')
const note = ref('')
const unit = ref('')

const sortOptions = computed(() => [
  { value: 'model', label: t('pricing.sort_model') },
  { value: 'input', label: t('pricing.sort_input') },
  { value: 'output', label: t('pricing.sort_output') },
  { value: 'mixed', label: t('pricing.sort_mixed') },
  { value: 'verified', label: t('pricing.sort_verified') },
])

const columns = computed(() => [
  { title: t('pricing.col_model'), dataIndex: 'model', key: 'model', ellipsis: true },
  { title: t('pricing.col_provider'), dataIndex: 'provider', key: 'provider', width: 140 },
  { title: t('pricing.col_input'), dataIndex: 'input_per_1k', key: 'input_per_1k', width: 130, align: 'right' as const },
  { title: t('pricing.col_output'), dataIndex: 'output_per_1k', key: 'output_per_1k', width: 130, align: 'right' as const },
  { title: t('pricing.col_sources'), dataIndex: 'sources', key: 'sources', width: 240 },
  { title: t('pricing.col_verified'), dataIndex: 'verified_at', key: 'verified_at', width: 180 },
])

async function load() {
  loading.value = true
  try {
    const catalog = await listModelPrices({
      q: keyword.value.trim() || undefined,
      provider: provider.value || undefined,
      sort: sort.value,
    })
    rows.value = catalog.models
    note.value = catalog.note
    unit.value = catalog.unit
    // provider 选项只在无筛选时刷新：否则选中某个 provider 后列表收缩成一项，
    // 用户就再也切不回别的了。
    if (!provider.value && !keyword.value.trim()) {
      providers.value = Array.from(new Set(catalog.models.map((m) => m.provider).filter(Boolean))).sort()
    }
  } finally {
    loading.value = false
  }
}

function money(v: number): string {
  // 量级从 0.0001 到几十不等：固定 6 位再裁尾零，避免出现 "0.0750" 这种噪声。
  if (!Number.isFinite(v)) return '-'
  return `$${v.toFixed(6).replace(/0+$/, '').replace(/\.$/, '')}`
}

function when(v: string): string {
  if (!v) return '-'
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString()
}

onMounted(load)
</script>

<template>
  <div class="pricing-view">
    <div class="pricing-header">
      <h2>{{ t('pricing.title') }}</h2>
      <Space :size="8" wrap>
        <Input
          v-model:value="keyword"
          :placeholder="t('pricing.search_placeholder')"
          allow-clear
          style="width: 220px"
          @press-enter="load"
        />
        <Select
          v-model:value="provider"
          :options="providers.map((p) => ({ value: p, label: p }))"
          :placeholder="t('pricing.provider_all')"
          allow-clear
          style="width: 180px"
          @change="load"
        />
        <Select v-model:value="sort" :options="sortOptions" style="width: 200px" @change="load" />
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ t('pricing.refresh') }}
        </a-button>
      </Space>
    </div>

    <Alert type="info" show-icon :closable="false" class="pricing-note">
      <template #message>
        {{ t('pricing.cross_verified_notice') }}
        {{ t('pricing.unit_hint', { unit: unit || 'USD / 1k tokens' }) }}
      </template>
      <template #description>
        {{ t('pricing.not_billed_warning') }}
        <span v-if="note">（{{ note }}）</span>
      </template>
    </Alert>

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, showSizeChanger: true, showTotal: (n: number) => t('pricing.total', { n }) }"
      :row-key="(r: ModelPrice) => `${r.model}|${r.provider}`"
      size="small"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'input_per_1k'">{{ money(record.input_per_1k) }}</template>
        <template v-else-if="column.key === 'output_per_1k'">{{ money(record.output_per_1k) }}</template>
        <template v-else-if="column.key === 'sources'">
          <Tooltip :title="record.sources.join(' + ')">
            <Tag v-for="s in record.sources" :key="s" color="blue">{{ s }}</Tag>
            <Tag v-if="record.source_count > 1" color="green">
              {{ t('pricing.sources_agree', { n: record.source_count }) }}
            </Tag>
          </Tooltip>
        </template>
        <template v-else-if="column.key === 'verified_at'">{{ when(record.verified_at) }}</template>
        <template v-else-if="column.key === 'provider'">
          <Tag v-if="record.provider">{{ record.provider }}</Tag>
          <span v-else class="muted">-</span>
        </template>
      </template>
      <template #emptyText>
        <Empty :description="t('pricing.empty')" />
      </template>
    </Table>
  </div>
</template>

<style scoped>
.pricing-view {
  padding: 16px;
}
.pricing-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.pricing-header h2 {
  margin: 0;
  font-size: 18px;
}
.pricing-note {
  margin-bottom: 12px;
}
.muted {
  color: var(--text-tertiary);
}
</style>
