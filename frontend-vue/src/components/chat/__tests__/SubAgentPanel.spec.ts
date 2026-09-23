import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import SubAgentPanel from '../SubAgentPanel.vue'

/**
 * 子 Agent 面板的审批链路（P3-后续）。
 *
 * 这条链路此前**根本不存在**：引擎只把 approval 转成一行 notice，前端没有任何控件，
 * 用户唯一能做的是看着子 Agent 空转 300 秒后失败。这里钉住三件事：
 *   1. 有审批在等时，**不点开也能看见**（运行行上的待确认标记）；
 *   2. 点开后有可操作的允许/拒绝按钮；
 *   3. 决定经 `/v1/agent/approval` 回传（与主 Agent 审批同一通道）。
 */
const api = vi.hoisted(() => ({ submitApproval: vi.fn() }))
vi.mock('../../../api', () => api)

const subagentApi = vi.hoisted(() => ({
  listSubagentRuns: vi.fn(),
  getSubagentRunEvents: vi.fn(),
  cancelSubagentRun: vi.fn(),
  cancelSessionSubagents: vi.fn(),
}))
vi.mock('../../../api/subagent', () => subagentApi)

enableAutoUnmount(afterEach)

const APPROVAL_EVENT = {
  type: 'subagent.approval',
  run_id: 'rs_1',
  depth: 1,
  tool_call_id: 'tc_1',
  tool_name: 'shell_exec',
  tool_arguments: '{"command":"ls"}',
  content: '请求执行 shell_exec（级别 high）',
}

function mountPanel(liveEvents: unknown[]) {
  return mount(SubAgentPanel, {
    props: { sessionId: 's1', liveEvents: liveEvents as never },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  subagentApi.listSubagentRuns.mockResolvedValue({ runs: [], source: 'redis' })
  subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [APPROVAL_EVENT], source: 'redis' })
  api.submitApproval.mockResolvedValue(true)
})

describe('SubAgentPanel 的审批链路', () => {
  it('运行行上的待确认标记：不点开也知道有东西在等', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()

    const badge = wrapper.find('.run-approval')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('待确认')
  })

  it('点待确认标记 → 打开事件流并渲染可操作的审批卡片', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    // 回放（事件端点）里的事件也要能渲染成卡片 —— 刷新页面后审批不能消失
    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_1')
    const card = wrapper.find('.approval-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('shell_exec')
    expect(wrapper.find('.approval-btn.allow').exists()).toBe(true)
  })

  it('点"允许"把决定回传到 /v1/agent/approval 并显示结果', async () => {    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(api.submitApproval).toHaveBeenCalledTimes(1)
    expect(api.submitApproval).toHaveBeenCalledWith({
      session_id: 's1',
      tool_call_id: 'tc_1',
      approved: true,
    })
    expect(wrapper.find('.approval-done').text()).toContain('已允许')
  })

  it('引擎说"没生效"时明确报错，而不是把卡片默默撤掉', async () => {
    api.submitApproval.mockResolvedValue(false)
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(wrapper.find('.approval-error').text()).toContain('审批未生效')
    // 决定未生效 → 不标记为已允许，用户可重试
    expect(wrapper.find('.approval-done').exists()).toBe(false)
    expect(wrapper.find('.approval-btn.allow').attributes('disabled')).toBeUndefined()
  })
})

describe('SubAgentPanel 的运行跟随', () => {
  it('新派发的子 Agent 会被自动选中（不必刷新页面）', async () => {
    // 一轮对话结束后仍在跑的后台 run：列表里第一个是**已完成的旧 run**。
    // 旧实现只在"从未选中"时才选 runs[0] —— 于是新 run 永远不会被选中，
    // 只有它的终态轮询在跑，用户看不到它的进度与审批，只能刷新页面。
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [
        { run_id: 'rs_done', status: 'completed', depth: 1 },
        { run_id: 'rs_live', status: 'running', depth: 1 },
      ],
      source: 'redis',
    })
    mountPanel([])
    await flushPromises()

    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_live')
  })

  it('全部终态时选中最近完成的一个（有东西可看）', async () => {
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [
        { run_id: 'rs_1', status: 'completed', depth: 1 },
        { run_id: 'rs_2', status: 'failed', depth: 1 },
      ],
      source: 'db',
    })
    mountPanel([])
    await flushPromises()

    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_2')
  })
})
