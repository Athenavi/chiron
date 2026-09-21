package api

import "testing"

// 目录完整性：id 唯一且合法、协议合法、默认端点形态正确、需 key 的项声明了 env 名。
// 目录是前端选型 / 引擎注册 / 模型发现的共同事实源，故对其施加结构约束。
func TestLLMProviderCatalogIntegrity(t *testing.T) {
	seen := map[string]bool{}
	for _, p := range llmProviderPresets() {
		if p.ID == "" {
			t.Fatal("目录存在空 id 项")
		}
		if seen[p.ID] {
			t.Fatalf("目录存在重复 id: %q", p.ID)
		}
		seen[p.ID] = true
		if !validLLMProviderName(p.ID) {
			t.Errorf("provider %q 不符合命名规则（会用作 settings 键前缀 / keyset 后缀）", p.ID)
		}
		if p.Kind != llmProviderKindOpenAI && p.Kind != llmProviderKindAnthropic {
			t.Errorf("provider %q 协议非法: %q", p.ID, p.Kind)
		}
		if p.Label == "" {
			t.Errorf("provider %q 缺少展示名", p.ID)
		}
		if p.BaseURL != "" && !validProviderBaseURL(p.BaseURL) {
			t.Errorf("provider %q 默认端点非法: %q", p.ID, p.BaseURL)
		}
		if p.RequiresKey && p.APIKeyEnv == "" {
			t.Errorf("provider %q 需要 key 但未声明 APIKeyEnv", p.ID)
		}
		if p.Cost < 0 || p.Quality < 0 || p.Quality > 1 {
			t.Errorf("provider %q 成本/质量分越界: cost=%v quality=%v", p.ID, p.Cost, p.Quality)
		}
	}
	// 关键 provider 必须在目录内：前两项是历史内建支持，其余覆盖国内/聚合/自托管
	for _, id := range []string{"openai", "anthropic", "deepseek", "moonshot", "zhipu", "dashscope", "openrouter", "opencode", "opencode-go", "ollama", "custom"} {
		if _, ok := llmProviderPresetByID(id); !ok {
			t.Errorf("目录缺少 provider %q", id)
		}
	}
}

func TestLLMProviderPresetLookup(t *testing.T) {
	if p, ok := llmProviderPresetByID(" Moonshot "); !ok || p.ID != "moonshot" {
		t.Errorf("id 查表应大小写/空白无关，得到 ok=%v p=%q", ok, p.ID)
	}
	if _, ok := llmProviderPresetByID("not-a-provider"); ok {
		t.Error("未收录的 id 不应命中目录")
	}
	// anthropic 原生协议不做模型发现；openai 兼容默认做
	if llmProviderSupportsModelDiscovery("anthropic") {
		t.Error("anthropic 不应默认做 /models 发现")
	}
	if !llmProviderSupportsModelDiscovery("deepseek") {
		t.Error("deepseek 应支持 /models 发现")
	}
	// 未收录的自定义 provider 视为支持（只要配置了端点）
	if !llmProviderSupportsModelDiscovery("my-gateway") {
		t.Error("目录外 provider 应默认允许模型发现")
	}
}

func TestLLMProviderEffectiveBaseURL(t *testing.T) {
	cases := []struct {
		name     string
		id       string
		override string
		want     string
	}{
		{"无覆盖回落目录默认", "deepseek", "", "https://api.deepseek.com"},
		{"覆盖优先于目录默认", "deepseek", "https://gw.internal/v1/", "https://gw.internal/v1"},
		{"仅空白覆盖视为未设置", "openai", "   ", "https://api.openai.com/v1"},
		{"目录无默认时返回空", "oneapi", "", ""},
		{"目录无默认但可覆盖", "oneapi", "http://oneapi.local/v1", "http://oneapi.local/v1"},
	}
	for _, c := range cases {
		if got := llmProviderEffectiveBaseURL(c.id, c.override); got != c.want {
			t.Errorf("%s: effectiveBaseURL(%q, %q) = %q, want %q", c.name, c.id, c.override, got, c.want)
		}
	}
}

func TestValidProviderBaseURL(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"https://api.openai.com/v1", true},
		{"http://localhost:11434/v1", true},
		{"ftp://api.example.com", false},
		{"api.example.com", false},
		{"", false},
		{"https://", false},
	}
	for _, c := range cases {
		if got := validProviderBaseURL(c.in); got != c.want {
			t.Errorf("validProviderBaseURL(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

func TestValidLLMProviderName(t *testing.T) {
	cases := []struct {
		in   string
		want bool
	}{
		{"moonshot", true},
		{"my-gateway.v2", true},
		{"gpt_4", true},
		{"Moonshot", false},   // 必须小写（键前缀一致）
		{"my gateway", false}, // 不允许空白
		{"../etc", false},     // 不允许路径穿越字符
		{"llm:keys", false},   // 不允许冒号（keyset 前缀分隔符）
		{"", false},
	}
	for _, c := range cases {
		if got := validLLMProviderName(c.in); got != c.want {
			t.Errorf("validLLMProviderName(%q) = %v, want %v", c.in, got, c.want)
		}
	}
	// 超长（>64）拒绝
	long := make([]byte, 65)
	for i := range long {
		long[i] = 'a'
	}
	if validLLMProviderName(string(long)) {
		t.Error("超过 64 字符的 provider 名应被拒绝")
	}
}

func TestLLMSettingsString(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{`"https://api.deepseek.com"`, "https://api.deepseek.com"}, // JSON 字符串解包
		{"https://api.deepseek.com", "https://api.deepseek.com"},   // 裸串
		{"null", ""},
		{`""`, ""},
		{"  ", ""},
	}
	for _, c := range cases {
		if got := llmSettingsString(c.in); got != c.want {
			t.Errorf("llmSettingsString(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestLLMProviderBaseURLKey(t *testing.T) {
	// 键名与 model_discovery.go / 引擎侧 provider_overrides 的约定一致
	if got := llmProviderBaseURLKey("moonshot"); got != "moonshot_base_url" {
		t.Errorf("llmProviderBaseURLKey = %q, want moonshot_base_url", got)
	}
}
