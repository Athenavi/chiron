import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import InteractionInbox from '../InteractionInbox.vue'

/**
 * 人工介入待办收件箱（M7c）。
 *
 * 钉住这条链路的四条行为，因为它们各自对应一个真实故障：
 *   1. 没有待办时**整块不渲染** —— 收件箱是补漏入口，不该在正常会话里占地方；
 *   2. 审批票的"允许/拒绝/编辑"都按库里的 kind 提交正确形状，成功后转成已答复态并发 `wake`；
 *   3. 提问票复用 AskCard，答案原样回传；
 *   4. 答复失败（网关把 not_found/stale/invalid 映射成 4xx）时**不唤醒**、就地报错，
 *      否则会发出一条没有下文的提交。
 */
const interactionsApi = vi.hoisted(() => ({
  listPendingInteractions: vi.fn(),
  resolveInteraction: vi.fn(),
}))
vi.mock('../../../api/interactions', () => interactionsApi)

const describeApiError = vi.hoisted(() => vi.fn(() => '服务器拒绝了这个答复'))
vi.mock('../../../utils/apiError', () => ({ describeApiError }))

enableAutoUnmount(afterEach)

// 每个用例都要拿到**新对象**：respond 成功时会就地改 item.status/decision，
// 复用模块级常量会让上一个用例的"已答复"泄漏进下一个用例。
const approvalItem = () => ({
  id: 'i1',
  kind: 'approval' as const,
  status: 'pending' as const,
  tool_name: 'shell_exec',
  request: { question: '请求执行 shell_exec', tool_name: 'shell_exec', arguments: { command: 'ls' } },
})
const questionItem = () => ({
  id: 'i2',
  kind: 'question' as const,
  status: 'pending' as const,
  tool_name: 'ask_user',
  request: { question: '选哪个环境？', options: ['dev', 'prod'] },
})

function mountInbox() {
  return mount(InteractionInbox, { props: { sessionId: 's1' } })
}

beforeEach(() => {
  vi.clearAllMocks()
  interactionsApi.resolveInteraction.mockResolvedValue(true)
})

describe('InteractionInbox', () => {
  it('没有待办时整块不渲染', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([])
    const wrapper = mountInbox()
    await flushPromises()
    expect(interactionsApi.listPendingInteractions).toHaveBeenCalledWith('s1')
    expect(wrapper.find('.interaction-inbox').exists()).toBe(false)
  })

  it('审批票点"允许"⇒ 按 approve 提交、转已答复、发一次唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem()])
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(interactionsApi.resolveInteraction).toHaveBeenCalledWith({
      interactionId: 'i1',
      sessionId: 's1',
      response: { decision: 'approve' },
    })
    expect(wrapper.emitted('wake')).toHaveLength(1)
    // 已答复后按钮消失，只留一行说明（不给重复作答留入口）
    expect(wrapper.find('.approval-btn.allow').exists()).toBe(false)
    expect(wrapper.find('.inbox-answered').exists()).toBe(true)
  })

  it('审批票点"拒绝"⇒ 按 reject 提交', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem()])
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.approval-btn.danger').trigger('click')
    await flushPromises()

    expect(interactionsApi.resolveInteraction.mock.calls[0][0].response).toEqual({ decision: 'reject' })
  })

  it('提问票选择选项⇒ 原样回传答案并唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([questionItem()])
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.ask-option').trigger('click')
    await flushPromises()

    expect(interactionsApi.resolveInteraction).toHaveBeenCalledWith({
      interactionId: 'i2',
      sessionId: 's1',
      response: { answer: 'dev' },
    })
    expect(wrapper.emitted('wake')).toHaveLength(1)
  })

  it('已经是 answered 的票只显示一行说明、没有可操作控件', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([{ ...approvalItem(), status: "answered" }])
    const wrapper = mountInbox()
    await flushPromises()

    expect(wrapper.find('.inbox-answered').exists()).toBe(true)
    expect(wrapper.find('.approval-btn.allow').exists()).toBe(false)
  })

  it('resolve 返回 false（未答上）⇒ 就地报错、不唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([questionItem()])
    interactionsApi.resolveInteraction.mockResolvedValue(false)
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.ask-option').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('wake')).toBeUndefined()
    // 200 但 data 为空（不是失败抛出）走本地兜底文案，而不是 describeApiError
    expect(wrapper.text()).toContain('回答提交失败')
    expect(describeApiError).not.toHaveBeenCalled()
  })

  it('resolve 抛错（4xx）⇒ 呈现后端原因、不唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([approvalItem()])
    interactionsApi.resolveInteraction.mockRejectedValue(new Error('boom'))
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('wake')).toBeUndefined()
    expect(describeApiError).toHaveBeenCalled()
    expect(wrapper.find('.approval-card').text()).toContain('服务器拒绝了这个答复')
  })
})
