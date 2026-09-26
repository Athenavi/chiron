/**
 * admin 域译文（ar，RTL）—— 管理后台页面。
 *
 * 键与 zh-CN/admin.ts 一一对应（scripts/check-i18n-keys.mjs 会强制校验）。
 * 含 {占位符} 的文案必须登记：vue-i18n 在键不存在时会原样返回 key 并跳过插值。
 */
export default {
  providers: {
    /** {name} = provider 名称 */
    configuredName: '{name} (تم تكوين المفتاح)',
  },
  keyCategories: {
    international: 'الشركات العالمية',
    internationalHint: 'OpenAI / Anthropic / Gemini / Grok / Groq / Mistral …',
    china: 'الشركات الصينية',
    chinaHint: 'DeepSeek / Kimi / GLM / Qwen / Hunyuan …',
    aggregator: 'البوابات المجمّعة',
    aggregatorHint: 'OpenRouter / SiliconFlow / One API مستضافة ذاتيًا',
    selfHosted: 'الاستضافة الذاتية والمحلية',
    selfHostedHint: 'Ollama / vLLM / LM Studio / نقطة نهاية مخصصة',
  },
  keyStatus: {
    active: 'يعمل',
    rateLimited: 'محدود المعدل',
    circuitOpen: 'قاطع الدائرة مفتوح',
  },
  keyKind: {
    openaiCompatible: 'متوافق مع OpenAI',
  },
  updateFailed: 'فشل التحديث: {msg}',
  deleteFailed: 'فشل الحذف: {msg}',
}
