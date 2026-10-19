package config

import (
	"strings"
	"testing"
)

// M20：破坏性改名的**唯一护栏**。旧名不再被任何一侧读取，所以"设了却不生效"必须
// 在启动时就变成显式失败 —— 否则部署者会带着一个看起来生效的旧配置跑下去。

func TestValidateRenamedAgentEnvPassesWhenClean(t *testing.T) {
	for old := range renamedAgentEnv {
		t.Setenv(old, "")
	}

	if err := ValidateRenamedAgentEnv(); err != nil {
		t.Fatalf("空值/未设置不应被当成旧配置, got: %v", err)
	}
}

func TestValidateRenamedAgentEnvRejectsEachOldName(t *testing.T) {
	for old := range renamedAgentEnv {
		for old2 := range renamedAgentEnv {
			t.Setenv(old2, "")
		}
		t.Setenv(old, "8192")

		err := ValidateRenamedAgentEnv()
		if err == nil {
			t.Fatalf("旧名 %s 被设置为非空值时必须拒绝启动", old)
		}
		if !strings.Contains(err.Error(), old) {
			t.Fatalf("错误信息要点名旧变量 %s: %v", old, err)
		}
	}
}

func TestValidateRenamedAgentEnvPointsAtTheNewNames(t *testing.T) {
	for old := range renamedAgentEnv {
		t.Setenv(old, "")
	}
	t.Setenv("AGENT_MAX_TOKENS", "8192")
	t.Setenv("AGENT_MAX_TURNS", "10")

	err := ValidateRenamedAgentEnv()
	if err == nil {
		t.Fatal("expected an error")
	}
	msg := err.Error()
	// 两侧的新名都要出现在提示里 —— 只给一个会让部署者猜"我该换成哪个"。
	for _, want := range []string{
		"AGENT_MAX_OUTPUT_TOKENS",
		"AGENT_RUN_MAX_TOKENS",
		"AGENT_DEFAULT_MAX_TURNS",
		"AGENT_RUN_MAX_TURNS",
	} {
		if !strings.Contains(msg, want) {
			t.Fatalf("迁移提示缺 %s: %v", want, msg)
		}
	}
}

func TestValidateRenamedAgentEnvRejectsUnparsableValue(t *testing.T) {
	for old := range renamedAgentEnv {
		t.Setenv(old, "")
	}
	// 判据只看"是否被设置为非空"，不看能否解析成数字：写错的旧名同样要拦，
	// 否则部署者以为它生效了。
	t.Setenv("AGENT_MAX_TOKENS", "not-a-number")

	if err := ValidateRenamedAgentEnv(); err == nil {
		t.Fatal("无法解析的旧名同样必须拒绝启动")
	}
}

func TestRenamedAgentEnvCoversTheKnownNames(t *testing.T) {
	// 为什么要有这条：上一条测试**遍历 map 本身** —— 从 map 里删掉一项，它的循环也就
	// 少跑一项，测试仍然全绿。而"删掉一项"恰恰会让那个旧名静默通过启动校验。
	want := []string{"AGENT_MAX_TOKENS", "AGENT_MAX_TURNS", "AGENT_CONTEXT_LIMIT"}
	for _, name := range want {
		if _, ok := renamedAgentEnv[name]; !ok {
			t.Fatalf("旧名清单缺 %s —— 该旧名会静默通过启动校验", name)
		}
	}
	if len(renamedAgentEnv) != len(want) {
		t.Fatalf("旧名清单多了未登记的项（会误拒合法配置）: %#v", renamedAgentEnv)
	}
}

func TestAgentEnvDefaultsUseTheNewNamesOnly(t *testing.T) {
	// 新名字段有默认值（漏配也不会变成 0 把预算关掉），旧名不在任何默认路径上。
	cfg, err := Load()
	if err != nil {
		t.Skipf("Load 需要完整部署环境: %v", err)
	}
	if cfg.AgentMaxOutputTokens != 8192 {
		t.Fatalf("AGENT_MAX_OUTPUT_TOKENS 默认应为 8192, got %d", cfg.AgentMaxOutputTokens)
	}
	if cfg.AgentDefaultMaxTurns != 5 {
		t.Fatalf("AGENT_DEFAULT_MAX_TURNS 默认应为 5, got %d", cfg.AgentDefaultMaxTurns)
	}
}
