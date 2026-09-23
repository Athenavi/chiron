package api

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

func handleHealth(w http.ResponseWriter, r *http.Request) {
	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    map[string]string{"status": "ok"},
	})
}

func handleReadiness(w http.ResponseWriter, r *http.Request) {
	// 产品决策(2026-08-22)：Redis 为必需依赖；就绪检查反映真实依赖状态，
	// 供编排器(compose/K8s)在 Redis 故障时触发重启。
	deps := map[string]string{
		"postgres": "up",
		"redis":    "up",
	}
	ready := true
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if !db.GlobalDBManager.IsAvailable() {
		deps["postgres"] = "down"
		ready = false
	} else if err := db.GlobalDBManager.Ping(ctx); err != nil {
		deps["postgres"] = "down"
		ready = false
	}
	if db.Redis == nil || db.Redis.Ping(ctx).Err() != nil {
		deps["redis"] = "down"
		ready = false
	}
	if !ready {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false,
			Error:   "dependencies not ready",
			Data:    deps,
		})
		return
	}
	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    deps,
	})
}

func handleCancel(w http.ResponseWriter, r *http.Request) {
	sessionID := r.URL.Query().Get("session_id")
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if v, ok := sessionCancels.LoadAndDelete(sessionID); ok {
		sc := v.(sessionCancel)
		if sc.userID != claims.UserID {
			// 恢复条目——不是当前用户的 session
			sessionCancels.Store(sessionID, sc)
			Forbidden(w, "not your session")
			return
		}
		// 用户**显式**停止：先告知子 Agent（父回合的取消本身不再连带取消它们，
		// 见 BroadcastSubagentSessionCancel 的说明），再取消本地 ctx 结束事件流。
		if err := BroadcastSubagentSessionCancel(r.Context(), sessionID, "parent"); err != nil {
			slog.Warn("subagent session cancel broadcast failed",
				"session_id", sessionID, "error", err)
		}
		sc.cancel()
		slog.Info("session cancelled", "session_id", sessionID)
		OK(w, map[string]string{"status": "cancelled", "session_id": sessionID})
	} else {
		// 本实例无该 session 任务：广播取消到其它网关实例（跨实例协调）
		if err := CancelSessionBroadcast(r.Context(), sessionID, claims.UserID); err == nil {
			OK(w, map[string]string{"status": "cancel_requested", "session_id": sessionID})
			return
		}
		OK(w, map[string]string{"status": "no_active_task", "session_id": sessionID})
	}
}
