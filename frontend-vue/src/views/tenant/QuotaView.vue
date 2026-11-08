<script setup lang="ts">
/**
 * 租户后台 · 配额管理。
 *
 * 三块内容：
 *  1. **当前配额与用量**：每个资源类型一行的进度条。用量与执行侧同源
 *     （token→usage_events、credits→credit_transactions、storage→media_assets），
 *     所以这里的百分比与"请求被拦"是同一个口径。
 *  2. **提交申请**：选资源 + 周期 + 目标值。同一资源已有待审申请时后端 409。
 *  3. **申请历史**：平台审批结果，含审核备注。
 *
 * 两个必须显式处理的边界（否则界面会骗人）：
 *  - `unlimited=true`（total_amount<=0）时**不能算百分比**：除以 0 会得到 NaN
 *    或 Infinity，进度条会画出满格或空白，两种都是错的。
 *  - `usage_source === 'none'` 表示"没有用量概念"（并发）或"查询失败"，
 *    此时显示 "—" 而不是 0%，否则用户会以为"一点都没用"。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Button,
  Card,
  Form,
  FormItem,
  Input,
  InputNumber,
  Select,
  Table,
  Tag,
  message,
} from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  createQuotaRequest,
  listTenantQuota,
  listTenantQuotaRequests,
  type QuotaPeriod,
  type QuotaPool,
  type QuotaRequest,
  type QuotaRequestStatus,
  type QuotaResourceType,
} from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const submitting = ref(false)
const pools = ref<QuotaPool[]>([])
const requests = ref<QuotaRequest[]>([])
const requestFilter = ref<QuotaRequestStatus | 'all'>('all')

const form = reactive<{
  resourceType: QuotaResourceType
  period: QuotaPeriod
  requestedAmount: number
  reason: string
}>({
  resourceType: 'token',
  period: 'monthly',
  requestedAmount: 0,
  reason: '',
})

const resourceOptions = computed(() =>
  (['token', 'credits', 'concurrency', 'storage_mb'] as QuotaResourceType[]).map(v => ({
    value: v,
    label: t(`admin.quota_resource_${v}`),
  })),
)

const periodOptions = computed(() =>
  (['daily', 'monthly'] as QuotaPeriod[]).map(v => ({
    value: v,
    label: t(`admin.quota_period_${v}`),
  })),
)

const filterOptions = computed(() => [
  { value: 'all', label: t('admin.invoice_review_filter_all') },
  { value: 'pending', label: t('admin.invoice_review_filter_pending') },
  { value: 'approved', label: t('admin.invoice_status_approved') },
  { value: 'rejected', label: t('admin.invoice_status_rejected') },
])

/** 配额卡的展示行：把用量换算成百分比（无限额时不给百分比）。 */
interface PoolRow extends QuotaPool {
  percent: number | null
  label: string
}

const poolRows = computed<PoolRow[]>(() =>
  pools.value.map((p) => {
    // concurrency 没有"已用量"概念，也没有百分比可说。
    const hasUsage = p.usage_source !== 'none'
    const percent =
      p.unlimited || !hasUsage || p.total_amount <= 0
        ? null
        : Math.min(100, Math.round((p.used_amount / p.total_amount) * 100))
    return {
      ...p,
      percent,
      label: t(`admin.quota_resource_${p.resource_type}`),
    }
  }),
)

const columns = computed(() => [
  { title: t('admin.quota_submitted_at'), dataIndex: 'created_at', key: 'created_at', width: 170 },
  { title: t('admin.quota_col_resource'), key: 'resource', width: 160 },
  { title: t('admin.quota_col_current'), dataIndex: 'current_amount', key: 'current_amount', width: 120 },
  { title: t('admin.quota_col_requested'), dataIndex: 'requested_amount', key: 'requested_amount', width: 120 },
  { title: t('admin.quota_col_reason'), dataIndex: 'reason', key: 'reason', ellipsis: true },
  { title: t('admin.invoice_review_col_status'), key: 'status', width: 110 },
  { title: t('admin.invoice_review_col_review_note'), dataIndex: 'review_note', key: 'review_note', ellipsis: true },
])

function formatTime(v?: string): string {
  if (!v) return '-'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return '-'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/**
 * 格式化配额数值。
 *
 * token/credits 是纯数量；storage_mb 单位是 MB；concurrency 是个数。
 * 千分位用 toLocaleString：一眼看出 1000000 与 100000 的差别。
 */
function formatAmount(row: PoolRow, v: number): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '-'
  const base = n.toLocaleString()
  return row.resource_type === 'storage_mb' ? `${base} MB` : base
}

function statusColor(status: QuotaRequestStatus): string {
  return status === 'approved' ? 'success' : status === 'rejected' ? 'error' : 'warning'
}

async function load() {
  loading.value = true
  try {
    const [p, r] = await Promise.all([
      listTenantQuota(),
      // 历史视图要看到全部（含已处理），否则用户不知道上次申请结果。
      listTenantQuotaRequests(requestFilter.value),
    ])
    pools.value = p
    requests.value = r
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

async function submit() {
  const amount = Number(form.requestedAmount)
  if (!Number.isFinite(amount) || amount < 0) {
    message.warning(t('admin.quota_amount_invalid'))
    return
  }
  submitting.value = true
  try {
    await createQuotaRequest({
      resource_type: form.resourceType,
      period: form.period,
      requested_amount: Math.round(amount),
      reason: form.reason.trim(),
    })
    message.success(t('admin.quota_request_submitted'))
    form.reason = ''
    await reloadRequests()
  } catch (e) {
    // 409（已有待审申请）也走这里：必须重拉，否则界面上看不到那条待审记录。
    message.error(serverErrorMessage(e, t('admin.quota_request_failed')))
    await reloadRequests()
  } finally {
    submitting.value = false
  }
}

/** 只重拉申请列表：配额池没变，不必整页刷新。 */
async function reloadRequests() {
  try {
    requests.value = await listTenantQuotaRequests(requestFilter.value)
  } catch {
    // 列表刷新失败不该覆盖刚才的成功提示，静默保留旧数据。
  }
}

onMounted(load)
</script>

<template>
  <div class="quota-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">
          {{ $t('admin.quota_title') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.quota_desc') }}
        </p>
      </div>
      <Button
        :loading="loading"
        @click="load"
      >
        {{ $t('common.refresh') }}
      </Button>
    </div>

    <Card
      :title="$t('admin.quota_current')"
      class="quota-card"
    >
      <div
        v-if="!poolRows.length"
        class="empty"
      >
        {{ $t('admin.quota_empty') }}
      </div>
      <div
        v-for="row in poolRows"
        :key="`${row.resource_type}-${row.period}`"
        class="pool-row"
      >
        <div class="pool-head">
          <span class="pool-name">{{ row.label }}</span>
          <span class="pool-period">{{ $t(`admin.quota_period_${row.period}`) }}</span>
          <span class="pool-amounts">
            <template v-if="row.unlimited">
              {{ $t('admin.quota_unlimited') }}
            </template>
            <template v-else>
              {{ formatAmount(row, row.used_amount) }} / {{ formatAmount(row, row.total_amount) }}
            </template>
          </span>
        </div>
        <!-- 无限额或没有用量概念时**不画进度条**：0% 与 100% 都是误导。 -->
        <div
          v-if="row.percent !== null"
          class="pool-bar"
          role="progressbar"
          :aria-valuenow="row.percent"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-label="row.label"
        >
          <div
            class="pool-bar-fill"
            :class="{ warn: row.percent >= 80, danger: row.percent >= 95 }"
            :style="{ width: `${row.percent}%` }"
          />
        </div>
        <div
          v-else
          class="pool-note"
        >
          {{ row.usage_source === 'none' ? $t('admin.quota_no_usage_concept') : $t('admin.quota_unlimited') }}
        </div>
      </div>
    </Card>

    <Card
      :title="$t('admin.quota_apply_title')"
      class="quota-card"
    >
      <Form
        layout="inline"
        @finish="submit"
      >
        <FormItem :label="$t('admin.quota_col_resource')">
          <Select
            v-model:value="form.resourceType"
            :options="resourceOptions"
            style="width: 160px"
          />
        </FormItem>
        <FormItem :label="$t('admin.quota_col_period')">
          <Select
            v-model:value="form.period"
            :options="periodOptions"
            style="width: 130px"
          />
        </FormItem>
        <FormItem :label="$t('admin.quota_col_requested')">
          <InputNumber
            v-model:value="form.requestedAmount"
            :min="0"
            :precision="0"
            style="width: 160px"
          />
        </FormItem>
        <FormItem :label="$t('admin.quota_col_reason')">
          <Input
            v-model:value="form.reason"
            :maxlength="500"
            style="width: 240px"
          />
        </FormItem>
        <FormItem>
          <Button
            type="primary"
            html-type="submit"
            :loading="submitting"
          >
            {{ $t('admin.quota_apply_submit') }}
          </Button>
        </FormItem>
      </Form>
      <p class="form-hint">
        {{ $t('admin.quota_apply_hint') }}
      </p>
    </Card>

    <Card
      :title="$t('admin.quota_history')"
      class="quota-card"
    >
      <template #extra>
        <Select
          v-model:value="requestFilter"
          :options="filterOptions"
          size="small"
          style="width: 130px"
          @change="reloadRequests"
        />
      </template>
      <Table
        :columns="columns"
        :data-source="requests"
        :loading="loading"
        :pagination="{ pageSize: 20, hideOnSinglePage: true }"
        row-key="id"
        size="small"
        :scroll="{ x: 900 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'created_at'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'resource'">
            {{ $t(`admin.quota_resource_${record.resource_type}`) }}
            <span class="sub">· {{ $t(`admin.quota_period_${record.period}`) }}</span>
          </template>
          <template v-else-if="column.key === 'current_amount'">
            {{ record.current_amount }}
          </template>
          <template v-else-if="column.key === 'requested_amount'">
            {{ record.requested_amount }}
          </template>
          <template v-else-if="column.key === 'status'">
            <Tag :color="statusColor(record.status)">
              {{ $t(`admin.quota_status_${record.status}`) }}
            </Tag>
          </template>
          <template v-else-if="column.key === 'reason'">
            <span class="sub">{{ record.reason || '-' }}</span>
          </template>
          <template v-else-if="column.key === 'review_note'">
            <span class="sub">{{ record.review_note || '-' }}</span>
          </template>
        </template>
      </Table>
    </Card>
  </div>
</template>

<style scoped>
.quota-page { padding: 4px; }
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.page-title { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.quota-card { margin-bottom: 16px; }
.empty { padding: 12px 0; color: var(--text-tertiary); font-size: 13px; }
.pool-row { padding: 10px 0; border-bottom: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.06)); }
.pool-row:last-child { border-bottom: none; }
.pool-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.pool-name { font-weight: var(--fw-medium, 500); }
.pool-period { font-size: 12px; color: var(--text-tertiary); }
.pool-amounts { margin-inline-start: auto; font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.pool-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--bg-subtle, rgba(0, 0, 0, 0.06));
  overflow: hidden;
}
.pool-bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--accent, #1677ff);
  transition: width var(--dur-slow) var(--ease-out);
}
.pool-bar-fill.warn { background: var(--warning, #faad14); }
.pool-bar-fill.danger { background: var(--danger, #ff4d4f); }
.pool-note { font-size: 12px; color: var(--text-tertiary); }
.form-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
.sub { font-size: 12px; color: var(--text-tertiary); }
</style>
