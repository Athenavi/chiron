/**
 * admin 域文案（管理后台页面）。
 *
 * 含 {占位符} 的文案**必须**登记在域文件里：vue-i18n 在 key 不存在时
 * 会原样返回 key 并跳过插值，界面上就会显示成 `{name}（已配置 Key）`。
 */
export default {
  providers: {
    /** {name} = provider 名称 */
    configuredName: '{name}（已配置 Key）',
  },
  keyCategories: {
    international: '国际厂商',
    internationalHint: 'OpenAI / Anthropic / Gemini / Grok / Groq / Mistral …',
    china: '国内厂商',
    chinaHint: 'DeepSeek / Kimi / 智谱 GLM / 通义千问 / 混元 …',
    aggregator: '聚合网关',
    aggregatorHint: 'OpenRouter / 硅基流动 / One API 自建网关',
    selfHosted: '自托管与本地',
    selfHostedHint: 'Ollama / vLLM / LM Studio / 自定义端点',
  },
  keyStatus: {
    active: '正常',
    rateLimited: '限流中',
    circuitOpen: '熔断',
  },
  keyKind: {
    openaiCompatible: 'OpenAI 兼容',
  },
  updateFailed: '更新失败: {msg}',
  deleteFailed: '删除失败: {msg}',
}
