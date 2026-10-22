import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ModelsView from '../ModelsView.vue'

/**
 * `/models` 页的分组、折叠与批量开关（2026-10-22）。
 *
 * 为什么这些行为值得钉住：
 *  - **分组**决定用户看得见什么。聚合型 provider（openrouter）一个 provider 下几百个模型，
 *    前缀切错（比如把 `anthropic/claude-*` 也切进「其他」）就等于没有二级分组。
 *  - **折叠**必须真的不渲染（`v-if`），否则"折叠"只是视觉上藏起来，几百行的渲染开销照付。
 *  - **选择态**最容易出的错是"跨组全选把同一个 id 选两次"或"反选把整批清空"；
 *    而批量接口正是拿这串 id 去改数据，错了就是**批量改错数据**。
 *  - 批量提交必须一次请求（后端一条 UPDATE），而不是 N 次单条 PUT —— 后者中途失败会留下
 *    "一半开一半关"的中间态。
 */

const apiMocks = vi.hoisted(() => ({
  // 显式 `Promise<unknown>`：否则推断成 `never[]`，后面的 mockResolvedValue 会被 tsc 拒掉
  listModels: vi.fn((): Promise<unknown> => Promise.resolve({})),
  listAdminModels: vi.fn((): Promise<unknown> => Promise.resolve([])),
  listProviderOptions: vi.fn((): Promise<unknown> => Promise.resolve([])),
  updateAdminModel: vi.fn((): Promise<unknown> => Promise.resolve({})),
  upsertAdminModel: vi.fn((): Promise<unknown> => Promise.resolve({})),
  deleteAdminModel: vi.fn((): Promise<unknown> => Promise.resolve({})),
  batchUpdateAdminModels: vi.fn((): Promise<unknown> => Promise.resolve(0)),
}))

vi.mock('../../api', () => ({ listModels: apiMocks.listModels }))
vi.mock('../../api/modelAdmin', () => ({
  listAdminModels: apiMocks.listAdminModels,
  listProviderOptions: apiMocks.listProviderOptions,
  updateAdminModel: apiMocks.updateAdminModel,
  upsertAdminModel: apiMocks.upsertAdminModel,
  deleteAdminModel: apiMocks.deleteAdminModel,
  batchUpdateAdminModels: apiMocks.batchUpdateAdminModels,
}))
vi.mock('../../composables/useOnboardingTour', () => ({ TOUR_ANCHORS: { modelsAdd: 'models-add' } }))

/** 一个 openrouter（两种前缀 + 一个独苗）与一个单模型 provider，覆盖两级分组的全部分支 */
const MODELS = [
  { id: 'm-sonnet', provider: 'openrouter', name: 'anthropic/claude-sonnet-4', enabled: true, context_window: 200000 },
  { id: 'm-opus', provider: 'openrouter', name: 'anthropic/claude-opus-4', enabled: false, context_window: 200000 },
  { id: 'm-gpt', provider: 'openrouter', name: 'openai/gpt-4o', enabled: true, context_window: 128000 },
  { id: 'm-deepseek', provider: 'deepseek', name: 'deepseek-chat', enabled: true, context_window: 128000 },
]

async function mountView(models: unknown[] = MODELS) {
  apiMocks.listAdminModels.mockResolvedValue(models)
  const wrapper = mount(ModelsView)
  await flushPromises()
  return wrapper
}

/** 按可见文字找一个按钮（antd 的 <button> 会带一堆空白与图标，故用 includes） */
function buttonByText(wrapper: ReturnType<typeof mount>, text: string, nth = 0) {
  const hits = wrapper.findAll('button').filter(b => b.text().includes(text))
  if (hits.length <= nth) {
    throw new Error(`找不到第 ${nth + 1} 个「${text}」按钮，现有：${wrapper.findAll('button').map(b => b.text()).join(' | ')}`)
  }
  return hits[nth]
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  apiMocks.listAdminModels.mockResolvedValue([])
  apiMocks.listProviderOptions.mockResolvedValue([])
})

describe('ModelsView 分组', () => {
  it('按 provider 分组，组内再按名称前缀分子组', async () => {
    const wrapper = await mountView()
    const groupHeads = wrapper.findAll('.mv-group-head')
    expect(groupHeads.length).toBe(2)
    expect(groupHeads[0].text()).toContain('openrouter')
    expect(groupHeads[0].text()).toContain('共 3 个 · 已启用 2')
    expect(groupHeads[1].text()).toContain('deepseek')

    // openrouter 下：anthropic（2 个）成组，孤零零的 openai/gpt-4o 归「其他」
    const subHeads = wrapper.findAll('.mv-subgroup-head')
    expect(subHeads.length).toBe(2)
    expect(subHeads[0].text()).toContain('anthropic')
    expect(subHeads[1].text()).toContain('其他')
  })

  it('只有一个子组时不渲染子组标题（那一层没有信息量）', async () => {
    const wrapper = await mountView([MODELS[3]])
    expect(wrapper.findAll('.mv-group-head').length).toBe(1)
    expect(wrapper.findAll('.mv-subgroup-head').length).toBe(0)
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(1)
  })
})

describe('ModelsView 折叠', () => {
  it('折叠 provider 组后其模型行不再渲染', async () => {
    const wrapper = await mountView()
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(4)

    await buttonByText(wrapper, 'openrouter').trigger('click')
    // openrouter 的 3 行整体消失（v-if，而不是单纯藏起来）
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(1)
    expect(wrapper.text()).toContain('deepseek-chat')
  })

  it('折叠状态写进 localStorage，重新挂载后仍生效', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, 'openrouter').trigger('click')
    expect(localStorage.getItem('modelsView:collapsed:v1')).toContain('openrouter')

    const again = await mountView()
    expect(again.findAll('.mv-group .mv-row').length).toBe(1)
  })

  it('搜索时忽略折叠状态：命中却看不见会被当成"没搜到"', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, 'openrouter').trigger('click')
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(1)

    await wrapper.find('.mv-search input').setValue('claude')
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(2)
  })

  it('「全部折叠 / 全部展开」作用于所有组与子组', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, '全部折叠').trigger('click')
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(0)
    await buttonByText(wrapper, '全部展开').trigger('click')
    expect(wrapper.findAll('.mv-group .mv-row').length).toBe(4)
  })
})

describe('ModelsView 选择与批量开关', () => {
  it('组内「全选」只选本组，「取消」清空本组', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, '全选', 0).trigger('click')
    // 第一个「全选」属于 openrouter 组 ⇒ 3 个，deepseek 的不受影响
    expect(wrapper.text()).toContain('已选 3 项')

    await buttonByText(wrapper, '取消', 0).trigger('click')
    expect(wrapper.text()).toContain('已选 0 项')
  })

  it('「反选」把本组已选的去掉、未选的加上', async () => {
    const wrapper = await mountView()
    await buttonByText(wrapper, '全选', 0).trigger('click')
    expect(wrapper.text()).toContain('已选 3 项')

    // 全选后再反选 ⇒ 本组一个都不剩（而不是"保持不变"）
    await buttonByText(wrapper, '反选', 0).trigger('click')
    expect(wrapper.text()).toContain('已选 0 项')

    await buttonByText(wrapper, '反选', 0).trigger('click')
    expect(wrapper.text()).toContain('已选 3 项')
  })

  it('选中后按「禁用选中」：一次批量请求，且本地行同步为禁用', async () => {
    const wrapper = await mountView()
    apiMocks.batchUpdateAdminModels.mockResolvedValue(2)

    await buttonByText(wrapper, '全选', 0).trigger('click') // openrouter 的 3 个
    await buttonByText(wrapper, '禁用选中').trigger('click')
    await flushPromises()

    expect(apiMocks.batchUpdateAdminModels).toHaveBeenCalledTimes(1)
    // `vi.fn()` 未声明形参 ⇒ `mock.calls[0]` 推断成空 tuple，这里按真实契约断言
    const [ids, enabled] = apiMocks.batchUpdateAdminModels.mock.calls[0] as unknown as [string[], boolean]
    expect(enabled).toBe(false)
    expect([...ids].sort()).toEqual(['m-gpt', 'm-opus', 'm-sonnet'])

    // 提交成功后选择清空，避免用户以为"还选着"再点一次
    expect(wrapper.text()).toContain('已选 0 项')
    // 只动选中的行：openrouter 的三个关掉，没参与选择的 deepseek 那行必须原样开着
    const groups = wrapper.findAll('.mv-group')
    const openrouterSwitches = groups[0].findAll('.ant-switch')
    expect(openrouterSwitches.length).toBe(3)
    expect(openrouterSwitches.every(s => !s.classes().includes('ant-switch-checked'))).toBe(true)
    expect(groups[1].find('.ant-switch').classes()).toContain('ant-switch-checked')
  })

  it('没有选中任何行时批量按钮不可点（不会发出空请求）', async () => {
    const wrapper = await mountView()
    const enableBtn = buttonByText(wrapper, '启用选中')
    expect(enableBtn.attributes('disabled')).toBeDefined()
  })

  it('批量请求失败时不改本地状态（否则界面会撒谎）', async () => {
    const wrapper = await mountView()
    apiMocks.batchUpdateAdminModels.mockRejectedValue(new Error('boom'))

    await buttonByText(wrapper, '全选', 0).trigger('click')
    await buttonByText(wrapper, '禁用选中').trigger('click')
    await flushPromises()

    // 选择保留（用户可以直接重试），行状态不变
    expect(wrapper.text()).toContain('已选 3 项')
  })
})
