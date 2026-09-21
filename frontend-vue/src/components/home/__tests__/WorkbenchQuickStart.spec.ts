import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { Select } from 'ant-design-vue'
import WorkbenchQuickStart from '../WorkbenchQuickStart.vue'

const push = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

const api = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listKnowledgeBases: vi.fn(),
  listPlugins: vi.fn(),
  listSkillResources: vi.fn(),
  listWorkflows: vi.fn(),
}))
vi.mock('../../../api', () => api)

function mockAllResources() {
  api.listKnowledgeBases.mockResolvedValue([{ id: 'kb-1', name: '产品文档' }])
  api.listAgents.mockResolvedValue([{ id: 'ag-1', name: '研究助手' }])
  api.listSkillResources.mockResolvedValue([{ id: 'pdf', name: 'PDF 解析' }])
  api.listWorkflows.mockResolvedValue([{ id: 'wf-1', name: '周报流水线' }])
  api.listPlugins.mockResolvedValue([{ id: 'fs-mcp', name: 'fs-mcp' }])
}

function mockAllFail() {
  api.listKnowledgeBases.mockRejectedValue(new Error('boom'))
  api.listAgents.mockRejectedValue(new Error('boom'))
  api.listSkillResources.mockRejectedValue(new Error('boom'))
  api.listWorkflows.mockRejectedValue(new Error('boom'))
  api.listPlugins.mockRejectedValue(new Error('boom'))
}

beforeEach(() => {
  push.mockReset()
  vi.clearAllMocks()
})

describe('WorkbenchQuickStart（首页协作入口）', () => {
  it('并行加载五类工作台资源，任一失败不影响其它', async () => {
    mockAllResources()
    api.listWorkflows.mockRejectedValue(new Error('graph service down'))
    const wrapper = mount(WorkbenchQuickStart)
    await flushPromises()

    expect(api.listKnowledgeBases).toHaveBeenCalled()
    // 关键：工作流挂了，但入口本身仍然可用（不显示"还没有可用资源"降级文案）
    expect(wrapper.text()).not.toContain('还没有可用资源')
    expect(wrapper.text()).toContain('带着工作台能力开对话')
  })

  it('五类全部不可用时给出降级提示，并指向各工作台自带的入口', async () => {
    mockAllFail()
    const wrapper = mount(WorkbenchQuickStart)
    await flushPromises()

    expect(wrapper.text()).toContain('还没有可用资源')
    expect(wrapper.text()).toContain('在对话中使用')
  })

  it('没有勾选任何能力时不能开始对话（避免推出一个空上下文）', async () => {
    mockAllResources()
    const wrapper = mount(WorkbenchQuickStart)
    await flushPromises()

    const button = wrapper.find('button')
    expect(button.attributes('disabled')).toBeDefined()
    expect(push).not.toHaveBeenCalled()
  })

  it('勾选多类能力后跳转的 URL 用重复参数（与对话页的解析约定一致）', async () => {
    mockAllResources()
    const wrapper = mount(WorkbenchQuickStart)
    await flushPromises()

    const selects = wrapper.findAllComponents(Select)
    // 知识库 / Agent / 技能 / 工作流 / 插件 / 记忆 —— 记忆此前是唯一没有入口的类型（问题 3）
    expect(selects).toHaveLength(6)
    await selects[0]!.vm.$emit('update:value', ['kb-1'])
    await selects[2]!.vm.$emit('update:value', ['pdf'])
    await selects[4]!.vm.$emit('update:value', ['fs-mcp'])
    await flushPromises()

    await wrapper.find('button').trigger('click')

    expect(push).toHaveBeenCalledWith({
      path: '/chat',
      query: { kb: ['kb-1'], skill: ['pdf'], plugin: ['fs-mcp'] },
    })
  })
})
