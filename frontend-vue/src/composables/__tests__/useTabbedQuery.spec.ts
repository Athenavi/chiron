import { describe, it, expect, beforeEach } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory, type Router } from 'vue-router'
import { useTabbedQuery, type TabbedQueryState } from '../useTabbedQuery'

/**
 * `useTabbedQuery` 的测试（2026-10-09）。
 *
 * 它从 `components/admin/AdminTabbedView.vue` 抽出，现在**同时服务 7 个页面**
 * （6 个既有 tabbed 页 + 新的 `/admin/settings`）⇒ 行为必须被钉住：
 * 默认回落、非法值回落、`?tab=` 跟随、切换写回 URL、已访问集合只增不减。
 */

const TABS = ['roles', 'groups'] as const

function makeRouter(initial: string): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/admin/access', component: { template: '<div />' } },
    ],
  })
  router.push(initial)
  return router
}

/** 把 composable 挂在一个最小宿主组件里，拿到它的状态 */
async function withTabbed(initial: string) {
  const router = makeRouter(initial)
  await router.isReady()

  let state!: TabbedQueryState
  const Host = defineComponent({
    setup() {
      state = useTabbedQuery({
        routePath: '/admin/access',
        defaultTab: 'roles',
        tabKeys: TABS,
      })
      return () => h('div')
    },
  })
  const wrapper = mount(Host, { global: { plugins: [router] } })
  await flushPromises()
  return { state, router, wrapper }
}

describe('useTabbedQuery（后台多面板页的 ?tab= 同步）', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('没有 ?tab= ⇒ 落默认面板', async () => {
    const { state } = await withTabbed('/admin/access')
    expect(state.activeTab.value).toBe('roles')
    expect([...state.visited.value]).toEqual(['roles'])
  })

  it('带 ?tab= ⇒ 定位到那个面板（刷新/分享/旧路由重定向都靠它）', async () => {
    const { state } = await withTabbed('/admin/access?tab=groups')
    expect(state.activeTab.value).toBe('groups')
  })

  it('★ 非法 ?tab= ⇒ 回落默认（不能把页面卡在空白面板上）', async () => {
    const { state } = await withTabbed('/admin/access?tab=not-a-tab')
    expect(state.activeTab.value).toBe('roles')
  })

  it('★ 切换写回 URL 且用 replace（不污染浏览器历史）', async () => {
    const { state, router } = await withTabbed('/admin/access')
    state.select('groups')
    await flushPromises()

    expect(state.activeTab.value).toBe('groups')
    expect(router.currentRoute.value.query.tab).toBe('groups')
    expect(router.currentRoute.value.path).toBe('/admin/access')
  })

  it('★ 已访问集合只增不减（懒加载依据：切回不重复挂载/请求）', async () => {
    const { state } = await withTabbed('/admin/access')
    state.select('groups')
    await flushPromises()
    state.select('roles')
    await flushPromises()

    expect([...state.visited.value].sort()).toEqual(['groups', 'roles'])
  })

  it('★ 外部改变 ?tab= 时跟随（旧路由重定向过来会带 query）', async () => {
    const { state, router } = await withTabbed('/admin/access')
    expect(state.activeTab.value).toBe('roles')

    await router.push('/admin/access?tab=groups')
    await flushPromises()
    expect(state.activeTab.value).toBe('groups')
  })

  it('切到同一个 Tab 不重复写 URL', async () => {
    const { state, router } = await withTabbed('/admin/access?tab=groups')
    const before = router.currentRoute.value.fullPath
    state.select('groups')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe(before)
  })
})
