package api

// 本文件由 agents.go 按职责外移而来（Agent 配置解析与上下文装配，全为纯逻辑）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"context"
	"encoding/json"
	"log/slog"
	"strconv"
	"strings"
)

func trimSpace(s string) string { return strings.TrimSpace(s) }
func itoa(n int) string { return strconv.Itoa(n) }
// jsonOrEmptyArray 把可能为空的 JSON 写进 json 列：空值写 '[]'，
// 因为 json/jsonb 都不接受空字符串（""::json 会直接报错）。
func jsonOrEmptyArray(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return "[]"
	}
	return string(raw)
}
// agentSkillNames 解析 Agent 绑定的技能名（json 字符串数组）；
// 未绑定或格式不对时返回 nil，等价于"不筛选技能"（引擎侧沿用全部已安装技能）。
func agentSkillNames(skills json.RawMessage) []string {
	if len(skills) == 0 {
		return nil
	}
	var names []string
	if err := json.Unmarshal(skills, &names); err != nil {
		return nil
	}
	return names
}
// agentContextPayload 把 Agent 组装成透传给引擎的 context.agent。
//
// 字段名必须与 python 侧读取处一致（app/api/unified_executor.py 的 _execute_via_agent、
// app/main.py 的 workbench_context 消费）。此前只带 system_prompt / max_turns / model
// —— 也就是只带人格不带能力：Agent 自带的工具、知识库、技能全部丢失，
// 用户选了 Agent 却发现它"不会用自己的工具"。
func agentContextPayload(a *Agent) map[string]any {
	if a == nil {
		return nil
	}
	payload := map[string]any{}
	if a.ID != "" {
		payload["id"] = a.ID
	}
	if a.Name != "" {
		payload["name"] = a.Name
	}
	if a.SystemPrompt != "" {
		payload["system_prompt"] = a.SystemPrompt
	}
	if a.MaxTurns > 0 {
		payload["max_turns"] = a.MaxTurns
	}
	if model := agentModel(a.LLMConfig); model != "" {
		payload["model"] = model
	}
	if tools := decodeJSONArray(a.Tools); len(tools) > 0 {
		payload["tools"] = tools
	}
	if a.KbID != "" {
		payload["kb_id"] = a.KbID
	}
	if skills := agentSkillNames(a.Skills); len(skills) > 0 {
		payload["skills"] = skills
	}
	if plugins := agentPluginNames(a.Plugins); len(plugins) > 0 {
		payload["plugins"] = plugins
	}
	if workflows := agentWorkflowIDs(a.Workflows); len(workflows) > 0 {
		payload["workflows"] = workflows
	}
	return payload
}
// agentPluginNames 解析 Agent 绑定的插件名（json 字符串数组）；未绑定或格式不对时
// 返回 nil，等价于"不筛选插件"。与 agentSkillNames 同构 —— 两者都是字符串数组列。
func agentPluginNames(plugins json.RawMessage) []string {
	return agentSkillNames(plugins)
}
// agentWorkflowIDs 解析 Agent 绑定的工作流 id（json 字符串数组）；未绑定或格式不对时
// 返回 nil，等价于"不绑定工作流"。与前两者同构 —— 都是字符串数组列。
func agentWorkflowIDs(workflows json.RawMessage) []string {
	return agentSkillNames(workflows)
}
// decodeJSONArray 解析 json 数组列；空值或格式不对时返回 nil（表示"不传该字段"）
func decodeJSONArray(raw json.RawMessage) []any {
	if len(raw) == 0 {
		return nil
	}
	var out []any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}
// agentModel 从 llm_config（json）里取模型名
func agentModel(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return ""
	}
	return llmString(cfg, "model", "")
}
// resolveAgentContext 在"只带 agent_id、没带 agent 配置"时补全 context.agent。
//
// 幂等：已有完整 agent 对象时不动。查不到 Agent（不存在/非本人/非共享）时保持原样并
// 记日志 —— 让请求继续走，只是"这次没带 Agent"，与改动前的行为一致；
// 不因为一个坏 agent_id 让整条对话失败。
func resolveAgentContext(ctx context.Context, wbCtx map[string]any, tenantID int16, userID string) {
	if wbCtx == nil {
		return
	}
	if existing, ok := wbCtx["agent"].(map[string]any); ok && len(existing) > 0 {
		return
	}
	agentID, _ := wbCtx["agent_id"].(string)
	if strings.TrimSpace(agentID) == "" {
		return
	}
	agent, err := loadAgent(ctx, tenantID, userID, agentID)
	if err != nil {
		slog.Warn("workbench: agent context fallback failed", "agent_id", agentID, "error", err)
		return
	}
	if payload := agentContextPayload(agent); payload != nil {
		wbCtx["agent"] = payload
		applyAgentBindings(wbCtx, agent)
		// 多选的其余 Agent → "可委派的专家"。一个对话只能有一个人格（首选的 Agent），
		// 但可以有多个可请教的专家 —— 这正是"多选 Agent"的语义。
		if experts := loadExpertPayloads(ctx, wbCtx, tenantID, userID); len(experts) > 0 {
			payload["experts"] = experts
		}
	}
}
// applyAgentBindings 把 Agent 自带的绑定提升到 workbench_context 顶层。
//
// 引擎读的是**顶层** kb_id / skill_names / plugin_names：
//   - kb_id、skill_names 见 python-engine/app/agent/prompt_engine.py
//   - plugin_names 见 python-engine/app/agent/runtime.py 的 _restrict_tools_to_plugins
//
// 只放在 context["agent"] 里引擎读不到，等于"绑了没生效"。用户本次显式选的值优先，
// Agent 上带的只是默认值（所以非空就不覆盖）。
//
// 从 resolveAgentContext 里抽出来是为了能直接单测：原来内联在那一层，而那一层要先
// 查库（loadAgent），测它就得先起一个带完整 schema 的 PostgreSQL。
func applyAgentBindings(wbCtx map[string]any, agent *Agent) {
	if wbCtx == nil || agent == nil {
		return
	}
	if kb, _ := wbCtx["kb_id"].(string); strings.TrimSpace(kb) == "" && agent.KbID != "" {
		wbCtx["kb_id"] = agent.KbID
	}
	if _, hasSkills := wbCtx["skill_names"]; !hasSkills {
		if skills := agentSkillNames(agent.Skills); len(skills) > 0 {
			wbCtx["skill_names"] = skills
		}
	}
	if _, hasPlugins := wbCtx["plugin_names"]; !hasPlugins {
		if plugins := agentPluginNames(agent.Plugins); len(plugins) > 0 {
			wbCtx["plugin_names"] = plugins
		}
	}
	// workflow_ids 与前三个同族：引擎侧由 selected_workflow_ids 读取（多值优先、
	// 单值回退），随后 _execute_via_workflow 按顺序执行 —— 多选即流水线。
	// 用户本次显式选的工作流优先，Agent 上带的只是默认值。
	if _, hasWorkflows := wbCtx["workflow_ids"]; !hasWorkflows {
		if workflows := agentWorkflowIDs(agent.Workflows); len(workflows) > 0 {
			wbCtx["workflow_ids"] = workflows
		}
	}
}
// loadExpertPayloads 取 agent_ids 里除首个之外的 Agent（主 Agent 之外的"可委派专家"）。
//
// 单个查不到就跳过：不该因为列表里一个坏 id 让其余专家一起丢失。专家只带
// 展示与人格所需字段，不带工具集 —— 委派出去的 child 复用主对话的工具预算。
func loadExpertPayloads(ctx context.Context, wbCtx map[string]any, tenantID int16, userID string) []map[string]any {
	ids := contextIDList(wbCtx, "agent")
	if len(ids) < 2 {
		return nil
	}
	experts := make([]map[string]any, 0, len(ids)-1)
	for _, id := range ids[1:] {
		agent, err := loadAgent(ctx, tenantID, userID, id)
		if err != nil || agent == nil {
			slog.Warn("workbench: expert agent not loadable", "agent_id", id, "error", err)
			continue
		}
		expert := map[string]any{"id": agent.ID}
		if agent.Name != "" {
			expert["name"] = agent.Name
		}
		if agent.Description != "" {
			expert["description"] = agent.Description
		}
		if agent.SystemPrompt != "" {
			expert["system_prompt"] = agent.SystemPrompt
		}
		experts = append(experts, expert)
	}
	return experts
}
// contextIDList 读多值 <name>_ids，缺失时回退单值 <name>_id。
// 与前端 buildContextQuery、python 侧 context_ids 同一口径（复数优先、单数兼容），
// 否则同一个 URL 在三条链路上会解析出不同结果。
//
// 数组同时接受 []any（请求体经 DecodeJSON 后的常态）与 []string（Go 侧构造
// context 时的手写形态）：只认前者会让后者静默退化成"没传多值"。
func contextIDList(wbCtx map[string]any, name string) []string {
	if raw, ok := wbCtx[name+"_ids"]; ok {
		out := make([]string, 0, 4)
		switch v := raw.(type) {
		case []any:
			for _, item := range v {
				if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		case []string:
			for _, s := range v {
				if strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	if s, ok := wbCtx[name+"_id"].(string); ok && strings.TrimSpace(s) != "" {
		return []string{strings.TrimSpace(s)}
	}
	return nil
}
func joinComma(items []string) string { return strings.Join(items, ", ") }
func llmString(m map[string]any, key, fallback string) string {
	if v, ok := m[key].(string); ok && v != "" {
		return v
	}
	return fallback
}
func llmInt(m map[string]any, key string, fallback int) int {
	if v, ok := m[key].(float64); ok && v > 0 {
		return int(v)
	}
	return fallback
}
func llmFloat(m map[string]any, key string, fallback float64) float64 {
	if v, ok := m[key].(float64); ok && v > 0 {
		return v
	}
	return fallback
}
func boolOf(v any) bool {
	b, _ := v.(bool)
	return b
}
