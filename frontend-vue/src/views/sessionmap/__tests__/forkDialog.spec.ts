/**
 * 分叉对话框的三档语义。
 *
 * 这里钉的是「用户看到什么、点确认时发什么」，而不是后端行为 ——
 * 参数映射本身在 `useMapActions.spec.ts::buildForkRequest` 里单独测过，
 * 两边分开测才能定位到底哪一层错了。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ForkDialog from '../components/ForkDialog.vue'
import type { ForkPoint } from '../types'

function point(index: number, preview = `第 ${index} 条`): ForkPoint {
  return { index, role: index % 2 === 1 ? 'user' : 'assistant', preview, created_at: '2026-01-01T00:00:00Z' }
}

const POINTS = [point(2), point(4), point(6), point(8), point(10)]

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(ForkDialog, {
    props: { sourceTitle: '源会话', points: POINTS, total: 10, ...props },
  })
}

/** 确认按钮：`.is-primary` 是唯一的提交入口。 */
function confirmBtn(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.find('.map-fork-btn.is-primary')
}

function preview(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.find('.map-fork-preview').text()
}

describe('ForkDialog 默认档', () => {
  it('默认选中全量档，并说明这一次会复制多少条', () => {
    const wrapper = mountDialog()

    expect(preview(wrapper)).toBe('将复制 10 条消息')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('确认时发出 full 档，落点为会话末尾', async () => {
    const wrapper = mountDialog()

    await confirmBtn(wrapper).trigger('click')

    expect(wrapper.emitted('confirm')?.[0]).toEqual([{ tier: 'full', fromIndex: 10 }])
  })
})

describe('ForkDialog 压缩档', () => {
  it('历史足够时预览"压成 1 条摘要"，且不谎报会保留原文', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[1].setValue(true)

    expect(preview(wrapper)).toBe('将压成 1 条摘要（共 10 条历史）')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('历史不足 3 条时禁用压缩档并说明原因（不静默降级成全量复制）', async () => {
    const wrapper = mountDialog({ total: 2, points: [point(1), point(2)] })
    await wrapper.findAll('input[type="radio"]')[1].setValue(true)

    expect(preview(wrapper)).toContain('历史不足 3 条')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()

    // 关键：禁用状态下点击不得发出 confirm —— 否则后端的"退回整段复制"
    // 会被用户当成"压缩成功"，点进新会话却发现是一堆原文。
    await confirmBtn(wrapper).trigger('click')
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('确认时发出 condensed 档（keep_tail=0 的语义在动作层落地）', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[1].setValue(true)
    await confirmBtn(wrapper).trigger('click')

    expect(wrapper.emitted('confirm')?.[0]).toEqual([{ tier: 'condensed', fromIndex: 10 }])
  })
})

describe('ForkDialog 自定义档', () => {
  it('展开落点下拉，列出每一条的可辨认标签', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[2].setValue(true)

    const options = wrapper.findAll('#map-fork-point option')
    expect(options).toHaveLength(POINTS.length)
    expect(options[0].text()).toBe('#2 · 第 2 条')
  })

  it('选中的落点会进 confirm 载荷（而不是仍用会话末尾）', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[2].setValue(true)
    await wrapper.find('#map-fork-point').setValue('4')
    await confirmBtn(wrapper).trigger('click')

    expect(wrapper.emitted('confirm')?.[0]).toEqual([
      { tier: 'custom', fromIndex: 4, mode: 'condensed' },
    ])
  })

  it('自定义档选"全量复制"时 mode 跟着变，预览也回到复制条数', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[2].setValue(true)
    await wrapper.find('#map-fork-point').setValue('4')
    await wrapper.find('#map-fork-custom-mode').setValue('full')

    expect(preview(wrapper)).toBe('将复制 4 条消息')

    await confirmBtn(wrapper).trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual([{ tier: 'custom', fromIndex: 4, mode: 'full' }])
  })

  it('落点太靠前时压缩档被禁用（提前拦下，就近显示原因）', async () => {
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[2].setValue(true)
    await wrapper.find('#map-fork-point').setValue('2')

    expect(preview(wrapper)).toContain('历史不足 3 条')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })
})

describe('ForkDialog 边界', () => {
  it('空会话时任何档位都不能提交，并说明原因', () => {
    const wrapper = mountDialog({ total: 0, points: [] })

    expect(preview(wrapper)).toBe('这个会话还没有消息，无法分叉')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('加载中不显示"空会话"，避免先闪一句错误提示', () => {
    const wrapper = mountDialog({ total: 0, points: [], loading: true })

    expect(preview(wrapper)).toBe('加载中…')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('Esc 关闭对话框，不误发 confirm', async () => {
    const wrapper = mountDialog()

    await wrapper.find('.map-fork').trigger('keydown', { key: 'Escape' })

    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('上次失败的原因照原样显示，不被本地化文案盖掉', () => {
    const wrapper = mountDialog({ error: 'cannot fork this conversation: boom' })

    expect(wrapper.find('.map-fork-error').text()).toContain('cannot fork this conversation')
  })

  it('总数变小时把越界的落点纠正回来，不留下指向不存在消息的选择', async () => {
    // 真实时序：对话框先按旧数据打开（10 条），刷新后总数缩到 3 条
    // （例如源会话被截断）。此时若沿用落点 10，请求会指向不存在的消息。
    const wrapper = mountDialog()
    await wrapper.findAll('input[type="radio"]')[2].setValue(true)
    await wrapper.find('#map-fork-point').setValue('10')

    await wrapper.setProps({ total: 3, points: [point(1), point(2), point(3)] })
    await wrapper.vm.$nextTick()

    expect((wrapper.find('#map-fork-point').element as HTMLSelectElement).value).toBe('3')
  })
})
