import chat from './chat'
import errors from './errors'
import legacy from './legacy'
import auth from './auth'
import admin from './admin'

/**
 * ar（RTL）。键与 zh-CN 一一对应（zh-CN 为源语言）；缺失的键会自动回退到 zh-CN。
 * 注意：RTL 由 <html dir="rtl"> 驱动（见 src/i18n/index.ts），新增样式请用逻辑属性
 * （margin-inline-*、padding-inline-*、inset-inline-*），不要写物理方向属性。
 * legacy 目前为空（存量抽取的模板文案待翻译），见 locales/ar/legacy.ts 的说明。
 */
export default {
  common: {
    confirm: 'تأكيد',
    cancel: 'إلغاء',
    save: 'حفظ',
    delete: 'حذف',
    edit: 'تعديل',
    search: 'بحث',
    refresh: 'تحديث',
    loading: 'جارٍ التحميل…',
    empty: 'لا توجد بيانات',
    retry: 'إعادة المحاولة',
    close: 'إغلاق',
    copy: 'نسخ',
    copied: 'تم النسخ',
    language: 'اللغة',
    unknownError: 'خطأ غير معروف',
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
