package api

import (
	"strings"
	"testing"
)

// ── Agent 列表的可见性口径（C1-5）──
//
// roadmap §12.4 第 5 步：「公开资产归团队、个人资产归个人」。落地在查询条件上
// 就两条：`tenant_id = $1`（租户隔离）+ `user_id = $2 OR visibility = 'tenant'`
// （自己的全可见，别人的只有共享的可见）。
//
// 这组断言守的是一个此前真实存在的半残行为：Detail 早就按上面那个条件放行，
// 而 List 只按 `user_id` 过滤 —— 于是团队共享的 Agent 能凭 id 打开、却在列表里
// 找不到。用户没法发现它，等于没共享。

func TestAgentListQuery_SharedAgentsVisible(t *testing.T) {
	if !strings.Contains(agentListQuery, "visibility = 'tenant'") {
		t.Errorf("列表查询缺少 visibility = 'tenant'，团队共享的 Agent 在列表里看不到:\n%s",
			agentListQuery)
	}
	if !strings.Contains(agentListQuery, "user_id = $2") {
		t.Errorf("列表查询缺少 user_id 条件，自己的私有 Agent 不该靠 visibility 兜:\n%s",
			agentListQuery)
	}
	// 两个条件是 OR：只写 AND 会让"自己的私有 agent"也消失。
	if !strings.Contains(agentListQuery, "(user_id = $2 OR visibility = 'tenant')") {
		t.Errorf("可见性条件必须是 OR 组合，否则私有 Agent 会从列表里消失:\n%s", agentListQuery)
	}
}

// 租户隔离不能被共享条件冲掉：一旦漏掉 tenant_id，任何租户都能看到别人的共享资产。
func TestAgentListQuery_TenantScoped(t *testing.T) {
	if !strings.Contains(agentListQuery, "tenant_id = $1") {
		t.Errorf("列表查询必须带 tenant_id = $1，否则跨租户泄漏:\n%s", agentListQuery)
	}
}

// 子 Agent Profile 不进对话列表 —— 这条约束原先写在 handler 里的注释中，
// 抽成常量后必须一并守住，否则重构时极易丢掉。
func TestAgentListQuery_ExcludesSubagents(t *testing.T) {
	if !strings.Contains(agentListQuery, "kind IS NULL OR kind = 'chat'") {
		t.Errorf("列表必须排除 kind='subagent'，否则子 Agent 会出现在对话列表里:\n%s",
			agentListQuery)
	}
}

// json 列的类型对齐是硬要求：写成 ::jsonb 或省略 ::json 会在运行时报
// "could not convert type jsonb to json"，编译期和单测都发现不了。
func TestAgentListQuery_JSONCastsAreJSON(t *testing.T) {
	if strings.Contains(agentListQuery, "::jsonb") {
		t.Errorf("agents 的 json 列不能用 ::jsonb，运行时会报类型转换错误:\n%s", agentListQuery)
	}
	for _, col := range []string{"tools", "llm_config", "skills", "plugins", "workflows"} {
		want := "COALESCE(" + col + ",'"
		if !strings.Contains(agentListQuery, want) {
			t.Errorf("列 %s 缺少带类型的 COALESCE 缺省值:\n%s", col, agentListQuery)
		}
	}
}

// 列表要带出归属与可见性，前端才能区分「我的」与「团队共享的」并把后者设为只读。
func TestAgentListQuery_SelectsOwnershipColumns(t *testing.T) {
	for _, col := range []string{"user_id::text", "visibility"} {
		if !strings.Contains(agentListQuery, col) {
			t.Errorf("列表查询未选 %q，前端无法区分自己的与团队共享的:\n%s", col, agentListQuery)
		}
	}
}
