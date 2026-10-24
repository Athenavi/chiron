/**
 * 阅读排版偏好（正文字号 / 行距 / 行间距 / 内容列宽）与**界面密度**。
 *
 * 为什么落到 <html> 的内联 CSS 变量而不是逐层透传 prop：这些值同时被消息正文、
 * 行间距、内容列宽以及后续的行式组件读取，逐个透传会让每个新组件都得记得接一遍；
 * 变量只写一次，且切换主题时不需要重新应用（主题改的是另一批变量）。
 *
 * **密度与排版是两套旋钮，互不越界**（CDS v2 §3.3）：
 * 排版管"正文怎么读"（字号/行距/间距/列宽），密度管"控件多紧凑"（按钮高度/内边距/行高）。
 * 混成一个档位的话，想"字号大一点但控件别变大"就做不到。
 */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

import { t } from '../i18n'
const STORAGE_KEY = 'chiron:typography:v1'

/** 界面密度：只影响控件几何，不影响正文排版。 */
export type Density = 'compact' | 'default' | 'comfortable'

export interface TypographyState {
  /** 正文字号（px） */
  textSize: number
  /** 正文行高倍数 */
  leading: number
  /** 消息行间距（px） */
  gap: number
  /** 对话内容列宽（px） */
  contentWidth: number
  /** 界面密度（控件高度 / 内边距 / 行头高度） */
  density: Density
}

/**
 * 前三个默认值与接入前的硬编码一致（不设置时视觉零变化）；
 * contentWidth 则是**有意**比原来的 720px 宽：桌面端同屏可读更多代码与表格，
 * 窄窗口由样式的 92% 上限兜底，不会溢出。
 * density 默认 'default' 同样等于既有几何（--btn-* 的原值），切档才变。
 */
export const TYPOGRAPHY_DEFAULTS: TypographyState = {
  textSize: 16,
  leading: 1.75,
  gap: 6,
  contentWidth: 960,
  density: 'default',
}

export const TEXT_SIZE_OPTIONS = [
  { label: t('common.compact'), value: 14 },
  { label: t('common.standard'), value: 16 },
  { label: t('common.spacious'), value: 18 },
]
export const LEADING_OPTIONS = [
  { label: t('common.strict'), value: 1.6 },
  { label: t('common.standard'), value: 1.75 },
  { label: t('common.loose'), value: 2 },
]
export const GAP_OPTIONS = [
  { label: t('common.compact'), value: 2 },
  { label: t('common.standard'), value: 6 },
  { label: t('common.roomy'), value: 14 },
]
export const CONTENT_WIDTH_OPTIONS = [
  { label: t('common.comfortable'), value: 720 },
  { label: t('common.wide'), value: 960 },
  { label: t('common.ultra_wide'), value: 1200 },
]
export const DENSITY_OPTIONS = [
  { label: t('common.compact'), value: 'compact' },
  { label: t('common.standard'), value: 'default' },
  { label: t('common.comfortable'), value: 'comfortable' },
]

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(max, Math.max(min, value))
    : fallback
}

/** 密度只认这三个字面量：旧存档里没有该字段、或被改坏时一律回默认档。 */
function readDensity(value: unknown): Density {
  return value === 'compact' || value === 'comfortable' ? value : TYPOGRAPHY_DEFAULTS.density
}

function read(): TypographyState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...TYPOGRAPHY_DEFAULTS }
    const parsed = JSON.parse(raw) as Partial<TypographyState>
    return {
      textSize: clampNumber(parsed.textSize, 12, 22, TYPOGRAPHY_DEFAULTS.textSize),
      leading: clampNumber(parsed.leading, 1.2, 2.4, TYPOGRAPHY_DEFAULTS.leading),
      gap: clampNumber(parsed.gap, 0, 32, TYPOGRAPHY_DEFAULTS.gap),
      contentWidth: clampNumber(parsed.contentWidth, 640, 1400, TYPOGRAPHY_DEFAULTS.contentWidth),
      density: readDensity(parsed.density),
    }
  } catch {
    return { ...TYPOGRAPHY_DEFAULTS }
  }
}

export const useTypographyStore = defineStore('typography', () => {
  const state = ref<TypographyState>(read())

  /** 写入 <html> 内联变量：组件样式只读变量，不感知 store */
  function apply() {
    const root = document.documentElement
    root.style.setProperty('--chat-text-size', `${state.value.textSize}px`)
    root.style.setProperty('--chat-text-leading', String(state.value.leading))
    root.style.setProperty('--chat-msg-gap', `${state.value.gap}px`)
    root.style.setProperty('--chat-content-width', `${state.value.contentWidth}px`)
    // 密度走属性而不是变量：档位要覆盖一批 --btn-* / --chat-row-height，
    // 用 CSS 里的 html[data-density='…'] 规则比在 JS 里逐个 setProperty 更集中也更好审。
    if (state.value.density === 'default') delete root.dataset.density
    else root.dataset.density = state.value.density
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.value))
    } catch { /* 隐私模式 / 配额满：偏好本次会话内生效即可 */ }
  }

  function setTextSize(value: number) { state.value = { ...state.value, textSize: value } }
  function setLeading(value: number) { state.value = { ...state.value, leading: value } }
  function setGap(value: number) { state.value = { ...state.value, gap: value } }
  function setContentWidth(value: number) { state.value = { ...state.value, contentWidth: value } }
  function setDensity(value: Density) { state.value = { ...state.value, density: readDensity(value) } }
  function reset() { state.value = { ...TYPOGRAPHY_DEFAULTS } }

  // flush: 'sync' —— 变量要立刻落到 <html>，否则读者会先看到旧字号再跳变
  watch(state, () => { apply(); persist() }, { immediate: true, deep: true, flush: 'sync' })

  return { state, apply, setTextSize, setLeading, setGap, setContentWidth, setDensity, reset }
})
