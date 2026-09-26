import { mount } from '@vue/test-utils'
import { describe, it, expect, afterEach } from 'vitest'
import LanguageSwitcher from '../LanguageSwitcher.vue'
import { i18n, setLocale } from '../../../i18n'
import { DEFAULT_LOCALE } from '../../../i18n/languages'

// 各用例之间恢复源语言，避免 localStorage / locale 状态跨用例污染
afterEach(() => setLocale(DEFAULT_LOCALE))

describe('LanguageSwitcher', () => {
  it('默认形态：渲染带 aria-label 的选择器，值为当前语言', () => {
    setLocale(DEFAULT_LOCALE)
    const wrapper = mount(LanguageSwitcher)
    const select = wrapper.find('.language-switcher')
    expect(select.exists()).toBe(true)
    expect(select.attributes('aria-label')).toBe('语言')
    expect(select.attributes('value')).toBe('zh-CN')
  })

  it('compact 形态：渲染地球图标按钮而非下拉选择器', () => {
    const wrapper = mount(LanguageSwitcher, { props: { compact: true } })
    const btn = wrapper.find('.lang-compact-btn')
    expect(btn.exists()).toBe(true)
    expect(wrapper.find('.language-switcher').exists()).toBe(false)
    expect(btn.attributes('aria-label')).toContain('语言')
  })

  it('setLocale 切换 en-US 时更新全局 locale 与 <html lang>', () => {
    setLocale(DEFAULT_LOCALE)
    setLocale('en-US')
    expect(i18n.global.locale.value).toBe('en-US')
    expect(document.documentElement.getAttribute('lang')).toBe('en-US')
  })

  it('setLocale 切换 ar（RTL）时更新 <html dir>', () => {
    setLocale('ar')
    expect(document.documentElement.getAttribute('lang')).toBe('ar')
    expect(document.documentElement.getAttribute('dir')).toBe('rtl')
    setLocale(DEFAULT_LOCALE)
  })
})
