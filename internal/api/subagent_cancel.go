package api

// ── 子 Agent 中止 API ──
//
//	POST /v1/subagent/runs/{run_id}/cancel          中止单个 run
//	POST /v1/subagent/sessions/{session_id}/cancel  中止该会话所有活跃 run
//
// **执行方不是网关**：后台 run 由「父 turn 所在的引擎实例」持有（asyncio 任务跑在那个进程里），
// 而网关没有 "run → 实例" 的映射。所以这里只做两件事：鉴权/租户校验 + 一次 Redis 广播；
// 各引擎实例的订阅者（`app/subagent/registry.py`）命中本地注册表才真正 `task.cancel()`。
// 这既不引入新的映射表，也与既有 `agent:cancel`（session_coord.go）的跨实例做法一致。
//
// 取消是**协作式**的：`task.cancel()` 在下一个 await 点生效 —— 等 LLM 响应时立即中断，
// 正在跑的同步工具要等它返回。引擎侧收尾会把原因写进 `subagent_runs.error`
// （cancelled_by_user / cancelled_by_session，与"失败"区分开）。

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// subagentCancelChannel 与引擎侧订阅名逐字一致（`app/subagent/registry.py`）。
func subagentCancelChannel() string { return db.RedisKey("subagent:cancel") }

type subagentCancelBroadcast struct {
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	UserID    string `json:"user_id,omitempty"`
	// Reason 是外部简写，引擎侧会映射成原因码：user → cancelled_by_user，session → cancelled_by_session
	Reason string `json:"reason"`
}

func (h *SubagentHandler) publishCancel(b subagentCancelBroadcast) error {
	if h.rdb == nil {
		return errRedisUnavailable
	}
	payload, err := json.Marshal(b)
	if err != nil {
		return err
	}
	// 用 Do 而非类型化方法：RedisClient 接口只保证 Do（项目既有用法，见 submit_handler.go）
	return h.rdb.Do(context.Background(), "PUBLISH", subagentCancelChannel(), payload).Err()
}

// CancelRun 中止单个子 Agent run。
func (h *SubagentHandler) CancelRun(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	// 租户校验：别人的 run 一律按"不存在"处理（与只读端点同一套判定，不泄露存在性）
	status, visible := h.runStatusForTenant(r.Context(), subagentTenant(claims), runID)
	if !visible {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "subagent run not found"})
		return
	}
	// 幂等：已终态的 run 不需要（也无法）再取消。此前无条件返回 "accepted"，前端会显示
	// "已请求停止"而状态永不变 —— 典型的假成功，比报错更难排查（报错会让人重试）。
	if terminalRunStatuses[status] {
		OK(w, map[string]any{
			"status": "not_running", "run_id": runID, "run_status": status,
		})
		return
	}
	if err := h.publishCancel(subagentCancelBroadcast{
		RunID: runID, UserID: claims.UserID, Reason: "user",
	}); err != nil {
		slog.Error("subagent cancel publish failed", "run_id", runID, "error", err)
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "cancel unavailable"})
		return
	}
	slog.Info("subagent cancel requested", "run_id", runID, "user_id", claims.UserID)
	OK(w, map[string]string{"status": "accepted", "run_id": runID})
}

// CancelSessionRuns 中止某会话下所有活跃子 Agent run。
func (h *SubagentHandler) CancelSessionRuns(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	// 会话归属校验：不能凭别人的 session_id 去停别人的子 agent
	if !h.sessionOwnedByUser(r.Context(), sessionID, claims.UserID) {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "session not found"})
		return
	}
	if err := h.publishCancel(subagentCancelBroadcast{
		SessionID: sessionID, UserID: claims.UserID, Reason: "session",
	}); err != nil {
		slog.Error("subagent session cancel publish failed", "session_id", sessionID, "error", err)
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "cancel unavailable"})
		return
	}
	slog.Info("subagent session cancel requested", "session_id", sessionID, "user_id", claims.UserID)
	OK(w, map[string]string{"status": "accepted", "session_id": sessionID})
}

// sessionOwnedByUser 判断会话是否属于该用户（与 runOwnedByTenant 同一套风格）。
func (h *SubagentHandler) sessionOwnedByUser(ctx context.Context, sessionID, userID string) bool {
	if sessionID == "" || userID == "" || db.GlobalDBManager == nil {
		return false
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT 1 AS ok FROM sessions WHERE id = $1 AND user_id = $2::uuid`, sessionID, userID)
	return err == nil && row != nil
}

// terminalRunStatuses 是不可逆的终态：处于这些状态的 run 无法也不需要再取消。
//
// 与引擎侧状态机保持一致（见 app/subagent/store.py 与 docs/subagent-interaction-redesign.md）。
// 其中 "lost" 尤其重要：它表示"失联"（进程重启/心跳超时后被回收器标记），
// 与 "cancelled"（被主动停掉）语义不同，但两者都属于终态。
var terminalRunStatuses = map[string]bool{
	"completed": true,
	"failed":    true,
	"cancelled": true,
	"lost":      true,
}

// runStatusForTenant 取 run 状态；第二个返回值表示"该 run 对本租户可见"。
//
// 可见性判定与 runOwnedByTenant 完全一致：别人的 run 一律按"不存在"处理，
// 因此调用方对 !visible 统一回 404，不泄露存在性。
func (h *SubagentHandler) runStatusForTenant(ctx context.Context, tenant, runID string) (string, bool) {
	if tenant == "" || db.GlobalDBManager == nil {
		return "", false
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT status FROM subagent_runs WHERE id = $1 AND tenant_id = $2`, runID, tenant)
	if err != nil || row == nil {
		return "", false
	}
	status, _ := row["status"].(string)
	return status, true
}
