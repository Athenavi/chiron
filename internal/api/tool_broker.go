package api

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// Tool Broker —— 危险工具（delete / external）的**服务端授权与审计**。
//
// 为什么需要它：引擎（Python）侧的 ToolGuard 跑在**处理不可信内容的同一个进程**里 ——
// 一旦它被 prompt injection 影响，判定与执行就落在同一信任域，等于没有边界。所以危险工具的
// 判定必须由服务端**独立**做一遍（见 internal/api/tool_policy.go），并留下审计。
//
// 端点：POST /v1/internal/tool-authorize（内部令牌；由引擎调用）
//
//	请求：{tenant_id, user_id, session_id, tool_call_id, tool_name, arguments, tools_mode}
//	响应：{allowed, level, requires_user_approval, requires_second_check, rollback,
//	       enforced, reason}
//
// 服务端**不执行**工具（实现仍在引擎），它只回答"允不允许执行 + 该走哪些关"。引擎侧的判定
// 若比服务端松，以服务端为准 —— 这是"判定与执行分处两个信任域"的落点。
//
// 注意：本端点解决"判定权威"，不解决"引擎绕过"。真正的硬约束是"引擎侧没有直连危险工具的
// 能力"（MCP 收窄 + 出口凭据不进引擎），见 docs/agent-safety-and-reliability.md §1.4。

type toolAuthorizeRequest struct {
	TenantID   string          `json:"tenant_id"`
	UserID     string          `json:"user_id"`
	SessionID  string          `json:"session_id"`
	ToolCallID string          `json:"tool_call_id"`
	ToolName   string          `json:"tool_name"`
	Arguments  json.RawMessage `json:"arguments"`
	ToolsMode  string          `json:"tools_mode"`
}

type toolAuthorizeResponse struct {
	Allowed              bool      `json:"allowed"`
	Level                ToolLevel `json:"level"`
	RequiresUserApproval bool      `json:"requires_user_approval"`
	RequiresSecondCheck  bool      `json:"requires_second_check"`
	Rollback             string    `json:"rollback"`
	// Enforced 为 true 表示**服务端收紧了引擎上报的模式**（用户的模式选择被覆盖）。
	// 前端应据此告诉用户"为什么选了全自动还要确认"。
	Enforced bool   `json:"enforced,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

// ToolAuthorizeHandler 是 Broker 的唯一入口：判定 + 收紧 + 审计。
func ToolAuthorizeHandler(w http.ResponseWriter, r *http.Request) {
	var body toolAuthorizeRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	body.ToolName = strings.TrimSpace(body.ToolName)
	if body.ToolName == "" {
		BadRequest(w, "tool_name is required")
		return
	}

	level := ToolLevelOf(body.ToolName, toolArgsMap(body.Arguments))
	mode := strings.TrimSpace(body.ToolsMode)
	if mode == "" {
		mode = "auto" // 与引擎一致：缺省按 auto（既不放行 delete/external，也不像 ask 那样全拦）
	}

	resp := toolAuthorizeResponse{
		Allowed:              true,
		Level:                level,
		RequiresUserApproval: RequiresConfirmation(level, mode),
		RequiresSecondCheck:  RequiresSecondCheck(level),
		Rollback:             ToolRollbackCapability(body.ToolName),
	}

	// 服务端可以**收紧**模式选择：delete / external 是"不可逆 + 触达外部"，即使会话选了
	// yolo（跳过全部确认），服务端仍要求人工确认 + 二次校验。这是刻意的：
	// 模式选择表达"我愿意多放手"，不能表达"我愿意承受不可撤销的后果"。
	if resp.RequiresSecondCheck && !resp.RequiresUserApproval {
		resp.RequiresUserApproval = true
		resp.Enforced = true
		resp.Reason = "服务端策略：不可逆或触达外部的操作始终需要人工确认"
	}

	// 审计：危险工具的每一次授权尝试都留痕（含等级、是否被收紧、可否回滚）。
	// 只读/普通写入不记 —— 审计日志的价值在于"异常与不可逆"，不是逐条流水。
	if resp.RequiresSecondCheck {
		detail := "level=" + string(level) + " mode=" + mode
		if resp.Enforced {
			detail += " enforced"
		}
		db.AuditLog(r.Context(), body.UserID, body.TenantID,
			"tool.authorize", "tool:"+body.ToolName, detail, clientIP(r),
			map[string]interface{}{
				"tool":       body.ToolName,
				"level":      string(level),
				"mode":       mode,
				"tool_call":  body.ToolCallID,
				"session_id": body.SessionID,
				"rollback":   resp.Rollback,
				"enforced":   resp.Enforced,
			})
	}

	OK(w, resp)
}

// toolArgsMap 解析引擎上报的参数；解析不了按"无参数"处理 ——
// ToolLevelOf 对命令类工具的保守默认（delete）会兜住这种情况。
func toolArgsMap(raw json.RawMessage) map[string]any {
	if len(raw) == 0 {
		return nil
	}
	var args map[string]any
	if err := json.Unmarshal(raw, &args); err != nil {
		return nil
	}
	return args
}
