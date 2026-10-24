/**
 * 界面音效（CDS v2/05 · Audio Feedback / Zero External Audio Assets）。
 *
 * 四个音全部用 Web Audio 振荡器**现场合成**：不引入任何音频文件，包体零增长，
 * 也不会因为资源 404 而静默失效。音高走向本身就是语义 —— 上行"进入"、下行"退出"、
 * 双音"有事等你"、下沉"断了"。
 *
 * **默认关闭，这是刻意的**：突然出声是侵入性的，必须由用户主动打开才合理，
 * 也给屏幕阅读器用户留了一条不受打扰的默认路径。开关在「显示设置」里。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'chiron:ui-sounds:v1'
/** 主增益。四个音都极短，0.05 已经清晰但不刺耳（不是 30% —— 那是系统音量）。 */
const MASTER_GAIN = 0.05
/** 起音时长：直接开关振荡器会"咔"一声，那是直流跳变而不是音色。 */
const ATTACK = 0.008

export type UiSound = 'voice-enter' | 'voice-exit' | 'tool-pending' | 'disconnected'

interface Tone {
  freq: number
  /** 相对整组起点的延迟（秒） */
  at: number
  /** 时长（秒） */
  dur: number
}

const RECIPES: Record<UiSound, Tone[]> = {
  'voice-enter': [{ freq: 660, at: 0, dur: 0.07 }, { freq: 880, at: 0.06, dur: 0.08 }],
  'voice-exit': [{ freq: 880, at: 0, dur: 0.07 }, { freq: 660, at: 0.06, dur: 0.09 }],
  'tool-pending': [{ freq: 880, at: 0, dur: 0.06 }, { freq: 1174.7, at: 0.085, dur: 0.1 }],
  disconnected: [{ freq: 330, at: 0, dur: 0.1 }, { freq: 196, at: 0.1, dur: 0.22 }],
}

function readEnabled(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false      // 隐私模式：默认关闭
  }
}

/** 供设置面板双向绑定。 */
export const soundsEnabled = ref(readEnabled())

export function setSoundsEnabled(value: boolean): void {
  soundsEnabled.value = value
  try {
    if (value) localStorage.setItem(STORAGE_KEY, '1')
    else localStorage.removeItem(STORAGE_KEY)
  } catch { /* 存不下就只在本次会话内生效 */ }
}

let ctx: AudioContext | null = null

/** 懒建 AudioContext：浏览器要求必须在用户手势之后创建，提前建会被挂起。 */
function audio(): AudioContext | null {
  if (typeof window === 'undefined') return null
  const Ctor = window.AudioContext
    ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctor) return null
  if (!ctx) {
    try {
      ctx = new Ctor()
    } catch {
      return null
    }
  }
  if (ctx.state === 'suspended') void ctx.resume()
  return ctx
}

/** 播放一个界面音效。关闭状态 / 无 AudioContext 时静默返回（绝不抛错）。 */
export function playUiSound(kind: UiSound): void {
  if (!soundsEnabled.value) return
  const ac = audio()
  if (!ac) return
  const base = ac.currentTime
  for (const tone of RECIPES[kind]) {
    const osc = ac.createOscillator()
    const gain = ac.createGain()
    osc.type = 'sine'
    const start = base + tone.at
    const end = start + tone.dur
    osc.frequency.setValueAtTime(tone.freq, start)
    // 指数包络：起音 8ms、收尾到几乎为零。linearRamp 到 0 会"啪"一下，所以走 0.0001。
    gain.gain.setValueAtTime(0.0001, start)
    gain.gain.exponentialRampToValueAtTime(MASTER_GAIN, start + ATTACK)
    gain.gain.exponentialRampToValueAtTime(0.0001, end)
    osc.connect(gain).connect(ac.destination)
    osc.start(start)
    osc.stop(end + 0.01)
  }
}
