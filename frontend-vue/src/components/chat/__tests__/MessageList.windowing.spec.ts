import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MessageList from '../MessageList.vue'
import type { ChatItem } from '../chat-types'

function makeItems(n: number): ChatItem[] {
  return Array.from({ length: n }, (_, i) => ({
    kind: 'text', role: 'user', content: `m${i}`, id: `m${i}`,
  }) as ChatItem)
}

/** 每行固定 100px、视口 400px —— 让窗口边界可手算。 */
const base = {
  measureRow: () => 100,
  viewportHeight: 400,
  windowingMinRows: 10,
  residentTailRows: 0,
}

/** 窗口化是"测量→发布→重算"的收敛过程，需多轮 tick 让其稳定。 */
async function settle(times = 8) {
  for (let i = 0; i < times; i++) await nextTick()
}

async function mountList(props: Record<string, unknown>) {
  const wrapper = mount(MessageList, { props: { loading: false, ...props } as any })
  await settle()
  return wrapper
}

function spacerTotalOf(wrapper: ReturnType<typeof mount>): number {
  return wrapper.findAll('.window-spacer').reduce((sum, s) => {
    const m = /height:\s*([\d.]+)px/.exec(s.attributes('style') || '')
    return sum + (m ? Number(m[1]) : 0)
  }, 0)
}

describe('MessageList 窗口化', () => {
  /**
   * §4.3 后半（2026-10-09）：更新驱动的「测量 + 窗口重算」**按帧合并**。
   *
   * 此前 `onUpdated` 每次都全量跑 `measureMountedRows()` —— 而流式期**每个 delta 都会
   * 触发一次 updated** ⇒ 一秒内几十次全量测量（`querySelectorAll` + 逐行 `offsetHeight`）。
   * 现在用现成的 `throttleRaf` 合并：**同一帧内多次更新只测一遍**。
   *
   * 观测量选 `querySelectorAll('[data-item-key]')` —— 那正是 `measureMountedRows()` 的第一件事，
   * 且这个选择器只有它用（Vue 内部不会带它）⇒ 计数即"测量跑了几遍"。
   *
   * ⚠ **不能断言"4 次更新 ⇒ 恰好 1 次测量"**：测量本身会写响应式状态（`measuredSizes`），
   * 于是又触发一轮 `onUpdated` —— 这是个**收敛过程**（直到 `ledger.publish()` 返回空为止），
   * 实测 4 次更新在几帧内总共跑了 7 遍。合并要钉的是「**同一帧内不重复**」：
   * 更新后**未让 rAF 落地前一次都不该测**（否则说明没合并），落地后至少测一次。
   */
  it('★ 更新驱动的测量按帧合并：同一帧内多次更新不重复测量', async () => {
    const spy = vi.spyOn(Element.prototype, 'querySelectorAll')
    const measured = () => spy.mock.calls.filter(c => c[0] === '[data-item-key]').length

    const wrapper = await mountList({ items: makeItems(300), ...base })
    await settle()
    const before = measured()

    // 同一帧内连续更新 4 次：`await nextTick()` 只推进微任务，**不推进 rAF**
    for (let i = 0; i < 4; i++) {
      await wrapper.setProps({ items: makeItems(300) })
    }
    // 合并成立的话，此刻一次都还没测（4 次更新只排了**一个**帧任务）
    expect(measured() - before).toBe(0)

    await new Promise(r => setTimeout(r, 40))   // 让 rAF 落地
    await settle()
    expect(measured() - before).toBeGreaterThanOrEqual(1)
    spy.mockRestore()
  })

  it('大量行只挂载窗口内的行（不渲染整表）', async () => {
    const wrapper = await mountList({ items: makeItems(300), ...base })
    const rows = wrapper.findAll('.chat-row').length
    expect(rows).toBeGreaterThan(0)
    expect(rows).toBeLessThan(300)
    // 视口 400 + 预渲染 800 → 至多约 12 行
    expect(rows).toBeLessThanOrEqual(20)
  })

  it('常驻尾部：上滚到顶部时末尾行仍然挂载，中间行不牵连', async () => {
    const wrapper = await mountList({ items: makeItems(300), ...base, residentTailRows: 20 })
    const rows = wrapper.findAll('.chat-row').length
    expect(rows).toBeGreaterThan(12)          // 主窗口之外确实多了尾部
    expect(rows).toBeLessThanOrEqual(40)      // 但不是整表
    expect(wrapper.text()).toContain('m299')  // 最后一行在挂载集合中
  })

  it('小会话不启用窗口化：全量渲染且无占位', async () => {
    const wrapper = await mountList({ items: makeItems(50), ...base, windowingMinRows: 150 })
    expect(wrapper.findAll('.chat-row').length).toBe(50)
    expect(wrapper.findAll('.window-spacer').length).toBe(0)
  })

  it('高度守恒：挂载行高 + 占位高 = 全部行高（滚动条不跳）', async () => {
    const wrapper = await mountList({ items: makeItems(300), ...base })
    const rows = wrapper.findAll('.chat-row').length
    const spacer = spacerTotalOf(wrapper)
    expect(spacer).toBeGreaterThan(0)
    // 未挂载行只有估计值（text=96），挂载行是实测值（100）：
    // 总高应落在 [全估计, 全实测] 区间内 —— 这正是"占位补齐未挂载行几何高度"的含义。
    // 不能用等式：未挂载行的实测值要等它们被滚动到才会产生。
    const total = rows * 100 + spacer
    expect(total).toBeGreaterThanOrEqual(300 * 96)
    expect(total).toBeLessThanOrEqual(300 * 100)
  })

  it('常驻尾部的行真实挂载，不被占位替代', async () => {
    const wrapper = await mountList({ items: makeItems(300), ...base, residentTailRows: 20 })
    const keys = wrapper.findAll('.chat-row').map(r => r.attributes('data-item-key') ?? '')
    expect(keys.length).toBeGreaterThan(0)
    for (let i = 280; i < 300; i++) {
      expect(keys.some(k => k.endsWith(`text:m${i}`))).toBe(true)
    }
  })
})
