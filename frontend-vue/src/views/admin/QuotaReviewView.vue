<script setup lang="ts">
/**
 * 平台后台 · 配额审批（仅 owner）。
 *
 * 通过后后端会**立即**把目标值写入 `ent_quota_pools` 并重载并发/存储闸门，
 * 所以这里不需要"等待生效"的提示 —— 批完即生效。
 *
 * 一个必须显式处理的边界：通过是**不可逆的**（配额被改大之后没有"撤销"按钮，
 * 要改回去得再走一次审批），所以确认弹窗必须写清楚这一点。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Modal, Select, Table, Tag, Textarea, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  listAdminQuotaRequests,
  reviewQuotaRequest,
  type QuotaRequest,
  type QuotaRequestStatus,
} from '../../api/admin'
import { describeApiError } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const submitting = ref(false)
const rows = ref<QuotaRequest[]>([])

/** 默认只看待审：审批队列的第一屏应该是"等我处理的事"。 */
const filter = ref<QuotaRequestStatus | 'all'>('pending')

const filterOptions = computed(() => [
  { value: 'pending', label: t('admin.invoice_review_filter_pending') },
  { value: 'approved', label: t('admin.invoice_status_approved') },
  { value: 'rejected', label: t('admin.invoice_status_rejected') },
  { value: 'all', label: t('admin.invoice_review_filter_all') },
])

const columns = computed(() => [
  { title: t('admin.quota_submitted_at'), key: 'created_at', width: 160 },
  { title: t('admin.quota_col_tenant'), dataIndex: 'tenant_id', key: 'tenant_id', width: 90 },
  { title: t('admin.quota_col_resource'), key: 'resource', width: 150 },
  { title: t('admin.quota_col_current'), dataIndex: 'current_amount', key: 'current_amount', width: 100 },
  { title: t('admin.quota_col_requested'), dataIndex: 'requested_amount', key: 'requested_amount', width: 100 },
  { title: t('admin.quota_col_reason'), key: 'reason', ellipsis: true },
  { title: t('admin.invoice_review_col_status'), key: 'status', width: 100 },
  { title: '', key: 'actions', width: 150 },
])

const action = reactive<{
  open: boolean
  kind: 'approve' | 'reject'
  request: QuotaRequest | null
  note: string
}>({ open: false, kind: 'approve', request: null, note: '' })

function formatTime(v?: string): string {
  if (!v) return '-'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return '-'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function statusColor(status: QuotaRequestStatus): string {
  return status === 'approved' ? 'success' : status === 'rejected' ? 'error' : 'warning'
}

async function load() {
  loading.value = true
  try {
    rows.value = await listAdminQuotaRequests(filter.value)
  } catch (e) {
    message.error(describeApiError(e))
  } finally {
    loading.value = false
  }
}

function openAction(record: Record<string, unknown>, kind: 'approve' | 'reject') {
  action.open = true
  action.kind = kind
  action.request = record as unknown as QuotaRequest
  action.note = ''
}

function closeAction() {
  action.open = false
  action.request = null
  action.note = ''
}

async function submit() {
  const req = action.request
  if (!req) return

  submitting.value = true
  try {
    await reviewQuotaRequest(req.id, {
      status: action.kind === 'approve' ? 'approved' : 'rejected',
      // 通过时备注可选；驳回时也允许空（配额申请不像发票那样必须说明理由）。
      review_note: action.note.trim() || undefined,
    })
    message.success(t(action.kind === 'approve'
      ? 'admin.quota_approved_msg'
      : 'admin.quota_rejected_msg'))
    closeAction()
    // 审完就重拉：待审队列里该条应立刻消失。
    await load()
  } catch (e) {
    // 409（别人先审了）也走这里 —— 必须重拉，否则界面上还是可操作状态。
    message.error(describeApiError(e))
    closeAction()
    await load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="quota-review-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.quota_review_title') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.quota_review_desc') }}
        </p>
      </div>
      <div class="controls">
        <Select
          v-model:value="filter"
          :options="filterOptions"
          size="small"
          style="width: 140px"
          @change="load"
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

    <Table
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :pagination="{ pageSize: 20, hideOnSinglePage: true }"
      size="small"
      row-key="id"
      :locale="{ emptyText: $t('admin.quota_review_empty') }"
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
          <strong>{{ record.requested_amount }}</strong>
        </template>
        <template v-else-if="column.key === 'reason'">
          <span class="sub">{{ record.reason || '-' }}</span>
        </template>
        <template v-else-if="column.key === 'status'">
          <Tag :color="statusColor(record.status)">
            {{ $t(`admin.quota_status_${record.status}`) }}
          </Tag>
        </template>
        <template v-else-if="column.key === 'actions'">
          <!-- 只有 pending 能操作：后端 UPDATE 带 `AND status = 'pending'`。 -->
          <template v-if="record.status === 'pending'">
            <Button
              type="link"
              size="small"
              @click="openAction(record, 'approve')"
            >
              {{ $t('admin.quota_review_approve') }}
            </Button>
            <Button
              type="link"
              size="small"
              danger
              @click="openAction(record, 'reject')"
            >
              {{ $t('admin.quota_review_reject') }}
            </Button>
          </template>
        </template>
      </template>
    </Table>

    <Modal
      v-model:open="action.open"
      :title="action.kind === 'approve'
        ? $t('admin.quota_review_confirm_approve')
        : $t('admin.quota_review_confirm_reject')"
      :confirm-loading="submitting"
      @ok="submit"
      @cancel="closeAction"
    >
      <div
        v-if="action.request"
        class="summary"
      >
        <div>
          <strong>{{ $t(`admin.quota_resource_${action.request.resource_type}`) }}</strong>
          · {{ $t(`admin.quota_period_${action.request.period}`) }}
        </div>
        <div class="sub">
          {{ action.request.current_amount }} → {{ action.request.requested_amount }}
        </div>
      </div>
      <div class="reason">
        <label for="quota-review-note">{{ $t('admin.quota_review_note_label') }}</label>
        <Textarea
          id="quota-review-note"
          v-model:value="action.note"
          :rows="3"
          :maxlength="500"
          show-count
        />
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.quota-review-view { padding: 4px; }
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.page-title { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.controls { display: flex; align-items: center; gap: 8px; }
.sub { font-size: 12px; color: var(--text-tertiary); }
.summary {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--bg-subtle, rgba(0, 0, 0, 0.03));
}
.reason { display: flex; flex-direction: column; gap: 8px; }
.reason label { font-weight: var(--fw-medium); color: var(--text-primary); }
</style>
