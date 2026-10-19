package api

import (
	"testing"

	"github.com/athenavi/chiron/config"
)

// M20：后台「系统设置」的 agent.max_tokens / agent.max_turns 必须**真的**被读取。
//
// 这一批的起因不是"值算错了"，而是"写进 config.Config 却没有任何读取点" ——
// 所以断言要打在"取值优先级"与"注入行为"上，而不是打在结构体字段上。

func TestAgentDefaultsPrefersConfiguredValues(t *testing.T) {
	h := &SubmitHandler{agentCfg: &config.Config{
		AgentMaxOutputTokens: 2048,
		AgentDefaultMaxTurns: 7,
	}}

	outTokens, turns := h.agentDefaults()

	if outTokens != 2048 {
		t.Fatalf("agent.max_tokens 未被读取: got %d", outTokens)
	}
	if turns != 7 {
		t.Fatalf("agent.max_turns 未被读取: got %d", turns)
	}
}

func TestAgentDefaultsFallsBackWhenMissing(t *testing.T) {
	// nil cfg（测试/嵌入场景）与非法值（<=0）都必须回落到包内默认，
	// 不能用"0 = 关掉预算"这种隐式语义把一次漏配变成无限输出。
	for _, h := range []*SubmitHandler{
		{},
		{agentCfg: &config.Config{AgentMaxOutputTokens: 0, AgentDefaultMaxTurns: -1}},
	} {
		outTokens, turns := h.agentDefaults()
		if outTokens != DefaultMaxOutputTokens || turns != DefaultMaxTurns {
			t.Fatalf("want (%d,%d), got (%d,%d)",
				DefaultMaxOutputTokens, DefaultMaxTurns, outTokens, turns)
		}
	}
}

func TestApplyMaxOutputDefaultInjectsWhenAbsent(t *testing.T) {
	llmConfig := map[string]interface{}{"model": "m"}

	applyMaxOutputDefault(llmConfig, 2048)

	if got, ok := llmConfig["max_tokens"]; !ok || got != 2048 {
		t.Fatalf("后台值必须注入到发给引擎的 llm_config: %#v", llmConfig)
	}
}

func TestApplyMaxOutputDefaultKeepsExplicitValue(t *testing.T) {
	// 前端 / Agent 档案显式选了单次上限时，后台全局默认不能盖掉它。
	llmConfig := map[string]interface{}{"max_tokens": float64(1234)}

	applyMaxOutputDefault(llmConfig, 2048)

	if got := llmConfig["max_tokens"]; got != float64(1234) {
		t.Fatalf("显式值被覆盖: %#v", got)
	}
}
