package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"golang.org/x/crypto/bcrypt"

	"github.com/athenavi/chiron/internal/db"
)

// account_deletion.go —— 账号注销：申请（冻结）→ 撤销（恢复）/ 到期（归档并清除）。
//
// ── 状态机 ──
//
//	active ──申请──▶ pending_deletion ──撤销──▶ active
//	                        │
//	                        └──到期──▶ purged（终态）
//
// ── 三个关键设计决定 ──
//
//  1. **"清空资产"是冻结，不是立即删除。**
//     产品要求「1 周内可随时撤销」，而删除是不可逆的 —— 申请时就删，
//     撤销就等于空话。所以申请时冻结：余额移入 `credits_frozen`、`credits` 归零
//     （阻止并发中的回合继续扣费）、API Key 吊销、定时任务停用、接管停止。
//     到期才真删。
//
//  2. **不吊销会话。**
//     直觉上"注销就该踢下线"，但那会与「只允许下载或导出」直接冲突：
//     踢下线后用户连导出都做不了。正确做法是**保留登录态、收窄权限** ——
//     中间件只放行导出与撤销两个接口，其余一律 403（见 account_deletion_gate.go）。
//     到期归档完成时才吊销会话。
//
//  3. **冻结前记录被我们停用了哪些对象。**
//     撤销时若笼统 `enabled = true`，会把用户自己早就停用的定时任务重新打开 ——
//     定时任务重新开跑、开始扣费，而用户未必察觉。所以把 id 列表存进
//     `deletion_freeze_state`，撤销时按名单精确恢复。

const (
	// accountDeletionGracePeriod 是撤销窗口。写进对用户的告知，因此落库
	// （`deletion_due_at`）而不是每次按 requested_at 现算 —— 将来调整默认值
	// 不应悄悄改变已申请用户的到期时间。
	accountDeletionGracePeriod = 7 * 24 * time.Hour

	accountStatusActive          = "active"
	accountStatusPendingDeletion = "pending_deletion"
	accountStatusPurged          = "purged"
)

// deletionFreezeState 记录冻结时被我们停用的对象，供撤销时精确恢复。
type deletionFreezeState struct {
	APIKeyIDs    []string `json:"api_key_ids"`
	CronJobIDs   []string `json:"cron_job_ids"`
	ScheduleIDs  []string `json:"schedule_ids"`
	TakeoverIDs  []string `json:"takeover_ids"`
}

// ── 申请注销 ──

type accountDeletionRequest struct {
	// Password 用于确认这是账号本人在操作。
	// 仅在账号设过密码时必填（SSO-only 账号没有密码可验）。
	Password string `json:"password"`
	// Confirm 是显式确认标记。注销是准不可逆操作，不能因为误点一个按钮就进入
	// 冻结状态 —— 前端必须要求用户主动勾选/输入。
	Confirm bool `json:"confirm"`
}

type accountDeletionStatus struct {
	Status      string     `json:"status"` // active / pending_deletion / purged
	RequestedAt *time.Time `json:"requested_at,omitempty"`
	DueAt       *time.Time `json:"due_at,omitempty"`
	// DaysRemaining 是距自动清除的剩余天数（向上取整，最小 0）。
	// 给前端直接展示，避免它自己按客户端时钟算 —— 客户端时间不可信。
	DaysRemaining int `json:"days_remaining"`
	// CanExport / CanCancel 让前端明确知道当前还能做什么。
	CanExport bool `json:"can_export"`
	CanCancel bool `json:"can_cancel"`
}

// RequestAccountDeletion POST /v1/me/deletion —— 申请注销并冻结资产。
func (h *AuthHandler) RequestAccountDeletion(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req accountDeletionRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// 没有显式确认就直接返回，**不做任何状态变更**：
	// 冻结资产会影响用户正常使用，不能靠一个误触的请求触发。
	if !req.Confirm {
		BadRequest(w, "confirmation required: set confirm=true to request account deletion")
		return
	}

	ctx := r.Context()

	var status string
	var role, passwordHash string
	var tenantID int16
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT account_status, COALESCE(role, ''), COALESCE(password_hash, ''), tenant_id
		   FROM users WHERE id = $1`, userID).Scan(&status, &role, &passwordHash, &tenantID); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "deletion request failed")
		return
	}

	// 幂等：已经申请过就返回当前状态，不重复冻结、不重置到期时间。
	// 重置到期时间会让用户无限延长"数据即将被删"的状态，那不符合任何一方利益。
	if status == accountStatusPendingDeletion {
		h.respondDeletionStatus(ctx, w, userID)
		return
	}
	if status == accountStatusPurged {
		// 已清除的账号不可能再"申请注销"，这里返回明确错误而不是静默成功。
		JSON(w, http.StatusGone, APIResponse{Success: false, Error: "account already deleted"})
		return
	}

	// 租户所有者不能自行注销：租户下还有其他成员，删掉 owner 会让整个租户
	// 失去管理者（且数据无主）。这类操作必须走租户转让或联系运维。
	if role == "owner" {
		Forbidden(w, "tenant owner cannot delete the account; transfer ownership first")
		return
	}

	// 密码确认：设过密码的账号必须验密码。
	// 不验的话，一次 XSS 或 CSRF 就能把用户的资产冻结一周 —— 而冻结本身
	// 已经是可感知的破坏（服务立刻不可用）。
	if passwordHash != "" {
		if req.Password == "" {
			BadRequest(w, "password is required to confirm deletion")
			return
		}
		if bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.Password)) != nil {
			db.AuditLog(ctx, userID, tenantID, "account.delete_denied",
				"/v1/me/deletion", "reason=bad_password", r.RemoteAddr, nil)
			Unauthorized(w, "invalid password")
			return
		}
	}

	dueAt, err := freezeUserAssets(ctx, userID)
	if err != nil {
		// 余额未清零是可自行解决的问题，给 400 + 余额参数，
		// 而不是让用户看到无从下手的 500。
		var balErr *deletionBalanceError
		if errors.As(err, &balErr) {
			db.AuditLog(ctx, userID, tenantID, "account.delete_denied",
				"/v1/me/deletion", "reason=balance_not_zero", r.RemoteAddr,
				map[string]interface{}{"credits": balErr.credits})
			JSON(w, http.StatusBadRequest, APIResponse{
				Success: false,
				Error:   balErr.Error(),
				Code:    "account_balance_not_zero",
				Params:  map[string]interface{}{"credits": balErr.credits},
			})
			return
		}
		slog.Error("account deletion: freeze failed", "user_id", userID, "error", err)
		logAndRespond(w, err, http.StatusInternalServerError, "deletion request failed")
		return
	}

	// 审计留痕：冻结是破坏性动作，必须可回溯"谁在什么时候申请了"。
	db.AuditLog(ctx, userID, tenantID, "account.delete_requested", "/v1/me/deletion",
		"", r.RemoteAddr, map[string]interface{}{
			"due_at": dueAt.UTC().Format(time.RFC3339),
		})

	// 立刻失效状态缓存：否则最多 10 秒内该账号仍能用（缓存里存的是 active）。
	// 冻结是"立即生效"的用户预期，不能靠 TTL 自然过期。
	invalidateAccountStatusCache(userID)

	// 打断该用户正在跑的回合。
	//
	// 位置很关键：必须在**事务提交之后**。放在事务内的话，一旦后续步骤失败回滚，
	// 用户会话已经被打断、注销却撤销了 —— 等于白挨一刀。
	//
	// 不打断的后果：余额刚被清零，进行中的回合跑到下一次扣费就会撞"余额不足"，
	// 用户看到的是"点了注销之后突然报错"，而真正的行为应该是"立即停止"。
	h.cancelUserRunningTurns(ctx, userID)

	OK(w, accountDeletionStatus{
		Status:        accountStatusPendingDeletion,
		DueAt:         &dueAt,
		DaysRemaining: daysRemaining(dueAt),
		CanExport:     true,
		CanCancel:     true,
	})
}

// cancelUserRunningTurns 打断该用户在**本实例**上正在跑的回合。
//
// 只遍历 `sessionCancels`（进程内活跃表）而不查 sessions 表：
//   - 后者包含全部历史会话（可能上百条），逐个走 cancelSessionRun 会产生
//     N 次归属查询 + N 次广播，而其中绝大多数并没有在跑；
//   - `sessionCancels` 恰好就是"此刻正在跑的回合"的权威集合。
//
// 跨副本的回合不在这里处理：那台副本会在下一次扣费时撞上余额不足
// （冻结已把 credits 清零）自然停下，而 AccountDeletionGate 也已让该账号
// 无法再发起任何新交互。为注销场景引入跨副本广播不值得 —— 收益是"早停几秒"，
// 代价是给一条本已很少走的路径增加 N 次网络往返。
func (h *AuthHandler) cancelUserRunningTurns(ctx context.Context, userID string) {
	canceled := 0
	sessionCancels.Range(func(key, value any) bool {
		sc, ok := value.(*sessionCancel)
		if !ok || sc == nil || sc.userID != userID {
			return true
		}
		sid, _ := key.(string)
		if sid == "" {
			return true
		}
		if _, err := cancelSessionRun(ctx, h.sessionMgr, sid, userID); err != nil {
			// 单个会话取消失败不该影响其它会话，也不该让注销申请失败。
			slog.Warn("account deletion: cancel running turn failed",
				"user_id", userID, "session", sid, "error", err)
			return true
		}
		canceled++
		return true
	})
	if canceled > 0 {
		slog.Info("account deletion: cancelled running turns",
			"user_id", userID, "count", canceled)
	}
}

// ── 撤销注销 ──

// CancelAccountDeletion POST /v1/me/deletion/cancel —— 在宽限期内撤销并恢复资产。
func (h *AuthHandler) CancelAccountDeletion(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	ctx := r.Context()

	var status string
	var tenantID int16
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT account_status, tenant_id FROM users WHERE id = $1`, userID).
		Scan(&status, &tenantID); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "deletion cancel failed")
		return
	}

	// 已经清除的账号无法撤销：数据已经归档并删除，恢复等于伪造存档。
	if status == accountStatusPurged {
		JSON(w, http.StatusGone, APIResponse{
			Success: false,
			Error:   "account already deleted; data has been archived and removed",
		})
		return
	}
	// 幂等：本来就没申请注销，就当成功。
	if status != accountStatusPendingDeletion {
		OK(w, accountDeletionStatus{Status: accountStatusActive})
		return
	}

	if err := restoreUserAssets(ctx, userID); err != nil {
		slog.Error("account deletion: restore failed", "user_id", userID, "error", err)
		logAndRespond(w, err, http.StatusInternalServerError, "deletion cancel failed")
		return
	}

	db.AuditLog(ctx, userID, tenantID, "account.delete_cancelled", "/v1/me/deletion/cancel",
		"", r.RemoteAddr, nil)
	// 撤销后必须立刻可用：冻结状态不缓存，但保险起见仍清一次
	//（万一将来改成双向缓存，这里不会漏）。
	invalidateAccountStatusCache(userID)
	OK(w, accountDeletionStatus{Status: accountStatusActive})
}

// ── 查询状态 ──

// AccountDeletionStatus GET /v1/me/deletion —— 供"注销确认页"与冻结期横幅渲染。
func (h *AuthHandler) AccountDeletionStatus(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	h.respondDeletionStatus(r.Context(), w, userID)
}

func (h *AuthHandler) respondDeletionStatus(ctx context.Context, w http.ResponseWriter, userID string) {
	var (
		status      string
		requestedAt *time.Time
		dueAt       *time.Time
	)
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT account_status, deletion_requested_at, deletion_due_at
		   FROM users WHERE id = $1`, userID).
		Scan(&status, &requestedAt, &dueAt); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "deletion status failed")
		return
	}
	out := accountDeletionStatus{
		Status:      status,
		RequestedAt: requestedAt,
		DueAt:       dueAt,
	}
	if status == accountStatusPendingDeletion {
		out.CanExport = true
		out.CanCancel = true
		if dueAt != nil {
			out.DaysRemaining = daysRemaining(*dueAt)
		}
	}
	OK(w, out)
}

// daysRemaining 返回距 dueAt 的剩余天数（向上取整，最小 0）。
//
// 向上取整是刻意的：还剩 3 小时应显示"1 天"而不是"0 天"。
// 显示 0 会让用户以为已经过期、放弃导出。
func daysRemaining(dueAt time.Time) int {
	d := time.Until(dueAt)
	if d <= 0 {
		return 0
	}
	// (d + 24h - 1ns) / 24h 向上取整
	return int((d + 24*time.Hour - 1) / (24 * time.Hour))
}

// ── 冻结 / 恢复 ──

// freezeUserAssets 冻结该用户的全部可变现资产，返回自动清除时刻。
//
// 全部在一个事务里：部分冻结（比如余额已清零但 Key 还能用）会让账号处于
// 自相矛盾的状态，而事务回滚能保证"要么全冻、要么全不动"。
func freezeUserAssets(ctx context.Context, userID string) (time.Time, error) {
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return time.Time{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 行锁：并发申请（双开页面双击）时只有一个能继续，另一个看到已 pending。
	var status string
	var credits int
	if err := tx.QueryRow(ctx,
		`SELECT account_status, COALESCE(credits, 0) FROM users WHERE id = $1 FOR UPDATE`,
		userID).Scan(&status, &credits); err != nil {
		return time.Time{}, err
	}
	if status != accountStatusActive {
		return time.Time{}, fmt.Errorf("account is not active (status=%s)", status)
	}

	// 余额必须为 0 才能申请注销。
	//
	// 为什么要求先清零而不是"冻结后到期作废"：账上余额是用户已付费购买的
	// 资产，直接作废等于没收；而注销后账号无法消费，留着又成了悬空余额
	// （既不能花、也没有归属）。让用户在申请前自行用完或走退款，
	// 责任与账目都清晰。
	//
	// 用 ErrDeletionBalanceNotZero 而非普通 error：handler 需要据此返回
	// 可操作的提示（带当前余额），而不是笼统的 500。
	if credits > 0 {
		return time.Time{}, &deletionBalanceError{credits: credits}
	}

	state := deletionFreezeState{
		APIKeyIDs:   []string{},
		CronJobIDs:  []string{},
		ScheduleIDs: []string{},
		TakeoverIDs: []string{},
	}

	// ① 吊销 API Key（软删，保留审计）。
	//    只记录本次真正吊销的那些：撤销时按名单恢复，避免把用户此前已吊销的
	//    Key 复活 —— 那等于凭空恢复一个已作废的凭据。
	rows, err := tx.Query(ctx,
		`UPDATE api_keys SET revoked = true
		  WHERE user_id = $1 AND COALESCE(revoked, false) = false
		  RETURNING id`, userID)
	if err != nil {
		return time.Time{}, fmt.Errorf("revoke api keys: %w", err)
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return time.Time{}, err
		}
		state.APIKeyIDs = append(state.APIKeyIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return time.Time{}, err
	}

	// ② 停用定时任务（用户级）。
	rows, err = tx.Query(ctx,
		`UPDATE cron_jobs SET enabled = false, updated_at = NOW()
		  WHERE user_id = $1 AND enabled = true
		  RETURNING id`, userID)
	if err != nil {
		return time.Time{}, fmt.Errorf("disable cron jobs: %w", err)
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return time.Time{}, err
		}
		state.CronJobIDs = append(state.CronJobIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return time.Time{}, err
	}

	// ③ 停用 Super-agent 定时接管任务。
	rows, err = tx.Query(ctx,
		`UPDATE superagent_schedules SET enabled = false, updated_at = NOW()
		  WHERE user_id = $1 AND enabled = true
		  RETURNING id`, userID)
	if err != nil {
		return time.Time{}, fmt.Errorf("disable schedules: %w", err)
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return time.Time{}, err
		}
		state.ScheduleIDs = append(state.ScheduleIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return time.Time{}, err
	}

	// ④ 停止正在运行的接管窗口。
	//    不停止的话，冻结期间接管仍会按节拍拉起回合并扣费 —— 而余额已经归零，
	//    会撞上余额不足的失败路径，用户看到的是"注销后还在报错"。
	rows, err = tx.Query(ctx,
		`UPDATE superagent_takeovers
		    SET status = 'stopped', stop_reason = 'account_deleted'
		  WHERE user_id = $1 AND status IN ('active', 'stopping')
		  RETURNING id`, userID)
	if err != nil {
		return time.Time{}, fmt.Errorf("stop takeovers: %w", err)
	}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return time.Time{}, err
		}
		state.TakeoverIDs = append(state.TakeoverIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return time.Time{}, err
	}

	stateJSON, err := json.Marshal(state)
	if err != nil {
		return time.Time{}, err
	}

	// ⑤ 余额移入 credits_frozen 并归零账户状态。
	//
	// `credits = 0` 是关键：计费路径直接读这一列，归零后并发中的回合
	// 会立刻因余额不足而停下，不会出现"注销了还在扣钱"。
	// 到期时间由数据库计算并落库（`deletion_due_at` 是写进告知的契约）。
	var dueAt time.Time
	if err := tx.QueryRow(ctx, `
		UPDATE users
		   SET credits_frozen = COALESCE(credits, 0),
		       credits = 0,
		       account_status = $2,
		       deletion_requested_at = NOW(),
		       deletion_due_at = NOW() + make_interval(secs => $3),
		       deletion_freeze_state = $4::jsonb,
		       updated_at = NOW()
		 WHERE id = $1
		RETURNING deletion_due_at`,
		userID, accountStatusPendingDeletion, int(accountDeletionGracePeriod.Seconds()),
		stateJSON).Scan(&dueAt); err != nil {
		return time.Time{}, err
	}

	if err := tx.Commit(ctx); err != nil {
		return time.Time{}, err
	}
	slog.Info("account deletion requested: assets frozen",
		"user_id", userID, "due_at", dueAt.UTC(),
		"api_keys", len(state.APIKeyIDs), "cron_jobs", len(state.CronJobIDs),
		"schedules", len(state.ScheduleIDs), "takeovers", len(state.TakeoverIDs))
	return dueAt, nil
}

// restoreUserAssets 撤销注销：恢复余额与各对象的启用状态。
func restoreUserAssets(ctx context.Context, userID string) error {
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var (
		status      string
		credits     int
		frozen      *int
		stateRaw    []byte
	)
	if err := tx.QueryRow(ctx,
		`SELECT account_status, COALESCE(credits, 0), credits_frozen, deletion_freeze_state
		   FROM users WHERE id = $1 FOR UPDATE`, userID).
		Scan(&status, &credits, &frozen, &stateRaw); err != nil {
		return err
	}
	if status != accountStatusPendingDeletion {
		// 状态已被别处改回（例如运维手工恢复）：不再动资产，直接提交空事务。
		return tx.Commit(ctx)
	}

	var state deletionFreezeState
	if len(stateRaw) > 0 {
		// 解析失败不是致命错误：宁可少恢复几个对象的启用状态，也要把
		// 账号状态与余额恢复 —— 否则用户会卡在"冻结中"却无法再次撤销。
		if err := json.Unmarshal(stateRaw, &state); err != nil {
			slog.Warn("account deletion: freeze state unreadable, restoring balance only",
				"user_id", userID, "error", err)
		}
	}

	// 余额：把冻结值加回去（用加法而非覆盖：万一冻结期间有退款/补偿进账，
	// 覆盖会把它抹掉）。
	frozenAmount := 0
	if frozen != nil {
		frozenAmount = *frozen
	}

	// 按名单精确恢复：只恢复"当初被我们停用的"，不碰用户自己停用的。
	if len(state.APIKeyIDs) > 0 {
		if _, err := tx.Exec(ctx,
			`UPDATE api_keys SET revoked = false WHERE id = ANY($1::text[])`,
			state.APIKeyIDs); err != nil {
			return fmt.Errorf("restore api keys: %w", err)
		}
	}
	if len(state.CronJobIDs) > 0 {
		if _, err := tx.Exec(ctx,
			`UPDATE cron_jobs SET enabled = true, updated_at = NOW()
			  WHERE id = ANY($1::text[])`, state.CronJobIDs); err != nil {
			return fmt.Errorf("restore cron jobs: %w", err)
		}
	}
	if len(state.ScheduleIDs) > 0 {
		if _, err := tx.Exec(ctx,
			`UPDATE superagent_schedules SET enabled = true, updated_at = NOW()
			  WHERE id = ANY($1::text[])`, state.ScheduleIDs); err != nil {
			return fmt.Errorf("restore schedules: %w", err)
		}
	}
	// 接管不恢复：那是一条正在跑的回合，冻结时已被强制结束。
	// 重新置为 active 会让用户莫名其妙多出一个正在花钱的接管窗口。

	if _, err := tx.Exec(ctx, `
		UPDATE users
		   SET credits = COALESCE(credits, 0) + $2,
		       credits_frozen = NULL,
		       account_status = $3,
		       deletion_requested_at = NULL,
		       deletion_due_at = NULL,
		       deletion_freeze_state = NULL,
		       updated_at = NOW()
		 WHERE id = $1`,
		userID, frozenAmount, accountStatusActive); err != nil {
		return err
	}

	if err := tx.Commit(ctx); err != nil {
		return err
	}
	slog.Info("account deletion cancelled: assets restored",
		"user_id", userID, "credits_restored", frozenAmount,
		"api_keys", len(state.APIKeyIDs), "cron_jobs", len(state.CronJobIDs),
		"schedules", len(state.ScheduleIDs))
	return nil
}

// errAccountPurged 供上层区分"账号已清除"这种终态。
var errAccountPurged = errors.New("account purged")

// deletionBalanceError 表示"余额未清零，不能申请注销"。
//
// 单独成类型是因为 handler 要据此返回**可操作**的提示（带上当前余额，
// 让用户知道还差多少），而不是笼统的 500。用字符串匹配判定错误类型
// 在改动文案时就会静默失效。
type deletionBalanceError struct {
	credits int
}

func (e *deletionBalanceError) Error() string {
	return fmt.Sprintf("account balance must be 0 before deletion (current: %d)", e.credits)
}
