import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 租户后台「配额管理」页的关键边界。
 *
 * 为什么钉这两条：
 *  - `unlimited=true` 时**不能算百分比**。除以 0 会得到 NaN / Infinity，
 *    进度条要么画满要么空白 —— 两种都在骗用户"你用完了"或"你一点没用"。
 *  - `usage_source === 'none'` 表示"没有用量概念"（并发）或"查询失败"。
 *    此时显示 0% 会让用户以为"完全没使用"，而真实情况是根本没这个指标。
 */
const apiMocks = vi.hoisted(() => ({
  listTenantQuota: vi.fn(),
  listTenantQuotaRequests: vi.fn(),
  createQuotaRequest: vi.fn(),
}))

vi.mock('../../api/admin', () => ({
  listTenantQuota: apiMocks.listTenantQuota,
  listTenantQuotaRequests: apiMocks.listTenantQuotaRequests,
  createQuotaRequest: apiMocks.createQuotaRequest,
}))

// antd 的 Select / Table 走 useBreakpoint → matchMedia，jsdom 没有实现
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

import TenantQuotaView from '../tenant/QuotaView.vue'

function pool(over: Record<string, unknown>) {
  return {
    resource_type: 'token',
    period: 'monthly',
    total_amount: 0,
    used_amount: 0,
    usage_source: 'usage_events',
    unlimited: false,
    ...over,
  }
}

async function mountWith(pools: unknown[], requests: unknown[] = []) {
  apiMocks.listTenantQuota.mockResolvedValue(pools)
  apiMocks.listTenantQuotaRequests.mockResolvedValue(requests)
  const wrapper = mount(TenantQuotaView)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  apiMocks.listTenantQuota.mockReset()
  apiMocks.listTenantQuotaRequests.mockReset()
  apiMocks.createQuotaRequest.mockReset()
})

describe('TenantQuotaView 配额进度条', () => {
  it('有限额且有用量时渲染进度条，百分比按已用/上限', async () => {
    const wrapper = await mountWith([
      pool({ resource_type: 'token', total_amount: 1000, used_amount: 250 }),
    ])
    const bar = wrapper.find('[role="progressbar"]')
    expect(bar.exists()).toBe(true)
    expect(bar.attributes('aria-valuenow')).toBe('25')
  })

  it('无限额（unlimited）时不渲染进度条，避免除以 0 得到 NaN/满格', async () => {
    const wrapper = await mountWith([
      pool({ resource_type: 'token', total_amount: 0, used_amount: 50, unlimited: true }),
    ])
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
  })

  it('并发这类没有用量概念的资源不渲染进度条', async () => {
    const wrapper = await mountWith([
      pool({
        resource_type: 'concurrency',
        total_amount: 10,
        used_amount: 0,
        usage_source: 'none',
      }),
    ])
    expect(wrapper.find('[role="progressbar"]').exists()).toBe(false)
  })

  it('超出上限时百分比封顶 100，不画出超过一圈的进度', async () => {
    const wrapper = await mountWith([
      pool({ resource_type: 'credits', total_amount: 100, used_amount: 180 }),
    ])
    const bar = wrapper.find('[role="progressbar"]')
    expect(bar.attributes('aria-valuenow')).toBe('100')
  })

  it('没有任何配额池时给出「尚未配置」提示，而不是空白页', async () => {
    const wrapper = await mountWith([])
    expect(wrapper.text()).toContain('尚未为本租户配置任何配额')
  })
})

describe('TenantQuotaView 申请与历史', () => {
  it('挂载时同时拉配额池与申请历史', async () => {
    await mountWith([], [])
    expect(apiMocks.listTenantQuota).toHaveBeenCalledTimes(1)
    expect(apiMocks.listTenantQuotaRequests).toHaveBeenCalledTimes(1)
  })

  it('历史列表展示申请的目标值与状态', async () => {
    const wrapper = await mountWith([], [
      {
        id: 'qr-1',
        tenant_id: 1,
        resource_type: 'token',
        period: 'monthly',
        current_amount: 1000,
        requested_amount: 5000,
        status: 'pending',
        created_at: '2026-11-08T02:00:00Z',
      },
    ])
    const text = wrapper.text()
    expect(text).toContain('5000')
    expect(text).toContain('待审批')
  })
})
