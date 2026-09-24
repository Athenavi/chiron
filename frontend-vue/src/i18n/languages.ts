/**
 * 支持的语言清单 —— 单一来源。
 *
 * 为什么集中在这里：语言不只是"译文目录"，它同时决定
 *   1) 书写方向（RTL 语言必须在 <html dir> 上反映，否则布局会整块错位）；
 *   2) ant-design-vue 组件库语言包（分页器、日期选择器、空态等）；
 *   3) dayjs 的语言包（相对时间、月份名、日期格式）。
 * 三者必须同步切换，任何一处漏掉都会出现"界面英文、日期中文"这类混搭。
 *
 * 新增语言：在此追加一项 + 新建 src/locales/<code>/ 目录（zh-CN 为源语言）。
 */
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import enUS from 'ant-design-vue/es/locale/en_US'
import arEG from 'ant-design-vue/es/locale/ar_EG'

export type LocaleCode = 'zh-CN' | 'en-US' | 'ar'

export interface LanguageMeta {
  /** BCP-47 语言标签，同时用作 locales 目录名与 <html lang> */
  code: LocaleCode
  /** 该语言的母语名称：切换器里始终用母语显示（用户看得懂自己的语言） */
  nativeName: string
  /** 书写方向；rtl 会让 <html dir="rtl"> 生效 */
  dir: 'ltr' | 'rtl'
  /** ant-design-vue 语言包 */
  antd: typeof zhCN
  /** dayjs 语言包 key（须在 src/i18n/index.ts 里 import 'dayjs/locale/<key>'） */
  dayjs: string
}

/** 源语言（source of truth）：新增文案先写 zh-CN，再补其它语言。 */
export const DEFAULT_LOCALE = 'zh-CN'

export const LANGUAGES: LanguageMeta[] = [
  { code: 'zh-CN', nativeName: '简体中文', dir: 'ltr', antd: zhCN, dayjs: 'zh-cn' },
  { code: 'en-US', nativeName: 'English', dir: 'ltr', antd: enUS, dayjs: 'en' },
  { code: 'ar', nativeName: 'العربية', dir: 'rtl', antd: arEG, dayjs: 'ar' },
]

/** 取语言元数据；未知代码回退源语言（不抛错，避免语言包缺失导致白屏）。 */
export function languageMeta(code: string | undefined | null): LanguageMeta {
  return LANGUAGES.find(l => l.code === code) ?? LANGUAGES[0]
}

/** 语言代码是否受支持。 */
export function isSupported(code: string | undefined | null): boolean {
  return !!code && LANGUAGES.some(l => l.code === code)
}
