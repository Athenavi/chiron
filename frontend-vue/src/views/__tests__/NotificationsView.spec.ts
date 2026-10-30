import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { Segmented, Switch, message } from 'ant-design-vue'

/**
 * `NotificationsView` 的页面级单测（P2-3 / T3 补网）。
 *
 * 纯函数（`notificationRender`）与偏好持久化已各有单测，这里只钉**接线**：
 * 页面拿到数据后有没有渲染出来、点了以后有没有真的调接口、失败时用户能不能
 * 看到可重试的提示。接线断掉不会崩，只会「静默不显示 / 静默不生效」，
 * 而这三个症状恰好是通知中心最容易退化且测试最难发现的地方。
 */

// antd 组件在 jsdom 下需要 matchMedia（无它则 Segmented/Switch 直接抛错）。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const apiMocks = vi.hoisted(() => ({
  // 显式 Promise<T>：否则 mockResolvedValue 会被推断成 never，tsc 会拒。
  listNotifications: vi.fn((): Promise<unknown> => Promise.resolve({ items: [], unread_count: 0 })),
  markNotificationsRead: vi.fn((): Promise<unknown> => Promise.resolve(1)),
  markAllNotificationsRead: vi.fn((): Promise<unknown> => Promise.resolve(0)),
  getNotificationPrefs: vi.fn((): Promise<unknown> => Promise.resolve({ email_enabled: {}, kinds: [] })),
  updateNotificationPref: vi.fn((): Promise<unknown> => Promise.resolve({ email_enabled: {}, kinds: [] })),
}))

vi.mock('../../api/notifications', () => apiMocks)

import NotificationsView from '../NotificationsView.vue'

type Note = Record<string, unknown>

function note(partial: Note): Note {
  return {
    id: 'n-1',
    kind: 'payment_succeeded',
    title: '服务端标题',
    body: '服务端正文',
    link: '',
    payload: { credits: 100 },
    read: false,
    read_at: null,
    created_at: '2026-10-01T08:00:00Z',
    ...partial,
  }
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/billing', component: { template: '<div />' } },
      { path: '/notifications', component: { template: '<div />' } },
    ],
  })
}

/** 挂载前把路由导航好：页面的 hash 直达是在 onMounted 里读 `route.hash` 的。 */
async function mountView(path = '/') {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(NotificationsView, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

let scrollSpy: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.clearAllMocks()
  scrollSpy = vi.fn()
  // jsdom 不实现 scrollIntoView；页面用它做「邮件设置」跳转与 hash 直达。
  Element.prototype.scrollIntoView = scrollSpy as unknown as typeof Element.prototype.scrollIntoView
  apiMocks.listNotifications.mockResolvedValue({ items: [], unread_count: 0 })
  apiMocks.markNotificationsRead.mockResolvedValue(1)
  apiMocks.markAllNotificationsRead.mockResolvedValue(0)
  apiMocks.getNotificationPrefs.mockResolvedValue({ email_enabled: {}, kinds: [] })
  apiMocks.updateNotificationPref.mockResolvedValue({ email_enabled: {}, kinds: [] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('NotificationsView', () => {
  it('列表按当前语言渲染，并显示未读角标', async () => {
    apiMocks.listNotifications.mockResolvedValue({
      items: [note({}), note({ id: 'n-2', read: true })],
      unread_count: 1,
    })
    const { wrapper } = await mountView()

    const rows = wrapper.findAll('.notification-item')
    expect(rows).toHaveLength(2)
    // 走真实 i18n（测试环境固定 zh-CN），证明模板真的渲染出来而不是键名。
    expect(rows[0].find('.notification-title').text()).toBe('支付成功')
    expect(rows[0].find('.notification-body').text()).toContain('100')
    // 未读角标
    expect(wrapper.text()).toContain('1')
    wrapper.unmount()
  })

  it('列表只展示服务端给的 kind 未知时原样显示 kind', async () => {
    apiMocks.listNotifications.mockResolvedValue({
      items: [note({ kind: 'future_event', title: '服务端标题', body: '服务端正文' })],
      unread_count: 1,
    })
    const { wrapper } = await mountView()

    const row = wrapper.find('.notification-item')
    // 未知类型不隐藏，否则用户会以为没有这条通知。
    expect(row.text()).toContain('future_event')
    expect(row.find('.notification-title').text()).toBe('服务端标题')
    wrapper.unmount()
  })

  it('切到「未读」会带上 unread_only 重新拉取（不做前端过滤）', async () => {
    const { wrapper } = await mountView()
    expect(apiMocks.listNotifications).toHaveBeenLastCalledWith({ limit: 100, unreadOnly: false })

    wrapper.findComponent(Segmented).vm.$emit('update:value', 'unread')
    await flushPromises()

    expect(apiMocks.listNotifications).toHaveBeenLastCalledWith({ limit: 100, unreadOnly: true })
    wrapper.unmount()
  })

  it('点未读通知：标已读、未读数减一、并按 link 跳转', async () => {
    apiMocks.listNotifications.mockResolvedValue({
      items: [note({ link: '/billing' })],
      unread_count: 1,
    })
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    await wrapper.find('.notification-item').trigger('click')
    await flushPromises()

    expect(apiMocks.markNotificationsRead).toHaveBeenCalledWith(['n-1'])
    expect(push).toHaveBeenCalledWith('/billing')
    wrapper.unmount()
  })

  it('标记失败不阻止跳转：用户点的是「看详情」', async () => {
    apiMocks.listNotifications.mockResolvedValue({
      items: [note({ link: '/billing' })],
      unread_count: 1,
    })
    apiMocks.markNotificationsRead.mockRejectedValue(new Error('boom'))
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => undefined as never)
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    await wrapper.find('.notification-item').trigger('click')
    await flushPromises()

    expect(errorSpy).toHaveBeenCalled()
    expect(push).toHaveBeenCalledWith('/billing')
    wrapper.unmount()
  })

  it('全部标读：没有未读时只提示，不刷新列表', async () => {
    apiMocks.listNotifications.mockResolvedValue({ items: [note({ read: true })], unread_count: 1 })
    apiMocks.markAllNotificationsRead.mockResolvedValue(0)
    const infoSpy = vi.spyOn(message, 'info').mockImplementation(() => undefined as never)
    const { wrapper } = await mountView()
    const callsBefore = apiMocks.listNotifications.mock.calls.length

    await wrapper.find('[data-test="notifications-mark-all"]').trigger('click')
    await flushPromises()

    expect(infoSpy).toHaveBeenCalled()
    expect(apiMocks.listNotifications.mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  it('加载失败显示可重试的错误提示', async () => {
    apiMocks.listNotifications.mockRejectedValue(new Error('network down'))
    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain('network down')
    // 有重试按钮 ⇒ 用户不必刷新整页
    expect(wrapper.find('.notifications-alert').exists()).toBe(true)
    wrapper.unmount()
  })

  it('邮件开关乐观更新，保存失败时回滚', async () => {
    apiMocks.getNotificationPrefs.mockResolvedValue({
      email_enabled: { payment_succeeded: true },
      kinds: ['payment_succeeded'],
    })
    let rejectPref: (e: Error) => void = () => {}
    apiMocks.updateNotificationPref.mockImplementation(
      () => new Promise((_resolve, reject) => { rejectPref = reject }),
    )
    vi.spyOn(message, 'success').mockImplementation(() => undefined as never)
    vi.spyOn(message, 'error').mockImplementation(() => undefined as never)

    const { wrapper } = await mountView()
    const sw = wrapper.findComponent(Switch)
    expect(sw.props('checked')).toBe(true)

    sw.vm.$emit('change', false)
    await flushPromises()
    // 先乐观关掉：否则用户会以为没点上而再点一次
    expect(apiMocks.updateNotificationPref).toHaveBeenCalledWith('payment_succeeded', false)
    expect(wrapper.findComponent(Switch).props('checked')).toBe(false)

    rejectPref(new Error('nope'))
    await flushPromises()
    // 失败必须回滚：不能让界面显示一个没保存上的值
    expect(wrapper.findComponent(Switch).props('checked')).toBe(true)
    wrapper.unmount()
  })

  it('「邮件设置」按钮滚到偏好区，不新增历史记录', async () => {
    apiMocks.getNotificationPrefs.mockResolvedValue({
      email_enabled: { payment_succeeded: true },
      kinds: ['payment_succeeded'],
    })
    const { wrapper, router } = await mountView()
    const push = vi.spyOn(router, 'push')

    await wrapper.find('[data-test="notifications-jump-prefs"]').trigger('click')
    await flushPromises()

    expect(scrollSpy).toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('外部链接 /notifications#notification-prefs 直达偏好区', async () => {
    await mountView('/notifications#notification-prefs')
    expect(scrollSpy).toHaveBeenCalled()
  })
})
