import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import CaptchaWidget from '../CaptchaWidget.vue'

/**
 * `CaptchaWidget` 的测试（2026-10-09）。
 *
 * 第 139 轮量出 `src/components/**` 有 **27 个组件没有任何 spec 引用**，本轮挑**安全相关**的那个 ✓。
 *
 * **两条不变量值得钉**：
 * ① **`custom` 必须 fail-loud、绝不伪装已验证**（`:8` / `:192-193` / `:248-250`）——
 *    它只展示部署方接入提示，**从不 emit `verified`** ✓。这条如果破了，等于**绕过了人机验证**：
 *    父组件会以为用户已经通过验证，于是带着空 token 去提交 ✓；
 * ② **`verified` 的载荷形状**：`emit('verified', { token })`（`:149`），
 *    腾讯那条还会带 `randstr`（`:11`）—— 父组件按这个形状取值，形状错了就等于没拿到 token ✓。
 */

const TURNSTILE_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/** 把 CDN 脚本"预置成已加载"：`loadScript` 见到 `data-loaded=1` 就直接 resolve（`:98-99`） */
function presetScript(src: string) {
  const s = document.createElement('script')
  s.src = src
  s.dataset.loaded = '1'
  document.head.appendChild(s)
  return s
}

/** 装上假的第三方全局（形状按 `:37-49`） */
function installFakeTurnstile() {
  const calls: { sitekey: string; opts: Record<string, unknown> }[] = []
  const fake = {
    render: vi.fn((_el: HTMLElement, opts: { sitekey: string } & Record<string, unknown>) => {
      calls.push({ sitekey: opts.sitekey, opts: opts as Record<string, unknown> })
      return 'w-1'
    }),
    reset: vi.fn(),
  }
  ;(window as unknown as { turnstile?: unknown }).turnstile = fake
  return { fake, calls }
}

describe('CaptchaWidget', () => {
  beforeEach(() => {
    delete (window as unknown as { turnstile?: unknown }).turnstile
  })

  afterEach(() => {
    delete (window as unknown as { turnstile?: unknown }).turnstile
    document.head.querySelectorAll('script').forEach(s => s.remove())
  })

  it('★ custom：fail-loud —— 只给部署方接入提示，**绝不** emit verified', async () => {
    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'custom', siteKey: '', verifyUrl: 'https://verify.example.com' },
    })
    await flushPromises()
    await wrapper.vm.$nextTick()

    // 提示里要带上部署方给的地址（否则用户不知道去哪验证）
    expect(wrapper.text()).toContain('https://verify.example.com')
    // 关键：**一次都不能**说"已验证"
    expect(wrapper.emitted('verified')).toBeUndefined()
  })

  it('★ turnstile：按 siteKey 渲染，验证回调把 token 交给父组件', async () => {
    presetScript(TURNSTILE_SRC)
    const { fake, calls } = installFakeTurnstile()

    const wrapper = mount(CaptchaWidget, {
      props: { provider: 'turnstile', siteKey: 'site-key-123' },
    })
    // `waitFor` 每 100ms 轮询一次 ⇒ 给它几轮
    await vi.waitFor(() => expect(fake.render).toHaveBeenCalled(), { timeout: 2000 })

    expect(calls[0]?.sitekey).toBe('site-key-123')

    // 模拟验证码服务回调
    const opts = calls[0]!.opts as { callback?: (t: string) => void; 'expired-callback'?: () => void }
    opts.callback?.('tok-abc')
    await wrapper.vm.$nextTick()

    expect(wrapper.emitted('verified')?.[0]?.[0]).toEqual({ token: 'tok-abc' })

    // 过期回调也要转发（父组件据此把 token 清掉）
    opts['expired-callback']?.()
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('expired')).toHaveLength(1)
  })
})
