import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SubAgentApprovalCard from '../SubAgentApprovalCard.vue'

/**
 * 子 Agent 工具确认卡片。
 *
 * 回归背景：子 Agent 默认 tools_mode=auto，shell_exec 这类工具在 auto 下就需要确认，
 * 所以"子 Agent 请求批准"几乎每次调用命令类工具都会发生。在此之前它只以一行 notice
 * 出现 —— 用户看得见却**批不了**，子 Agent 空转到 300s 超时后才以 approval timed out
 * 被拒（一次必然发生的空转）。这个卡片的作用就是让"看得见"变成"能决定"。
 */
describe('SubAgentApprovalCard（子 Agent 工具确认）', () => {
  it('把决定抛给父组件（允许 / 拒绝各一次）', async () => {
    const wrapper = mount(SubAgentApprovalCard, {
      props: { toolName: 'shell_exec', args: '{"command":"ls"}' },
    })
    await wrapper.find('.approval-btn.allow').trigger('click')
    await wrapper.find('.approval-btn.danger').trigger('click')

    expect(wrapper.emitted('decide')?.map(e => e[0])).toEqual([true, false])
  })

  it('批准前必须能看清要执行什么（工具名 + 参数）', () => {
    const wrapper = mount(SubAgentApprovalCard, {
      props: {
        toolName: 'shell_exec',
        args: '{"command":"rm -rf /tmp/x"}',
        content: '请求执行 shell_exec（级别 high）',
      },
    })
    expect(wrapper.text()).toContain('shell_exec')
    expect(wrapper.text()).toContain('rm -rf /tmp/x')
    expect(wrapper.text()).toContain('级别 high')
  })

  it('参数不是合法 JSON 时原样展示（展示不该影响可用性）', () => {
    const wrapper = mount(SubAgentApprovalCard, { props: { toolName: 'x', args: 'not-json' } })
    expect(wrapper.find('.approval-args').text()).toBe('not-json')
  })

  it('提交中禁用按钮，避免同一个调用被决定两次', async () => {
    const wrapper = mount(SubAgentApprovalCard, {
      props: { toolName: 'x', submitting: true },
    })
    expect(wrapper.find('.approval-btn.allow').attributes('disabled')).toBeDefined()
    await wrapper.find('.approval-btn.allow').trigger('click')
    expect(wrapper.emitted('decide')).toBeUndefined()
  })

  it('已有结论时显示结果而不是按钮', () => {
    const wrapper = mount(SubAgentApprovalCard, {
      props: { toolName: 'x', decision: 'approved' },
    })
    expect(wrapper.find('.approval-done').text()).toContain('已允许')
    expect(wrapper.find('.approval-btn').exists()).toBe(false)
  })

  it('提交失败时保留按钮并写明原因（不做假成功）', () => {
    const wrapper = mount(SubAgentApprovalCard, {
      props: { toolName: 'x', error: '审批未生效（可能已超时）' },
    })
    expect(wrapper.find('.approval-error').text()).toContain('可能已超时')
    expect(wrapper.find('.approval-btn.allow').attributes('disabled')).toBeUndefined()
  })
})
