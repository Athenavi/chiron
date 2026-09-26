/**
 * chat 域文案（ar，RTL）。键必须与 zh-CN/chat.ts 一一对应。
 *
 * 相对时间用**命名插值**（`{n}`）而不是字符串拼接：阿拉伯语的语序与中文不同
 * （`{n} 分钟前` / `قبل {n} دقيقة`），拼接必然出错。
 */
export default {
  time: {
    justNow: 'الآن',
    minutesAgo: 'قبل {n} دقيقة',
    hoursAgo: 'قبل {n} ساعة',
    monthDay: '{day}/{month}',
  },
  input: {
    modelTitle: 'النموذج الحالي: {model} (يؤثر على الرسائل التالية فقط)',
    modelDefault: 'الافتراضي (توجيه الخادم)',
    stopSend: 'إيقاف التوليد (اكتب المحتوى ثم اضغط Enter للمقاطعة والإرسال)',
    send: 'إرسال',
  },
  status: {
    online: 'متصل',
    offline: 'غير متصل',
  },
  tabs: {
    runs: 'التشغيلات',
    output: 'المخرجات',
    usage: 'الاستهلاك',
    artifacts: 'المنتجات',
    events: 'تدفق الأحداث',
  },
  stats: {
    turns: 'عدد الأدوار',
    inputTokens: 'رموز الإدخال',
    outputTokens: 'رموز الإخراج',
    cachedTokens: 'رموز مخزّنة مؤقتًا',
    cacheHitRate: 'معدل إصابة الذاكرة المؤقتة',
    cost: 'التكلفة',
    ttftP50: 'زمن أول رمز p50',
    outputTpsP50: 'سرعة الإخراج p50',
    outputTpsP95: 'سرعة الإخراج p95',
  },
  chip: {
    kb: 'قاعدة المعرفة #{id}',
    skill: 'مهارة {name}',
    workflow: 'سير عمل {name}',
    plugin: 'ملحق {name}',
    memory: 'ذاكرة {name}',
  },
  toolUnit: {
    lines: 'سطور',
    codeLines: 'سطور برمجية',
  },
  sessionmap: {
    /** 与 zh-CN 同名注释一致：这是**协议前缀**，必须与 public/sessionmap/adapter.js 的
        FOLLOWUP_RE 匹配，刻意不随界面语言变化（en-US 同理），改它之前先确认 adapter 仍匹配。 */
    followup: '【请简短回答问题】:({text})',
    newSession: 'محادثة جديدة',
    serviceFailed: 'فشل استدعاء خدمة الجلسات',
    missingSource: 'جلسة المصدر مفقودة',
    missingTarget: 'جلسة الهدف مفقودة',
    emptyPatch: 'لا توجد حقول للتحديث',
    emptyMessage: 'محتوى الرسالة فارغ',
  },
  share: {
    defaultTitle: 'سجل المحادثة',
    title: 'مشاركة «{name}»',
    newSession: 'محادثة جديدة',
    visibilityHint: 'رابط المشاركة متاح لأي شخص يحصل عليه',
    selectMessages: 'اختر الرسائل للمشاركة ({selected}/{total})',
    roleUser: 'المستخدم',
    emptyMessage: '(رسالة فارغة)',
  },
  agent: {
    createHint: 'سيُستخدم نص المحادثة ({count} حرفًا) كتوجيه النظام له؛ بعد الإنشاء يمكنك تعديل التوجيه والأدوات وربط مساحة العمل من صفحة الوكلاء.',
  },
}
