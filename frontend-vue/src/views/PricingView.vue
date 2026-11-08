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
import { Alert, Collapse, CollapsePanel, Empty, Input, Popover, Select, Space, Table, Tag } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { listModelPrices, type ModelPrice } from '../api'

const { t } = useI18n()

const loading = ref(false)
const rows = ref<ModelPrice[]>([])
const providers = ref<string[]>([])
const keyword = ref('')
const provider = ref<string>('')
const sort = ref('confidence')
const note = ref('')
const unit = ref('')

const sortOptions = computed(() => [
  // 默认「可信度优先」：已验证（≥2 源一致）排在前面，单源参考价在后。
  { value: 'confidence', label: t('pricing.sort_confidence') },
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
  // 可信度：verified=true 表示 ≥2 源在 5% 容差内一致；false 是单源/分歧参考价
  { title: t('pricing.col_confidence'), dataIndex: 'verified', key: 'confidence', width: 110 },
  { title: t('pricing.col_sources'), dataIndex: 'sources', key: 'sources', width: 260 },
  { title: t('pricing.col_verified'), dataIndex: 'verified_at', key: 'verified_at', width: 180 },
])

interface ProviderGroup {
  key: string
  provider: string
  models: ModelPrice[]
  verifiedCount: number
}

// 按厂商分组：折叠面板的 key 用 provider（空值归入"其他"）。
const grouped = computed<ProviderGroup[]>(() => {
  const map = new Map<string, ModelPrice[]>()
  for (const row of rows.value) {
    const key = row.provider || '__unknown__'
    const list = map.get(key)
    if (list) list.push(row)
    else map.set(key, [row])
  }
  return Array.from(map.entries())
    .map(([key, models]) => ({
      key,
      provider: key === '__unknown__' ? t('pricing.provider_unknown') : key,
      models,
      verifiedCount: models.filter(m => m.verified).length,
    }))
    // 已验证多的组排前面（与默认「可信度优先」一致），同分按模型数、再按名称。
    .sort((a, b) =>
      (b.verifiedCount - a.verifiedCount)
      || (b.models.length - a.models.length)
      || a.provider.localeCompare(b.provider),
    )
})

// 折叠态：默认展开第一个组；筛选后若原展开组已不在结果里，回落到第一个组。
const activeKeys = ref<string[]>([])

function syncActiveKeys() {
  const keys = new Set(grouped.value.map(g => g.key))
  const kept = activeKeys.value.filter(k => keys.has(k))
  activeKeys.value = kept.length
    ? kept
    : (grouped.value.length ? [grouped.value[0].key] : [])
}

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
    syncActiveKeys()
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

    <Collapse
      v-if="grouped.length"
      v-model:active-key="activeKeys"
      :destroy-inactive-panel="true"
      class="pricing-groups"
    >
      <CollapsePanel
        v-for="g in grouped"
        :key="g.key"
      >
        <template #header>
          <span class="group-head">
            <span class="group-provider">{{ g.provider }}</span>
            <Tag
              v-if="g.verifiedCount"
              color="green"
            >{{ t('pricing.group_verified_n', { n: g.verifiedCount }) }}</Tag>
            <span class="group-count">{{ t('pricing.group_models_n', { n: g.models.length }) }}</span>
          </span>
        </template>

        <Table
          :columns="columns"
          :data-source="g.models"
          :loading="loading"
          :pagination="{ pageSize: 10, hideOnSinglePage: true }"
          :row-key="(r: ModelPrice) => `${r.model}|${r.provider}`"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'input_per_1k'">{{ money(record.input_per_1k) }}</template>
            <template v-else-if="column.key === 'output_per_1k'">{{ money(record.output_per_1k) }}</template>
            <template v-else-if="column.key === 'confidence'">
              <Tag v-if="record.verified" color="green">{{ t('pricing.verified') }}</Tag>
              <Tag v-else color="orange">{{ t('pricing.single_source') }}</Tag>
            </template>
            <template v-else-if="column.key === 'sources'">
              <Popover trigger="click" placement="bottomLeft">
                <template #content>
                  <div class="source-details">
                    <div class="source-detail-head">{{ t('pricing.source_details_hint') }}</div>
                    <div v-for="d in record.source_details" :key="d.source" class="source-detail-row">
                      <Tag color="blue">{{ d.source }}</Tag>
                      <span class="source-detail-price">{{ money(d.input_per_1k) }} / {{ money(d.output_per_1k) }}</span>
                    </div>
                    <div v-if="!record.source_details?.length" class="source-detail-row muted">
                      {{ t('pricing.source_details_missing') }}
                    </div>
                  </div>
                </template>
                <button type="button" class="sources-link">
                  {{ record.sources.join(' + ') || '-' }}
                </button>
              </Popover>
              <Tag v-if="record.verified" color="green">{{ t('pricing.sources_agree', { n: record.source_count }) }}</Tag>
              <Tag v-else-if="record.source_count > 1" color="orange">{{ t('pricing.sources_differ', { n: record.source_count }) }}</Tag>
            </template>
            <template v-else-if="column.key === 'verified_at'">{{ when(record.verified_at) }}</template>
            <template v-else-if="column.key === 'provider'">
              <Tag v-if="record.provider">{{ record.provider }}</Tag>
              <span v-else class="muted">-</span>
            </template>
          </template>
        </Table>
      </CollapsePanel>
    </Collapse>
    <Empty
      v-else-if="!loading"
      :description="t('pricing.empty')"
    />
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
.pricing-groups { background: transparent; border: none; }
.pricing-groups :deep(.ant-collapse-item) { border-bottom: var(--hairline-width) solid var(--border); }
.pricing-groups :deep(.ant-collapse-header) { padding: 10px 4px; align-items: center; }
.pricing-groups :deep(.ant-collapse-content-box) { padding: 0 0 12px; }
.group-head { display: inline-flex; align-items: center; gap: 8px; }
.group-provider { font-weight: var(--fw-semibold); color: var(--text-primary); }
.group-count { font-size: var(--fs-xs); color: var(--text-tertiary); }
.sources-link {
  padding: 0;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--primary);
  background: none;
  border: none;
  cursor: pointer;
  text-decoration: underline dotted;
}
.source-details {
  max-width: 320px;
}
.source-detail-head {
  margin-bottom: 6px;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.source-detail-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  font-size: var(--fs-xs);
}
.source-detail-price {
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
</style>
