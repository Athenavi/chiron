/**
 * 录音时的 6 频段电平（CDS v1 §5.1.3 的 LiveMicActivityBars）。
 *
 * **一个必须说清的代价**：本仓的录音走 Web Speech API（`SpeechRecognition`），
 * 它**不暴露音频流**。要画波形只能自己再开一路 `getUserMedia` —— 等于为纯视觉效果
 * 多占一个麦克风实例。所以这里全程容错：拿不到流（权限被拒 / 设备被独占 / 无
 * AudioContext / 非安全上下文）就什么都不画，**录音与识别照常工作**。
 *
 * 数值全部取自手册：512 点 FFT、smoothing 0.5、−70/−10 dB 截断、8% 噪声门、
 * 0.7 次幂 gamma 压缩（放大细语、压制爆音，让动态范围分布均匀），映射到 4–24px。
 */
import { onBeforeUnmount } from 'vue'

const FFT_SIZE = 512
const SMOOTHING = 0.5
const MIN_DB = -70
const MAX_DB = -10
/** 8% 噪声门：彻底滤掉空调底噪引起的虚假微动 */
const NOISE_FLOOR = 0.08
/** gamma 压缩指数 */
const GAMMA = 0.7
/** 压缩后的增益（手册 1.5） */
const GAIN = 1.5

export const MIC_BAND_COUNT = 6

export interface MicBars {
  start: () => Promise<void>
  stop: () => void
}

/**
 * @param onLevels 每帧回调一次，参数是 6 个 0..1 的电平。
 *   刻意不给响应式数组：60fps 改 6 个数字不该触发整个 Composer 重渲染，
 *   调用方应直接用 DOM 写 `--level`（见 ChatInput 的 barEls）。
 */
export function useMicBars(onLevels: (levels: number[]) => void): MicBars {
  let stream: MediaStream | null = null
  let ctx: AudioContext | null = null
  let analyser: AnalyserNode | null = null
  let data: Uint8Array<ArrayBuffer> | null = null
  let raf = 0

  function tick() {
    if (!analyser || !data) return
    analyser.getByteFrequencyData(data)
    const bands = new Array<number>(MIC_BAND_COUNT).fill(0)
    const per = Math.max(1, Math.floor(data.length / MIC_BAND_COUNT))
    for (let b = 0; b < MIC_BAND_COUNT; b++) {
      let sum = 0
      for (let i = b * per; i < (b + 1) * per; i++) sum += data[i] ?? 0
      const raw = sum / per / 255
      const gated = Math.max(0, raw - NOISE_FLOOR) / (1 - NOISE_FLOOR)
      bands[b] = Math.min(1, Math.pow(gated, GAMMA) * GAIN)
    }
    onLevels(bands)
    raf = requestAnimationFrame(tick)
  }

  async function start() {
    try {
      if (!navigator.mediaDevices?.getUserMedia) return
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) {
        stop()
        return
      }
      ctx = new Ctor()
      analyser = ctx.createAnalyser()
      analyser.fftSize = FFT_SIZE
      analyser.smoothingTimeConstant = SMOOTHING
      analyser.minDecibels = MIN_DB
      analyser.maxDecibels = MAX_DB
      ctx.createMediaStreamSource(stream).connect(analyser)
      data = new Uint8Array(analyser.frequencyBinCount)
      raf = requestAnimationFrame(tick)
    } catch {
      // 拿不到流：静默退化为"没有波形"
      stop()
    }
  }

  function stop() {
    if (raf) cancelAnimationFrame(raf)
    raf = 0
    analyser = null
    data = null
    stream?.getTracks().forEach(track => track.stop())
    stream = null
    if (ctx) {
      void ctx.close().catch(() => { /* 已关闭 */ })
      ctx = null
    }
    // 归零，避免波形停在最后一帧的高度上
    onLevels(new Array<number>(MIC_BAND_COUNT).fill(0))
  }

  onBeforeUnmount(stop)
  return { start, stop }
}
