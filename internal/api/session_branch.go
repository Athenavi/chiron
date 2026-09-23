package api

// ── 会话分支 API（裁剪 + 压缩）──
//
//	POST /v1/conversations/{id}/branch
//	{from_index, keep_tail?, mode?, title?}
//	  → 201 {session_id, parent_session_id, branch_from_seq, branch_mode, branch_state, copied, condensed}
//
// 与 `/fork` 的分工：`/fork` 是**旧语义**（逐字复制前 N 条，等价 mode='truncate'），
// 保留是为了不破坏既有前端与地图连线；`/branch` 是**新语义**（裁剪 + 让引擎压出核心上下文）。
// 两者共用 `session.Manager.BranchSession`。
//
// 为什么响应要带 branch_mode / branch_state：前端拿到就要立刻把"压缩中 / 已压缩 / 无需压缩"
// 画在列表与地图卡片上（用户第 3 条诉求），不该让它再发一次请求。

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/session"
)

// BranchConversationHandler 从既有会话裁出一段上下文，落到一个带血缘标记的新会话。
func BranchConversationHandler(sessionMgr *session.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		sessionID := strings.TrimSpace(r.PathValue("id"))
		if sessionID == "" {
			BadRequest(w, "session id is required")
			return
		}

		var body struct {
			// FromIndex：分叉点 —— 保留到源会话的第几条消息（含第 FromIndex 条）
			FromIndex int `json:"from_index"`
			// KeepTail：压缩式裁剪下保留多少条**原文**（0 = 用默认值 4）
			KeepTail int `json:"keep_tail"`
			// Mode："condense"（默认）| "truncate"
			Mode string `json:"mode"`
			Title string `json:"title"`
			// IncludeFuture（P3 预留，当前忽略）：把被裁掉的后段也压成一句"后续走向"
			// 追加在摘要末尾。默认关闭 —— 否则新会话会"记得未来"，语义混乱（设计 2.3）。
			IncludeFuture bool `json:"include_future"`
		}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		if body.FromIndex <= 0 {
			BadRequest(w, "from_index must be greater than 0")
			return
		}
		if body.Mode != "" && body.Mode != "condense" && body.Mode != "truncate" {
			BadRequest(w, "mode must be condense or truncate")
			return
		}
		if body.KeepTail < 0 {
			BadRequest(w, "keep_tail must not be negative")
			return
		}

		res, err := sessionMgr.BranchSession(r.Context(), sessionID, claims.UserID, session.BranchOptions{
			Title:     body.Title,
			FromIndex: body.FromIndex,
			KeepTail:  body.KeepTail,
			Mode:      body.Mode,
		})
		if err != nil {
			slog.Warn("branch conversation failed", "session", sessionID, "error", err)
			// 与 /fork 一致：分不清"不存在"与"无权限"时一律 400，不泄露存在性
			JSON(w, http.StatusBadRequest, APIResponse{
				Success: false,
				Error:   "cannot branch this conversation: " + err.Error(),
			})
			return
		}
		slog.Info("conversation branched",
			"source", sessionID, "new", res.SessionID,
			"mode", res.BranchMode, "state", res.BranchState, "copied", res.Copied)

		JSON(w, http.StatusCreated, APIResponse{
			Success: true,
			Data: map[string]interface{}{
				"session_id":        res.SessionID,
				"parent_session_id": res.ParentSessionID,
				"branch_from_seq":   res.BranchFromSeq,
				"branch_mode":       res.BranchMode,
				"branch_state":      res.BranchState,
				"copied":            res.Copied,
				// condensed=false 时前端显示"无需压缩"（压缩区太短，没有花模型调用）
				"condensed": res.Condensed,
			},
		})
	}
}
