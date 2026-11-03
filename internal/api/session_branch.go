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
// 画在列表与地图卡片上，不该让它再发一次请求。

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
)

// branchCondenseTimeout：压缩是一次模型调用，给足时间但别无限等。
const branchCondenseTimeout = 120 * time.Second

// BranchConversationHandler 从既有会话裁出一段上下文，落到一个带血缘标记的新会话。
//
// pythonClient 用于**后台**触发压缩（见 condenseBranchAsync）；为 nil 时（测试/裁剪模式）
// 只建会话与标记，不压缩 —— 与既有的 "truncate" 行为一致。
func BranchConversationHandler(sessionMgr *session.Manager, pythonClient *engine.PythonClient) http.HandlerFunc {
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
			// KeepTail：压缩式裁剪下保留多少条**原文**。
			// 用指针区分「没传」与「显式 0」：前者用默认值 4，后者表示
			// 一条原文都不留 —— 整段进压缩区，新会话里只剩一条摘要。
			// 地图的「压缩分叉」档位就是靠 `keep_tail: 0` 表达的。
			KeepTail *int `json:"keep_tail"`
			// Mode："condense"（默认）| "truncate"
			Mode  string `json:"mode"`
			Title string `json:"title"`
			// IncludeFuture（P3 预留，当前忽略）：把被裁掉的后段也压成一句"后续走向"
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
		if body.KeepTail != nil && *body.KeepTail < 0 {
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

		// 两段式异步的第二段：**先返回**（会话与原文已就绪，用户可立刻用），
		// 压缩在后台跑，完成后把摘要落进会话并把状态推进到 ready/failed。
		if res.Condensed {
			condenseBranchAsync(pythonClient, sessionMgr, res.SessionID, res.ParentSessionID, res.BranchFromSeq, res.KeepTail)
		}

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

// BranchRetryHandler 重新触发一次分支压缩（`POST /v1/conversations/{id}/branch/retry`）。
//
// 为什么需要它：压缩是异步的，失败会把 `branch_state` 停在 `failed`（网关重启则停在
// `pending`）。没有重试入口时，这两个状态就是**死胡同** —— 前端只能一直显示"压缩失败"，
// 用户唯一的出路是重新分叉一次（多一份冗余会话）。`ResetBranchStateForRetry` 早就写好了，
// 但全仓没有任何调用方，这个端点把它接上。
//
// 语义：重置状态 → 复用创建分支时的同一份归一化参数（`branch_from_seq` /
// `branch_keep_tail`）重跑压缩。**不重新裁剪、不重建会话** —— 分支的原文已经是最终形态，
// 重试只补那份缺掉的摘要。
func BranchRetryHandler(sessionMgr *session.Manager, pythonClient *engine.PythonClient) http.HandlerFunc {
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

		info, err := sessionMgr.GetBranchInfo(r.Context(), sessionID)
		if err != nil {
			// 与其它分支接口一致：不区分"不存在"与"无权限"，避免探测存在性
			NotFound(w, "branch not found")
			return
		}
		// 归属校验：只能重试自己的分支。`user_id` 在库里是 varchar，按字符串比
		// （与 manager_branch.go 的注释同源：转 uuid 会直接报 operator does not exist）。
		if info.UserID != "" && claims.UserID != "" && info.UserID != claims.UserID {
			NotFound(w, "branch not found")
			return
		}
		if info.BranchMode != "condense" {
			BadRequest(w, "only condensed branches can be retried")
			return
		}
		if info.ParentSessionID == "" {
			BadRequest(w, "branch has no parent session to re-condense")
			return
		}
		if info.BranchState == "pending" {
			// 已有一次压缩在跑：重复触发会写下两份摘要（AppendBranchSummary 的 CAS
			// 只挡住"非 pending"，挡不住"两个 pending 同时抢"）。直接告诉前端现状，
			// 让它继续轮询而不是再排队一次模型调用。
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "condense already in progress",
			})
			return
		}

		// 先重置再触发：顺序不能反 —— 反了会先写出摘要、再被重置回 pending，
		// AppendBranchSummary 的 CAS 就会把刚写好的摘要当成"已被别人写好"而跳过。
		reset, err := sessionMgr.ResetBranchStateForRetry(r.Context(), sessionID)
		if err != nil {
			slog.Warn("branch retry reset failed", "session", sessionID, "error", err)
			JSON(w, http.StatusInternalServerError, APIResponse{
				Success: false,
				Error:   "cannot reset branch state: " + err.Error(),
			})
			return
		}
		if !reset {
			// 并发下别人刚重置成功：同样按"已在压缩中"处理，不重复触发
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "condense already in progress",
			})
			return
		}

		condenseBranchAsync(pythonClient, sessionMgr, sessionID, info.ParentSessionID, info.BranchFromSeq, info.KeepTail)
		slog.Info("branch condense retried", "session", sessionID, "source", info.ParentSessionID)
		JSON(w, http.StatusAccepted, APIResponse{
			Success: true,
			Data: map[string]interface{}{
				"session_id":  sessionID,
				"branch_mode": info.BranchMode,
				// 状态一定是 pending：异步任务刚投出，前端据此继续显示"压缩中"
				"branch_state": "pending",
			},
		})
	}
}

// condenseBranchAsync 在后台把分支的"压缩区"交给引擎压成核心上下文摘要，并落进新会话。
//
// 为什么后台：压缩要调一次模型（几秒~数十秒），不能把用户卡在"创建分支"的请求里
// （用户确认的方案：两段式异步 —— 先建会话立即返回 pending，后台完成后再写摘要）。
//
// 职责边界：引擎只返回摘要文本，**落库在 Go**（AppendBranchSummary）。
//
// 失败语义：会话**始终可用**（它已经有尾部原文），只是没有摘要 —— 状态置 failed，
// 前端显示"压缩失败，可重试"。
//
// 已知边界：这个 goroutine 依附于网关进程，未持久化 —— 网关在压缩途中重启会丢任务，
// 会话停在 pending。彻底解决要把它投进 `engine:tasks` 流并让引擎回调 Go（与
// agent_followup 同构），属于下一步；在此之前 pending 卡住可由重试入口恢复。
func condenseBranchAsync(
	pythonClient *engine.PythonClient,
	sessionMgr *session.Manager,
	sessionID, srcSessionID string,
	fromIndex, keepTail int,
) {
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), branchCondenseTimeout)
		defer cancel()

		if pythonClient == nil {
			slog.Warn("branch condense skipped: engine client unavailable", "session", sessionID)
			_, _ = sessionMgr.MarkBranchState(ctx, sessionID, "failed")
			return
		}

		var resp struct {
			Summary        string `json:"summary"`
			Condensed      bool   `json:"condensed"`
			Degraded       bool   `json:"degraded"`
			Reason         string `json:"reason"`
			SourceMessages int    `json:"source_messages"`
		}
		body := map[string]any{
			"session_id":        srcSessionID,
			"from_index":        fromIndex,
			"keep_tail":         keepTail,
			"include_future":    false,
			"target_session_id": sessionID,
		}
		if err := pythonClient.PostJSON(ctx, "/v1/context/condense", body, &resp); err != nil {
			slog.Warn("branch condense request failed", "session", sessionID, "error", err)
			_, _ = sessionMgr.MarkBranchState(ctx, sessionID, "failed")
			return
		}

		summary := strings.TrimSpace(resp.Summary)
		if summary == "" {
			// 引擎说"没有可压缩的内容"（condensed=false）→ 没有任何摘要要写，
			// 把状态推进到 ready，前端就不再显示"压缩中"。
			if _, err := sessionMgr.MarkBranchState(ctx, sessionID, "ready"); err != nil {
				slog.Warn("branch condense mark ready failed", "session", sessionID, "error", err)
			}
			return
		}

		wrote, err := sessionMgr.AppendBranchSummary(ctx, sessionID, summary)
		if err != nil {
			slog.Warn("branch summary persist failed", "session", sessionID, "error", err)
			_, _ = sessionMgr.MarkBranchState(ctx, sessionID, "failed")
			return
		}
		slog.Info("branch condensed",
			"session", sessionID, "source", srcSessionID,
			"chars", len(summary), "degraded", resp.Degraded, "wrote", wrote)
	}()
}
