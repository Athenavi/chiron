import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * 计费页「用量分摊」（P0-1）的前端接线。
 *
 * 为什么值得钉住：
 *  - 后端 `GetUsage` 有两个数据源：`groupBy=day` 走 credit_transactions（记账口径，含充值），
 *    其它分组走 usage_events（真实 token 用量）。**前端一旦不传参数，后端默认 day**，
 *    用户点了"按模型"却看到按日流水 —— 界面不报错，但数据是错的，这比报错更难发现。
 *  - 切换分组/区间只应重查用量（`/v1/billing/usage`），不该把整页打回骨架屏（余额卡片闪一次）。
 *  - 明细表的 bucket 为空时后端给 'unknown'，前端要翻译成可读文案而不是直接显示英文标识符。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  listPaymentChannels: vi.fn(),
}))

// antd 的 Table 走 useBreakpoint → responsiveObserve，jsdom 没有 matchMedia。
// 不补这一层，每个用例结束时都会抛 Unhandled Rejection，把通过数也带塌。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../api', () => ({
  api: { get: apiMocks.get, post: apiMocks.post },
  listPaymentChannels: apiMocks.listPaymentChannels,
}))

import BillingView from '../BillingView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [{ path: '/billing', component: { template: '<div />' } }],
})

const BALANCE = { balance: 1200, daily_free_limit: 10, daily_free_used: 2, daily_free_remaining: 8 }

const DAY_USAGE = {
  daily: [{ day: '2026-10-28', tx_count: 3, credits_spent: 42, credits_added: 0 }],
  total_spent: 42,
  total_added: 0,
  period_days: 30,
}

const MODEL_USAGE = {
  group_by: 'model',
  period_days: 30,
  breakdown: [
    { bucket: 'gpt-4o', event_count: 3, input_tokens: 100, output_tokens: 50, cached_tokens: 10, cost_cents: 12 },
    // 空模型名后端归入 'unknown'，前端需翻译
    { bucket: 'unknown', event_count: 1, input_tokens: 7, output_tokens: 3, cached_tokens: 0, cost_cents: 1 },
  ],
  total_input: 107,
  total_output: 53,
  total_cached: 10,
  total_cost: 13,
  daily: [],
}

/**
 * 被后端截断的响应：只回 Top 100，但 total_* 仍是**全量**合计。
 * 这两个字段的一致性是最容易被改坏的 —— 若有人把 total_* 改成"返回行之和"，
 * 用户拿明细永远对不上账，而且界面上不会有任何报错。
 */
const TRUNCATED_USAGE = {
  group_by: 'model',
  period_days: 90,
  breakdown: [
    { bucket: 'gpt-4o', event_count: 3, input_tokens: 100, output_tokens: 50, cached_tokens: 10, cost_cents: 12 },
  ],
  total_input: 999,
  total_output: 999,
  total_cached: 0,
  total_cost: 500,
  truncated: true,
  total_buckets: 240,
  limit: 100,
  daily: [],
}

/** 按 URL 返回替身数据，并记录每次请求的 params */
function stubApi() {
  apiMocks.get.mockImplementation((url: string) => {
    if (url === '/v1/billing/balance') return Promise.resolve({ data: { data: BALANCE } })
    if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE } })
    if (url === '/v1/billing/history') return Promise.resolve({ data: { data: { history: [] } } })
    return Promise.resolve({ data: { data: {} } })
  })
}

/** 取 usage 端点最近一次请求带的 params */
function lastUsageParams() {
  const calls = apiMocks.get.mock.calls.filter((c) => c[0] === '/v1/billing/usage')
  return calls.length ? calls[calls.length - 1][1]?.params : undefined
}

/** antd 的 Radio.Button 渲染成 <input type=radio value=...>，勾选它即可触发 @change */
async function chooseRadio(wrapper: ReturnType<typeof mount>, value: string) {
  const input = wrapper.findAll('input[type="radio"]').find(i => (i.element as HTMLInputElement).value === value)
  if (!input) {
    throw new Error(`找不到 value=${value} 的单选，现有：${wrapper.findAll('input[type="radio"]').map(i => (i.element as HTMLInputElement).value).join(' | ')}`)
  }
  await input.setValue(true)
  await flushPromises()
}

async function mountView() {
  const wrapper = mount(BillingView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('BillingView 用量分摊', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stubApi()
    apiMocks.listPaymentChannels.mockResolvedValue([{ id: 'alipay', currency: 'CNY' }])
  })

  it('首次加载显式带上默认的 groupBy=day & range=30d', async () => {
    await mountView()
    // 显式传参：默认值前后端各有一份，省略参数会让"界面标题"与"实际查询"可能不一致
    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '30d' })
  })

  it('切到「按模型」只重查用量，并按明细渲染', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')

    expect(lastUsageParams()).toEqual({ groupBy: 'model', range: '30d' })
    // 余额卡片不应被重新拉取：切分组不是刷新整页
    const balanceCalls = apiMocks.get.mock.calls.filter(c => c[0] === '/v1/billing/balance')
    expect(balanceCalls).toHaveLength(1)

    const text = wrapper.text()
    expect(text).toContain('gpt-4o')
    // 'unknown' 必须翻译成可读文案，而不是原样显示
    expect(text).toContain('未知')
    expect(text).not.toContain('unknown')
  })

  it('切到 90 天时把 range 带进请求', async () => {
    const wrapper = await mountView()
    await chooseRadio(wrapper, '90d')
    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '90d' })
  })

  it('切回「按天」恢复趋势视图，不报错', async () => {    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    await chooseRadio(wrapper, 'session')
    await chooseRadio(wrapper, 'day')

    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '30d' })
    expect(wrapper.text()).toContain('每日消耗趋势')
  })

  it('明细被截断时提示"仅显示前 N 项"，且合计数不被截断', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: TRUNCATED_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    // 用户必须知道"这里显示不全"，否则会以为明细与合计对不上是数据错了
    expect(text).toContain('仅显示花费最高的')
    expect(text).toContain('240')

    // 合计来自后端全量汇总（999/500），不是 1 行明细之和（100/12）
    expect(text).toContain('999')
    expect(text).toContain('500')
  })

  it('未被截断时不显示截断提示', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')
    await flushPromises()

    // truncated 缺省（老后端）不能误报"显示不全"
    expect(wrapper.text()).not.toContain('仅显示花费最高的')
  })
})

/**
 * 导出入口（P1-1 / roadmap §10.2）。
 *
 * 钉住三件事，都是"界面不会报错但行为错了"的类型：
 *  - 请求必须带 `responseType: 'blob'`，否则 axios 会把 CSV 当 JSON 解析并抛错；
 *  - 导出范围要跟当前用量视图选的天数一致，否则用户导出的不是他刚看到的区间；
 *  - 导出中按钮要禁用，避免连点并发打同一个重查询。
 */
describe('BillingView 交易明细导出', () => {
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    stubApi()
    apiMocks.listPaymentChannels.mockResolvedValue([{ id: 'alipay', currency: 'CNY' }])
    createObjectURL = vi.fn(() => 'blob:mock-url')
    revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', {
      value: createObjectURL, writable: true, configurable: true,
    })
    Object.defineProperty(URL, 'revokeObjectURL', {
      value: revokeObjectURL, writable: true, configurable: true,
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  })

  /** antd 的 Tabs 默认只渲染激活面板，导出按钮在「交易记录」页里，得先切过去 */
  async function openHistoryTab(wrapper: ReturnType<typeof mount>) {
    const tab = wrapper.findAll('.ant-tabs-tab').find(el => el.text().includes('交易记录'))
    if (!tab) throw new Error(`找不到「交易记录」标签页，现有：${wrapper.findAll('.ant-tabs-tab').map(e => e.text()).join(' | ')}`)
    await tab.trigger('click')
    await flushPromises()
  }

  function exportCalls() {
    return apiMocks.get.mock.calls.filter(c => String(c[0]).startsWith('/v1/billing/transactions.csv'))
  }

  it('点击导出以 blob 方式请求当前时间范围', async () => {
    const wrapper = await mountView()
    await openHistoryTab(wrapper)

    const button = wrapper.find('[data-test="export-transactions"]')
    expect(button.exists()).toBe(true)
    await button.trigger('click')
    await flushPromises()

    const calls = exportCalls()
    expect(calls).toHaveLength(1)
    const [url, config] = calls[0]
    // 默认区间是 30d（与用量视图一致）
    expect(String(url)).toContain('from=')
    expect(String(url)).toContain('to=')
    expect(config?.responseType).toBe('blob')
    // 真的触发了下载，而不是把内容留在内存里
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledTimes(1)
  })

  it('先切到 90 天，导出请求的区间也随之变宽', async () => {
    const wrapper = await mountView()
    await chooseRadio(wrapper, '90d')
    await openHistoryTab(wrapper)

    await wrapper.find('[data-test="export-transactions"]').trigger('click')
    await flushPromises()

    const [url] = exportCalls()[0]
    // 解析回两端时间，确认跨度确实是 90 天而不是固定 30 天
    const parsed = new URL(String(url), 'http://localhost')
    const from = Date.parse(parsed.searchParams.get('from')!)
    const to = Date.parse(parsed.searchParams.get('to')!)
    expect((to - from) / 86_400_000).toBe(90)
  })
})

/**
 * 计费页「发票申请」（P1-2）的前端接线。
 *
 * 为什么值得钉住：
 *  - 用户要开票必须先选「哪一笔订单」，而这个列表在后端是**单独端点**
 *    （`/v1/billing/invoiceable-orders`）—— 交易流水（credit_transactions）里没有 payment_id，
 *    前端若误从流水里取订单，下拉永远是空的，且不会报错。
 *  - `amount_cents` 是最小单位（分）。直接当元显示会把 ¥12.34 显示成 ¥1234，
 *    差 100 倍，而界面上看不出任何异常。
 *  - 驳回理由必须展示：没有它用户只会反复提交同一笔，每次都被 409 拦下。
 */
describe('BillingView 发票申请', () => {
  const PAID_ORDER = {
    id: 'pay_0123456789abcdef',
    amount_cents: 1234,
    currency: 'CNY',
    credits: 1000,
    paid_at: '2026-10-20T10:00:00Z',
  }

  const REJECTED_INVOICE = {
    id: 'inv-1',
    payment_id: 'pay_0123456789abcdef',
    title: '某某科技有限公司',
    tax_no: '91310000MA1K3XYZ12',
    amount_cents: 1234,
    currency: 'CNY',
    status: 'rejected',
    review_note: '抬头与税号不匹配',
    created_at: '2026-10-21T09:00:00Z',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listPaymentChannels.mockResolvedValue([{ id: 'alipay', currency: 'CNY' }])
  })

  function stubInvoiceApi(orders: unknown[], invoices: unknown[]) {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/balance') return Promise.resolve({ data: { data: BALANCE } })
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE } })
      if (url === '/v1/billing/invoiceable-orders') return Promise.resolve({ data: { data: orders } })
      if (url === '/v1/billing/invoices') return Promise.resolve({ data: { data: invoices } })
      return Promise.resolve({ data: { data: {} } })
    })
  }

  /** antd 的 Tabs 默认只渲染激活面板，发票 tab 得先切过去才会发请求 */
  async function openInvoiceTab(wrapper: ReturnType<typeof mount>) {
    const tab = wrapper.findAll('.ant-tabs-tab').find(el => el.text().includes('发票申请'))
    if (!tab) throw new Error(`找不到「发票申请」标签页，现有：${wrapper.findAll('.ant-tabs-tab').map(e => e.text()).join(' | ')}`)
    await tab.trigger('click')
    await flushPromises()
  }

  function callsTo(path: string) {
    return apiMocks.get.mock.calls.filter(c => c[0] === path)
  }

  it('切到发票页才加载，且订单与申请是两个独立请求', async () => {
    stubInvoiceApi([PAID_ORDER], [])
    const wrapper = await mountView()

    // 懒加载：没进 tab 之前不该发这两个请求
    expect(callsTo('/v1/billing/invoiceable-orders')).toHaveLength(0)
    expect(callsTo('/v1/billing/invoices')).toHaveLength(0)

    await openInvoiceTab(wrapper)

    expect(callsTo('/v1/billing/invoiceable-orders')).toHaveLength(1)
    expect(callsTo('/v1/billing/invoices')).toHaveLength(1)
  })

  it('金额按最小单位换算，1234 分显示为 ¥12.34 而不是 ¥1234', async () => {
    stubInvoiceApi([], [REJECTED_INVOICE])
    const wrapper = await mountView()
    await openInvoiceTab(wrapper)

    const text = wrapper.text()
    expect(text).toContain('¥12.34')
    expect(text).not.toContain('¥1234')
  })

  it('驳回的申请仍展示，并带出审核意见', async () => {
    stubInvoiceApi([], [REJECTED_INVOICE])
    const wrapper = await mountView()
    await openInvoiceTab(wrapper)

    const text = wrapper.text()
    // 状态翻译成中文，不能原样显示 'rejected'
    expect(text).toContain('已驳回')
    expect(text).not.toContain('rejected')
    // 没有驳回理由，用户只能反复提交同一笔
    expect(text).toContain('抬头与税号不匹配')
  })

  it('没有可开票订单时给出空态说明，且提交按钮不可点', async () => {
    stubInvoiceApi([], [])
    const wrapper = await mountView()
    await openInvoiceTab(wrapper)

    expect(wrapper.text()).toContain('暂无可开票的已支付订单')
    const submit = wrapper.find('[data-test="invoice-submit"]')
    expect(submit.exists()).toBe(true)
    // 表单空着就不该允许提交：避免一次必然 400 的往返
    expect(submit.attributes('disabled')).toBeDefined()
  })
})
