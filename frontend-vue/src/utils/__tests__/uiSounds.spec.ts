import { beforeEach, describe, expect, it, vi } from 'vitest'
import { playUiSound, setSoundsEnabled, soundsEnabled } from '../uiSounds'

/** 最小的 Web Audio 假实现：只统计"开了几个振荡器"，其它 API 只要不抛错即可。 */
let oscStarted = 0
let ctxCreated = 0

class FakeParam {
  setValueAtTime() { /* noop */ }
  exponentialRampToValueAtTime() { /* noop */ }
}
class FakeGain {
  gain = new FakeParam()
  connect<T>(node: T): T { return node }
}
class FakeOsc {
  type = 'sine'
  frequency = new FakeParam()
  connect<T>(node: T): T { return node }
  start() { oscStarted++ }
  stop() { /* noop */ }
}
class FakeCtx {
  currentTime = 0
  state = 'running'
  destination = {}
  constructor() { ctxCreated++ }
  createOscillator() { return new FakeOsc() }
  createGain() { return new FakeGain() }
  resume() { return Promise.resolve() }
}

beforeEach(() => {
  setSoundsEnabled(false)
  localStorage.clear()
  oscStarted = 0
  ctxCreated = 0
  vi.stubGlobal('AudioContext', FakeCtx)
})

describe('交互音效', () => {
  it('默认关闭：不出声，连 AudioContext 都不创建', () => {
    expect(soundsEnabled.value).toBe(false)
    playUiSound('voice-enter')
    expect(ctxCreated).toBe(0)
    expect(oscStarted).toBe(0)
  })

  it('开启后按配方发声：voice-enter 是上行两音', () => {
    setSoundsEnabled(true)
    playUiSound('voice-enter')
    expect(oscStarted).toBe(2)
  })

  it('四个场景都可调用，且不会抛错', () => {
    setSoundsEnabled(true)
    for (const kind of ['voice-enter', 'voice-exit', 'tool-pending', 'disconnected'] as const) {
      expect(() => playUiSound(kind)).not.toThrow()
    }
  })

  it('开关落到 localStorage；关掉后立刻静音', () => {
    setSoundsEnabled(true)
    expect(localStorage.getItem('chiron:ui-sounds:v1')).toBe('1')

    setSoundsEnabled(false)
    expect(localStorage.getItem('chiron:ui-sounds:v1')).toBeNull()

    const before = oscStarted
    playUiSound('voice-enter')
    expect(oscStarted).toBe(before)   // 关闭后一句都不再开
  })

  it('环境没有 AudioContext 时静默返回（不能因为音效而崩）', () => {
    setSoundsEnabled(true)
    vi.stubGlobal('AudioContext', undefined)
    expect(() => playUiSound('disconnected')).not.toThrow()
  })
})
