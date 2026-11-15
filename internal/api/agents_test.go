package api

import (
	"encoding/json"
	"testing"
)

// ── applyAgentBindings：Agent 上的绑定 → workbench_context 顶层 ──
//
// 这组断言守的是"绑了要真的生效"。Go 侧把 agent 的绑定提升到顶层，引擎才读得到：
// 引擎读的是顶层 kb_id / skill_names / plugin_names，不看 context["agent"] 里面。
// 不提升 = Agent 上配了插件/技能/知识库，派发时却完全不起作用（"静默失效"）。

func rawJSON(s string) json.RawMessage { return json.RawMessage(s) }

func TestApplyAgentBindings_PromotesAllThree(t *testing.T) {
	wb := map[string]any{}
	agent := &Agent{
		KbID:    "kb-1",
		Skills:  rawJSON(`["skill-a","skill-b"]`),
		Plugins: rawJSON(`["fs","gh"]`),
	}
	applyAgentBindings(wb, agent)

	if got := wb["kb_id"]; got != "kb-1" {
		t.Errorf("kb_id: want %q, got %v", "kb-1", got)
	}
	skills, ok := wb["skill_names"].([]string)
	if !ok || len(skills) != 2 || skills[0] != "skill-a" || skills[1] != "skill-b" {
		t.Errorf("skill_names: want [skill-a skill-b], got %v", wb["skill_names"])
	}
	plugins, ok := wb["plugin_names"].([]string)
	if !ok || len(plugins) != 2 || plugins[0] != "fs" || plugins[1] != "gh" {
		t.Errorf("plugin_names: want [fs gh], got %v", wb["plugin_names"])
	}
}

// 用户本次显式选的值优先于 Agent 上带的默认值 —— 三者都是同一个口径。
func TestApplyAgentBindings_ExplicitChoiceWins(t *testing.T) {
	wb := map[string]any{
		"kb_id":        "kb-explicit",
		"skill_names":  []string{"explicit-skill"},
		"plugin_names": []string{"explicit-plugin"},
	}
	agent := &Agent{
		KbID:    "kb-from-agent",
		Skills:  rawJSON(`["agent-skill"]`),
		Plugins: rawJSON(`["agent-plugin"]`),
	}
	applyAgentBindings(wb, agent)

	if got := wb["kb_id"]; got != "kb-explicit" {
		t.Errorf("kb_id 被覆盖了: want %q, got %v", "kb-explicit", got)
	}
	if s, _ := wb["skill_names"].([]string); len(s) != 1 || s[0] != "explicit-skill" {
		t.Errorf("skill_names 被覆盖了: got %v", wb["skill_names"])
	}
	if p, _ := wb["plugin_names"].([]string); len(p) != 1 || p[0] != "explicit-plugin" {
		t.Errorf("plugin_names 被覆盖了: got %v", wb["plugin_names"])
	}
}

// 空白 kb_id 视为"没选"，可以被 Agent 的补上。
func TestApplyAgentBindings_BlankKbIDIsTreatedAsUnset(t *testing.T) {
	for _, blank := range []string{"", "   ", "\t"} {
		wb := map[string]any{"kb_id": blank}
		applyAgentBindings(wb, &Agent{KbID: "kb-1"})
		if got := wb["kb_id"]; got != "kb-1" {
			t.Errorf("kb_id=%q 时未被填充: got %v", blank, got)
		}
	}
}

// Agent 没绑定的那一项不该产出空键 —— 空数组会被引擎当成"筛选到空集合"。
func TestApplyAgentBindings_UnsetBindingsProduceNoKeys(t *testing.T) {
	wb := map[string]any{}
	agent := &Agent{
		KbID:    "",
		Skills:  rawJSON(`[]`),
		Plugins: rawJSON(`[]`),
	}
	applyAgentBindings(wb, agent)

	for _, k := range []string{"kb_id", "skill_names", "plugin_names"} {
		if v, exists := wb[k]; exists {
			t.Errorf("%s 不该被写入，实际是 %v", k, v)
		}
	}
}

// nil / 空 context 与脏 JSON 都不能 panic：这是"坏 agent 不该让整条对话失败"的底线。
func TestApplyAgentBindings_Robustness(t *testing.T) {
	applyAgentBindings(nil, &Agent{KbID: "kb-1"}) // nil map
	applyAgentBindings(map[string]any{}, nil)     // nil agent

	wb := map[string]any{}
	applyAgentBindings(wb, &Agent{
		KbID:    "kb-1",
		Skills:  rawJSON(`not json`),
		Plugins: rawJSON(`null`),
	})
	if got := wb["kb_id"]; got != "kb-1" {
		t.Errorf("kb_id: want kb-1, got %v", got)
	}
	if _, exists := wb["skill_names"]; exists {
		t.Error("坏 JSON 不该产出 skill_names")
	}
	if _, exists := wb["plugin_names"]; exists {
		t.Error("null 不该产出 plugin_names")
	}
}

// ── agentPluginNames / agentSkillNames：同一套解析 ──

func TestAgentPluginNames(t *testing.T) {
	cases := []struct {
		name string
		in   json.RawMessage
		want int
	}{
		{"正常数组", rawJSON(`["fs","gh"]`), 2},
		{"空数组", rawJSON(`[]`), 0},
		{"null", rawJSON(`null`), 0},
		{"空值", nil, 0},
		{"脏数据", rawJSON(`{"a":1}`), 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := agentPluginNames(tc.in); len(got) != tc.want {
				t.Errorf("want %d 项, got %v", tc.want, got)
			}
		})
	}
}

// plugins 与 skills 走的是同一个解析函数（agentPluginNames 只是薄封装），
// 断言这一点是为了防止以后有人只改一边。
func TestAgentPluginNames_MatchesSkillNames(t *testing.T) {
	in := rawJSON(`["a","b","c"]`)
	got := agentPluginNames(in)
	want := agentSkillNames(in)
	if len(got) != len(want) {
		t.Fatalf("长度不一致: plugins=%v skills=%v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("第 %d 项不一致: plugins=%q skills=%q", i, got[i], want[i])
		}
	}
}

// ── contextIDList：多值 context 的读取口径（*_ids 优先，回退单值）──

// ── applyAgentDefaults：新建 Agent 的默认轮次 ──
//
// 这条断言存在的理由：`max_turns` 会被**落库**，之后经 `agentContextPayload` 下发给引擎，
// 覆盖掉引擎侧的全局默认。所以 Create 里的默认值写错，不是"兜底不生效"，而是把这条
// Agent 永久钉死在那个数字上 —— 历史缺陷正是硬编码 5，任何新建的 Agent 都只跑 5 轮，
// 且 5 轮后 checkpoint 续跑被夹到 `max_turns-1`、接不上。

func TestApplyAgentDefaults_MaxTurnsUsesSharedDefault(t *testing.T) {
	// 显式配过的不许被改
	kept := Agent{MaxTurns: 120, TimeoutSeconds: 300}
	applyAgentDefaults(&kept)
	if kept.MaxTurns != 120 || kept.TimeoutSeconds != 300 {
		t.Fatalf("显式配置被覆盖: %+v", kept)
	}

	// 未配（含脏的负数）⇒ 与 /submit、引擎侧同一个默认值
	for _, turns := range []int{0, -1} {
		a := Agent{MaxTurns: turns}
		applyAgentDefaults(&a)
		if a.MaxTurns != DefaultMaxTurns {
			t.Errorf("MaxTurns=%d 应回落到 DefaultMaxTurns=%d，实得 %d",
				turns, DefaultMaxTurns, a.MaxTurns)
		}
		if a.MaxTurns != 50 {
			t.Errorf("DefaultMaxTurns 须与引擎侧 DEFAULT_MAX_TURNS 一致（50），实为 %d",
				DefaultMaxTurns)
		}
		if a.TimeoutSeconds != 120 {
			t.Errorf("TimeoutSeconds 应回落 120，实得 %d", a.TimeoutSeconds)
		}
	}
}

func TestContextIDList(t *testing.T) {
	// 请求体经 DecodeJSON 后数组是 []any（真实链路），Go 侧手写的则是 []string。
	// 两种都必须认，否则同一份 context 在两条路径上解析出不同结果。
	shapes := []struct {
		name string
		val  any
	}{
		{"[]any（JSON 反序列化后的常态）", []any{"a", "b"}},
		{"[]string（Go 侧手写）", []string{"a", "b"}},
	}

	for _, sh := range shapes {
		t.Run("多值优先于单值/"+sh.name, func(t *testing.T) {
			wb := map[string]any{"agent_ids": sh.val, "agent_id": "fallback"}
			got := contextIDList(wb, "agent")
			if len(got) != 2 || got[0] != "a" || got[1] != "b" {
				t.Errorf("want [a b], got %v", got)
			}
		})
	}

	t.Run("回退到单值", func(t *testing.T) {
		wb := map[string]any{"agent_id": "only"}
		if got := contextIDList(wb, "agent"); len(got) != 1 || got[0] != "only" {
			t.Errorf("want [only], got %v", got)
		}
	})

	t.Run("多值里的空白项被跳过", func(t *testing.T) {
		wb := map[string]any{"agent_ids": []any{"a", "", "  ", "b"}}
		got := contextIDList(wb, "agent")
		if len(got) != 2 || got[0] != "a" || got[1] != "b" {
			t.Errorf("want [a b], got %v", got)
		}
	})

	t.Run("多值全为空白时回退单值", func(t *testing.T) {
		wb := map[string]any{"agent_ids": []any{"", " "}, "agent_id": "fallback"}
		if got := contextIDList(wb, "agent"); len(got) != 1 || got[0] != "fallback" {
			t.Errorf("want [fallback], got %v", got)
		}
	})

	t.Run("都不要时为空", func(t *testing.T) {
		if got := contextIDList(map[string]any{}, "agent"); len(got) != 0 {
			t.Errorf("want empty, got %v", got)
		}
	})
}
