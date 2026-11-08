<script setup lang="ts">
/**
 * 租户后台 · 退款管理。
 *
 * 退款按订单归属租户隔离（后端校验 payments.user_id → users.tenant_id）。
 * 一个订单允许多次部分退款；这里提供"按订单号退款"表单与租户退款记录列表。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Button,
  Card,
  Form,
  FormItem,
  Input,
  InputNumber,
  message,
  Table,
} from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { createRefund, listTenantRefunds, type PaymentRefund } from '../../api/admin'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const loading = ref(false)
const submitting = ref(false)
const rows = ref<PaymentRefund[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const form = reactive({ paymentId: '', amount: 0, reason: '' })

const pagination = computed(() => ({
  current: page.value,
  pageSize,
  total: total.value,
  showSizeChanger: false,
  onChange: (p: number) => {
    page.value = p
    void load()
  },
}))

const columns = computed(() => [
  { title: t('admin.refund_order_id'), dataIndex: 'payment_id', key: 'payment_id', ellipsis: true },
  {
    title: t('admin.refund_amount'),
    dataIndex: 'amount_cents',
    key: 'amount_cents',
    width: 120,
    customRender: ({ text }: { text: number }) => `¥${(Number(text) / 100).toFixed(2)}`,
  },
  { title: t('admin.refund_channel'), dataIndex: 'channel', key: 'channel', width: 100 },
  {
    title: t('admin.refund_status'),
    dataIndex: 'status',
    key: 'status',
    width: 110,
    customRender: ({ text }: { text: string }) => {
      const color = text === 'succeeded' ? 'green' : text === 'failed' ? 'red' : 'blue'
      return { children: text, props: { color } }
    },
  },
  { title: t('admin.refund_created_at'), dataIndex: 'created_at', key: 'created_at', width: 180 },
  { title: t('admin.refund_error'), dataIndex: 'error_message', key: 'error_message', ellipsis: true },
])

async function load() {
  loading.value = true
  try {
    const res = await listTenantRefunds(page.value, pageSize)
    rows.value = res.refunds
    total.value = res.total
  } catch (e) {
    message.error(serverErrorMessage(e, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

async function submit() {
  const paymentId = form.paymentId.trim()
  if (!paymentId) {
    message.warning(t('admin.refund_order_id_required'))
    return
  }
  const amountCents = Math.round(Number(form.amount) * 100)
  if (!Number.isFinite(amountCents) || amountCents <= 0) {
    message.warning(t('admin.refund_amount_required'))
    return
  }
  submitting.value = true
  try {
    await createRefund(paymentId, amountCents, form.reason.trim())
    message.success(t('admin.refund_submitted'))
    form.paymentId = ''
    form.amount = 0
    form.reason = ''
    page.value = 1
    await load()
  } catch (e) {
    message.error(serverErrorMessage(e, t('admin.refund_failed')))
    await load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="refunds-page">
    <div class="page-header">
      <h2>{{ $t('admin.refunds') }}</h2>
      <p class="page-desc">
        {{ $t('admin.refunds_desc') }}
      </p>
    </div>

    <Card
      :title="$t('admin.refund_new')"
      class="refund-form-card"
    >
      <Form
        layout="inline"
        @finish="submit"
      >
        <FormItem :label="$t('admin.refund_order_id')">
          <Input
            v-model:value="form.paymentId"
            placeholder="pay_..."
            allow-clear
            style="width: 260px"
          />
        </FormItem>
        <FormItem :label="$t('admin.refund_amount')">
          <InputNumber
            v-model:value="form.amount"
            :min="0.01"
            :precision="2"
            style="width: 140px"
          />
        </FormItem>
        <FormItem :label="$t('admin.refund_reason')">
          <Input
            v-model:value="form.reason"
            :maxlength="200"
            style="width: 220px"
          />
        </FormItem>
        <FormItem>
          <Button
            type="primary"
            html-type="submit"
            :loading="submitting"
          >
            {{ $t('admin.refund_submit') }}
          </Button>
        </FormItem>
      </Form>
    </Card>

    <Card
      :title="$t('admin.refund_history')"
      class="refund-list-card"
    >
      <Table
        :columns="columns"
        :data-source="rows"
        :loading="loading"
        :pagination="pagination"
        row-key="id"
        size="small"
        :scroll="{ x: 900 }"
      />
    </Card>
  </div>
</template>

<style scoped>
.refunds-page { padding: 4px; }
.page-header { margin-bottom: 12px; }
.page-header h2 { margin: 0; font-size: 18px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-tertiary); }
.refund-form-card { margin-bottom: 16px; }
</style>
