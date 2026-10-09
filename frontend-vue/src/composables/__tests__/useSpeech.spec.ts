import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

/**
 * `composables/useSpeech.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 从第 149 轮"有人用"那堆里挑（92 行）—— 它是**浏览器 API 封装**里真逻辑最多的一个，
 * 而且注释写明了三条判据 ✓：
 * ① **`speak` 前先 `cancel`**（`:55`）—— "连续点击不该把多段内容排进队列" ✓；
 * ② **`resolveVoice` 找不到同名音色时回退默认**（`:5-6` / `:48-51`）—— 音色列表来自
 *    **用户操作系统**，换机器可能匹配不到 ⇒ 要**回退**而不是**静默不发声** ✓；
 * ③ **音色列表异步就绪**（`:8-10`）⇒ 暴露"是否已有音色"，让设置界面显示加载态而不是空下拉框 ✓。
 *
 * ⚠ `isSupported` 是**模块加载时**算的（`:23`）⇒ 必须在 **import 之前**把
 * `window.speechSynthesis` 装好，否则永远走"不支持"分支。所以用 `vi.hoisted`（先于 import 执行）✓。
 */

const speech = vi.hoisted(() => {
  const makeVoice = (voiceURI: string, lang: string) =>
    ({ voiceURI, lang, name: voiceURI, default: false, localService: true }) as SpeechSynthesisVoice

  const utterances: Array<Record<string, unknown>> = []
  class FakeUtterance {
    text: string
    voice: SpeechSynthesisVoice | null = null
    rate = 1
    pitch = 1
    onstart: (() => void) | null = null
    onend: (() => void) | null = null
    onerror: (() => void) | null = null
    constructor(text: string) {
      this.text = text
      utterances.push(this as unknown as Record<string, unknown>)
    }
  }

  const listeners = new Set<() => void>()
  const synth = {
    getVoices: vi.fn((): SpeechSynthesisVoice[] => []),
    speak: vi.fn(),
    cancel: vi.fn(),
    addEventListener: vi.fn((_t: string, fn: () => void) => { listeners.add(fn) }),
    removeEventListener: vi.fn((_t: string, fn: () => void) => { listeners.delete(fn) }),
  }

  // 必须在模块求值前装好（`useSpeech` 的 `isSupported` 在加载时判定）
  ;(globalThis as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = FakeUtterance
  Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true })

  return { synth, utterances, listeners, makeVoice, FakeUtterance }
})

import { useSpeech } from '../useSpeech'

/** 一个最小宿主组件：让 `onUnmounted` 能真的跑（否则清理路径测不到） */
const Host = defineComponent({
  setup() {
    const api = useSpeech()
    return { api }
  },
  render() {
    return h('div')
  },
})

describe('composables/useSpeech', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    speech.utterances.length = 0
    speech.listeners.clear()
    speech.synth.getVoices.mockReturnValue([])
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('★ speak 之前先 cancel（连续点击不排队），并返回"确实发起了"', () => {
    const wrapper = mount(Host)
    const api = (wrapper.vm as unknown as { api: ReturnType<typeof useSpeech> }).api

    const ok = api.speak('  你好  ', { rate: 1.5 })
    expect(ok).toBe(true)
    // 关键顺序：先 cancel 再 speak（否则多段内容会排队）
    expect(speech.synth.cancel).toHaveBeenCalled()
    expect(speech.synth.speak).toHaveBeenCalledTimes(1)

    const utter = speech.utterances[0] as { text: string; rate: number }
    expect(utter.text).toBe('你好')      // 去掉了首尾空白
    expect(utter.rate).toBe(1.5)
    wrapper.unmount()
  })

  it('★ 空/纯空白文本不发声，也不调 API（返回 false）', () => {
    const wrapper = mount(Host)
    const api = (wrapper.vm as unknown as { api: ReturnType<typeof useSpeech> }).api

    expect(api.speak('')).toBe(false)
    expect(api.speak('   \n  ')).toBe(false)
    expect(speech.synth.speak).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('★★ speaking 跟随发声生命周期：onend 与 onerror 都要复位（失败不能卡在"正在朗读"）', async () => {
    const wrapper = mount(Host)
    const api = (wrapper.vm as unknown as { api: ReturnType<typeof useSpeech> }).api

    api.speak('abc')
    const utter = speech.utterances[0] as { onstart: () => void; onend: () => void; onerror: () => void }

    expect(api.speaking.value).toBe(false)   // 还没开始
    utter.onstart()
    expect(api.speaking.value).toBe(true)
    utter.onend()
    expect(api.speaking.value).toBe(false)

    // 再来一次，这次走**错误**路径 —— 同样必须复位
    api.speak('def')
    const u2 = speech.utterances[1] as { onstart: () => void; onerror: () => void }
    u2.onstart()
    expect(api.speaking.value).toBe(true)
    u2.onerror()
    expect(api.speaking.value).toBe(false)
    wrapper.unmount()
  })

  it('★ resolveVoice：找到就返回，找不到返回 null（⇒ 用系统默认，而不是静默不发声）', () => {
    const v1 = speech.makeVoice('uri-1', 'zh-CN')
    speech.synth.getVoices.mockReturnValue([v1])

    const wrapper = mount(Host)
    const api = (wrapper.vm as unknown as { api: ReturnType<typeof useSpeech> }).api
    api.voices.value = [v1]      // 模拟音色已就绪

    expect(api.resolveVoice({ voiceURI: 'uri-1' })?.voiceURI).toBe('uri-1')
    // 换设备后匹配不到 ⇒ null（调用方据此不设 utter.voice ⇒ 走系统默认）
    expect(api.resolveVoice({ voiceURI: 'uri-gone' })).toBeNull()
    expect(api.resolveVoice({})).toBeNull()
    wrapper.unmount()
  })

  it('★ voicesByLang：按语言分组且**排序**（设置界面要能扫读上百个音色）', () => {
    const wrapper = mount(Host)
    const api = (wrapper.vm as unknown as { api: ReturnType<typeof useSpeech> }).api
    api.voices.value = [
      speech.makeVoice('b', 'zh-CN'),
      speech.makeVoice('a', 'en-US'),
      speech.makeVoice('c', 'zh-CN'),
      speech.makeVoice('d', ''),
    ]

    const groups = api.voicesByLang.value
    expect(groups.map(([lang]) => lang)).toEqual(['en-US', 'other', 'zh-CN'])   // 已排序
    expect(groups.find(([lang]) => lang === 'zh-CN')?.[1]).toHaveLength(2)
    wrapper.unmount()
  })

  it('★ 卸载时移除 voiceschanged 监听**并 cancel**（不留后台朗读与监听泄漏）', () => {
    const wrapper = mount(Host)
    expect(speech.listeners.size).toBe(1)      // 挂载时注册了
    vi.clearAllMocks()

    wrapper.unmount()

    expect(speech.listeners.size).toBe(0)      // 监听被移除
    expect(speech.synth.removeEventListener).toHaveBeenCalled()
    expect(speech.synth.cancel).toHaveBeenCalled()   // 且停掉正在朗读的内容
  })
})
