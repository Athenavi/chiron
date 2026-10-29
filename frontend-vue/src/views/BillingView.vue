<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick, h } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import dayjs from 'dayjs'
import QRCode from 'qrcode'
import {
  Card, Button, Spin, Input, Radio, Tabs, TabPane, Table, Tag, Progress,
  Alert, Modal, message, Tooltip, Select,
} from 'ant-design-vue'
import EmptyState from '../components/common/EmptyState.vue'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import { describeApiError, errorMessageText } from '../utils/apiError'
import {
  CreditCardOutlined, WalletOutlined, ThunderboltOutlined, BarChartOutlined,
  ShoppingOutlined, QrcodeOutlined, PayCircleOutlined, DownloadOutlined,
  FileTextOutlined,
} from '@ant-design/icons-vue'
import { api, listPaymentChannels } from '../api'
import {
  blobErrorPayload, downloadBlob, exportFileName, exportPath,
} from '../utils/transactionExport'

import { useI18n } from 'vue-i18n'

const { t, te } = useI18n()
// ── 类型 ──

interface CreditTx {
  id: string
  amount: number
  balance: number
  reason: string
  created_at: string
}

interface DailyUsage {
  day: string
  tx_count: number
  credits_spent: number
  credits_added: number
}

/** 非按天分组时的一行（后端 usage_events 聚合结果） */
interface BreakdownRow {
  bucket: string
  event_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_cents: number
}

interface UsageData {
  daily: DailyUsage[]
  /** 按天分组时才有意义；按模型/会话分组时后端返回空数组占位 */
  total_spent?: number
  total_added?: number
  period_days: number
  /** 以下字段仅在 groupBy != day 时出现 */
  group_by?: UsageGroup
  breakdown?: BreakdownRow[]
  total_input?: number
  total_output?: number
  total_cached?: number
  total_cost?: number
  /** 后端只返回花费最高的 limit 项；truncated=true 表示还有没返回的 bucket */
  truncated?: boolean
  /** 聚合后的 bucket 总数（截断前），用于提示"共 N 项" */
  total_buckets?: number
  limit?: number
}

/** 与后端 usageGroupByWhitelist 一一对应；写死成联合类型，参数拼错在编译期就红 */
type UsageGroup = 'day' | 'model' | 'session'
type UsageRange = '7d' | '30d' | '90d'

interface BalanceData {
  balance: number
  daily_free_limit: number
  daily_free_used: number
  daily_free_remaining: number
  within_free_quota: boolean
}

/** 可开票订单（GET /v1/billing/invoiceable-orders 的投影，只含选择所需字段） */
interface InvoiceableOrder {
  id: string
  amount_cents: number
  currency: string
  credits: number
  paid_at?: string
}

/** 发票申请（GET /v1/billing/invoices） */
interface InvoiceRequest {
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

// ── 状态 ──

const route = useRoute()
const router = useRouter()

const balance = ref<number | null>(null)
const freeInfo = ref<{ limit: number; used: number; remaining: number } | null>(null)
const usage = ref<UsageData | null>(null)
const history = ref<CreditTx[]>([])
const historyLoading = ref(false)

// ── 发票申请（P1-2，工单式）──

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

// 用量视图的分组/区间：与后端 `groupBy` / `range` 白名单一一对应。
// 默认值必须与后端一致（day + 30d），否则首次加载会与"改造前"的口径不同。
const usageGroup = ref<UsageGroup>('day')
const usageRange = ref<UsageRange>('30d')
/** 切换分组/区间时的单独 loading，避免整页闪成骨架屏 */
const usageLoading = ref(false)
/** 明细表分页：90 天按会话分组可能上百行，必须分页而不是一次全渲染 */
const breakdownPage = ref(1)
const BREAKDOWN_PAGE_SIZE = 10

const GROUP_OPTIONS = computed<{ label: string; value: UsageGroup }[]>(() => [
  { label: t('billing.usage_group_day'), value: 'day' },
  { label: t('billing.usage_group_model'), value: 'model' },
  { label: t('billing.usage_group_session'), value: 'session' },
])

const RANGE_OPTIONS: { label: string; value: UsageRange; days: number }[] = [
  { label: t('billing.usage_range_days', { n: 7 }), value: '7d', days: 7 },
  { label: t('billing.usage_range_days', { n: 30 }), value: '30d', days: 30 },
  { label: t('billing.usage_range_days', { n: 90 }), value: '90d', days: 90 },
]

/** 区间天数：优先用后端回传的 period_days（是权威值），没有时按选择项推算 */
const rangeDays = computed(() =>
  usage.value?.period_days
  ?? RANGE_OPTIONS.find(o => o.value === usageRange.value)?.days
  ?? 30,
)

/**
 * 区间的人类可读标签（如「近 30 天」）。
 *
 * 不复用 `RANGE_OPTIONS[].label`：那个数组在模块初始化时就把 `t()` 求值了，
 * 用户切换语言后不会跟着变。这里用 computed 包一层，文案才会随 locale 刷新。
 * 导出按钮的提示语要说明"导出的是哪段范围"，写错范围比不写更糟。
 */
const rangeLabel = computed(() => t('billing.usage_range_days', { n: rangeDays.value }))

const loading = ref(true)
const checkoutLoading = ref(false)
const activeTab = ref('balance')

const credits = ref(1000)
const customCredits = ref('')
const provider = ref('')

// 支付结果提示（从 URL 参数读取一次后清除）
const payResult = ref<{ type: 'success' | 'info'; text: string } | null>(null)

// 二维码支付弹层状态
const qrVisible = ref(false)
const qrCode = ref('')
const currentOrderId = ref('')
const qrChannel = ref<'alipay' | 'wechat'>('alipay')
const qrCanvas = ref<HTMLCanvasElement | null>(null)
const payStatus = ref<'pending' | 'paid' | 'expired' | 'failed'>('pending')
let pollTimer: number | undefined
const PAY_POLL_INTERVAL = 3000

// ── 常量 ──

const PRESET_CREDITS = [500, 1000, 2000, 5000, 10000]

/** 渠道展示名（后端只返回渠道 id 与币种，文案在前端维护） */
const CHANNEL_LABELS: Record<string, string> = {
  alipay: t('billing.alipay'),
  wechat: t('billing.wechat_pay'),
  paypal: 'PayPal',
}

/** 渠道查询失败时的兜底：仍展示全部渠道，可用性由下单接口把关（不让一次查询失败锁死充值页） */
const FALLBACK_CHANNELS = ['alipay', 'wechat', 'paypal']

/** 后台已配置启用的渠道；为空表示当前没有可用渠道 */
const availableChannels = ref<string[]>([])

/**
 * 支付方式选项：只展示后台「支付配置」里已生效的渠道。
 * 此前无条件展示三种渠道，用户选中未配置的渠道要等下单才拿到 501。
 */
const providerOptions = computed(() =>
  availableChannels.value.map(id => ({ label: CHANNEL_LABELS[id] ?? id, value: id })),
)

async function loadChannels() {
  try {
    const channels = await listPaymentChannels()
    availableChannels.value = channels.map(c => c.id)
  } catch {
    availableChannels.value = [...FALLBACK_CHANNELS]
  }
  // 默认选中第一个可用渠道（原默认 alipay 在未启用时会变成非法选项）
  if (!availableChannels.value.includes(provider.value)) {
    provider.value = availableChannels.value[0] ?? ''
  }
}

const REASON_MAP: Record<string, { label: string; color: string }> = {
  llm_token: { label: t('common.llm_calls'), color: 'blue' },
  llm_call: { label: t('common.llm_calls'), color: 'blue' },
  image_gen: { label: t('media.image_generation'), color: 'purple' },
  stripe_topup: { label: t('common.stripe_recharge'), color: 'green' },
  paypal_topup: { label: t('common.paypal_recharge'), color: 'green' },
  recharge: { label: t('common.admin_top_up'), color: 'green' },
  admin: { label: t('common.admin_adjustment'), color: 'orange' },
}

// ── 计算属性 ──

const todaySpent = computed(() => {
  if (!usage.value?.daily?.length) return 0
  const today = dayjs().format('YYYY-MM-DD')
  return usage.value.daily.find(d => d.day === today)?.credits_spent ?? 0
})

/** 近 N 天消耗序列（升序，用于柱状图）；仅按天分组时有数据 */
const chartData = computed(() => {
  const daily = [...(usage.value?.daily ?? [])].reverse()
  return daily.map(d => ({
    day: d.day,
    label: dayjs(d.day).format('MM-DD'),
    spent: d.credits_spent,
  }))
})

const chartMax = computed(() => Math.max(1, ...chartData.value.map(d => d.spent)))

/** 是否处于"按天"视图：决定展示柱状图还是明细表 */
const isDayGroup = computed(() => usageGroup.value === 'day')

/** 明细表的行：把后端的 bucket 翻译成可读文本（空值后端归为 'unknown'） */
const breakdownRows = computed(() =>
  (usage.value?.breakdown ?? []).map(row => ({
    ...row,
    label: row.bucket === 'unknown'
      ? t('billing.usage_unknown_bucket')
      : row.bucket,
  })),
)

/**
 * 明细表列：只在分组维度上显示一列（day / model / session），
 * 而不是把三列都渲染出来 —— 另外两列必然是空的，用户会以为数据丢了。
 */
const breakdownColumns = computed(() => {
  const first = {
    day: { title: t('billing.usage_col_day'), key: 'label', dataIndex: 'label', width: 160 },
    model: { title: t('billing.usage_col_model'), key: 'label', dataIndex: 'label', width: 240 },
    session: { title: t('billing.usage_col_session'), key: 'label', dataIndex: 'label', width: 240 },
  }[usageGroup.value]
  return [
    first,
    { title: t('billing.usage_col_events'), key: 'event_count', dataIndex: 'event_count', width: 100, align: 'right' as const },
    { title: t('billing.usage_col_input_tokens'), key: 'input_tokens', dataIndex: 'input_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_output_tokens'), key: 'output_tokens', dataIndex: 'output_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_cached_tokens'), key: 'cached_tokens', dataIndex: 'cached_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_cost'), key: 'cost_cents', dataIndex: 'cost_cents', width: 130, align: 'right' as const },
  ]
})

const effectiveCredits = computed(() => {
  const n = customCredits.value ? parseInt(customCredits.value, 10) : credits.value
  return Number.isFinite(n) && n > 0 ? n : 0
})

const priceHint = computed(() => {
  if (!effectiveCredits.value) return ''
  // 支付宝/微信：1 credit = 1 分人民币；PayPal：1 credit = 1 美分
  if (provider.value === 'paypal') {
    return `≈ $${(effectiveCredits.value / 100).toFixed(2)} USD`
  }
  return `≈ ¥${(effectiveCredits.value / 100).toFixed(2)} CNY`
})

const historyColumns = [
  { title: t('common.time'), key: 'created_at', width: 180 },
  { title: t('common.reason'), key: 'reason', width: 180 },
  { title: t('common.amount'), key: 'amount', width: 120, align: 'right' as const },
  { title: t('billing.balance'), key: 'balance', width: 120, align: 'right' as const },
]

// ── 数据加载 ──

onMounted(async () => {
  readPayResultParam()
  await Promise.all([loadBalanceAndUsage(), loadChannels()])
  if (activeTab.value === 'history') await loadHistory()
  if (activeTab.value === 'invoice') await loadInvoices()
})

// 切换 tab 时懒加载历史
watch(activeTab, async (tab) => {
  if (tab === 'history' && history.value.length === 0 && !historyLoading.value) {
    await loadHistory()
  }
  // 发票 tab 每次进入都重拉：管理员可能刚审核完，缓存会让用户看到过期状态。
  if (tab === 'invoice' && !invoiceOrdersLoading.value && !invoiceListLoading.value) {
    await loadInvoices()
  }
})

function readPayResultParam() {
  const success = route.query.success
  const canceled = route.query.canceled
  if (success === '1') {
    payResult.value = { type: 'success', text: t('billing.payment_successful_credits_credited_sync_may_take_a_few_seconds') }
  } else if (canceled === '1') {
    payResult.value = { type: 'info', text: t('billing.payment_canceled_credits_unchanged') }
  }
  if (success === '1' || canceled === '1') {
    // 清除 URL 参数，避免刷新后重复提示
    router.replace({ query: {} })
  }
}

async function loadBalanceAndUsage() {
  try {
    loading.value = true
    const [balRes, usageRes] = await Promise.all([
      api.get('/v1/billing/balance'),
      api.get('/v1/billing/usage', { params: usageParams() }),
    ])
    const bal = balRes.data?.data as BalanceData | undefined
    balance.value = bal?.balance ?? 0
    if (bal && (bal.daily_free_limit || bal.daily_free_used)) {
      freeInfo.value = {
        limit: bal.daily_free_limit,
        used: bal.daily_free_used,
        remaining: bal.daily_free_remaining,
      }
    }
    usage.value = (usageRes.data?.data as UsageData | undefined) ?? null
    breakdownPage.value = 1
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

/** 只请求用量（切换分组/区间时用），不动余额卡片，避免整页骨架屏闪烁 */
async function loadUsage() {
  try {
    usageLoading.value = true
    const resp = await api.get('/v1/billing/usage', { params: usageParams() })
    usage.value = (resp.data?.data as UsageData | undefined) ?? null
    breakdownPage.value = 1
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load')))
  } finally {
    usageLoading.value = false
  }
}

/**
 * 查询参数：**总是显式传** groupBy/range。
 * 虽然省略时后端默认 day+30d，但显式传参让"页面显示的区间"与"请求的区间"永远一致——
 * 默认值在前后端各写一份，任一改动都会导致界面标题与实际数据不符。
 */
function usageParams(): Record<string, string> {
  return { groupBy: usageGroup.value, range: usageRange.value }
}

/**
 * 切换分组/区间时重新查询。
 * 「按天」分组用 credit_transactions（记账口径，含充值），其它分组用 usage_events（真实用量），
 * 两者不可混用，否则"按模型看花费"会拿到空数组。
 */
function onUsageQueryChange() {
  void loadUsage()
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const resp = await api.get('/v1/billing/history')
    history.value = (resp.data?.data?.history as CreditTx[] | undefined) ?? []
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load_transaction_history')))
  } finally {
    historyLoading.value = false
  }
}

// ── 发票申请（P1-2）──

/**
 * 加载可开票订单与我的申请。
 *
 * 两个请求并行：它们互不依赖，串行只会让 tab 打开时多等一个往返。
 * 单个失败不blocked另一个 —— 订单列表为空时用户仍应能看到历史申请的状态。
 */
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

// ── 交易明细导出（P1-1）──

/** 导出中禁用按钮：同步流式接口，重复点击会并发拉同一个查询。 */
const exporting = ref(false)

/**
 * 导出交易明细 CSV。
 *
 * 用 `responseType: 'blob'` 而不是普通 JSON 请求：这是文件下载，响应体是 CSV。
 * 代价是**错误响应体也变成 Blob**，拿不到 `.error` 字段，所以失败时要把 Blob
 * 读回文本再解析（见 `blobErrorPayload`）—— 否则用户看不到"请缩小时间范围"
 * 这句唯一有用的提示，只会得到"请求失败"。
 *
 * 范围沿用当前用量视图选的天数（7/30/90）：用户刚在页面上看过这个区间，
 * 导出同一个区间才符合直觉，也不需要再造一个选择器。
 */
async function exportTransactions() {
  if (exporting.value) return
  exporting.value = true
  try {
    const days = RANGE_OPTIONS.find(o => o.value === usageRange.value)?.days ?? 30
    const resp = await api.get(exportPath(days), { responseType: 'blob' })

    // 后端可能以 200 返回一个 JSON 错误体（理论上不该有，但代理/网关改写时会出现）。
    // 内容类型不是 CSV 就按错误处理，避免把一段 JSON 存成 .csv。
    const contentType = String(resp.headers?.['content-type'] ?? '')
    if (contentType && !contentType.includes('csv')) {
      const payload = await blobErrorPayload(resp.data as Blob)
      message.error(payload?.message ?? t('errors.request_failed'))
      return
    }

    downloadBlob(resp.data as Blob, exportFileName(days))
    message.success(t('billing.export_started'))
  } catch (error) {
    // 4xx/5xx：响应体是 Blob，先解析出 code/message 再决定文案。
    // 带 code 时优先走本地化文案（`errors.<code>_in` 会插入实际条数）。
    const data = (error as { response?: { data?: unknown } })?.response?.data
    if (data instanceof Blob) {
      const payload = await blobErrorPayload(data)
      if (payload?.code) {
        const key = `errors.${payload.code}_in`
        if (hasTranslation(key)) {
          message.error(t(key, payload.params ?? {}))
          return
        }
      }
      if (payload?.message) {
        message.error(payload.message)
        return
      }
    }
    message.error(describeApiError(error, t('errors.failed_to_export_transactions')))
  } finally {
    exporting.value = false
  }
}

/** 判断某个文案键在当前语言里是否真的存在（避免把 `errors.xxx_in` 原样显示）。 */
function hasTranslation(key: string): boolean {
  // te() 只查当前 locale；缺键时 t() 会返回键名本身，那比兜底文案更难懂。
  return te(key)
}

// ── 充值 ──

function selectPreset(n: number) {
  credits.value = n
  customCredits.value = ''
}

async function handlePurchase() {
  const amount = effectiveCredits.value
  if (amount <= 0) {
    message.warning(t('common.please_enter_a_valid_recharge_amount'))
    return
  }
  if (!provider.value) {
    message.warning(t('billing.please_select_a_payment_method_first'))
    return
  }
  checkoutLoading.value = true
  try {
    const response = await api.post('/v1/billing/pay', {
      credits: amount,
      channel: provider.value,
    })
    const data = response.data?.data
    if (!data) throw new Error(t('errors.failed_to_create_order'))

    if (provider.value === 'paypal') {
      // PayPal：跳转授权页
      if (data.checkout_url) {
        // S 安全：校验协议为 http/https，防 javascript:/data: 等 XSS 向量
        if (!/^https?:\/\//i.test(data.checkout_url)) {
          throw new Error(t('billing.illegal_payment_link'))
        }
        window.location.href = data.checkout_url
      } else {
        throw new Error(t('billing.could_not_get_paypal_payment_link'))
      }
      return
    }

    // 支付宝/微信：展示二维码并轮询订单状态
    if (!data.qr_code) throw new Error(t('billing.could_not_get_payment_qr_code'))
    qrCode.value = data.qr_code
    currentOrderId.value = data.id
    qrChannel.value = provider.value as 'alipay' | 'wechat'
    payStatus.value = 'pending'
    qrVisible.value = true
    await nextTick()
    renderQRCode()
    startPolling()
  } catch (error) {
    message.error(describeApiError(error, t('errors.failed_to_create_payment_order')))
  } finally {
    checkoutLoading.value = false
  }
}

async function renderQRCode() {
  if (!qrCanvas.value || !qrCode.value) return
  try {
    await QRCode.toCanvas(qrCanvas.value, qrCode.value, {
      width: 220, margin: 1, errorCorrectionLevel: 'M',
    })
  } catch {
    message.error(t('errors.qr_code_generation_failed'))
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(async () => {
    if (!currentOrderId.value) return
    try {
      const resp = await api.get(`/v1/billing/orders/${encodeURIComponent(currentOrderId.value)}`)
      const order = resp.data?.data
      if (!order) return
      if (order.status === 'paid') {
        payStatus.value = 'paid'
        stopPolling()
        qrVisible.value = false
        message.success(t('billing.top_up_successful_credits_credited'))
        await loadBalanceAndUsage()
        if (activeTab.value === 'history') await loadHistory()
      } else if (order.status === 'expired' || order.status === 'failed') {
        payStatus.value = order.status
        stopPolling()
      }
    } catch {
      // 轮询出错静默，等待下一次
    }
  }, PAY_POLL_INTERVAL)
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

function closeQR() {
  stopPolling()
  qrVisible.value = false
}

onUnmounted(stopPolling)

// ── 展示辅助 ──

function formatTime(v: string): string {
  return v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '-'
}

function reasonInfo(reason: string) {
  return REASON_MAP[reason] ?? { label: reason || t('common.unknown'), color: 'default' }
}

function amountText(amount: number): string {
  return amount > 0 ? `+${amount}` : `${amount}`
}
</script>

<template>
  <div class="billing-container">
    <div class="billing-header">
      <CreditCardOutlined style="font-size: 26px; color: var(--warning)" />
      <h1>{{ $t('billing.billing_management') }}</h1>
    </div>

    <Alert
      v-if="payResult"
      :type="payResult.type"
      :message="payResult.text"
      style="margin-bottom: 16px"
      show-icon
      :closable="true"
      @close="payResult = null"
    />

    <div
      v-if="loading"
      class="loading-state"
    >
      <PageSkeleton
        variant="cards"
        :columns="2"
      />
    </div>

    <template v-else>
      <!-- 概览卡片 -->
      <div class="overview-grid">
        <Card class="balance-card">
          <template #title>
            <WalletOutlined /> {{ $t('billing.current_balance') }}
          </template>
          <div class="balance-display">
            <span class="balance-amount">{{ balance ?? 0 }}</span>
            <Tag color="var(--warning)">
              credits
            </Tag>
          </div>
          <div
            v-if="freeInfo"
            class="free-quota"
          >
            <div class="free-quota-label">
              {{ $t('errors.today_s_free_conversation_quota') }}
              <span class="free-quota-count">{{ freeInfo.used }} / {{ freeInfo.limit }}</span>
            </div>
            <Progress
              :percent="freeInfo.limit > 0 ? Math.round((freeInfo.used / freeInfo.limit) * 100) : 0"
              :status="freeInfo.remaining > 0 ? 'active' : 'exception'"
              size="small"
            />
          </div>
        </Card>

        <Card class="usage-card">
          <template #title>
            <BarChartOutlined />{{ $t('common.used_in_the_last_n_days', { n: rangeDays }) }}
          </template>
          <div class="usage-stats">
            <!-- 按天：记账口径（含充值）。非按天：真实用量口径，此时"累计充值"没有意义，
                 硬展示会让人以为明细视图把充值算进去了 -->
            <template v-if="isDayGroup">
              <div class="stat-item">
                <span class="stat-label">{{ $t('common.consumed_today') }}</span>
                <span class="stat-value stat-spent">{{ todaySpent }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">{{ $t('common.total_consumed') }}</span>
                <span class="stat-value stat-spent">{{ usage?.total_spent ?? 0 }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">{{ $t('common.total_recharged') }}</span>
                <span class="stat-value stat-added">{{ usage?.total_added ?? 0 }}</span>
              </div>
            </template>
            <template v-else>
              <div class="stat-item">
                <span class="stat-label">{{ $t('billing.usage_col_input_tokens') }}</span>
                <span class="stat-value">{{ usage?.total_input ?? 0 }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">{{ $t('billing.usage_col_output_tokens') }}</span>
                <span class="stat-value">{{ usage?.total_output ?? 0 }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">{{ $t('billing.usage_col_cached_tokens') }}</span>
                <span class="stat-value">{{ usage?.total_cached ?? 0 }}</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">{{ $t('billing.usage_col_cost') }}</span>
                <span class="stat-value stat-spent">{{ usage?.total_cost ?? 0 }}</span>
              </div>
            </template>
          </div>
        </Card>
      </div>

      <!-- 分组 / 区间切换：放在图表与明细之上，切换只重查用量，不影响余额卡片 -->
      <div class="usage-filters">
        <span class="filter-label">{{ $t('billing.usage_group_by') }}</span>
        <Radio.Group
          v-model:value="usageGroup"
          :options="GROUP_OPTIONS"
          option-type="button"
          button-style="solid"
          @change="onUsageQueryChange"
        />
        <Radio.Group
          v-model:value="usageRange"
          :options="RANGE_OPTIONS"
          option-type="button"
          @change="onUsageQueryChange"
        />
      </div>

      <!-- 按天：消耗趋势；按模型/会话：明细表 -->
      <Card
        v-if="isDayGroup && chartData.length"
        class="chart-card"
        style="margin-top: 16px"
      >
        <template #title>
          <ThunderboltOutlined /> {{ $t('common.daily_consumption_trend') }}
        </template>
        <div class="bar-chart">
          <div
            v-for="d in chartData"
            :key="d.day"
            class="bar-col"
            :title="`${d.day}: ${d.spent} credits`"
          >
            <div class="bar-value">
              {{ d.spent > 0 ? d.spent : '' }}
            </div>
            <div
              class="bar"
              :style="{ height: Math.max(2, Math.round((d.spent / chartMax) * 120)) + 'px' }"
              :class="{ 'bar-zero': d.spent === 0 }"
            />
            <div class="bar-label">
              {{ d.label }}
            </div>
          </div>
        </div>
      </Card>

      <Card
        v-else-if="!isDayGroup"
        class="breakdown-card"
        style="margin-top: 16px"
      >
        <template #title>
          <ThunderboltOutlined /> {{ $t('billing.usage_breakdown_title') }}
        </template>
        <Spin :spinning="usageLoading">
          <Table
            v-if="breakdownRows.length"
            :columns="breakdownColumns"
            :data-source="breakdownRows"
            :row-key="(r: BreakdownRow) => r.bucket"
            size="small"
            :scroll="{ x: 860 }"
            :pagination="{
              current: breakdownPage,
              pageSize: BREAKDOWN_PAGE_SIZE,
              showSizeChanger: false,
              onChange: (p: number) => { breakdownPage = p },
              showTotal: (n: number) => $t('common.total_n', { n }),
            }"
          />
          <EmptyState
            v-else-if="!usageLoading"
            :description="$t('billing.usage_breakdown_empty')"
          />
          <div class="breakdown-hint">
            {{ $t('billing.usage_breakdown_hint') }}
          </div>
          <!-- 截断提示：后端只回花费最高的 limit 项。总量仍是全量合计，所以要说清
               "显示不全"而不是"数据不准"，否则用户会以为统计错了。 -->
          <div
            v-if="usage?.truncated"
            class="breakdown-hint breakdown-truncated"
          >
            {{ $t('billing.usage_breakdown_truncated', {
              shown: breakdownRows.length,
              total: usage.total_buckets ?? breakdownRows.length,
            }) }}
          </div>
        </Spin>
      </Card>
    </template>

    <Tabs
      v-model:active-key="activeTab"
      style="margin-top: 20px"
    >
      <!-- 充值 -->
      <TabPane
        key="purchase"
        :tab="$t('common.top_up')"
      >
        <Card>
          <template #title>
            <ShoppingOutlined /> {{ $t('billing.top_up_credits') }}
          </template>

          <div class="purchase-form">
            <div class="form-item">
              <label>{{ $t('common.quick_select') }}</label>
              <div class="preset-row">
                <Button
                  v-for="n in PRESET_CREDITS"
                  :key="n"
                  :type="credits === n && !customCredits ? 'primary' : 'default'"
                  @click="selectPreset(n)"
                >
                  {{ n >= 1000 ? `${n / 1000}k` : n }}
                </Button>
              </div>
            </div>

            <div class="form-item">
              <label>{{ $t('billing.custom_amount_credits') }}</label>
              <Input
                v-model:value="customCredits"
                type="number"
                min="1"
                :placeholder="$t('common.enter_any_amount_e_g_3000')"
                class="credits-input"
              />
            </div>

            <div class="form-item">
              <label>{{ $t('billing.payment_method') }}</label>
              <Radio.Group
                v-if="providerOptions.length"
                v-model:value="provider"
                :options="providerOptions"
                option-type="button"
                button-style="solid"
              />
              <Alert
                v-else
                type="warning"
                show-icon
                :message="$t('billing.the_admin_has_not_configured_an_available_payment_channel_online_top_up_is_temporarily_unavailable')"
              />
            </div>

            <div class="price-hint">
              {{ effectiveCredits ? $t('billing.this_top_up_n_credits_hint', { n: effectiveCredits, hint: priceHint }) : $t('common.please_choose_or_enter_a_top_up_amount') }}
            </div>

            <Button
              type="primary"
              size="large"
              :loading="checkoutLoading"
              :disabled="effectiveCredits <= 0"
              class="purchase-submit"
              @click="handlePurchase"
            >
              <template #icon>
                <WalletOutlined />
              </template>
              {{ $t('common.recharge_now') }}
            </Button>

            <div class="purchase-note">
              {{ provider === 'paypal'
                ? $t('billing.proceed_to_paypal_to_complete_payment_1_credit_1_cent_usd_top_ups_are_non_refundable_please_confirm_the_amount')
                : $t('billing.complete_payment_by_scanning_the_qr_code_1_credit_1_fen_cny_credits_are_credited_automatically_after_payment_top_ups_are_non_refundable_please_confirm_the_amount') }}
            </div>
          </div>
        </Card>
      </TabPane>

      <!-- 交易历史 -->
      <TabPane
        key="history"
        :tab="$t('common.transactions')"
      >
        <Card>
          <template #title>
            <BarChartOutlined /> {{ $t('common.transaction_history') }}
          </template>
          <!-- 导出入口（P1-1）：范围跟随上方用量视图的天数选择，避免再造一个选择器 -->
          <template #extra>
            <Tooltip :title="$t('billing.export_hint', { range: rangeLabel })">
              <Button
                size="small"
                :icon="h(DownloadOutlined)"
                :loading="exporting"
                :disabled="exporting"
                data-test="export-transactions"
                @click="exportTransactions"
              >
                {{ $t('billing.export_csv') }}
              </Button>
            </Tooltip>
          </template>
          <Spin :spinning="historyLoading">
            <Table
              v-if="history.length"
              :columns="historyColumns"
              :data-source="history"
              row-key="id"
              :scroll="{ x: 640 }"
              :pagination="{ pageSize: 10, showSizeChanger: false, showTotal: (n: number) => $t('common.total_n', { n }) }"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'created_at'">
                  {{ formatTime(record.created_at) }}
                </template>
                <template v-else-if="column.key === 'reason'">
                  <Tag :color="reasonInfo(record.reason).color">
                    {{ reasonInfo(record.reason).label }}
                  </Tag>
                </template>
                <template v-else-if="column.key === 'amount'">
                  <span :class="record.amount >= 0 ? 'amount-add' : 'amount-deduct'">
                    {{ amountText(record.amount) }}
                  </span>
                </template>
                <template v-else-if="column.key === 'balance'">
                  {{ record.balance }}
                </template>
              </template>
            </Table>
            <EmptyState
              v-else-if="!historyLoading"
              :description="$t('common.no_transactions_yet')"
            />
          </Spin>
        </Card>
      </TabPane>

      <!-- 发票申请（P1-2）：工单式，一期只支持全额开票 + 不可撤销 -->
      <TabPane
        key="invoice"
        :tab="$t('billing.invoice_title')"
      >
        <Card>
          <template #title>
            <FileTextOutlined /> {{ $t('billing.invoice_title') }}
          </template>

          <Alert
            type="info"
            :message="$t('billing.invoice_hint')"
            show-icon
            style="margin-bottom: 16px"
          />

          <!-- 申请表单 -->
          <div class="purchase-form">
            <div class="form-item">
              <label>{{ $t('billing.invoice_order') }}</label>
              <Select
                v-model:value="invoiceForm.paymentId"
                :loading="invoiceOrdersLoading"
                :placeholder="invoiceableOrders.length
                  ? $t('billing.invoice_order_placeholder')
                  : $t('billing.invoice_order_empty')"
                :not-found-content="$t('billing.invoice_order_empty')"
                :disabled="!invoiceableOrders.length"
                style="min-width: 320px"
                data-test="invoice-order-select"
              >
                <Select.Option
                  v-for="o in invoiceableOrders"
                  :key="o.id"
                  :value="o.id"
                >
                  {{ invoiceOrderLabel(o) }}
                </Select.Option>
              </Select>
              <!-- 禁用的空下拉本身什么也不说明，这里补一句可读的空态。
                   （placeholder 在 antd 里是伪元素，禁用态下用户看不出"为什么点不动"） -->
              <span
                v-if="!invoiceOrdersLoading && !invoiceableOrders.length"
                class="invoice-empty-hint"
                data-test="invoice-order-empty"
              >
                {{ $t('billing.invoice_order_empty') }}
              </span>
            </div>

            <div class="form-item">
              <label>{{ $t('billing.invoice_title_label') }}</label>
              <Input
                v-model:value="invoiceForm.title"
                :placeholder="$t('billing.invoice_title_placeholder')"
                :maxlength="255"
                style="max-width: 320px"
                data-test="invoice-title-input"
              />
            </div>

            <div class="form-item">
              <label>{{ $t('billing.invoice_tax_no_label') }}</label>
              <Input
                v-model:value="invoiceForm.taxNo"
                :placeholder="$t('billing.invoice_tax_no_placeholder')"
                :maxlength="64"
                style="max-width: 320px"
                data-test="invoice-tax-no-input"
              />
            </div>

            <div class="form-item">
              <label>{{ $t('billing.invoice_note_label') }}</label>
              <Input
                v-model:value="invoiceForm.note"
                :maxlength="500"
                style="max-width: 320px"
              />
            </div>

            <Button
              type="primary"
              :loading="invoiceSubmitting"
              :disabled="!invoiceFormValid"
              data-test="invoice-submit"
              @click="submitInvoice"
            >
              {{ $t('billing.invoice_submit') }}
            </Button>
          </div>

          <!-- 我的申请 -->
          <div class="invoice-list">
            <h3>{{ $t('billing.invoice_list_title') }}</h3>
            <Spin :spinning="invoiceListLoading">
              <Table
                v-if="invoiceRequests.length"
                :data-source="invoiceRequests"
                row-key="id"
                size="small"
                :scroll="{ x: 720 }"
                :pagination="{ pageSize: 10, showSizeChanger: false, showTotal: (n: number) => $t('common.total_n', { n }) }"
                :columns="[
                  { title: $t('billing.invoice_col_created'), key: 'created_at', width: 170 },
                  { title: $t('billing.invoice_col_order'), key: 'payment_id', width: 140 },
                  { title: $t('billing.invoice_col_title'), key: 'title', width: 180 },
                  { title: $t('billing.invoice_col_amount'), key: 'amount_cents', width: 120, align: 'right' },
                  { title: $t('billing.invoice_col_status'), key: 'status', width: 120 },
                  { title: $t('billing.invoice_review_note'), key: 'review_note' },
                ]"
              >
                <template #bodyCell="{ column, record }">
                  <template v-if="column.key === 'created_at'">
                    {{ formatTime(record.created_at) }}
                  </template>
                  <template v-else-if="column.key === 'payment_id'">
                    <code>{{ String(record.payment_id).slice(0, 8) }}</code>
                  </template>
                  <template v-else-if="column.key === 'amount_cents'">
                    {{ invoiceAmountText(record.amount_cents, record.currency) }}
                  </template>
                  <template v-else-if="column.key === 'status'">
                    <Tag :color="invoiceStatusInfo(record.status).color">
                      {{ invoiceStatusInfo(record.status).label }}
                    </Tag>
                  </template>
                  <template v-else-if="column.key === 'review_note'">
                    <!-- 驳回理由对用户是必要信息：没有它只能反复提交同一笔 -->
                    {{ record.review_note || '-' }}
                  </template>
                </template>
              </Table>
              <EmptyState
                v-else-if="!invoiceListLoading"
                :description="$t('billing.invoice_list_empty')"
              />
            </Spin>
          </div>
        </Card>
      </TabPane>
    </Tabs>

    <!-- 扫码支付弹层 -->
    <Modal
      :open="qrVisible"
      :footer="null"
      :closable="true"
      :mask-closable="false"
      width="400px"
      :title="$t('billing.scan_to_pay')"
      @cancel="closeQR"
    >
      <div class="qr-body">
        <div class="qr-channel">
          {{ qrChannel === 'alipay' ? $t('billing.alipay') : $t('billing.wechat_pay') }}
          <Tag color="var(--warning)">
            {{ effectiveCredits }} credits
          </Tag>
        </div>

        <Spin
          :spinning="payStatus === 'pending' && !qrCode"
          :tip="$t('common.generating_qr_code')"
        >
          <canvas
            v-show="qrCode"
            ref="qrCanvas"
            class="qr-canvas"
          />
        </Spin>

        <div
          v-if="payStatus === 'pending'"
          class="qr-tip"
        >
          <QrcodeOutlined />{{ $t('billing.please_scan_with_channel_to_complete_payment', { channel: qrChannel === 'alipay' ? $t('billing.alipay') : $t('chat.wechat') }) }}
          <br>
          <span class="qr-sub">{{ $t('billing.the_page_will_auto_detect_the_payment_result_no_manual_refresh_needed') }}</span>
        </div>
        <div
          v-else-if="payStatus === 'paid'"
          class="qr-success"
        >
          <PayCircleOutlined /> {{ $t('billing.payment_successful_credits_credited') }}
        </div>
        <div
          v-else
          class="qr-expired"
        >
          <Alert
            type="warning"
            :message="$t('billing.order_status', { status: payStatus === 'expired' ? $t('errors.timeout_2') : $t('errors.failed') })"
            :description="$t('common.please_close_and_re_initiate_the_recharge')"
          />
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.billing-container { padding: 24px; max-width: 1080px; margin: 0 auto; }
.billing-header { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
.billing-header h1 { margin: 0; font-size: 24px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.loading-state { padding: 0; }

/* 概览卡片：auto-fill 网格，窄屏自动折行（无需手写断点） */
.overview-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }

.balance-display { display: flex; align-items: baseline; gap: 12px; }
.balance-amount { font-size: 44px; font-weight: var(--fw-bold); color: var(--colorWarning, var(--warning)); line-height: 1.2; font-variant-numeric: tabular-nums; }
.free-quota { margin-top: 16px; }
.free-quota-label { display: flex; justify-content: space-between; color: var(--text-tertiary); font-size: 13px; margin-bottom: 4px; }
.free-quota-count { font-weight: var(--fw-semibold); color: var(--text-primary); font-variant-numeric: tabular-nums; }

/* 使用统计：auto-fill 自适应列数（窄屏自动从 3 列降为 2 列） */
.usage-stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 12px; }
.stat-item { text-align: center; padding: 14px 8px; background: var(--bg-secondary, rgba(0,0,0,0.02)); border-radius: var(--radius-lg); }
.stat-label { display: block; color: var(--text-tertiary); font-size: 12px; margin-bottom: 6px; }
.stat-value { font-size: 22px; font-weight: var(--fw-semibold); color: var(--text-primary); font-variant-numeric: tabular-nums; }
.stat-spent { color: var(--colorError, var(--error)); }
.stat-added { color: var(--colorSuccess, #10b981); }

/* 分组/区间切换条：窄屏自动折行，不挤压卡片 */
.usage-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; }
.filter-label { font-size: 13px; color: var(--text-tertiary); }
.breakdown-hint { margin-top: 10px; font-size: 12px; color: var(--text-tertiary); }

.bar-chart { display: flex; align-items: flex-end; gap: 6px; height: 170px; overflow-x: auto; padding-top: 8px; -webkit-overflow-scrolling: touch; }
.bar-col { flex: 1; min-width: 26px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar-value { font-size: var(--fs-xs); color: var(--text-tertiary); margin-bottom: 2px; white-space: nowrap; }
.bar { width: 70%; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--colorWarning, var(--warning)), var(--colorWarning-bg, #fbbf24)); }
.bar-zero { background: var(--border-card, #e5e7eb); }
.bar-label { font-size: var(--fs-xs); color: var(--text-tertiary); margin-top: 4px; white-space: nowrap; transform: rotate(-30deg); transform-origin: top left; }

.purchase-form { display: flex; flex-direction: column; gap: 18px; max-width: 480px; }
.form-item { display: flex; flex-direction: column; gap: 8px; }
.form-item label { font-weight: var(--fw-medium); color: var(--text-primary); }
/* 发票申请列表：与上方表单拉开距离，避免"提交按钮紧贴表格标题" */
.invoice-list { margin-top: 28px; }
.invoice-list h3 { margin: 0 0 12px; font-size: 16px; font-weight: var(--fw-semibold); color: var(--text-primary); }
/* 可开票订单为空时的说明：禁用下拉本身不解释原因 */
.invoice-empty-hint { font-size: 12px; color: var(--text-tertiary); }
.preset-row { display: flex; flex-wrap: wrap; gap: 8px; }
.preset-row :deep(.ant-btn) { min-height: 40px; } /* 触控目标 */
/* 充值方式单选按钮组：触控目标 ≥ 40px */
.purchase-form :deep(.ant-radio-button-wrapper) { min-height: 40px; line-height: 38px; padding: 0 16px; }
/* 自定义数量/充值按钮：桌面限宽，窄屏全宽 */
.credits-input { max-width: 240px; }
.purchase-submit { max-width: 240px; }
.price-hint { font-size: 14px; color: var(--text-tertiary); }
.purchase-note { font-size: 12px; color: var(--text-tertiary); }

.amount-add { color: var(--colorSuccess, #10b981); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
.amount-deduct { color: var(--colorError, var(--error)); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }

.qr-body { text-align: center; padding: 8px 0; }
.qr-channel { margin-bottom: 14px; font-size: 15px; font-weight: var(--fw-semibold); display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--text-primary); }
.qr-canvas { width: 220px; height: 220px; border: var(--hairline-width) solid var(--border-card); border-radius: var(--radius-lg); }
.qr-tip { margin-top: 14px; color: var(--text-primary); font-size: 14px; }
.qr-sub { color: var(--text-tertiary); font-size: 12px; }
.qr-success { margin-top: 16px; color: var(--colorSuccess, #10b981); font-size: 16px; font-weight: var(--fw-semibold); }
.qr-expired { margin-top: 16px; text-align: start; }

/* 窄屏（≤576px，与 .u-hide-sm 断点一致）：卡片贴边、表单全宽、iOS 防缩放 */
@media (max-width: 576px) {
  .billing-container { padding: 12px; }
  .billing-header h1 { font-size: 20px; }
  .balance-amount { font-size: 36px; }
  .stat-value { font-size: 18px; }
  .billing-container :deep(.ant-card-body) { padding: 16px; }
  .billing-container :deep(.ant-input) { font-size: 16px; }
  .purchase-form :deep(.ant-btn:not(.ant-btn-sm)) { min-height: 40px; }
  .credits-input, .purchase-submit { max-width: 100%; width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .bar { transition: none; }
}
</style>
