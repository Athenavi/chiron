import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import InteractionBadge from '../InteractionBadge.vue'

/**
 * 待办角标（M21）。
 *
 * 钉住三件事，各自对应一个真实故障：
 *   1. **没有待办时整块不渲染** —— 角标不该在正常会话里占地方；
 *   2. **点击跳到那条待办所属的会话**（跨会话列表的 `session_id` 就是为此而存在）；
 *   3. **用户级事件到达时重新拉取** —— 推送是"更快被看见"，而不是唯一的数据源
 *      （拉取失败时保持上一次读数，不把"拉不到"显示成"没有待办"）。
 */
const interactionsApi = vi.hoisted(() => ({
  listMyPendingInteractions: vi.fn(),
  createUserEventsConnection: vi.fn(),
}))
vi.mock('../../../api/interactions', () => interactionsApi)

enableAutoUnmount(afterEach)

const fakeSource = { close: vi.fn() }

function mountBadge() {
  return mount(InteractionBadge, {
    global: { mocks: { $t: (key: string) => key } },
  })
}

beforeEach(() => {
  interactionsApi.listMyPendingInteractions.mockReset()
  interactionsApi.createUserEventsConnection.mockReset()
  interactionsApi.createUserEventsConnection.mockReturnValue(fakeSource)
  fakeSource.close.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('InteractionBadge', () => {
  it('无待办时不渲染，但仍然建立推送连接', async () => {
    interactionsApi.listMyPendingInteractions.mockResolvedValue([])

    const wrapper = mountBadge()
    await flushPromises()

    expect(wrapper.find('.interaction-badge').exists()).toBe(false)
    // 连接必须建：下一张票可能在任何时刻出现，那时才建连接就晚了。
    expect(interactionsApi.createUserEventsConnection).toHaveBeenCalledTimes(1)
  })

  it('有待办时显示计数，点击跳到那条待办所属的会话', async () => {
    interactionsApi.listMyPendingInteractions.mockResolvedValue([
      { id: 'i1', kind: 'approval', status: 'pending', session_id: 's-42' },
      { id: 'i2', kind: 'question', status: 'pending', session_id: 's-7' },
    ])

    const wrapper = mountBadge()
    await flushPromises()

    const btn = wrapper.find('.interaction-badge')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')

    expect(wrapper.emitted('select')).toEqual([['s-42']])
  })

  it('推送到达时重新拉取列表', async () => {
    interactionsApi.listMyPendingInteractions.mockResolvedValue([])

    mountBadge()
    await flushPromises()
    expect(interactionsApi.listMyPendingInteractions).toHaveBeenCalledTimes(1)

    // 模拟服务端推来一条待办事件：回调里应当重新拉取（而不是自己算计数）。
    const onEvent = interactionsApi.createUserEventsConnection.mock.calls[0][0] as (
      data: unknown,
    ) => void
    onEvent({ type: 'interaction_pending', session_id: 's-1' })
    await flushPromises()

    expect(interactionsApi.listMyPendingInteractions).toHaveBeenCalledTimes(2)
  })

  it('拉取失败时保持上一次读数，不把失败显示成“没有待办”', async () => {
    interactionsApi.listMyPendingInteractions.mockResolvedValueOnce([
      { id: 'i1', kind: 'approval', status: 'pending', session_id: 's-42' },
    ])

    const wrapper = mountBadge()
    await flushPromises()
    expect(wrapper.find('.interaction-badge').exists()).toBe(true)

    interactionsApi.listMyPendingInteractions.mockRejectedValueOnce(new Error('boom'))
    const onEvent = interactionsApi.createUserEventsConnection.mock.calls[0][0] as (
      data: unknown,
    ) => void
    onEvent({ type: 'interaction_pending' })
    await flushPromises()

    expect(wrapper.find('.interaction-badge').exists()).toBe(true)
  })

  it('卸载时关闭推送连接', async () => {
    interactionsApi.listMyPendingInteractions.mockResolvedValue([])

    const wrapper = mountBadge()
    await flushPromises()
    wrapper.unmount()

    expect(fakeSource.close).toHaveBeenCalled()
  })
})
