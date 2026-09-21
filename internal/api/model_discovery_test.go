package api

import "testing"

// TestInferContextWindow 锁死 P1-f 的行为：
// 模型发现写回 llm_models 时必须给出**可用**的上下文窗口（此前写死 0，
// 导致前端上下文环分母恒为 0、环永远不显示），且推断不出时宁可返回 0。
func TestInferContextWindow(t *testing.T) {
	cases := []struct {
		model string
		want  int
	}{
		// 名字里显式声明窗口 —— 优先级最高
		{"claude-sonnet-4-128k", 128000},
		{"qwen3-32k", 32000},
		{"some-model-1m", 1000000},
		{"openai/gpt-4o_16k", 16000},
		// 已知家族（名字未声明时的保守估计）
		{"claude-sonnet-4-20250514", 200000},
		{"gpt-4o", 128000},
		{"gpt-4.1-mini", 1000000},
		{"gemini-2.5-pro", 1000000},
		{"deepseek-chat", 128000},
		{"kimi-k2-0905-preview", 128000},
		{"glm-4.6", 128000},
		{"qwen-max", 131072},
		{"llama-3.3-70b", 131072},
		{"grok-4", 131072},
		// 无法判断：返回 0，由前端决定不显示上下文环（不给误导性的分母）
		{"opencode-zen-unknown", 0},
		{"", 0},
	}
	for _, c := range cases {
		if got := inferContextWindow(c.model); got != c.want {
			t.Errorf("inferContextWindow(%q) = %d, want %d", c.model, got, c.want)
		}
	}
}
