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
    message.error(e?.response?.data?.error || t('加载失败'))
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
    message.error(e?.response?.data?.error || t('用量加载失败'))
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
    message.warning(t('租户 ID 必填'))
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
      message.success(t('已创建'))
    } else {
      await updateQuota(poolForm.value.id, {
        resource_type: poolForm.value.resource_type,
        total_amount: poolForm.value.total_amount,
        period: poolForm.value.period,
      })
      message.success(t('已更新'))
    }
    poolModalVisible.value = false
    fetchPools()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('保存失败'))
  } finally {
    poolSaving.value = false
  }
}

function confirmDeletePool(p: QuotaPoolWithAllocated) {
  Modal.confirm({
    title: t('删除配额池'),
    content: t('确认删除「{type} / {period}」？关联分配将级联删除。', { type: p.resource_type, period: p.period }),
    okText: t('删除'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: async () => {
      try {
        await deleteQuota(p.id)
        message.success(t('已删除'))
        fetchPools()
      } catch (e: any) {
        message.error(e?.response?.data?.error || t('删除失败'))
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
    message.error(e?.response?.data?.error || t('加载分配失败'))
    return
  }
  allocDrawerVisible.value = true
}

async function addAllocation() {
  if (!currentPool.value) return
  if (!allocForm.value.target_id.trim()) {
    message.warning(t('目标 ID 必填'))
    return
  }
  allocSaving.value = true
  try {
    await createAllocation(currentPool.value.id, {
      target_type: allocForm.value.target_type,
      target_id: allocForm.value.target_id,
      amount: allocForm.value.amount,
    })
    message.success(t('已分配'))
    // 刷新分配列表与池的 allocated
    const res = await getQuota(currentPool.value.id)
    allocations.value = res.allocations
    fetchPools()
    allocForm.value.target_id = ''
    allocForm.value.amount = 0
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('分配失败'))
  } finally {
    allocSaving.value = false
  }
}

async function removeAllocation(allocID: string) {
  if (!currentPool.value) return
  try {
    await deleteAllocation(currentPool.value.id, allocID)
    message.success(t('已删除'))
    allocations.value = allocations.value.filter(a => a.id !== allocID)
    fetchPools()
  } catch (e: any) {
    message.error(e?.response?.data?.error || t('删除失败'))
  }
}

function fmtAmount(n: number, type: string): string {
  if (!Number.isFinite(n)) return '—'
  if (n === 0) return t('无限制')
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
  { title: t('资源类型'), dataIndex: 'resource_type', key: 'resource_type', width: 120 },
  { title: t('周期'), dataIndex: 'period', key: 'period', width: 100 },
  { title: t('总量'), key: 'total', width: 140, customRender: ({ record }) => fmtAmount(record.total_amount, record.resource_type) },
  { title: t('已分配'), key: 'allocated', width: 140, customRender: ({ record }) => fmtAmount(record.allocated, record.resource_type) },
  { title: t('当前用量'), key: 'usage', width: 110, customRender: ({ record }) => usagePercent(record) },
  { title: t('租户'), dataIndex: 'tenant_id', key: 'tenant_id', width: 120, ellipsis: true },
  { title: t('操作'), key: 'action', width: 220, fixed: 'right' },
])

const allocColumns: TableColumnsType = [
  { title: t('目标类型'), dataIndex: 'target_type', key: 'target_type', width: 100 },
  { title: t('目标 ID'), dataIndex: 'target_id', key: 'target_id', ellipsis: true },
  { title: t('配额'), dataIndex: 'amount', key: 'amount', width: 120 },
  { title: t('操作'), key: 'action', width: 80, fixed: 'right' },
]

onMounted(fetchPools)
</script>

<template>
  <div class="cost-view">
    <div class="page-header">
      <h2 class="page-title">
        {{ $t('成本中心与资源池化') }}
      </h2>
      <a-space class="filter-bar">
        <a-input
          v-model:value="currentTenantID"
          :placeholder="$t('租户 ID（按租户过滤）')"
          allow-clear
          style="width: 300px"
          @press-enter="() => { fetchPools(); fetchUsage() }"
        />
        <a-button @click="() => { fetchPools(); fetchUsage() }">
          {{ $t('查询') }}
        </a-button>
        <a-button
          :disabled="!currentTenantID"
          @click="fetchUsage"
        >
          {{ $t('刷新用量') }}
        </a-button>
        <a-button
          type="primary"
          @click="openCreatePool"
        >
          {{ $t('新建配额池') }}
        </a-button>
      </a-space>
    </div>

    <a-alert
      type="info"
      show-icon
      :message="$t('total_amount = 0 表示无限制（不校验超额）；token 类型用量优先读 Redis 计数器，缺失时从 billing_records SQL 聚合。')"
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
          <span class="empty-icon">📭</span><span class="empty-text">{{ $t('暂无数据') }}</span>
        </div>
      </template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'action'">
          <a-button
            type="link"
            size="small"
            @click="openAllocDrawer(record as QuotaPoolWithAllocated)"
          >
            {{ $t('分配') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            @click="openEditPool(record as QuotaPoolWithAllocated)"
          >
            {{ $t('编辑') }}
          </a-button>
          <a-button
            type="link"
            size="small"
            danger
            @click="confirmDeletePool(record as QuotaPoolWithAllocated)"
          >
            {{ $t('删除') }}
          </a-button>
        </template>
      </template>
    </a-table>

    <a-modal
      v-model:open="poolModalVisible"
      :title="poolModalMode === 'create' ? $t('新建配额池') : $t('编辑配额池')"
      :confirm-loading="poolSaving"
      @ok="savePool"
    >
      <a-form layout="vertical">
        <a-form-item :label="$t('租户 ID（UUID）')">
          <a-input
            v-model:value="poolForm.tenant_id"
            :disabled="poolModalMode === 'edit'"
            :placeholder="$t('租户 UUID')"
          />
        </a-form-item>
        <a-form-item :label="$t('资源类型')">
          <a-select v-model:value="poolForm.resource_type">
            <a-select-option value="token">
              {{ $t('token（令牌数）') }}
            </a-select-option>
            <a-select-option value="storage_mb">
              {{ $t('storage_mb（存储 MB）') }}
            </a-select-option>
            <a-select-option value="concurrency">
              {{ $t('concurrency（并发数）') }}
            </a-select-option>
            <a-select-option value="credits">
              {{ $t('credits（积分）') }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item :label="$t('总量（0 = 无限制）')">
          <a-input-number
            v-model:value="poolForm.total_amount"
            :min="0"
            style="width: 100%"
          />
        </a-form-item>
        <a-form-item :label="$t('周期')">
          <a-radio-group v-model:value="poolForm.period">
            <a-radio value="daily">
              {{ $t('日') }}
            </a-radio>
            <a-radio value="monthly">
              {{ $t('月') }}
            </a-radio>
          </a-radio-group>
        </a-form-item>
      </a-form>
    </a-modal>

    <a-drawer
      v-model:open="allocDrawerVisible"
      :title="currentPool ? $t('配额分配 - {type}/{period}', { type: currentPool.resource_type, period: currentPool.period }) : $t('配额分配')"
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
          <a-descriptions-item :label="$t('总量')">
            {{ fmtAmount(currentPool.total_amount, currentPool.resource_type) }}
          </a-descriptions-item>
          <a-descriptions-item :label="$t('已分配')">
            {{ fmtAmount(currentPool.allocated, currentPool.resource_type) }}
          </a-descriptions-item>
        </a-descriptions>

        <div class="alloc-form">
          <a-select
            v-model:value="allocForm.target_type"
            style="width: 100px"
          >
            <a-select-option value="group">
              {{ $t('群组') }}
            </a-select-option>
            <a-select-option value="user">
              {{ $t('用户') }}
            </a-select-option>
          </a-select>
          <a-input
            v-model:value="allocForm.target_id"
            :placeholder="$t('目标 ID（UUID）')"
            style="flex: 1"
          />
          <a-input-number
            v-model:value="allocForm.amount"
            :min="0"
            :placeholder="$t('配额')"
            style="width: 140px"
          />
          <a-button
            type="primary"
            :loading="allocSaving"
            @click="addAllocation"
          >
            {{ $t('添加') }}
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
              <span class="empty-icon">📭</span><span class="empty-text">{{ $t('暂无数据') }}</span>
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
                {{ $t('删除') }}
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
