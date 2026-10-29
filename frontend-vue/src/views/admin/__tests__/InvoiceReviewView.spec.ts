import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 管理端「发票审核」页（P1-2）的关键接线。
 *
 * 为什么值得钉住：
 *  - **默认只看待审**。审核页第一屏必须是"等我处理的事"；如果默认拉全量，
 *    待审条目会被历史记录冲散。这条契约靠后端默认值 + 前端显式传参共同保证，
 *    任何一侧改动都会让审核员漏审。
 *  - **只有 pending 行给操作按钮**。后端 UPDATE 带 `AND status = 'pending'`，
 *    已审的再点一次只会收到 409 —— 界面上不该出现这个必然失败的按钮。
 *  - `amount_cents` 是分。差 100 倍的金额在审核场景下等于给错发票。
 */

const apiMocks = vi.hoisted(() => ({
  listInvoices: vi.fn(),
  reviewInvoice: vi.fn(),
}))

vi.mock('../../../api/admin', () => ({
  listInvoices: apiMocks.listInvoices,
  reviewInvoice: apiMocks.reviewInvoice,
}))

// antd 的 Table / Select 走 useBreakpoint → matchMedia，jsdom 没有实现
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

import InvoiceReviewView from '../InvoiceReviewView.vue'

const PENDING = {
  id: 'inv-pending',
  payment_id: 'pay_0123456789abcdef',
  title: '某某科技有限公司',
  tax_no: '91310000MA1K3XYZ12',
  amount_cents: 1234,
  currency: 'CNY',
  status: 'pending' as const,
  note: '请尽快',
  created_at: '2026-10-21T09:00:00Z',
}

const APPROVED = {
  id: 'inv-approved',
  payment_id: 'pay_fedcba9876543210',
  title: '另一家公司',
  tax_no: '91310000MA1K3AAA99',
  amount_cents: 5000,
  currency: 'CNY',
  status: 'approved' as const,
  review_note: '',
  reviewed_at: '2026-10-22T09:00:00Z',
  created_at: '2026-10-20T09:00:00Z',
}

async function mountView() {
  const wrapper = mount(InvoiceReviewView)
  await flushPromises()
  return wrapper
}

describe('InvoiceReviewView 发票审核', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMocks.listInvoices.mockResolvedValue([PENDING, APPROVED])
  })

  it('默认只拉待审队列', async () => {
    await mountView()
    // 省略参数会让后端用默认值，但这里显式传 'pending' 把契约钉在调用点
    expect(apiMocks.listInvoices).toHaveBeenCalledWith('pending')
  })

  it('切换筛选后按新状态重拉', async () => {
    const wrapper = await mountView()
    // 直接调用组件内的 load 走的是同一个入口，但这里走 UI：选中「全部」
    const select = wrapper.findComponent({ name: 'ASelect' })
    await select.vm.$emit('update:value', 'all')
    await select.vm.$emit('change', 'all')
    await flushPromises()

    expect(apiMocks.listInvoices).toHaveBeenLastCalledWith('all')
  })

  it('只有待审行给通过/驳回按钮', async () => {
    const wrapper = await mountView()

    // 两行数据 → 只有一组操作按钮
    expect(wrapper.findAll('[data-test="invoice-approve"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-test="invoice-reject"]')).toHaveLength(1)
  })

  it('金额按最小单位换算，1234 分显示为 ¥12.34', async () => {
    const wrapper = await mountView()
    const text = wrapper.text()

    expect(text).toContain('¥12.34')
    expect(text).toContain('¥50.00')
    // 直接当元显示会得到 1234 / 5000，差 100 倍
    expect(text).not.toContain('¥1234')
    expect(text).not.toContain('¥5000')
  })

  it('把抬头与税号一起展示，避免审核员开错票', async () => {
    const wrapper = await mountView()
    const text = wrapper.text()

    expect(text).toContain('某某科技有限公司')
    expect(text).toContain('91310000MA1K3XYZ12')
  })
})
