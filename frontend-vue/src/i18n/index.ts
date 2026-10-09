/**
 * i18n 单一入口：实例创建、语言持久化、与 antd / dayjs / 文档方向联动。
 *
 * 设计要点：
 *  1) **zh-CN 是源语言**，其它语言缺失的键自动回退（fallbackLocale），
 *     因此迁移过程中不会出现空白文案，只会短暂显示中文原文；
 *  2) 语言与书写方向由 `applyDocumentLocale()` 写到 <html lang/dir>，
 *     首屏必须在 mount 之前调用（否则 RTL 界面会先按 LTR 渲染再跳变）；
 *  3) 非组件上下文（axios 拦截器、工具函数）用本文件导出的 `t()`，
 *     不要直接 import vue-i18n 的 useI18n（它只能在 setup 里用）。
 */
import { createI18n } from 'vue-i18n'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import 'dayjs/locale/en'
import 'dayjs/locale/ar'

import { DEFAULT_LOCALE, LANGUAGES, languageMeta, isSupported, type LanguageMeta } from './languages'
import zhCN from '../locales/zh-CN'
import enUS from '../locales/en-US'
import ar from '../locales/ar'

/** localStorage 键：用户显式选择的语言（优先级最高） */
const STORAGE_KEY = 'chiron.locale'

function readStoredLocale(): string | null {
  try {
    const v = globalThis.localStorage?.getItem(STORAGE_KEY) ?? null
    return isSupported(v) ? v : null
  } catch {
    return null // 隐私模式 / 存储被禁用：忽略，继续走浏览器语言
  }
}

function writeStoredLocale(code: string): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, code)
  } catch {
    /* 存储不可用不影响本次会话内的切换 */
  }
}

/** 按浏览器语言偏好挑选受支持的语言（Accept-Language 顺序即 navigator.languages 顺序） */
function matchNavigatorLocale(): string | null {
  const preferred = globalThis.navigator?.languages ?? [globalThis.navigator?.language].filter(Boolean)
  for (const raw of preferred as string[]) {
    if (isSupported(raw)) return raw
    const base = raw.split('-')[0] // zh-Hans-CN → zh，en-GB → en
    const hit = LANGUAGES.find(l => l.code === base || l.code.split('-')[0] === base)
    if (hit) return hit.code
  }
  return null
}

/**
 * 初始语言：显式选择 → 浏览器语言 → 源语言。
 * 测试环境固定源语言：否则断言会随运行机器的浏览器偏好变化（CI 上莫名其妙变红）。
 */
function initialLocale(): string {
  if (import.meta.env.MODE === 'test') return DEFAULT_LOCALE
  return readStoredLocale() ?? matchNavigatorLocale() ?? DEFAULT_LOCALE
}

export const i18n = createI18n({
  legacy: false,
  globalInjection: true, // 模板里可直接用 $t
  locale: initialLocale(),
  fallbackLocale: DEFAULT_LOCALE,
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
    ar,
  },
  // 仅开发环境提示缺失键：生产环境静默回退，避免控制台噪音
  missingWarn: import.meta.env.DEV,
  fallbackWarn: import.meta.env.DEV,
})

/** 当前语言代码 */
export function currentLocale(): string {
  return i18n.global.locale.value as string
}

/**
 * 把语言、书写方向与**标签页标题**写到文档上，并同步 dayjs 语言。
 * antd 组件库的 locale 由 App.vue 的 <ConfigProvider :locale> 绑定（见 languages.ts）。
 *
 * **标签页标题（2026-10-09 补）**：`index.html` 里的 `<title>` 是**首屏之前的**静态值（必须有，
 * 因为 JS 还没跑），此前**没有任何地方在运行时更新它** ⇒ 切到 `en-US`/`ar` 后标签页仍是中文。
 * 放在这里是因为 `applyDocumentLocale()` 同时覆盖**首次挂载**与**每次 `setLocale()`** 两条路径。
 */
export function applyDocumentLocale(meta: LanguageMeta = languageMeta(currentLocale())): void {
  if (typeof document !== 'undefined') {
    const root = document.documentElement
    root.setAttribute('lang', meta.code)
    root.setAttribute('dir', meta.dir)
    document.title = t('common.appTitle')
  }
  dayjs.locale(meta.dayjs)
}

/** 切换语言（切换器 / 用户设置 / 登录后按服务端偏好同步时调用） */
export function setLocale(code: string): void {
  const meta = languageMeta(code)
  i18n.global.locale.value = meta.code
  writeStoredLocale(meta.code)
  applyDocumentLocale(meta)
}

/**
 * 非组件上下文的翻译入口（axios 拦截器、utils、store 等）。
 * 组件内请用 useI18n() 以获得响应式切换。
 */
export function t(key: string, named?: Record<string, unknown>): string {
  if (!named) return i18n.global.t(key)
  const translate = i18n.global.t as unknown as (k: string, n?: Record<string, unknown>) => string
  return translate(key, named)
}

/** 某个错误码是否有本地化文案（供错误处理回退判断） */
export function hasMessage(key: string): boolean {
  return i18n.global.te(key)
}
