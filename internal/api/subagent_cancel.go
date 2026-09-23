package api

// ── 子 Agent 中止 API ──
//
//	POST /v1/subagent/runs/{run_id}/cancel          中止单个 run
//	POST /v1/subagent/sessions/{session_id}/cancel  中止该会话所有活跃 run
//
// **执行方不是网关**：后台 run 由「父 turn 所在的引擎实例」持有（asyncio 任务跑在那个进程里），
// 而网关**按 run 归属映射**判断该作业是否真的还有人在跑（`subagent:run:{run_id}`，
// 引擎在 run 启动时登记、收尾/关机时注销，见 python-engine/app/subagent/affinity.py）。
// 取消的判定链是：
//
//	广播 subagent:cancel  →  引擎侧命中本地注册表则取消并写回执 subagent:cancel:ack:{run_id}
//	                      →  网关等回执：等到 = cancelled 已生效；等不到 = **无人认领**
//
// 等不到回执时不再返回 "accepted"（那是假成功：前端显示正在停止、DB 里却永远停在
// running），而是把作业收敛为 `lost` 并如实返回。
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
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// subagentCancelChannel 与引擎侧订阅名逐字一致（`app/subagent/registry.py`）。
func subagentCancelChannel() string { return db.RedisKey("subagent:cancel") }

// cancelAckKey 与引擎侧 `app/subagent/affinity.py` 的 ack_key 逐字一致。
//
// 引擎在**真的发出取消**之后往这个键 LPUSH，网关 BLPOP 等它 —— 这是"取消是否发生"
// 的唯一可信依据（广播成功 ≠ 有人认领）。
func cancelAckKey(runID string) string { return db.RedisKey("subagent:cancel:ack:") + runID }

// cancelAckWaitSeconds 等待回执的秒数。持有该 run 的实例命中注册表后立刻回执
// （毫秒级），所以这个等待只会在"无人认领"时耗尽。
const cancelAckWaitSeconds = 1

// lostNoOwner 是"取消时没有任何实例认领该 run"时写入 subagent_runs.error 的原因码。
const lostNoOwner = "no_instance_claims_run"

// cancelStatusFor 决定取消请求该返回什么：有回执 → 取消确实生效；没有 → 作业无人认领，
// 必须按 `lost` 如实回答。抽成纯函数是为了让它能直接被单测（这段判定是"假成功"的边界）。
func cancelStatusFor(acked bool) string {
	if acked {
		return "accepted"
	}
	return "lost"
}

// waitCancelAck 等待持有该 run 的实例回执；false 表示**没有实例认领**。
func (h *SubagentHandler) waitCancelAck(ctx context.Context, runID string) bool {
	if h.rdb == nil || runID == "" {
		return false
	}
	// 比 BLPOP 自身的超时多 1s：留出网络往返，避免把"回执在路上"误判成"无人认领"。
	lctx, cancel := context.WithTimeout(ctx, (cancelAckWaitSeconds+1)*time.Second)
	defer cancel()
	res := h.rdb.Do(lctx, "BLPOP", cancelAckKey(runID), cancelAckWaitSeconds)
	vals, err := res.Slice()
	return err == nil && len(vals) > 0
}

// markRunLost 把无人认领的 run 收敛为 lost（只动仍处于活跃状态的行）。
//
// 为什么由网关来写：等待回执的结论（没有实例持有）只有网关知道，而这一行如果继续停在
// running，前端会永远显示"运行中"—— 正是本次要消灭的形态。
func (h *SubagentHandler) markRunLost(ctx context.Context, tenant, runID, reason string) bool {
	if tenant == "" || runID == "" || db.GlobalDBManager == nil {
		return false
	}
	n, err := db.GlobalDBManager.Execute(ctx, `
UPDATE subagent_runs
   SET status = 'lost', finished_at = now(), error = $3
 WHERE id = $1 AND tenant_id = $2
   AND status NOT IN ('completed', 'failed', 'cancelled', 'lost')`, runID, tenant, reason)
	if err != nil {
		slog.Warn("subagent mark lost failed", "run_id", runID, "error", err)
		return false
	}
	return n > 0
}

// BroadcastSubagentSessionCancel 直接向 `subagent:cancel` 频道发一条「停止该会话所有子 Agent」。
//
// 为什么必须由网关来做这件事（用户显式停止 vs 回合被截断）：
//
//	① 用户点停止（/v1/agent/cancel、跨实例广播）→ **发**这条广播，持有该会话子 Agent
//	   的实例会真的取消它们 —— 用户的意图是"这一轮连同子任务都停"；
//	② 回合被 DefaultAgentTimeout 截断 / 浏览器断流 → **不发**，子 Agent 继续跑完：
//	   它们本就跑在独立任务里，终态落库、结论仍能回到对话。
//
// 引擎侧因此不再在"父回合被取消"时一律连带取消子 Agent —— 那条分支分不清 ①/②，
// 一律连带的结果是"父回合超时 → 子任务被杀 → 结论永远回不来"。
func BroadcastSubagentSessionCancel(ctx context.Context, sessionID, reason string) error {
	if sessionID == "" {
		return nil
	}
	if db.Redis == nil {
		return errRedisUnavailable
	}
	if reason == "" {
		reason = "parent"
	}
	payload, err := json.Marshal(subagentCancelBroadcast{SessionID: sessionID, Reason: reason})
	if err != nil {
		return err
	}
	return db.Redis.Publish(ctx, subagentCancelChannel(), payload).Err()
}

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

	// P4-1：登记在引擎启动 run 时写入（subagent:run:{run_id}）。映射的价值不只是"知道谁在跑"，
	// 更在于**没有映射时可以下明确结论**：这个作业已经没人持有了。
	owner, ownerMapped := engine.SubagentRunOwner(r.Context(), runID)

	// P4-2：等回执 —— 只有持有该 run 的实例命中注册表并真的发出取消，才算取消生效。
	// 此前这里无条件返回 accepted，于是"实例已重启/被驱逐"的场景下前端显示"正在停止"、
	// DB 里的行却永远停在 running（假成功，比报错更难排查）。
	acked := h.waitCancelAck(r.Context(), runID)
	replyStatus := cancelStatusFor(acked)
	if acked {
		slog.Info("subagent cancel accepted", "run_id", runID, "user_id", claims.UserID,
			"owner", owner.InstanceID)
		OK(w, map[string]string{"status": replyStatus, "run_id": runID})
		return
	}

	// 没有实例认领：实例已退出（映射随 TTL 过期或已在关机时注销）或 Redis 抖动。
	// 如实收敛为 lost —— 否则这行会一直"运行中"，而用户已经点了停止。
	marked := h.markRunLost(r.Context(), subagentTenant(claims), runID, lostNoOwner)
	slog.Warn("subagent cancel had no owner", "run_id", runID, "user_id", claims.UserID,
		"owner_mapped", ownerMapped, "marked_lost", marked)
	OK(w, map[string]any{
		"status":       replyStatus,
		"run_id":       runID,
		"reason":       lostNoOwner,
		"owner_mapped": ownerMapped,
		"marked_lost":  marked,
	})
}

// CancelSessionRuns 中止某会话下所有活跃子 Agent run。
//
// 刻意**不等待回执**：一次回执无法代表 N 个 run（有多少个实例认领、认领了哪几个都不好
// 在一条回复里表达）。会话级取消的语义是"尽力停止"：未被认领的 run 会由收口器
// （按最后活跃时间判 lost）兜住。单 run 取消则不同 —— 那里能给出确定的结论（见 CancelRun）。
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
