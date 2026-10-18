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
 *      否则会发出一条没有下文的提交；
 *   5. M15c 的第三类 `budget` 与上面两类**形状不同**：提交 `{cap_tokens}`、即刻生效，
 *      所以成功后是"重拉列表"而不是就地转 `answered`（那一行已经被关掉，留着已答复行是谎）。
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
// M15c：任务线预算耗尽。request 里的数字由引擎写成 JSON number（不是字符串），
// 少了哪一列就不写 —— 卡片必须能扛住"读不到账目"。
const budgetItem = () => ({
  id: 'i3',
  kind: 'budget' as const,
  status: 'pending' as const,
  tool_name: 'subagent',
  request: {
    // 与引擎 `_report_line_exhaustion` 写进 request 的那句逐字一致（数字用 Python 的千分位）
    question: '这条子 Agent 任务线已花到 token 上限（2,000,000），新的段已被拦下。要把上限提到多少？',
    root_run_id: 'rt_root1',
    axis: 'line_tokens',
    cap_tokens: 2000000,
    settled_tokens: 1234567,
    reserved_tokens: 0,
    suggested_cap_tokens: 4000000,
    run_id: 'rt_child1',
  },
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

  // ── M15c：预算提额票 ──

  it('提额票渲染账目并预填建议上限', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([budgetItem()])
    const wrapper = mountInbox()
    await flushPromises()

    expect(wrapper.find('.inbox-budget-question').text()).toContain('要把上限提到多少？')
    // 数字按千分位呈现，但分隔符属于运行环境 locale ⇒ 与组件用同一个调用算出期望值
    const numbers = wrapper.find('.inbox-budget-numbers').text()
    expect(numbers).toContain((1234567).toLocaleString())
    expect(numbers).toContain((2000000).toLocaleString())
    // 预填：那是引擎给的起点，不是一个空框
    const capInput = wrapper.find('.inbox-budget-field input').element as HTMLInputElement
    expect(capInput.value).toBe('4000000')
  })

  it('读不到账目时显示破折号，不编一个 0 出来', async () => {
    // 线账的读路径是 fail-open 的：取不到 state 时后端**不写**这几列（缺席 ≠ 0）
    interactionsApi.listPendingInteractions.mockResolvedValue([
      { ...budgetItem(), request: { question: '预算已用尽', suggested_cap_tokens: 4000000 } },
    ])
    const wrapper = mountInbox()
    await flushPromises()

    expect(wrapper.find('.inbox-budget-numbers').text()).toContain('—')
    expect(wrapper.find('.inbox-budget-numbers').text()).not.toContain('0')
  })

  it('提交新上限 ⇒ 按 cap_tokens 提交、重拉列表、发一次唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([budgetItem()])
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.inbox-budget-field input').setValue('6000000')
    await wrapper.find('.inbox-budget-submit').trigger('click')
    await flushPromises()

    // 数字不是字符串：后端按 `isinstance(raw_cap, int)` 校验，发字符串会被判成 invalid_decision
    expect(interactionsApi.resolveInteraction).toHaveBeenCalledWith({
      interactionId: 'i3',
      sessionId: 's1',
      response: { cap_tokens: 6000000 },
    })
    expect(wrapper.emitted('wake')).toHaveLength(1)
    // 提额即刻生效、那一行随即被关闭 ⇒ 不就地转 answered，而是回到列表去取真实状态
    expect(wrapper.find('.inbox-answered').exists()).toBe(false)
    expect(interactionsApi.listPendingInteractions).toHaveBeenCalledTimes(2)
  })

  it('非法上限 ⇒ 就地报错，不发请求、不唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([budgetItem()])
    const wrapper = mountInbox()
    await flushPromises()

    // 空 / 0 / 负数 / 小数 / 非数字都算非法（`-1` 后端子面是"不限"，只能由配置给）
    for (const raw of ['', '0', '-1', '2.5', 'abc']) {
      await wrapper.find('.inbox-budget-field input').setValue(raw)
      await wrapper.find('.inbox-budget-submit').trigger('click')
      await flushPromises()
    }

    expect(interactionsApi.resolveInteraction).not.toHaveBeenCalled()
    expect(wrapper.emitted('wake')).toBeUndefined()
    expect(wrapper.find('.inbox-budget').text()).toContain('请输入大于 0 的整数')
  })

  it('提额被后端拒 ⇒ 就地报错、不重拉、不唤醒', async () => {
    interactionsApi.listPendingInteractions.mockResolvedValue([budgetItem()])
    interactionsApi.resolveInteraction.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 500'), {
        response: { status: 500, data: { error: 'could not update the line budget' } },
      }),
    )
    const wrapper = mountInbox()
    await flushPromises()

    await wrapper.find('.inbox-budget-field input').setValue('6000000')
    await wrapper.find('.inbox-budget-submit').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('wake')).toBeUndefined()
    // 上限没动 ⇒ 这张票还该看得见，重拉只会把它擦掉又立刻重新出现，不如就地保留草稿
    expect(interactionsApi.listPendingInteractions).toHaveBeenCalledTimes(1)
    expect((wrapper.find('.inbox-budget-field input').element as HTMLInputElement).value)
      .toBe('6000000')
    expect(wrapper.find('.inbox-budget').text()).toContain('服务器拒绝了这个答复')
  })
})
