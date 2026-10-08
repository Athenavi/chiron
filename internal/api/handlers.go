package api

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
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

func handleCancel(w http.ResponseWriter, r *http.Request, sessionMgr *session.Manager) {
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
	if v, ok := sessionCancels.Load(sessionID); ok {
		sc := v.(*sessionCancel)
		if sc.userID != claims.UserID {
			// 不动别人的条目（不要 LoadAndDelete + Store：那会有一段"条目不在"的窗口，
			// 期间属主自己的取消会落到广播分支而**取消不到本地这个正在跑的任务**）
			Forbidden(w, "not your session")
			return
		}
		// 原子认领：期间若条目已被替换/删除（run 已结束或被新 run 接管），就不取消。
		if !sessionCancels.CompareAndDelete(sessionID, v) {
			OK(w, map[string]string{"status": "no_active_task", "session_id": sessionID})
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
		// 本实例没有该 session 的活动任务：任务可能跑在别的副本上，需要广播。
		//
		// ⚠️ 广播前**必须**先校验归属：载荷里只有 sessionID（给子 Agent 的那条更是完全不带
		// 身份），接收端无从验证 —— 不校验就等于任何登录用户拿着 sessionID 就能取消别人的
		// 运行（跨租户 DoS）。口径与 /submit 的 IDOR 校验一致。
		if sessionMgr != nil {
			sess, sessErr := sessionMgr.GetSession(r.Context(), sessionID)
			if sessErr != nil {
				if !errors.Is(sessErr, session.ErrSessionNotFound) {
					InternalError(w, "session check failed")
					return
				}
				// 会话不存在 ⇒ 没有可取消的任务，也不广播（否则所有副本都去找一个不存在的会话）
				OK(w, map[string]string{"status": "no_active_task", "session_id": sessionID})
				return
			}
			if sess == nil || sess.UserID != claims.UserID {
				Forbidden(w, "session does not belong to the current user")
				return
			}
		}
		// 本实例无该 session 任务：广播取消到其它网关实例（跨实例协调）
		if err := CancelSessionBroadcast(r.Context(), sessionID, claims.UserID); err == nil {
			OK(w, map[string]string{"status": "cancel_requested", "session_id": sessionID})
			return
		}
		OK(w, map[string]string{"status": "no_active_task", "session_id": sessionID})
	}
}
