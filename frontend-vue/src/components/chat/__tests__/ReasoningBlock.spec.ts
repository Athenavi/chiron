import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ReasoningBlock from '../ReasoningBlock.vue'

/**
 * `ReasoningBlock` 的测试（2026-10-09）。
 *
 * 承第 139–142 轮的组件人口（27 个无 spec），本轮挑**思考块**：它承载 §12.1 那条
 * `thinking` 落库链路的**渲染端** ✓。
 *
 * **三条不变量**：
 * ① **自动折叠策略 + 用户意图优先**（`:33-48`）：流式开始 ⇒ 展开（让过程可见）、
 *    流式结束 ⇒ 自动折叠（不占版面），**但用户手动切换过就不再自动改变** ✓ ——
 *    原注释把这条与主列表滚动的 `following` 归为同一哲学：
 *    **「自动行为只在用户没表达过意图时生效」** ✓；
 * ② **折叠摘要的语义**（`:17-26`）：**流式中取最后一行**（跟着最新思考走）、
 *    **完成后取第一行**（给出结论/开头）✓ —— 取反了就会出现"思考时摘要停在开头不动" ✓；
 * ③ 状态与 a11y：`data-state` / `aria-expanded` 如实反映 ✓。
 */

const mountBlock = (content: string, streaming?: boolean) =>
  mount(ReasoningBlock, { props: { content, streaming } })

describe('ReasoningBlock', () => {
  it('★ 流式开始自动展开（让用户看得见思考过程）', () => {
    const wrapper = mountBlock('第一行\n第二行', true)
    expect(wrapper.find('.reasoning-main').attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.think-body-wrap').exists()).toBe(true)
    expect(wrapper.attributes('data-state')).toBe('running')
  })

  it('★ 流式结束自动折叠（不占版面）', async () => {
    const wrapper = mountBlock('第一行\n第二行', true)
    await wrapper.setProps({ streaming: false })

    expect(wrapper.find('.reasoning-main').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.think-body-wrap').exists()).toBe(false)
    expect(wrapper.attributes('data-state')).toBe('ok')
  })

  it('★ 用户手动切换过之后，自动策略不再覆盖他的选择', async () => {
    const wrapper = mountBlock('第一行\n第二行', true)
    // 用户在看思考的过程中手动收起
    await wrapper.find('.reasoning-main').trigger('click')
    expect(wrapper.find('.reasoning-main').attributes('aria-expanded')).toBe('false')

    // 流式结束 —— 自动策略**不应**再改（它本来就是收起，这里换个方向更严格：手动展开）
    const w2 = mountBlock('第一行\n第二行', true)
    await w2.find('.reasoning-main').trigger('click')   // 手动收起
    await w2.find('.reasoning-main').trigger('click')   // 手动再展开
    await w2.setProps({ streaming: false })
    expect(w2.find('.reasoning-main').attributes('aria-expanded')).toBe('true')
  })

  it('★ 摘要语义：流式取最后一行，完成取第一行', async () => {
    const wrapper = mountBlock('开头结论\n中间\n最新思考', true)
    expect(wrapper.find('.think-summary').text()).toBe('最新思考')

    await wrapper.setProps({ streaming: false })
    expect(wrapper.find('.think-summary').text()).toBe('开头结论')
  })

  it('单行内容两种状态都取那一行（不会取空）', async () => {
    const wrapper = mountBlock('只有一行', true)
    expect(wrapper.find('.think-summary').text()).toBe('只有一行')
    await wrapper.setProps({ streaming: false })
    expect(wrapper.find('.think-summary').text()).toBe('只有一行')
  })
})
