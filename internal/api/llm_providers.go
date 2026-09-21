package api

// ── LLM 服务提供商目录（provider catalog）──
//
// 单一事实源：网关维护 provider 预设目录，三处复用：
//  1. 管理端 GET /v1/admin/llm-providers —— 前端「添加服务提供商」面板选型；
//  2. 内部端点 GET /v1/internal/engine-config —— 以 llm_provider_catalog 下发给
//     Python 引擎，引擎按目录注册 OpenAI 兼容 / Anthropic 原生 provider；
//  3. model_discovery.go —— 解析各 provider 的默认 /models 端点。
//
// 目录项 id 必须与 llm_provider_keys.provider、Redis keyset(llm:keys:{id}) 同名；
// 端点覆盖写 system_settings(python 分类)的 {id}_base_url，为空即回落目录默认值。
// Python 侧兜底目录见 python-engine/app/providers/catalog.py（id/kind 必须保持一致）。

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/url"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 目录分组：前端按此顺序分区展示。
const (
	llmProviderCategoryGlobal     = "international" // 国际厂商
	llmProviderCategoryChina      = "china"         // 国内厂商
	llmProviderCategoryAggregator = "aggregator"    // 聚合网关
	llmProviderCategorySelfHosted = "self_hosted"   // 自托管推理
)

// 接入协议：openai=OpenAI 兼容（/chat/completions），anthropic=Anthropic Messages。
const (
	llmProviderKindOpenAI    = "openai"
	llmProviderKindAnthropic = "anthropic"
)

type llmProviderPreset struct {
	ID     string `json:"id"`     // 与 keyset/DB 的 provider 名一致（小写）
	Label  string `json:"label"`  // 展示名
	Vendor string `json:"vendor"` // 厂商名（用于卡片副标题）
	// Category 见 llmProviderCategory* 常量
	Category string `json:"category"`
	// Kind 见 llmProviderKind* 常量
	Kind string `json:"kind"`
	// BaseURL 为目录默认端点；管理员可在 DB 覆盖（{id}_base_url）。空 = 必须手工填写。
	BaseURL string `json:"base_url"`
	// APIKeyEnv 为引擎侧读取的 key 环境变量名（env 种子兜底）
	APIKeyEnv string `json:"api_key_env"`
	// APIKeyPrefix 用于前端提示 key 形态（如 "sk-"），空则不提示
	APIKeyPrefix string `json:"api_key_prefix"`
	// ModelPrefixes 供引擎按模型名路由（GatewayRouter._find_candidates）
	ModelPrefixes []string `json:"model_prefixes"`
	DocsURL       string   `json:"docs_url"`
	// Cost 为参考成本（$/1M tokens）、Quality 为参考质量（0-1），供加权路由
	Cost    float64 `json:"cost"`
	Quality float64 `json:"quality"`
	// RequiresKey=false 表示本地/自托管端点可无 key 直接注册（key 用占位符）
	RequiresKey bool `json:"requires_key"`
	// ModelDiscovery=false 表示不做 /v1/models 自动发现（如 Anthropic 原生协议）
	ModelDiscovery bool `json:"model_discovery"`
}

// llmProviderCatalog 是内建目录。新增提供商只需在此追加一项（并同步 Python 兜底目录）；
// 引擎侧注册条件为「env 种子非空 或 keyset 已存在该 provider 的 key 或 RequiresKey=false」。
var llmProviderCatalog = []llmProviderPreset{
	// ── 国际厂商 ──
	{
		ID: "openai", Label: "OpenAI", Vendor: "OpenAI", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.openai.com/v1",
		APIKeyEnv: "OPENAI_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{"gpt", "o1", "o3", "o4", "davinci", "text-embedding"},
		DocsURL:       "https://platform.openai.com/docs/api-reference",
		Cost:          5.0, Quality: 0.90, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "anthropic", Label: "Anthropic Claude", Vendor: "Anthropic", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindAnthropic, BaseURL: "https://api.anthropic.com",
		APIKeyEnv: "ANTHROPIC_API_KEY", APIKeyPrefix: "sk-ant-",
		ModelPrefixes: []string{"claude"},
		DocsURL:       "https://docs.anthropic.com/en/api/messages",
		Cost:          3.0, Quality: 0.95, RequiresKey: true, ModelDiscovery: false,
	},
	{
		ID: "google", Label: "Google Gemini", Vendor: "Google", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://generativelanguage.googleapis.com/v1beta/openai",
		APIKeyEnv: "GEMINI_API_KEY", APIKeyPrefix: "AIza",
		ModelPrefixes: []string{"gemini", "gemma"},
		DocsURL:       "https://ai.google.dev/gemini-api/docs/openai",
		Cost:          1.5, Quality: 0.90, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "xai", Label: "xAI Grok", Vendor: "xAI", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.x.ai/v1",
		APIKeyEnv: "XAI_API_KEY", APIKeyPrefix: "xai-",
		ModelPrefixes: []string{"grok"},
		DocsURL:       "https://docs.x.ai/docs/api-reference",
		Cost:          3.0, Quality: 0.88, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "groq", Label: "Groq", Vendor: "Groq", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.groq.com/openai/v1",
		APIKeyEnv: "GROQ_API_KEY", APIKeyPrefix: "gsk_",
		ModelPrefixes: []string{"llama", "mixtral", "gemma", "whisper"},
		DocsURL:       "https://console.groq.com/docs/openai",
		Cost:          0.6, Quality: 0.82, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "mistral", Label: "Mistral AI", Vendor: "Mistral AI", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.mistral.ai/v1",
		APIKeyEnv:     "MISTRAL_API_KEY",
		ModelPrefixes: []string{"mistral", "codestral", "ministral", "pixtral"},
		DocsURL:       "https://docs.mistral.ai/api/",
		Cost:          1.0, Quality: 0.84, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "together", Label: "Together AI", Vendor: "Together", Category: llmProviderCategoryGlobal,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.together.xyz/v1",
		APIKeyEnv:     "TOGETHER_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "https://docs.together.ai/reference",
		Cost:          0.9, Quality: 0.82, RequiresKey: true, ModelDiscovery: true,
	},

	// ── 国内厂商 ──
	{
		ID: "deepseek", Label: "DeepSeek", Vendor: "深度求索", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.deepseek.com",
		APIKeyEnv: "DEEPSEEK_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{"deepseek"},
		DocsURL:       "https://api-docs.deepseek.com/zh-cn/",
		Cost:          0.2, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "moonshot", Label: "月之暗面 Kimi", Vendor: "Moonshot AI", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.moonshot.cn/v1",
		APIKeyEnv: "MOONSHOT_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{"moonshot", "kimi"},
		DocsURL:       "https://platform.moonshot.cn/docs/api/chat",
		Cost:          1.2, Quality: 0.85, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "zhipu", Label: "智谱 GLM", Vendor: "智谱 AI", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://open.bigmodel.cn/api/paas/v4",
		APIKeyEnv:     "ZHIPU_API_KEY",
		ModelPrefixes: []string{"glm", "chatglm"},
		DocsURL:       "https://open.bigmodel.cn/dev/api",
		Cost:          0.6, Quality: 0.82, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "dashscope", Label: "阿里云通义千问", Vendor: "阿里云", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://dashscope.aliyuncs.com/compatible-mode/v1",
		APIKeyEnv: "DASHSCOPE_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{"qwen", "qwq", "tongyi"},
		DocsURL:       "https://help.aliyun.com/zh/model-studio/developer-reference/compatibility-of-openai-with-dashscope",
		Cost:          0.8, Quality: 0.84, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "minimax", Label: "MiniMax", Vendor: "MiniMax", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.minimax.chat/v1",
		APIKeyEnv:     "MINIMAX_API_KEY",
		ModelPrefixes: []string{"minimax", "abab"},
		DocsURL:       "https://platform.minimaxi.com/document/guides/chat-model/V2",
		Cost:          1.0, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "baichuan", Label: "百川智能", Vendor: "百川智能", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.baichuan-ai.com/v1",
		APIKeyEnv:     "BAICHUAN_API_KEY",
		ModelPrefixes: []string{"baichuan"},
		DocsURL:       "https://platform.baichuan-ai.com/docs/api",
		Cost:          0.6, Quality: 0.78, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "hunyuan", Label: "腾讯混元", Vendor: "腾讯云", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.hunyuan.cloud.tencent.com/v1",
		APIKeyEnv:     "HUNYUAN_API_KEY",
		ModelPrefixes: []string{"hunyuan"},
		DocsURL:       "https://cloud.tencent.com/document/product/1729/111007",
		Cost:          0.7, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "stepfun", Label: "阶跃星辰 Step", Vendor: "阶跃星辰", Category: llmProviderCategoryChina,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.stepfun.com/v1",
		APIKeyEnv:     "STEPFUN_API_KEY",
		ModelPrefixes: []string{"step"},
		DocsURL:       "https://platform.stepfun.com/docs/overview/concept",
		Cost:          0.8, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},

	// ── 聚合网关 ──
	{
		ID: "openrouter", Label: "OpenRouter", Vendor: "OpenRouter", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindOpenAI, BaseURL: "https://openrouter.ai/api/v1",
		APIKeyEnv: "OPENROUTER_API_KEY", APIKeyPrefix: "sk-or-",
		ModelPrefixes: []string{"openrouter/"},
		DocsURL:       "https://openrouter.ai/docs/api-reference/overview",
		Cost:          5.0, Quality: 0.85, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "siliconflow", Label: "硅基流动 SiliconFlow", Vendor: "SiliconFlow", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindOpenAI, BaseURL: "https://api.siliconflow.cn/v1",
		APIKeyEnv: "SILICONFLOW_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{"Qwen/", "deepseek-ai/", "THUDM/", "Pro/"},
		DocsURL:       "https://docs.siliconflow.cn/reference/chat-completions-1",
		Cost:          0.5, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "oneapi", Label: "One API / New API 自建网关", Vendor: "自建网关", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindOpenAI, BaseURL: "",
		APIKeyEnv: "ONEAPI_API_KEY", APIKeyPrefix: "sk-",
		ModelPrefixes: []string{},
		DocsURL:       "https://github.com/songquanpeng/one-api",
		Cost:          5.0, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		// OpenCode Zen：OpenCode 官方模型网关（按量计费）。key 从 https://opencode.ai/auth 获取，
		// 认证为 `Authorization: Bearer`（OpenAI 兼容），模型可由 {base}/models 自动发现。
		// model_prefixes 留空：该网关直通各家模型（gpt-5.4 / claude-sonnet-5 / deepseek-v4-pro …），
		// 设前缀会抢走其它直连 provider 的模型路由；改由 provider_hint / 租户路由显式指定。
		ID: "opencode", Label: "OpenCode Zen", Vendor: "OpenCode", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindOpenAI, BaseURL: "https://opencode.ai/zen/v1",
		APIKeyEnv: "OPENCODE_API_KEY", APIKeyPrefix: "",
		ModelPrefixes: []string{},
		DocsURL:       "https://opencode.ai/docs/zen/",
		Cost:          2.0, Quality: 0.88, RequiresKey: true, ModelDiscovery: true,
	},
	{
		// OpenCode Zen 的 Anthropic 协议端点（Messages API）；SDK 会拼 /v1/messages。
		ID: "opencode-anthropic", Label: "OpenCode Zen（Anthropic 协议）", Vendor: "OpenCode", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindAnthropic, BaseURL: "https://opencode.ai/zen",
		APIKeyEnv: "OPENCODE_API_KEY", APIKeyPrefix: "",
		ModelPrefixes: []string{},
		DocsURL:       "https://opencode.ai/docs/zen/",
		Cost:          2.0, Quality: 0.88, RequiresKey: true, ModelDiscovery: false,
	},
	{
		// OpenCode Go：$10/月订阅（低成本的开放模型集）。官方要求客户端发稳定会话头
		// `x-opencode-session`（用于路由与 prompt 缓存）—— Chiron 目前未发送该头，
		// 不影响可用性，仅路由/缓存次优；如需补上见 docs/service-providers.md。
		ID: "opencode-go", Label: "OpenCode Go（订阅）", Vendor: "OpenCode", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindOpenAI, BaseURL: "https://opencode.ai/zen/go/v1",
		APIKeyEnv: "OPENCODE_GO_API_KEY", APIKeyPrefix: "",
		ModelPrefixes: []string{},
		DocsURL:       "https://opencode.ai/docs/go/",
		Cost:          0.4, Quality: 0.85, RequiresKey: true, ModelDiscovery: true,
	},
	{
		// OpenCode Go 的 Anthropic 协议端点。
		ID: "opencode-go-anthropic", Label: "OpenCode Go（Anthropic 协议）", Vendor: "OpenCode", Category: llmProviderCategoryAggregator,
		Kind: llmProviderKindAnthropic, BaseURL: "https://opencode.ai/zen/go",
		APIKeyEnv: "OPENCODE_GO_API_KEY", APIKeyPrefix: "",
		ModelPrefixes: []string{},
		DocsURL:       "https://opencode.ai/docs/go/",
		Cost:          0.4, Quality: 0.85, RequiresKey: true, ModelDiscovery: false,
	},

	// ── 自托管推理 ──
	{
		ID: "ollama", Label: "Ollama（本地）", Vendor: "Ollama", Category: llmProviderCategorySelfHosted,
		Kind: llmProviderKindOpenAI, BaseURL: "http://localhost:11434/v1",
		APIKeyEnv:     "OLLAMA_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "https://github.com/ollama/ollama/blob/main/docs/openai.md",
		Cost:          0.0, Quality: 0.60, RequiresKey: false, ModelDiscovery: true,
	},
	{
		ID: "vllm", Label: "vLLM（自托管）", Vendor: "vLLM", Category: llmProviderCategorySelfHosted,
		Kind: llmProviderKindOpenAI, BaseURL: "http://localhost:8000/v1",
		APIKeyEnv:     "VLLM_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
		Cost:          0.0, Quality: 0.62, RequiresKey: false, ModelDiscovery: true,
	},
	{
		ID: "lmstudio", Label: "LM Studio（本地）", Vendor: "LM Studio", Category: llmProviderCategorySelfHosted,
		Kind: llmProviderKindOpenAI, BaseURL: "http://localhost:1234/v1",
		APIKeyEnv:     "LMSTUDIO_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "https://lmstudio.ai/docs/app/api",
		Cost:          0.0, Quality: 0.58, RequiresKey: false, ModelDiscovery: true,
	},

	// ── 自定义 ──
	{
		ID: "custom", Label: "自定义（OpenAI 兼容）", Vendor: "自定义", Category: llmProviderCategorySelfHosted,
		Kind: llmProviderKindOpenAI, BaseURL: "",
		APIKeyEnv:     "CUSTOM_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "",
		Cost:          5.0, Quality: 0.80, RequiresKey: true, ModelDiscovery: true,
	},
	{
		ID: "custom-anthropic", Label: "自定义（Anthropic 协议）", Vendor: "自定义", Category: llmProviderCategorySelfHosted,
		Kind: llmProviderKindAnthropic, BaseURL: "",
		APIKeyEnv:     "CUSTOM_ANTHROPIC_API_KEY",
		ModelPrefixes: []string{},
		DocsURL:       "",
		Cost:          3.0, Quality: 0.85, RequiresKey: true, ModelDiscovery: false,
	},
}

// llmProviderPresets 返回内建目录（只读，调用方不得修改）。
func llmProviderPresets() []llmProviderPreset { return llmProviderCatalog }

// llmProviderPresetByID 按 id 查目录项。
func llmProviderPresetByID(id string) (llmProviderPreset, bool) {
	id = strings.ToLower(strings.TrimSpace(id))
	for _, p := range llmProviderCatalog {
		if p.ID == id {
			return p, true
		}
	}
	return llmProviderPreset{}, false
}

// llmProviderBaseURLKey 返回端点覆盖在 system_settings(python 分类)中的键名。
func llmProviderBaseURLKey(id string) string { return id + "_base_url" }

// llmProviderDefaultBaseURL 返回目录默认端点（无则空字符串）。
func llmProviderDefaultBaseURL(id string) string {
	if p, ok := llmProviderPresetByID(id); ok {
		return p.BaseURL
	}
	return ""
}

// llmProviderSupportsModelDiscovery 判断该 provider 是否默认做 /v1/models 发现。
// 未收录的自定义 provider 视为支持（只要配置了 base_url）。
func llmProviderSupportsModelDiscovery(id string) bool {
	if p, ok := llmProviderPresetByID(id); ok {
		return p.ModelDiscovery
	}
	return true
}

// llmProviderCatalogPayload 是下发给引擎的目录载荷（engine-config 的
// llm_provider_catalog 键），引擎据此注册 provider。
func llmProviderCatalogPayload() []llmProviderPreset { return llmProviderCatalog }

// llmSettingsString 解包 system_settings 中 JSON 编码的字符串值（"https://x" → https://x）。
func llmSettingsString(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "null" {
		return ""
	}
	var s string
	if json.Unmarshal([]byte(raw), &s) == nil {
		raw = s
	}
	return strings.TrimSpace(raw)
}

// llmProviderBaseURLOverrides 读取目录中各 provider 在 system_settings(python)的端点覆盖。
func llmProviderBaseURLOverrides(ctx context.Context) map[string]string {
	out := map[string]string{}
	if db.Pool == nil {
		return out
	}
	keys := make([]string, 0, len(llmProviderCatalog))
	for _, p := range llmProviderCatalog {
		keys = append(keys, llmProviderBaseURLKey(p.ID))
	}
	rows, err := db.Pool.Query(ctx,
		`SELECT key, value::text FROM system_settings WHERE category = 'python' AND key = ANY($1)`, keys)
	if err != nil {
		slog.Debug("provider base url overrides load failed", "error", err)
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var key, raw string
		if rows.Scan(&key, &raw) != nil {
			continue
		}
		if v := strings.TrimRight(llmSettingsString(raw), "/"); v != "" {
			out[strings.TrimSuffix(key, "_base_url")] = v
		}
	}
	return out
}

// llmProviderKeyCounts 统计各 provider 的密钥条数（配置状态）。
func llmProviderKeyCounts(ctx context.Context) map[string]int {
	out := map[string]int{}
	if db.Pool == nil {
		return out
	}
	rows, err := db.Pool.Query(ctx, `SELECT provider, COUNT(*) FROM llm_provider_keys GROUP BY provider`)
	if err != nil {
		slog.Debug("provider key counts load failed", "error", err)
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var provider string
		var n int
		if rows.Scan(&provider, &n) != nil {
			continue
		}
		out[provider] = n
	}
	return out
}

// llmProviderEffectiveBaseURL 解析生效端点：DB 覆盖优先，其次目录默认。
func llmProviderEffectiveBaseURL(id, override string) string {
	if v := strings.TrimRight(strings.TrimSpace(override), "/"); v != "" {
		return v
	}
	return strings.TrimRight(llmProviderDefaultBaseURL(id), "/")
}

// validProviderBaseURL 校验端点形态（http/https + 有 host），避免误填写坏引擎配置。
func validProviderBaseURL(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return false
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return false
	}
	return u.Host != ""
}

// validLLMProviderName 校验 provider 名：会用作 system_settings 键前缀与 Redis keyset
// 后缀（llm:keys:{provider}），故限定小写字面集与长度。
func validLLMProviderName(name string) bool {
	if name == "" || len(name) > 64 {
		return false
	}
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
		default:
			return false
		}
	}
	return true
}

// ── HTTP handlers ──

// llmProviderView 是目录项 + 运行时状态的返回形态。
type llmProviderView struct {
	llmProviderPreset
	Configured       bool   `json:"configured"`
	KeyCount         int    `json:"key_count"`
	BaseURLOverride  string `json:"base_url_override"`
	EffectiveBaseURL string `json:"effective_base_url"`
}

// ListLLMProviders GET /v1/admin/llm-providers
// 返回完整目录与各 provider 的配置状态（key 条数 / 生效端点），供管理端选型。
func (h *AdminHandler) ListLLMProviders(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	overrides := llmProviderBaseURLOverrides(ctx)
	keyCounts := llmProviderKeyCounts(ctx)

	items := make([]llmProviderView, 0, len(llmProviderCatalog))
	for _, p := range llmProviderCatalog {
		override := overrides[p.ID]
		items = append(items, llmProviderView{
			llmProviderPreset: p,
			Configured:        keyCounts[p.ID] > 0,
			KeyCount:          keyCounts[p.ID],
			BaseURLOverride:   override,
			EffectiveBaseURL:  llmProviderEffectiveBaseURL(p.ID, override),
		})
	}
	OK(w, map[string]interface{}{"providers": items})
}

// SetLLMProviderConfig PUT /v1/admin/llm-providers/{id}
// 保存 provider 的端点覆盖（写 system_settings 的 python 分类 {id}_base_url）；
// base_url 传空即删除覆盖，回落目录默认。引擎重启或热更新后生效。
func (h *AdminHandler) SetLLMProviderConfig(w http.ResponseWriter, r *http.Request) {
	id := strings.ToLower(strings.TrimSpace(r.PathValue("id")))
	preset, found := llmProviderPresetByID(id)
	if !found {
		NotFound(w, "unknown provider: "+id)
		return
	}
	var body struct {
		BaseURL string `json:"base_url"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	base := strings.TrimSpace(body.BaseURL)
	if base != "" && !validProviderBaseURL(base) {
		BadRequest(w, "invalid base_url: must be an http(s) URL with a host")
		return
	}
	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	var userID string
	if claims := auth.GetClaims(r.Context()); claims != nil {
		userID = claims.ID
	}
	// nil 值 = 删除该键（回落目录默认）
	var value interface{}
	if base != "" {
		value = strings.TrimRight(base, "/")
	}
	config := map[string]interface{}{llmProviderBaseURLKey(id): value}
	if err := store.SaveConfig(r.Context(), "python", config, userID); err != nil {
		slog.Error("save provider base url failed", "provider", id, "error", err)
		InternalError(w, "failed to save provider config")
		return
	}
	OK(w, map[string]interface{}{
		"status":             "saved",
		"provider":           id,
		"kind":               preset.Kind,
		"base_url_override":  strings.TrimRight(base, "/"),
		"effective_base_url": llmProviderEffectiveBaseURL(id, base),
	})
}
