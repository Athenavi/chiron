package api

// ── 会话分叉 API ──
//
//	POST /v1/conversations/{id}/fork   {from_index, title?} → 201 {session_id, copied, branch_from_seq}
//
// 为什么需要：会话地图要按**真实分支**连线（参照 `vendor/dsh-synapse`：它按 DSH 原生
// `session.header.parentSession` 画边、用 `seedLength` 记分叉点）。我们此前没有 fork，
// 地图只能是孤立方块。这个端点补上"分支"这个事实本身，地图才有资格连边。
//
// 分工：真正的复制与事务在 `session.Manager.ForkSession`（贴近消息表）；
// 这里只做鉴权、参数校验与错误语义。

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/session"
)

// ForkConversationHandler 从既有会话分叉出新会话。
func ForkConversationHandler(sessionMgr *session.Manager) http.HandlerFunc {
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
			// FromIndex：保留前多少条消息（含第 FromIndex 条）。1 表示"只留第一条"。
			FromIndex int    `json:"from_index"`
			Title     string `json:"title"`
		}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		if body.FromIndex <= 0 {
			BadRequest(w, "from_index must be greater than 0")
			return
		}

		newID, copied, err := sessionMgr.ForkSession(r.Context(), sessionID, claims.UserID, body.Title, body.FromIndex)
		if err != nil {
			slog.Warn("fork conversation failed", "session", sessionID, "error", err)
			// 分不清"不存在"与"无权限"时一律 400：与只读接口一致，不泄露存在性
			JSON(w, http.StatusBadRequest, APIResponse{
				Success: false,
				Error:   "cannot fork this conversation: " + err.Error(),
			})
			return
		}
		slog.Info("conversation forked", "source", sessionID, "new", newID, "copied", copied)
		JSON(w, http.StatusCreated, APIResponse{
			Success: true,
			Data: map[string]interface{}{
				"session_id":        newID,
				"parent_session_id": sessionID,
				"branch_from_seq":   body.FromIndex,
				"copied":            copied,
			},
		})
	}
}
