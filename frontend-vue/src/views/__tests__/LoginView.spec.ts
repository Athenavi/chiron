import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `LoginView` 的冒烟网 + 两条**接线**（2026-10-09）。
 *
 * 六视图补网第 5 个（487 → 589 → 702 → 854 → 本轮 964）。
 *
 * **测哪两条接线**：
 * ① **登录提交**：`handleLogin` 必须把**用户输入的邮箱/密码**交给 `authStore.login`
 *    （`LoginView.vue:349`），并按**角色**跳转（`:355`：`admin`/`owner` → `/admin`，其余 → `/chat`）——
 *    这是**安全相关**的：把普通用户送进管理后台、或把管理员留在对话页，都不是外观问题 ✓；
 * ② **短信倒计时**：`useSmsCountdown`（`:79`）没接上的话"获取验证码"按钮**一直可点** ⇒ 可被连续触发短信 ✓
 *    （同 `RegisterView` 那条，但登录页是**双通道**：短信 `:79` + 邮箱 `:203`）。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const authApi = vi.hoisted(() => ({
  // 显式 `Promise<unknown>`：否则推断成 `never[]`/具体形状，`mockResolvedValue` 会被 tsc 拒掉
  getCaptchaPublicConfig: vi.fn((): Promise<unknown> => Promise.resolve({ enabled: false })),
  // ⚠ 门是**两个字段**：`:324` 是 `smsEnabled = !!(st.enabled && st.login_enabled)`
  // （邮箱那条同理）。只给 `enabled` 的话短信/邮箱 tab **根本不渲染** —— 这与 RegisterView
  // 的 `es.enabled && es.register_verify` 是**同一个坑**，第二次踩 ⇒ 已记入台账。
  getSmsStatus: vi.fn((): Promise<unknown> => Promise.resolve({ enabled: true, login_enabled: true })),
  getEmailStatus: vi.fn((): Promise<unknown> => Promise.resolve({ enabled: true, login_enabled: true, login_verify: false })),
  sendSmsCode: vi.fn((): Promise<unknown> => Promise.resolve({ interval: 60 })),
  sendEmailCode: vi.fn((): Promise<unknown> => Promise.resolve({ interval: 60 })),
  smsLogin: vi.fn((): Promise<unknown> => Promise.resolve({})),
  emailLogin: vi.fn((): Promise<unknown> => Promise.resolve({})),
  isValidPhone: vi.fn(() => true),
  isValidEmail: vi.fn(() => true),
}))

/** 稳定的 store 替身（每次调用返回同一个对象，否则没法断言 login 被怎么调） */
const authStoreMock = vi.hoisted(() => ({
  // 返回类型必须与真实 store 一致：`login()` 一直返回响应对象（含 token/user），
  // 2026 起还附带 `mfaRequired`。此前这里写成 `Promise<void>` 并 resolve undefined，
  // 与真实契约不符 —— 只是当时 LoginView 不读返回值所以没暴露。
  // 替身与真实实现不一致，等于测试在一个不存在的行为上通过。
  login: vi.fn((): Promise<{ mfaRequired: boolean }> => Promise.resolve({ mfaRequired: false })),
  completeMfaLogin: vi.fn((): Promise<{ role?: string }> => Promise.resolve({})),
  user: null as null | { role?: string },
  loading: false,
  token: '',
}))

vi.mock('../../api/auth', () => authApi)
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authStoreMock }))

import LoginView from '../LoginView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
    { path: '/admin', component: { template: '<div />' } },
  ],
})

function mountView() {
  return mount(LoginView, { global: { plugins: [router] } })
}

describe('LoginView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 与真实契约一致：login() 返回响应对象，不是 undefined。
    // 写成 undefined 会让 LoginView 读 `result.mfaRequired` 时 TypeError，
    // 且被 catch 吞成"登录失败"—— 表现为路由没跳转而错误提示为空。
    authStoreMock.login.mockResolvedValue({ mfaRequired: false })
    authStoreMock.user = null
    authApi.getCaptchaPublicConfig.mockResolvedValue({ enabled: false })
    authApi.getSmsStatus.mockResolvedValue({ enabled: true, login_enabled: true })
    authApi.getEmailStatus.mockResolvedValue({ enabled: true, login_enabled: true, login_verify: false })
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(authApi.getCaptchaPublicConfig).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 登录：把输入的邮箱/密码交给 store，普通用户跳 /chat', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { email: string; password: string }
      handleLogin: () => Promise<void>
    }
    vm.form.email = 'user@example.com'
    vm.form.password = 'secret-123'

    await vm.handleLogin()
    await flushPromises()

    expect(authStoreMock.login).toHaveBeenCalledWith(
      'user@example.com', 'secret-123', expect.objectContaining({}),
    )
    expect(router.currentRoute.value.path).toBe('/chat')
    wrapper.unmount()
  })

  it('★ 登录：管理员跳 /admin（按角色分流，不是外观问题）', async () => {
    authStoreMock.user = { role: 'admin' }
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { email: string; password: string }
      handleLogin: () => Promise<void>
    }
    vm.form.email = 'admin@example.com'
    vm.form.password = 'secret-123'

    await vm.handleLogin()
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/admin')
    wrapper.unmount()
  })

  it('★ 发送短信验证码后按钮进入倒计时禁用态（useSmsCountdown 的接线）', async () => {
    const wrapper = mountView()
    await flushPromises()

    // ⚠ antd `Tabs` **只挂载当前激活的 tab** ⇒ 先切到短信 tab，否则按钮根本不在 DOM 里
    // （第一版就是在这失败的：只渲染了"密码登录"一个 tab）。
    const tabs = wrapper.findAll('.ant-tabs-tab')
    const smsTab = tabs.find(t => /短信|sms|phone|手机/i.test(t.text()))
    expect(smsTab).toBeTruthy()
    await smsTab!.trigger('click')
    await flushPromises()

    const btn = wrapper.findAll('button').find(b => /获取验证码|发送|重发|send/i.test(b.text()))
    expect(btn).toBeTruthy()
    expect(btn!.attributes('disabled')).toBeUndefined()

    await btn!.trigger('click')
    await flushPromises()

    expect(authApi.sendSmsCode).toHaveBeenCalled()
    const after = wrapper.findAll('button').find(b => /获取验证码|发送|重发|send|\d/i.test(b.text()))
    expect(after!.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  // ── 双因子登录（两步）──
  //
  // 这条路径此前零覆盖，而它一旦接错，**开启 MFA 的用户会被锁在应用外**：
  // 后端在双因子启用时不签发正式 token（只给 5 分钟的挑战令牌），
  // 若登录页读完 handleLogin 就直接 router.push，用户会进到一个没有 token 的
  // 页面，随后每个请求 401 —— 看起来像"登录成功但什么都打不开"。

  it('★ 登录：需要 MFA 时停在验证码这一步，不跳转', async () => {
    authStoreMock.login.mockResolvedValue({ mfaRequired: true })
    // router 是模块级单例，前面的用例可能已把路径推到 /admin。
    // 先归位，否则"没有跳转"这个断言会被残留状态误判（实测踩过）。
    await router.push('/')
    await flushPromises()
    const before = router.currentRoute.value.path

    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { email: string; password: string }
      handleLogin: () => Promise<void>
      mfaStep: boolean
    }
    vm.form.email = 'mfa@example.com'
    vm.form.password = 'secret-123'

    await vm.handleLogin()
    await flushPromises()

    // 必须在原地等待验证码，而不是带着「没有 token」的状态进应用。
    expect(vm.mfaStep).toBe(true)
    expect(router.currentRoute.value.path).toBe(before)
    wrapper.unmount()
  })

  it('★ 登录：提交验证码后按角色跳转', async () => {
    authStoreMock.login.mockResolvedValue({ mfaRequired: true })
    authStoreMock.completeMfaLogin.mockResolvedValue({ role: 'admin' })
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { email: string; password: string }
      handleLogin: () => Promise<void>
      mfaCode: string
      handleMfaVerify: () => Promise<void>
    }
    vm.form.email = 'mfa@example.com'
    vm.form.password = 'secret-123'
    await vm.handleLogin()
    await flushPromises()

    vm.mfaCode = '123456'
    await vm.handleMfaVerify()
    await flushPromises()

    expect(authStoreMock.completeMfaLogin).toHaveBeenCalledWith('123456')
    expect(router.currentRoute.value.path).toBe('/admin')
    wrapper.unmount()
  })
})
