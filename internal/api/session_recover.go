package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 手动修复卡住的会话（用户可见的"救回来"通道）。
//
// 背景：`ux_agent_runs_session_active` 规定"同会话同时只能有一个活跃 run"
// （status ∈ running/checkpointed/resuming）。一次失败若没写成终态，这一行就会
// 长期占位，该会话此后提交不了新消息 —— 而用户看到的只有
// "请求失败，请稍后重试"，完全指不出原因。
//
// 自动恢复由引擎 reconciler 负责（每 60s 巡检，超过 stale 阈值未更新的活跃行收口）。
// 本端点给用户一条**立即**恢复的通道，两者**共享同一套判据**，避免出现
// "手动说还没到时间、自动却已经开始处理"的自相矛盾。
const (
	// sessionRecoverStaleAfter 与引擎侧 `resume.py` 的 STALE_AFTER_SECONDS 对齐（6*60）。
	// 两侧不一致会让"还要等多久"的提示与实际自动恢复时机不符。
	sessionRecoverStaleAfter = 6 * time.Minute
	// reconcilerSweepInterval 是引擎 reconciler 的巡检间隔（`resume.py` 每 60s）。
	// 给用户算"最坏还要等多久"时用它兜底：过了 stale 阈值后，最多再等一次巡检。
	reconcilerSweepInterval = 60 * time.Second
)

// RecoverSession 把会话中确认停滞的活跃 run 收口为 abandoned，让该会话可以再次提交。
//
// **严格模式**（用户选定）：只修确实停滞的，宁可拒绝也不误杀在跑的 run。
//   - 闸门 A：Redis 运行锁仍存在 ⇒ 有实例正持有它（60s 心跳续期）⇒ 409。
//   - 闸门 B：该行最后更新距今不足 stale 阈值 ⇒ 仍可能在跑 ⇒ 409 + 剩余秒数。
//   - 通过后按 `(id, run_token)` 更新：期间若已有新 run 改写这一行，本次 0 命中，
//     因此**不会踩踏**新 run。
//
// 锁在闸门 A 已确认不存在，所以这里不需要（也不应该）去删锁。
func (h *SubmitHandler) RecoverSession(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.UserID == "" {
		Unauthorized(w, "authentication required")
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "database unavailable")
		return
	}
	ctx := r.Context()

	// 归属校验：不属于本用户的会话统一 404，不泄露存在性。
	owned, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT 1 AS ok FROM sessions WHERE id = $1 AND user_id = $2::uuid`, sessionID, claims.UserID)
	if err != nil {
		slog.Error("session recover: ownership query failed", "session_id", sessionID, "error", err)
		ServiceUnavailable(w, "database unavailable")
		return
	}
	if owned == nil {
		NotFound(w, "session not found")
		return
	}

	row, err := db.GlobalDBManager.FetchOne(ctx, `
		SELECT id::text AS id, status, COALESCE(run_token, '') AS run_token,
		       COALESCE(checkpoint_at, updated_at, created_at) AS touched_at
		  FROM agent_runs
		 WHERE session_id = $1
		   AND status IN ('running', 'checkpointed', 'resuming')
		 ORDER BY COALESCE(checkpoint_at, updated_at, created_at) DESC
		 LIMIT 1`, sessionID)
	if err != nil {
		slog.Error("session recover: active run query failed", "session_id", sessionID, "error", err)
		ServiceUnavailable(w, "database unavailable")
		return
	}
	if row == nil {
		// 没有活跃 run：本来就没卡，或刚被 reconciler 收口了。对用户都是"已经好了"。
		OK(w, map[string]interface{}{
			"recovered":           0,
			"reason":              "no_active_run",
			"retry_after_seconds": 0,
		})
		return
	}

	runID := recoverAsString(row["id"])
	status := recoverAsString(row["status"])
	runToken := recoverAsString(row["run_token"])
	touchedAt := recoverAsTime(row["touched_at"])

	held, lockErr := sessionRunLockHeld(ctx, sessionID)
	if lockErr != nil {
		// 锁状态未知时不敢动手：宁可让用户再等，也不误杀一个可能仍在跑的 run。
		ServiceUnavailable(w, "lock state unavailable")
		return
	}
	if held {
		// retryable=true：等上一轮跑完（或 reconciler 收口）后同一个请求就能过 —— 这正是
		// "重试有效"的定义。此前没有这个字段，客户端只能拿 409 当"请求有问题"处理。
		JSONWithRetryable(w, http.StatusConflict, CodeSessionBusy, map[string]interface{}{
			"retry_after_seconds": int(reconcilerSweepInterval.Seconds()),
		}, "session run still active", true)
		return
	}

	if wait := sessionRecoverStaleAfter - time.Since(touchedAt); wait > 0 {
		JSONWithRetryable(w, http.StatusConflict, CodeSessionBusy, map[string]interface{}{
			"retry_after_seconds": int((wait + reconcilerSweepInterval).Seconds()),
		}, "session run not stale yet", true)
		return
	}

	// 双重检查：闸门 A 与这条 UPDATE 之间有微小窗口，新 run 可能刚抢到锁并改写这一行。
	// 再查一次成本极低，而误杀一次新 run 的代价很高。
	if held, lockErr = sessionRunLockHeld(ctx, sessionID); lockErr == nil && held {
		JSONWithRetryable(w, http.StatusConflict, CodeSessionBusy, map[string]interface{}{
			"retry_after_seconds": int(reconcilerSweepInterval.Seconds()),
		}, "session run still active", true)
		return
	}

	// `run_token` 条件是关键：它把本次收口**钉在**我们刚读到的那一条上。
	// 若期间该行已被新 run 接管，这次更新 0 命中（而不是把新 run 标成 abandoned）。
	affected, err := db.GlobalDBManager.Execute(ctx, `
		UPDATE agent_runs
		   SET status = 'abandoned', updated_at = NOW()
		 WHERE id = $1
		   AND run_token = $2
		   AND status IN ('running', 'checkpointed', 'resuming')`, runID, runToken)
	if err != nil {
		slog.Error("session recover: update failed", "session_id", sessionID, "run_id", runID, "error", err)
		ServiceUnavailable(w, "database unavailable")
		return
	}
	if affected == 0 {
		OK(w, map[string]interface{}{
			"recovered":           0,
			"reason":              "already_settled",
			"retry_after_seconds": 0,
		})
		return
	}

	slog.Info("session recovered manually",
		"session_id", sessionID, "run_id", runID,
		"previous_status", status, "user_id", claims.UserID)
	OK(w, map[string]interface{}{
		"recovered":           1,
		"run_id":              runID,
		"previous_status":     status,
		"retry_after_seconds": 0,
	})
}

// sessionRunLockHeld 报告会话的运行锁是否仍被持有。
// Redis 不可用时返回 error —— 调用方必须当作"未知"处理，不能当作"没锁"。
func sessionRunLockHeld(ctx context.Context, sessionID string) (bool, error) {
	if db.Redis == nil {
		return false, fmt.Errorf("redis unavailable")
	}
	n, err := db.Redis.Exists(ctx, agentRunLockPrefix+sessionID).Result()
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

func recoverAsString(v interface{}) string {
	switch t := v.(type) {
	case string:
		return t
	case []byte:
		return string(t)
	case nil:
		return ""
	default:
		return fmt.Sprint(t)
	}
}

// recoverAsTime 取行的时间戳。取不到就返回"现在" —— 这让闸门 B 判定为"仍新鲜"，
// 也就是**偏向拒绝**（严格模式），而不是冒然收口一个时间未知的 run。
func recoverAsTime(v interface{}) time.Time {
	if t, ok := v.(time.Time); ok {
		return t
	}
	return time.Now()
}
