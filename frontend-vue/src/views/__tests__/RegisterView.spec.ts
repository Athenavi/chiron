import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `RegisterView` 的冒烟网 + **`useSmsCountdown` 的接线**（2026-10-09）。
 *
 * 为什么现在补：第 133 轮按"helper 有单测、调用方有没有测"的启发式扫了一遍，发现
 * `src/views/__tests__/` 里**只有 ChatView / ConflictCard 等 5 个 spec** ——
 * `LoginView` / `RegisterView` / `AgentsView` / `KnowledgeView` / `PluginsView` / `SkillsView`
 * **六个视图一个测试都没有** ✗。`useSmsCountdown` 自己**有单测** ✓，但"按钮到底有没有被它禁用"**没人测** ✗。
 *
 * **为什么这条不是外观问题**：`RegisterView` 的验证码按钮 `:disabled="emailRemaining > 0 || emailSending"`
 * （`:295`）—— 计数没接上的话按钮**一直可点** ⇒ 用户（或脚本）能**连续触发短信** ✗，
 * 那是**成本与滥用**问题，不是样式问题 ✓。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

const authApi = vi.hoisted(() => ({
  sendEmailCode: vi.fn(() => Promise.resolve({ interval: 60 })),
  getCaptchaPublicConfig: vi.fn(() => Promise.resolve({ enabled: false })),
  // ⚠ 必须**两个**字段都真：`RegisterView.vue:143` 是
  // `emailVerifyRequired = !!(es.enabled && es.register_verify)` —— 只给 `enabled`
  // 验证码那一行根本不会渲染（第一版就栽在这，按钮找不到）。
  getEmailStatus: vi.fn(() => Promise.resolve({ enabled: true, register_verify: true })),
  isValidEmail: vi.fn(() => true),
}))

vi.mock('../../api/auth', () => authApi)
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ register: vi.fn(), loading: false, user: null, token: '' }),
}))

import RegisterView from '../RegisterView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/login', component: { template: '<div />' } },
  ],
})

function mountView() {
  return mount(RegisterView, { global: { plugins: [router] } })
}

/** 验证码那一行的"获取验证码 / 重发倒计时"按钮。 */
function codeButton(wrapper: ReturnType<typeof mountView>) {
  return wrapper.findAll('button').find(b => /获取验证码|重发|resend|verification/i.test(b.text()))
}

describe('RegisterView（此前零测试）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authApi.sendEmailCode.mockResolvedValue({ interval: 60 })
  })

  it('能挂载、跑完初始加载、卸载不抛错', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('注册')
    expect(authApi.getCaptchaPublicConfig).toHaveBeenCalled()
    expect(() => wrapper.unmount()).not.toThrow()
  })

  it('★ 发送验证码后按钮进入倒计时禁用态（useSmsCountdown 的接线）', async () => {
    const wrapper = mountView()
    await flushPromises()

    // 先填好邮箱（否则 handler 会因校验提前返回）
    const inputs = wrapper.findAll('input')
    const emailInput = inputs.find(i => i.attributes('type') === 'email' || /邮箱|email/i.test(i.attributes('placeholder') || ''))
    expect(emailInput).toBeTruthy()
    await emailInput!.setValue('someone@example.com')

    const btn = codeButton(wrapper)
    expect(btn).toBeTruthy()
    expect(btn!.attributes('disabled')).toBeUndefined()   // 未发送时可点

    await btn!.trigger('click')
    await flushPromises()

    expect(authApi.sendEmailCode).toHaveBeenCalled()
    // 接线断言：计数已启动 ⇒ 按钮禁用，且文案变成倒计时
    const after = codeButton(wrapper)
    expect(after!.attributes('disabled')).toBeDefined()
    expect(after!.text()).toMatch(/\d/)
    wrapper.unmount()
  })
})
