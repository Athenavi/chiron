import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ToolCallCard from '../ToolCallCard.vue'
import type { ToolCallItem } from '../chat-types'

/**
 * `ToolCallCard` 的测试（2026-10-09）。
 *
 * 承第 139–141 轮的组件人口（27 个无 spec），本轮挑**聊天里最核心的一行**：
 * 一屏工具调用就是由它渲染的 ✓。
 *
 * **四条不变量**：
 * ① **`data-family` 必须与投影层同一套分类**（`:20` 复用 `toolGroupKind`）——
 *    单条行与折叠组头用**同一个** `toolGroupKind`（`transcriptProjection.ts:160-165`），
 *    所以「这行是什么家族」两边永远一致 ✓；若这里另写一套，就会出现"单条是探索色、组头说它是修改" ✓；
 * ② **未知工具落到 `modify`**（`:165` 的默认）—— 那是最"显眼"的家族色（warning 色），
 *    对没登记的工具**宁可让人多看一眼** ✓；
 * ③ **参数展开**：`aria-expanded` 如实反映状态（a11y）✓，展开后把 JSON **美化**输出 ✓；
 * ④ **参数不是合法 JSON 时不能崩**（`:11-17` 的 try/catch）—— 工具参数可能是被截断的半成品 ✓。
 */

const call = (over: Partial<ToolCallItem> = {}): ToolCallItem => ({
  kind: 'tool_call',
  id: 'c1',
  name: 'read_file',
  arguments: '{"path":"a.txt"}',
  status: 'running',
  ...over,
})

function mountCard(item: ToolCallItem, depth?: number) {
  return mount(ToolCallCard, { props: depth === undefined ? { item } : { item, depth } })
}

describe('ToolCallCard', () => {
  it('★ 家族与投影层同一套：读类→explore，写类→modify', () => {
    expect(mountCard(call({ name: 'read_file' })).attributes('data-family')).toBe('explore')
    expect(mountCard(call({ name: 'grep_files' })).attributes('data-family')).toBe('explore')
    expect(mountCard(call({ name: 'write_file' })).attributes('data-family')).toBe('modify')
  })

  it('★ 未登记的工具落到 modify（显眼优先，宁可让人多看一眼）', () => {
    const wrapper = mountCard(call({ name: 'some_brand_new_tool' }))
    expect(wrapper.attributes('data-family')).toBe('modify')
    // 工具名照原样显示（不因为没登记就藏起来）
    expect(wrapper.text()).toContain('some_brand_new_tool')
  })

  it('★ 展开：aria-expanded 如实反映，且参数被美化输出', async () => {
    const wrapper = mountCard(call({ arguments: '{"b":2,"a":1}' }))
    const btn = wrapper.find('button.tool-main')

    expect(btn.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.tool-args').exists()).toBe(false)

    await btn.trigger('click')
    expect(btn.attributes('aria-expanded')).toBe('true')

    const args = wrapper.find('.tool-args pre')
    expect(args.exists()).toBe(true)
    // 美化过：有换行与缩进（不是原样一行）
    expect(args.text()).toContain('\n')
    expect(args.text()).toContain('"a"')
  })

  it('★ 参数不是合法 JSON 时不崩，原样展示（截断的半成品很常见）', async () => {
    const wrapper = mountCard(call({ arguments: '{"path": "a.tx' }))
    await wrapper.find('button.tool-main').trigger('click')

    expect(wrapper.find('.tool-args pre').text()).toContain('{"path": "a.tx')
  })

  it('状态透到 data-state（running 的流光靠它）', () => {
    expect(mountCard(call({ status: 'running' })).attributes('data-state')).toBe('running')
    expect(mountCard(call({ status: 'done' })).attributes('data-state')).toBe('done')
  })
})
