import chat from './chat'
import errors from './errors'
import legacy from './legacy'
import auth from './auth'
import admin from './admin'

/**
 * en-US。键与 zh-CN 一一对应（zh-CN 为源语言）；缺失的键会自动回退到 zh-CN。
 * legacy 目前为空（存量抽取的模板文案待翻译），见 locales/en-US/legacy.ts 的说明。
 */
export default {
  common: {
    confirm: 'OK',
    cancel: 'Cancel',
    save: 'Save',
    delete: 'Delete',
    edit: 'Edit',
    search: 'Search',
    refresh: 'Refresh',
    loading: 'Loading…',
    empty: 'No data',
    retry: 'Retry',
    close: 'Close',
    copy: 'Copy',
    copied: 'Copied',
    language: 'Language',
    unknownError: 'Unknown error',
  },
  chat,
  errors,
  auth,
  admin,
  // legacy 域**展平**到顶层：它的键就是 zh-CN 原文（gettext 风格），
  // 迁移代码写的是 t('原文') 而不是 t('legacy.原文')。此前它被嵌套成 legacy 域，
  // 导致**裸键永远命不中** —— 无插值的文案会看起来正常（回退时用键当消息，而键即原文），
  // 带插值的文案却渲染出 {n} 字面量，且 en-US/ar 的译文永远不会生效。
  // 展平不会与上面的域冲突：其它域的键是英文/点分键，legacy 的键是中文原文。
  ...legacy,
}
