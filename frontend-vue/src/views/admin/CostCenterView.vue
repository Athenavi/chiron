<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import {
  listQuotas, createQuota, updateQuota, deleteQuota,
  createAllocation, deleteAllocation, getQuotaUsage, getQuota,
} from '../../api/costcenter'
import type { QuotaPoolWithAllocated, QuotaAllocation, QuotaUsageRow } from '../../api/costcenter'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const loading = ref(false)
const pools = ref<QuotaPoolWithAllocated[]>([])
const usage = ref<QuotaUsageRow[]>([])
const currentTenantID = ref('')

// 配额池 Modal
const poolModalVisible = ref(false)
const poolModalMode = ref<'create' | 'edit'>('create')
const poolForm = ref({ id: '', tenant_id: '', resource_type: 'token', total_amount: 0, period: 'monthly' })
const poolSaving = ref(false)

// 分配抽屉
const allocDrawerVisible = ref(false)
const currentPool = ref<QuotaPoolWithAllocated | null>(null)
const allocations = ref<QuotaAllocation[]>([])
const allocForm = ref<{ target_type: 'user' | 'group'; target_id: string; amount: number }>({ target_type: 'group', target_id: '', amount: 0 })
const allocSaving = ref(false)

async function fetchPools() {
  loading.value = true
  try {
    const res = await listQuotas(currentTenantID.value || undefined)
    pools.value = res.pools
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

async function fetchUsage() {
  if (!currentTenantID.value) return
  try {
    const res = await getQuotaUsage(currentTenantID.value)
    usage.value = res.pools ?? []
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load_usage_2'))
  }
}

function usageMap(): Record<string, QuotaUsageRow> {
  const m: Record<string, QuotaUsageRow> = {}
  for (const u of usage.value) m[u.pool_id] = u
  return m
}

function openCreatePool() {
  poolModalMode.value = 'create'
  poolForm.value = { id: '', tenant_id: currentTenantID.value, resource_type: 'token', total_amount: 0, period: 'monthly' }
  poolModalVisible.value = true
}

function openEditPool(p: QuotaPoolWithAllocated) {
  poolModalMode.value = 'edit'
  poolForm.value = { id: p.id, tenant_id: p.tenant_id, resource_type: p.resource_type, total_amount: p.total_amount, period: p.period }
  poolModalVisible.value = true
}

async function savePool() {
  if (!poolForm.value.tenant_id.trim()) {
    message.warning(t('errors.tenant_id_is_required'))
    return
  }
  poolSaving.value = true
  try {
    if (poolModalMode.value === 'create') {
      await createQuota({
        tenant_id: poolForm.value.tenant_id,
        resource_type: poolForm.value.resource_type,
        total_amount: poolForm.value.total_amount,
        period: poolForm.value.period,
      })
      message.success(t('common.created'))
    } else {
      await updateQuota(poolForm.value.id, {
        resource_type: poolForm.value.resource_type,
        total_amount: poolForm.value.total_amount,
        period: poolForm.value.period,
      })
      message.success(t('common.updated'))
    }
    poolModalVisible.value = false
    fetchPools()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.save_failed'))
  } finally {
    poolSaving.value = false
  }
}

function confirmDeletePool(p: QuotaPoolWithAllocated) {
  Modal.confirm({
    title: t('errors.delete_quota_pool'),
    content: t('确认删除「{type} / {period}」？关联分配将级联删除。', { type: p.resource_type, period: p.period }),
    okText: t('common.delete'),
    okType: 'danger',
    cancelText: t('common.cancel'),
    onOk: async () => {
      try {
        await deleteQuota(p.id)
        message.success(t('common.deleted'))
        fetchPools()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('errors.delete_failed'))
      }
    },
  })
}

async function openAllocDrawer(p: QuotaPoolWithAllocated) {
  currentPool.value = p
  allocForm.value = { target_type: 'group', target_id: '', amount: 0 }
  try {
    const res = await getQuota(p.id)
    allocations.value = res.allocations
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.failed_to_load_allocation'))
    return
  }
  allocDrawerVisible.value = true
}

async function addAllocation() {
  if (!currentPool.value) return
  if (!allocForm.value.target_id.trim()) {
    message.warning(t('errors.target_id_is_required'))
    return
  }
  allocSaving.value = true
  try {
    await createAllocation(currentPool.value.id, {
      target_type: allocForm.value.target_type,
      target_id: allocForm.value.target_id,
      amount: allocForm.value.amount,
    })
    message.success(t('common.assigned'))
    // 刷新分配列表与池的 allocated
    const res = await getQuota(currentPool.value.id)
    allocations.value = res.allocations
    fetchPools()
    allocForm.value.target_id = ''
    allocForm.value.amount = 0
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.assignment_failed'))
  } finally {
    allocSaving.value = false
  }
}

async function removeAllocation(allocID: string) {
  if (!currentPool.value) return
  try {
    await deleteAllocation(currentPool.value.id, allocID)
    message.success(t('common.deleted'))
    allocations.value = allocations.value.filter(a => a.id !== allocID)
    fetchPools()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('errors.delete_failed'))
  }
}

function fmtAmount(n: number, type: string): string {
  if (!Number.isFinite(n)) return '—'
  if (n === 0) return t('common.unlimited')
  if (type === 'storage_mb') return `${n} MB`
  if (type === 'credits') return `${n} credits`
  return n.toLocaleString()
}

function usagePercent(p: QuotaPoolWithAllocated): string {
  const um = usageMap()
  const u = um[p.id]
  if (!u || p.total_amount === 0 || !Number.isFinite(u.usage_ratio)) return '-'
  return `${(u.usage_ratio * 100).toFixed(1)}%`
}

const poolColumns = computed<TableColumnsType>(() => [
  { title: t('common.resource_type'), dataIndex: 'resource_type', key: 'resource_type', width: 120 },
  { title: t('common.period'), dataIndex: 'period', key: 'period', width: 100 },
  { title: t('common.total'), key: 'total', width: 140, customRender: ({ record }) => fmtAmount(record.total_amount, record.resource_type) },
  { title: t('common.assigned'), key: 'allocated', width: 140, customRender: ({ record }) => fmtAmount(record.allocated, record.resource_type) },
  { title: t('common.current_usage'), key: 'usage', width: 110, customRender: ({ record }) => usagePercent(record) },
  { title: t('admin.tenant'), dataIndex: 'tenant_id', key: 'tenant_id', width: 120, ellipsis: true },
  { title: t('common.action'), key: 'action', width: 220, fixed: 'right' },
])

const allocColumns: TableColumnsType = [
  { title: t('common.target_type'), dataIndex: 'target_type', key: 'target_type', width: 100 },
  { title: t('common.target_id'), dataIndex: 'target_id', key: 'target_id', ellipsis: true },
  { title: t('errors.quota'), dataIndex: 'amount', key: 'amount', width: 120 },
  { title: t('common.action'), key: 'action', width: 80, fixed: 'right' },
]

onMounted(fetchPools)
</script>

<template>
  <div class="cost-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('common.cost_center_resource_pooling') }}
      </h2>
      <a-space class="filter-bar">
        <a-input
          v-model:value="currentTenantID"
          :placeholder="$t('admin.tenant_id_filter_by_tenant')"
          allow-clear
          style="width: 300px"
          @press-enter="() => { fetchPools(); fetchUsage() }"
        />
        <a-button @click="() => { fetchPools(); fetchUsage() }">
          {{ $t('common.query') }}
        </a-button>
        <a-button
          :disabled="!currentTenantID"
          @click="fetchUsage"
        >
          {{ $t('common.refresh_usage') }}
        </a-button>
        <a-button
          type="primary"
          @click="openCreatePool"
        >
          {{ $t('errors.new_quota_pool') }}
        </a-button>
      </a-space>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('errors.total_amount_0_means_unlimited_no_overage_check_token_usage_reads_the_redis_counter_first_falling_back_to_billing_records_sql_aggregation_when_missing')"
      style="margin-bottom: 16px"
    />

    <a-table
      :columns="poolColumns"
      :data-source="pools"
      :loading="loading"
      :row-key="(r: QuotaPoolWithAllocated) => r.id"
      :pagination="false"
      :scroll="{ x: 950 }"
      size="small"
    >
      <template #emptyText>
        <div class="empty-block">
          <span class="empty-icon">📭</span><span class="empty-text">{{ $t('common.no_data_yet') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="openAllocDrawer(record as QuotaPoolWithAllocated)"
          >
            {{ $t('common.assign') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            @click="openEditPool(record as QuotaPoolWithAllocated)"
          >
            {{ $t('common.edit_2') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDeletePool(record as QuotaPoolWithAllocated)"
          >
            {{ $t('common.delete') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="poolModalVisible"
      :title="poolModalMode === 'create' ? $t('errors.new_quota_pool') : $t('errors.edit_quota_pool')"
      :confirm-loading="poolSaving"
      @ok="savePool"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('admin.tenant_id_uuid')">
          <a-input
            v-model:value="poolForm.tenant_id"
            :disabled="poolModalMode === 'edit'"
            :placeholder="$t('admin.tenant_uuid')"
          />
        </a-form-item>
        <a-form-item :label="$t('common.resource_type')">
          <a-select v-model:value="poolForm.resource_type">
            <a-select-option value="token">
              {{ $t('common.token_token_count') }}
            </a-select-option>
            <a-select-option value="storage_mb">
              {{ $t('common.storage_mb_storage_mb') }}
            </a-select-option>
            <a-select-option value="concurrency">
              {{ $t('common.concurrency_parallel_count') }}
            </a-select-option>
            <a-select-option value="credits">
              {{ $t('billing.credits_points') }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="$t('common.total_0_unlimited')">
          <a-input-number
            v-model:value="poolForm.total_amount"
            :min="0"
            style="width: 100%"
          />
        </a-form-item>
        <a-form-item :label="$t('common.period')">
          <a-radio-group v-model:value="poolForm.period">
            <a-radio value="daily">
              {{ $t('common.day') }}
            </a-radio>
            <a-radio value="monthly">
              {{ $t('common.month') }}
            </a-radio>
          </a-radio-group>
        </a-form-item>
      </a-form>
    </a-modal>

    <a-drawer
      v-model:open="allocDrawerVisible"
      :title="currentPool ? $t('配额分配 - {type}/{period}', { type: currentPool.resource_type, period: currentPool.period }) : $t('errors.quota_allocation')"
      width="640"
      placement="right"
    >
      <template v-if="currentPool">
        <a-descriptions
          :column="2"
          size="small"
          bordered
          style="margin-bottom: 16px"
        >
          <a-descriptions-item :label="$t('common.total')">
            {{ fmtAmount(currentPool.total_amount, currentPool.resource_type) }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('common.assigned')">
            {{ fmtAmount(currentPool.allocated, currentPool.resource_type) }}
          </a-descriptions-item>
        </a-descriptions>

        <div class="alloc-form">
          <a-select
            v-model:value="allocForm.target_type"
            style="width: 100px"
          >
            <a-select-option value="group">
              {{ $t('admin.group') }}
            </a-select-option>
            <a-select-option value="user">
              {{ $t('admin.user_2') }}
            </a-select-option>
          </a-select>
          <a-input
            v-model:value="allocForm.target_id"
            :placeholder="$t('common.target_id_uuid')"
            style="flex: 1"
          />
          <a-input-number
            v-model:value="allocForm.amount"
            :min="0"
            :placeholder="$t('errors.quota')"
            style="width: 140px"
          />
          <a-button
            type="primary"
            :loading="allocSaving"
            @click="addAllocation"
          >
            {{ $t('common.add') }}
          </a-button>
        </div>

        <a-table
          :columns="allocColumns"
          :data-source="allocations"
          :row-key="(r: QuotaAllocation) => r.id"
          :pagination="false"
          :scroll="{ x: 520 }"
          size="small"
          style="margin-top: 16px"
        >
          <template #emptyText>
            <div class="empty-block">
              <span class="empty-icon">📭</span><span class="empty-text">{{ $t('common.no_data_yet') }}</span>
            </div>
          </template>
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'action'">
              <a-button
                type="link"
                size="small"
                danger
                @click="removeAllocation(record.id)"
              >
                {{ $t('common.delete') }}
              </a-button>
            </template>
          </template>
        </a-table>
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.cost-view { padding: 16px 24px; }
.page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.page-title { margin: 0; font-size: 20px; }
.alloc-form { display: flex; gap: 8px; align-items: center; }

/* 空状态统一 */
.empty-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 28px 0;
  color: var(--text-tertiary);
}
.empty-icon { font-size: 26px; line-height: 1; opacity: 0.8; }
.empty-text { font-size: 13px; }

/* 窄屏:筛选竖排、抽屉分配表单竖排、触控目标 ≥ 40px */
@media (max-width: 768px) {
  .cost-view .page-header { flex-direction: column; align-items: stretch; }
  .cost-view .page-header :deep(.ant-space) { width: 100%; }
  .cost-view .page-header :deep(.ant-space-item) { flex: 1 1 auto; min-width: 0; }
  .cost-view .page-header :deep(.ant-input) { width: 100% !important; }
  .cost-view .page-header :deep(.ant-btn) { width: 100%; min-height: 40px; }
  .cost-view .alloc-form { flex-direction: column; align-items: stretch; }
  .cost-view .alloc-form :deep(.ant-input),
  .cost-view .alloc-form :deep(.ant-input-number),
  .cost-view .alloc-form :deep(.ant-select) { width: 100% !important; }
  .cost-view .alloc-form :deep(.ant-btn) { width: 100%; min-height: 40px; }
}
</style>
