import { ref, computed } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import { describeApiError, errorMessageText } from '../utils/apiError'

/** 可开票订单（GET /v1/billing/invoiceable-orders 的投影，只含选择所需字段） */
export interface InvoiceableOrder {
  id: string
  amount_cents: number
  currency: string
  credits: number
  paid_at?: string
}

/** 发票申请（GET /v1/billing/invoices） */
export interface InvoiceRequest {
  id: string
  payment_id: string
  title: string
  tax_no: string
  amount_cents: number
  currency: string
  status: 'pending' | 'approved' | 'rejected'
  note?: string
  review_note?: string
  reviewed_at?: string
  created_at?: string
}

/**
 * 发票申请（P1-2）：可开票订单、我的申请、提交表单。
 *
 * 从 `BillingView.vue` 抽出，逻辑逐字未改。`formatTime` 由调用方注入，
 * 避免此处重复一份时间格式化实现而出现两处口径漂移。
 *
 * 之所以整簇搬走：这三块状态只被发票 tab 使用，与支付/套餐链路零交叉。
 */
export function useBillingInvoices(formatTime: (v: string) => string) {
  const { t } = useI18n()

  /** 可开票订单：只含"选哪一笔"所需的信息 */
  const invoiceableOrders = ref<InvoiceableOrder[]>([])
  const invoiceOrdersLoading = ref(false)
  /** 我的申请列表（含已驳回，用户需要看到为什么没开成） */
  const invoiceRequests = ref<InvoiceRequest[]>([])
  const invoiceListLoading = ref(false)
  const invoiceSubmitting = ref(false)
  /** 表单：一期只支持全额开票，所以金额由所选订单决定，用户不能改 */
  const invoiceForm = ref({ paymentId: '', title: '', taxNo: '', note: '' })

  /** 表单校验放在前端只为减少一次无效往返；权威校验仍在后端。 */
  const invoiceFormValid = computed(() => (
    invoiceForm.value.paymentId.trim() !== ''
    && invoiceForm.value.title.trim() !== ''
    && invoiceForm.value.taxNo.trim() !== ''
  ))

  async function loadInvoices() {
    invoiceOrdersLoading.value = true
    invoiceListLoading.value = true
    try {
      const [ordersRes, listRes] = await Promise.allSettled([
        api.get('/v1/billing/invoiceable-orders'),
        api.get('/v1/billing/invoices'),
      ])
      if (ordersRes.status === 'fulfilled') {
        invoiceableOrders.value = (ordersRes.value.data?.data as InvoiceableOrder[] | undefined) ?? []
      } else {
        message.error(errorMessageText(ordersRes.reason, t('errors.failed_to_load')))
      }
      if (listRes.status === 'fulfilled') {
        invoiceRequests.value = (listRes.value.data?.data as InvoiceRequest[] | undefined) ?? []
      } else {
        message.error(errorMessageText(listRes.reason, t('errors.failed_to_load')))
      }
    } finally {
      invoiceOrdersLoading.value = false
      invoiceListLoading.value = false
    }
  }

  /**
   * 提交开票申请。
   *
   * 成功后**重新拉取两个列表**而不是本地插入：后端把该订单从"可开票"里排除，
   * 本地只 push 一条申请的话，下拉里还会留着那笔已申请的订单，用户再选一次才收到 409。
   */
  async function submitInvoice() {
    if (invoiceSubmitting.value) return
    if (!invoiceFormValid.value) {
      message.warning(t('billing.invoice_required_hint'))
      return
    }
    invoiceSubmitting.value = true
    try {
      await api.post('/v1/billing/invoices', {
        payment_id: invoiceForm.value.paymentId,
        title: invoiceForm.value.title.trim(),
        tax_no: invoiceForm.value.taxNo.trim(),
        note: invoiceForm.value.note.trim(),
      })
      message.success(t('billing.invoice_submitted'))
      invoiceForm.value = { paymentId: '', title: '', taxNo: '', note: '' }
      await loadInvoices()
    } catch (error) {
      message.error(describeApiError(error, t('errors.request_failed')))
    } finally {
      invoiceSubmitting.value = false
    }
  }

  /** 订单下拉的展示文本：金额 + 时间 + 短 id，用来辨认"是哪一笔" */
  function invoiceOrderLabel(o: InvoiceableOrder): string {
    return t('billing.invoice_order_option', {
      amount: invoiceAmountText(o.amount_cents, o.currency),
      time: formatTime(o.paid_at ?? ''),
      id: o.id.slice(0, 8),
    })
  }

  /** 分金额按最小单位换算；后端给的是 cents（含币种），不要当成元直接显示 */
  function invoiceAmountText(cents: number, currency: string): string {
    const symbol = currency === 'CNY' ? '¥' : (currency === 'USD' ? '$' : `${currency} `)
    return `${symbol}${(cents / 100).toFixed(2)}`
  }

  function invoiceStatusInfo(status: string): { label: string; color: string } {
    switch (status) {
      case 'approved': return { label: t('billing.invoice_status_approved'), color: 'success' }
      case 'rejected': return { label: t('billing.invoice_status_rejected'), color: 'error' }
      case 'pending':
      default: return { label: t('billing.invoice_status_pending'), color: 'warning' }
    }
  }

  return {
    invoiceableOrders,
    invoiceOrdersLoading,
    invoiceRequests,
    invoiceListLoading,
    invoiceSubmitting,
    invoiceForm,
    invoiceFormValid,
    loadInvoices,
    submitInvoice,
    invoiceOrderLabel,
    invoiceAmountText,
    invoiceStatusInfo,
  }
}
