package api

import (
	"log/slog"
	"net/http"
	"strconv"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/session"
)

// tool_output.go —— 工具调用 output 的按需分段读取。
//
// 列表接口只返回前 outputPreviewBytes 个字符（见 internal/session），
// 全文由本端点按 offset/limit 分段取。没有它，前端拿到 output_truncated=true
// 却无处可展开，用户只能看到被截短的预览并被误导以为那就是全部。

// ToolOutputChunk 是一次分段读取的结果。
type ToolOutputChunk struct {
	// Content 是本段原文；TotalBytes 是库中 output 的完整字节数，
	// 前端据此判断还剩多少未加载。
	Content    string `json:"content"`
	TotalBytes int    `json:"total_bytes"`
	// Offset 是本次请求的起始字符偏移；Limit 是服务端实际采用的上限
	// （可能小于客户端请求值，因为服务端强制夹取）。
	Offset int `json:"offset"`
	Limit  int `json:"limit"`
}

// ToolOutput 返回一条工具调用 output 的指定片段。
//
// 鉴权顺序是刻意的：先确认会话归属，再查 output。
// 跨用户/跨租户一律 404（不是 403）——403 会告诉探测者"这个 ref 真实存在"，
// 而 tool_calls.id 是可枚举的 UUID，不是能力凭证。
func (h *ConversationHandler) ToolOutput(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	sessionID := r.PathValue("id")
	toolCallID := r.PathValue("tcID")
	if sessionID == "" || toolCallID == "" {
		NotFound(w, "tool call not found")
		return
	}

	sess, err := h.sessionMgr.GetSession(r.Context(), sessionID)
	if err != nil || sess.UserID != claims.UserID {
		// 会话不存在与不归你所有，对外必须不可区分。
		NotFound(w, "tool call not found")
		return
	}

	offset := 0
	if v := r.URL.Query().Get("offset"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			offset = parsed
		}
	}
	limit := 0 // 0 → 用默认值，夹取在 session 层做
	if v := r.URL.Query().Get("limit"); v != "" {
		if parsed, err := strconv.Atoi(v); err == nil {
			limit = parsed
		}
	}

	content, total, found, err := h.sessionMgr.GetToolCallOutput(
		r.Context(), sessionID, toolCallID, offset, limit)
	if err != nil {
		slog.Warn("tool output read failed", "session", sessionID, "tool_call", toolCallID, "error", err)
		InternalError(w, "failed to read tool output")
		return
	}
	if !found {
		NotFound(w, "tool call not found")
		return
	}

	// 回填服务端**实际采用**的上限，而不是客户端请求值：客户端要 1MB 时真实
	// 只读了 64KB，回 1MB 会让前端按"片段长度 < 请求上限"判定为未到末尾，
	// 于是用错误的 offset 继续请求，陷入空转。夹取函数与查询共用同一个实现。
	limit = session.ClampToolOutputLimit(limit)

	// 审计：只记元数据，不记内容 —— 记内容等于把原文又抄一份到日志系统。
	slog.Info("tool output read",
		"tenant_id", claims.TenantID, "session_id", sessionID,
		"ref", toolCallID, "offset", offset, "limit", limit, "bytes", total)

	JSON(w, http.StatusOK, APIResponse{Success: true, Data: ToolOutputChunk{
		Content:    content,
		TotalBytes: total,
		Offset:     offset,
		Limit:      limit,
	}})
}
