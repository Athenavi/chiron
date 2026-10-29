<template>
  <div class="invoice-review-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.invoice_review') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.invoice_review_desc') }}
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
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'created_at'">
          {{ formatTime(record.created_at) }}
        </template>
        <template v-else-if="column.key === 'order'">
          <code class="mono">{{ String(record.payment_id).slice(0, 10) }}</code>
        </template>
        <template v-else-if="column.key === 'title'">
          <div>{{ record.title }}</div>
          <div class="sub">
            {{ record.tax_no }}
          </div>
        </template>
        <template v-else-if="column.key === 'amount'">
          {{ formatAmount(record.amount_cents, record.currency) }}
        </template>
        <template v-else-if="column.key === 'note'">
          <span class="sub">{{ record.note || '-' }}</span>
        </template>
        <template v-else-if="column.key === 'status'">
          <Tag :color="statusColor(record.status)">
            {{ $t(`admin.invoice_status_${record.status}`) }}
          </Tag>
          <div
            v-if="record.review_note"
            class="sub"
          >
            {{ record.review_note }}
          </div>
        </template>
        <template v-else-if="column.key === 'actions'">
          <!-- 只有 pending 能操作：后端 UPDATE 带 `AND status = 'pending'`，
               已经审过的再点一次只会收到 409，不如直接不给按钮 -->
          <template v-if="record.status === 'pending'">
            <Button
              type="link"
              size="small"
              data-test="invoice-approve"
              @click="openAction(record, 'approve')"
            >
              {{ $t('admin.invoice_review_approve') }}
            </Button>
            <Button
              type="link"
              size="small"
              danger
              data-test="invoice-reject"
              @click="openAction(record, 'reject')"
            >
              {{ $t('admin.invoice_review_reject') }}
            </Button>
          </template>
          <span
            v-else
            class="sub"
          >{{ formatTime(record.reviewed_at) }}</span>
        </template>
      </template>
      <template #emptyText>
        <div class="empty">
          {{ $t('admin.invoice_review_empty') }}
        </div>
      </template>
    </Table>

    <Modal
      :open="action.open"
      :title="action.kind === 'approve'
        ? $t('admin.invoice_review_confirm_approve_title')
        : $t('admin.invoice_review_confirm_reject_title')"
      :confirm-loading="submitting"
      :ok-text="action.kind === 'approve'
        ? $t('admin.invoice_review_approve')
        : $t('admin.invoice_review_reject')"
      :ok-button-props="{ danger: action.kind === 'reject' }"
      @ok="submit"
      @cancel="closeAction"
    >
      <p>{{ action.kind === 'approve'
        ? $t('admin.invoice_review_confirm_approve_desc')
        : $t('admin.invoice_review_reason_required') }}</p>

      <div
        v-if="action.invoice"
        class="summary"
      >
        <div>{{ action.invoice.title }}</div>
        <div class="sub">
          {{ action.invoice.tax_no }} ·
          {{ formatAmount(action.invoice.amount_cents, action.invoice.currency) }}
        </div>
      </div>

      <!-- 驳回必须给理由：没有它用户只能反复提交同一笔，每次都被 409 拦下 -->
      <div
        v-if="action.kind === 'reject'"
        class="reason"
      >
        <label>{{ $t('admin.invoice_review_reason_label') }}</label>
        <Textarea
          v-model:value="action.reason"
          :rows="3"
          :maxlength="500"
          :placeholder="$t('admin.invoice_review_reason_placeholder')"
          data-test="invoice-reject-reason"
        />
      </div>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Button, Modal, Select, Table, Tag, Textarea, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { listInvoices, reviewInvoice, type AdminInvoice, type InvoiceStatus } from '../../api/admin'
import { describeApiError } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const submitting = ref(false)
const rows = ref<AdminInvoice[]>([])

/**
 * 默认只看待审。「全部」是排查用的次要视图 ——
 * 审核页的第一屏应该是"等我处理的事"，而不是历史记录。
 */
const filter = ref<InvoiceStatus | 'all'>('pending')

const filterOptions = computed(() => [
  { value: 'pending', label: t('admin.invoice_review_filter_pending') },
  { value: 'approved', label: t('admin.invoice_status_approved') },
  { value: 'rejected', label: t('admin.invoice_status_rejected') },
  { value: 'all', label: t('admin.invoice_review_filter_all') },
])

const columns = computed(() => [
  { title: t('admin.invoice_review_col_created'), key: 'created_at', width: 160 },
  { title: t('admin.invoice_review_col_user_payment'), key: 'order', width: 130 },
  { title: t('admin.invoice_review_col_title'), key: 'title', width: 220 },
  { title: t('admin.invoice_review_col_amount'), key: 'amount', width: 120, align: 'right' as const },
  { title: t('admin.invoice_review_col_note'), key: 'note', width: 160 },
  { title: t('admin.invoice_review_col_status'), key: 'status', width: 200 },
  { title: '', key: 'actions', width: 150 },
])

const action = reactive<{
  open: boolean
  kind: 'approve' | 'reject'
  invoice: AdminInvoice | null
  reason: string
}>({ open: false, kind: 'approve', invoice: null, reason: '' })

async function load() {
  loading.value = true
  try {
    rows.value = await listInvoices(filter.value)
  } catch (e) {
    message.error(describeApiError(e))
  } finally {
    loading.value = false
  }
}

/**
 * antd 的 `#bodyCell` 插槽把 `record` 声明成 `Record<string, any>`（它是泛型无关的），
 * 所以这里收窄一次，而不是在模板里给每个调用点写 `as`。
 */
function openAction(record: Record<string, any>, kind: 'approve' | 'reject') {
  const invoice = record as AdminInvoice
  action.open = true
  action.kind = kind
  action.invoice = invoice
  action.reason = ''
}

function closeAction() {
  action.open = false
  action.invoice = null
  action.reason = ''
}

async function submit() {
  const invoice = action.invoice
  if (!invoice) return

  // 驳回空理由会让用户完全不知所措，直接拦住而不是让后端 400
  if (action.kind === 'reject' && !action.reason.trim()) {
    message.warning(t('admin.invoice_review_reason_required'))
    return
  }

  submitting.value = true
  try {
    await reviewInvoice(invoice.id, {
      status: action.kind === 'approve' ? 'approved' : 'rejected',
      review_note: action.kind === 'reject' ? action.reason.trim() : undefined,
    })
    message.success(t(action.kind === 'approve'
      ? 'admin.invoice_review_approved_msg'
      : 'admin.invoice_review_rejected_msg'))
    closeAction()
    // 审完就重拉：待审队列里该条应立刻消失，本地改状态会让"还剩几张"对不上
    await load()
  } catch (e) {
    // 409（别人先审了）也走这里 —— 必须重拉，否则界面上还是可操作状态
    message.error(describeApiError(e))
    closeAction()
    await load()
  } finally {
    submitting.value = false
  }
}

function formatTime(v?: string): string {
  if (!v) return '-'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return '-'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** 金额是**最小单位（分）**，直接显示会差 100 倍 */
function formatAmount(cents: number, currency?: string): string {
  const n = Number(cents)
  if (!Number.isFinite(n)) return '-'
  const symbol = !currency || currency === 'CNY' ? '¥' : `${currency} `
  return `${symbol}${(n / 100).toFixed(2)}`
}

function statusColor(status: InvoiceStatus): string {
  return status === 'approved' ? 'success' : status === 'rejected' ? 'error' : 'warning'
}

onMounted(load)
</script>

<style scoped>
.invoice-review-view { padding: 4px; }
.sub { font-size: 12px; color: var(--text-tertiary); }
.mono { font-family: var(--font-mono, monospace); font-size: 12px; }
.empty { padding: 24px 0; color: var(--text-tertiary); }
.summary {
  margin: 12px 0;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--bg-subtle, rgba(0, 0, 0, 0.03));
}
.reason { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.reason label { font-weight: var(--fw-medium); color: var(--text-primary); }
</style>
