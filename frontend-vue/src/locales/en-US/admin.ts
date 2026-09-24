/**
 * admin 域文案（管理后台页面）。
 *
 * 含 {占位符} 的文案**必须**登记在域文件里：vue-i18n 在 key 不存在时
 * 会原样返回 key 并跳过插值，界面上就会显示成 `{name}（已配置 Key）`。
 */
export default {
  providers: {
    /** {name} = provider name */
    configuredName: '{name} (Key configured)',
  },
  keyCategories: {
    international: 'International',
    internationalHint: 'OpenAI / Anthropic / Gemini / Grok / Groq / Mistral …',
    china: 'China',
    chinaHint: 'DeepSeek / Kimi / GLM / Qwen / Hunyuan …',
    aggregator: 'Aggregators',
    aggregatorHint: 'OpenRouter / SiliconFlow / self-hosted One API',
    selfHosted: 'Self-hosted & local',
    selfHostedHint: 'Ollama / vLLM / LM Studio / custom endpoint',
  },
  keyStatus: {
    active: 'Active',
    rateLimited: 'Rate limited',
    circuitOpen: 'Tripped',
  },
  keyKind: {
    openaiCompatible: 'OpenAI-compatible',
  },
  updateFailed: 'Update failed: {msg}',
  deleteFailed: 'Delete failed: {msg}',
}
