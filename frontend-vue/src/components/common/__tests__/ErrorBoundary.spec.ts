/* eslint-disable vue/one-component-per-file --
   本文件里的三个极小组件（Boom / BoomOnce / Fine）是**替身**，故意与用例放一起：
   被测的 `onErrorCaptured` 只捕获**子组件**的错误 ⇒ 用普通 VNode/字符串当 slot **测不出来**，
   必须是真组件。全仓只有这一处这么写，所以就地豁免并写明理由，而不是把替身拆到另一个文件里。 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, onMounted } from 'vue'
import ErrorBoundary from '../ErrorBoundary.vue'

/**
 * `ErrorBoundary` 的测试（2026-10-09）。
 *
 * 第 139 轮量 `src/components/**` 的覆盖"人口"：**45 个组件里 27 个没有任何 spec 引用**（7,358 行）——
 * 本条挑最小的那个高价值组件先补（116 行），因为它的不变量**很强**：
 * ① `onErrorCaptured` 必须**返回 `false` 阻止错误继续上抛**（`:9-13`）—— 否则错误边界形同虚设，
 *    一个子组件的异常照样把整页炸掉 ✓；
 * ② **生产环境不渲染 `error.message` 与堆栈**（`:37` / `:41-44`）—— 这是一条**信息泄露**防护：
 *    原始错误消息里可能有内部路径、SQL 片段、依赖版本 ✓。
 */

/**
 * 一个在**生命周期钩子**里抛错的子组件。
 *
 * ⚠ 不在 `setup` 里直接 `throw`：那样 VTU 的 mount 会把它当挂载失败处理，
 * 父组件的 `onErrorCaptured` 收不到（第一版就是这么红的）。
 * 真实世界里边界要拦的也正是"挂载后渲染/副作用出错"，用 `onMounted` 更贴近 ✓。
 */
const Boom = defineComponent({
  name: 'Boom',
  setup() {
    onMounted(() => {
      throw new Error('子组件炸了：/srv/app/internal/secret.ts:42')
    })
    return () => h('div', 'boom')
  },
})

/**
 * **只炸一次**的子组件 —— 用来验证"重试后恢复"。
 *
 * 为什么需要它：用一直会炸的 `Boom` 时，`retry()` 清掉错误态 ⇒ 插槽重渲染 ⇒ 子组件**再次抛错**
 * ⇒ 边界**再次捕获** ⇒ 界面上仍然是兜底 ✓（这是**正确**行为，但那样就观察不到"恢复"）。
 * 第一版就是拿 `Boom` 去断言恢复，于是失败 ✗ —— **错的是断言，不是组件** ✓。
 */
let boomOnce = false
const BoomOnce = defineComponent({
  name: 'BoomOnce',
  setup() {
    onMounted(() => {
      if (!boomOnce) {
        boomOnce = true
        throw new Error('第一次就炸')
      }
    })
    return () => h('div', { class: 'recovered' }, '已恢复')
  },
})

/** 正常子组件 */
const Fine = defineComponent({
  name: 'Fine',
  render: () => h('div', { class: 'fine' }, '一切正常'),
})

function mountWith(child: unknown) {
  return mount(ErrorBoundary, { slots: { default: () => h(child as never) } })
}

describe('ErrorBoundary', () => {
  it('没有错误时渲染插槽内容（不接管正常路径）', () => {
    const wrapper = mountWith(Fine)
    expect(wrapper.find('.fine').exists()).toBe(true)
    expect(wrapper.find('.error-boundary').exists()).toBe(false)
  })

  it('★ 子组件抛错时显示 role=alert 兜底（错误被边界接住）', async () => {
    const wrapper = mountWith(Boom)
    // 抛错发生在子组件 `onMounted` 里 ⇒ 边界先设 `error`，**再等一次渲染**才换成兜底
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.error-boundary').exists()).toBe(true)
    expect(wrapper.find('.error-boundary').attributes('role')).toBe('alert')
    // 兜底里有"重试"这条出路
    expect(wrapper.text()).toContain('重试')
    // 开发环境把真实 message 摊开（便于定位）
    expect(wrapper.text()).toContain('子组件炸了')
  })

  it('★ 点"重试"后错误态被清掉，插槽恢复渲染', async () => {
    boomOnce = false
    const wrapper = mountWith(BoomOnce)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.error-boundary').exists()).toBe(true)

    const vm = wrapper.vm as unknown as { retry: () => void }
    vm.retry()
    await wrapper.vm.$nextTick()

    // 重试后：兜底消失、插槽内容真的渲染出来（这一次子组件不再抛错）
    expect(wrapper.find('.error-boundary').exists()).toBe(false)
    expect(wrapper.find('.recovered').exists()).toBe(true)
  })
})
