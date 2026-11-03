import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import TodoPanel from '../TodoPanel.vue'
import { projectTodoBoard } from '../todoProjection'

/**
 * 面板只做展示：这里钉住的是"用户在界面上能看见什么"，
 * 而不是数据怎么来（那是 todoProjection.spec.ts 的事）。
 */
const board = (items: unknown[]) => projectTodoBoard(items)

function mountPanel(items: unknown[], collapsed = false) {
  return mount(TodoPanel, {
    props: { board: board(items), collapsed },
  })
}

describe('TodoPanel（可折叠任务清单）', () => {
  it('空板不渲染任何东西，不占位', () => {
    const wrapper = mountPanel([])
    expect(wrapper.find('.todo-panel').exists()).toBe(false)
  })

  it('按编号渲染每一项，状态体现为符号与 data-status', () => {
    const wrapper = mountPanel([
      { id: 't1', content: '读代码', status: 'completed' },
      { id: 't2', content: '改一处', status: 'in_progress' },
      { id: 't3', content: '写测试', status: 'pending' },
    ])

    const rows = wrapper.findAll('.todo-panel-item')
    expect(rows).toHaveLength(3)
    expect(rows.map((r) => r.find('.todo-panel-num').text())).toEqual(['#1', '#2', '#3'])
    expect(rows[0].attributes('data-status')).toBe('completed')
    expect(rows[1].attributes('data-status')).toBe('in_progress')
    expect(rows[0].find('.todo-panel-mark').text()).toBe('☑')
    expect(rows[1].find('.todo-panel-mark').text()).toBe('◐')
    expect(rows[2].find('.todo-panel-mark').text()).toBe('☐')
  })

  it('标题处给出完成数与进度条宽度', () => {
    const wrapper = mountPanel([
      { id: 't1', content: 'a', status: 'completed' },
      { id: 't2', content: 'b', status: 'pending' },
    ])
    expect(wrapper.find('.todo-panel-count').text()).toBe('1/2')
    expect(wrapper.find('.todo-panel-fill').attributes('style')).toContain('width: 50%')
  })

  it('折叠后隐藏条目，但仍显示进度与"正在做什么"', () => {
    const wrapper = mountPanel(
      [
        { id: 't1', content: '读代码', status: 'completed' },
        { id: 't2', content: '正在改的东西', status: 'in_progress' },
      ],
      true,
    )
    expect(wrapper.find('.todo-panel-body').exists()).toBe(false)
    // 折叠不等于失明：进度和当前项必须还看得见
    expect(wrapper.find('.todo-panel-count').text()).toBe('1/2')
    expect(wrapper.find('.todo-panel-active').text()).toBe('正在改的东西')
    expect(wrapper.find('.todo-panel-head').attributes('aria-expanded')).toBe('false')
  })

  it('点击标题发出 toggle 事件（状态由外部持有，组件无内部状态）', async () => {
    const wrapper = mountPanel([{ id: 't1', content: 'a' }])
    await wrapper.find('.todo-panel-head').trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
  })

  it('被依赖阻塞的项显示在等哪个编号', () => {
    const wrapper = mountPanel([
      { id: 't1', content: 'a', status: 'pending' },
      { id: 't2', content: 'b', status: 'pending', depends_on: ['t1'] },
    ])
    const rows = wrapper.findAll('.todo-panel-item')
    expect(rows[1].attributes('data-blocked')).toBe('1')
    expect(rows[1].find('.todo-panel-blocked').text()).toContain('t1')
    // 没被阻塞的项不该有阻塞标记
    expect(rows[0].find('.todo-panel-blocked').exists()).toBe(false)
  })

  it('只有一个认领者时不显示归属（避免每行都挂 @main 的噪音）', () => {
    const wrapper = mountPanel([
      { id: 't1', content: 'a', owner: 'main' },
      { id: 't2', content: 'b', owner: 'main' },
    ])
    expect(wrapper.find('.todo-panel-owner').exists()).toBe(false)
  })

  it('多个认领者时逐项标出归属（共享看板）', () => {
    const wrapper = mountPanel([
      { id: 't1', content: 'a', owner: 'main' },
      { id: 't2', content: 'b', owner: 'run-42' },
    ])
    const owners = wrapper.findAll('.todo-panel-owner')
    expect(owners).toHaveLength(2)
    expect(owners[1].text()).toBe('@run-42')
  })

  it('多个 agent 同时 in_progress 都能渲染（不假设全局只有一个）', () => {
    const wrapper = mountPanel([
      { id: 't1', content: 'a', status: 'in_progress', owner: 'main' },
      { id: 't2', content: 'b', status: 'in_progress', owner: 'run-42' },
    ])
    const rows = wrapper.findAll('.todo-panel-item')
    expect(rows.map((r) => r.attributes('data-status'))).toEqual([
      'in_progress',
      'in_progress',
    ])
  })
})
