package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/robfig/cron/v3"

	"github.com/athenavi/chiron/internal/db"
)

// superagent_schedule.go —— 用户级 Super-agent 定时任务。
//
// 与 `/v1/admin/cron-jobs` 的区别不在实现而在归属：那张表按 tenant_id 过滤、task
// 直打引擎且不过会话锁，是平台维护用途；这里的任务属于个人，动作只有一件 ——
// 到点给自己的会话开一个接管窗口，之后仍由同一个驱动循环推进，因此天然落在
// 会话锁、租户并发闸门与计费预检之内。

const (
	// maxSuperAgentSchedules 是每用户的定时任务条数上限（判据取 user_id）。
	maxSuperAgentSchedules = 10

	minScheduleWindowSeconds = 60
	maxScheduleWindowSeconds = 86400
	maxScheduleNameLength    = 128
)

// scheduleRow 是 superagent_schedules 的一行（含调度器需要的归属字段）。
type scheduleRow struct {
	ID            string
	TenantID      int16
	UserID        string
	Name          string
	Schedule      string
	WindowSeconds int
	SessionIDs    []string
	Enabled       bool
	LastRunAt     *time.Time
	LastStatus    string
}

// scheduleView 是对外投影。不暴露 lease/tenant 等内部字段。
type scheduleView struct {
	ID            string     `json:"id"`
	Name          string     `json:"name,omitempty"`
	Schedule      string     `json:"schedule"`
	WindowSeconds int        `json:"window_seconds"`
	SessionIDs    []string   `json:"session_ids"`
	Enabled       bool       `json:"enabled"`
	LastRunAt     *time.Time `json:"last_run_at,omitempty"`
	LastStatus    string     `json:"last_status,omitempty"`
}

func (s scheduleRow) view() scheduleView {
	return scheduleView{
		ID:            s.ID,
		Name:          s.Name,
		Schedule:      s.Schedule,
		WindowSeconds: s.WindowSeconds,
		SessionIDs:    s.SessionIDs,
		Enabled:       s.Enabled,
		LastRunAt:     s.LastRunAt,
		LastStatus:    s.LastStatus,
	}
}

type scheduleRequest struct {
	Name          string   `json:"name"`
	Schedule      string   `json:"schedule"`
	WindowSeconds int      `json:"window_seconds"`
	SessionIDs    []string `json:"session_ids"`
	Enabled       bool     `json:"enabled"`
}

// tickOfWindow 给定时任务开出的接管挑一个节拍。
//
// 窗口比一拍还短时取窗口本身：否则驱动循环的候选查询永远等不到第二拍，
// 用户看到"开了窗口"却一次都没推进（与 SaveConfig 的同名校验同一理由）。
func tickOfWindow(windowSeconds int) int {
	tick := defaultTickSeconds
	if windowSeconds < tick {
		tick = windowSeconds
	}
	if tick < minTickSeconds {
		tick = minTickSeconds
	}
	return tick
}

// normalizeSchedule 归一化并校验请求体，返回给用户看的错误文案（空串 = 通过）。
func normalizeSchedule(body scheduleRequest) (scheduleRequest, string) {
	spec := strings.TrimSpace(body.Schedule)
	if spec == "" {
		return body, "schedule is required"
	}
	if _, err := cron.ParseStandard(spec); err != nil {
		return body, "invalid cron expression"
	}
	name := strings.TrimSpace(body.Name)
	if len([]rune(name)) > maxScheduleNameLength {
		return body, "name is too long"
	}
	ids := dedupeNonEmpty(body.SessionIDs)
	if len(ids) == 0 {
		return body, "session_ids is required"
	}
	// 一个任务一次最多开这么多个窗口：它本身就是每用户同时生效接管的上限。
	if len(ids) > maxActiveTakeovers {
		return body, "too many sessions in one schedule"
	}
	if body.WindowSeconds < minScheduleWindowSeconds || body.WindowSeconds > maxScheduleWindowSeconds {
		return body, "window_seconds out of range"
	}
	body.Name, body.Schedule, body.SessionIDs = name, spec, ids
	return body, ""
}

func loadSchedule(ctx context.Context, id, userID string) (scheduleRow, error) {
	var row scheduleRow
	var idsJSON string
	var tenantID int16
	err := db.GlobalDBManager.QueryRow(ctx, `
		SELECT id, tenant_id, COALESCE(name, ''), schedule, window_seconds,
		       session_ids::text, enabled, last_run_at, COALESCE(last_status, '')
		  FROM superagent_schedules
		 WHERE id = $1 AND user_id = $2`, id, userID).
		Scan(&row.ID, &tenantID, &row.Name, &row.Schedule, &row.WindowSeconds,
			&idsJSON, &row.Enabled, &row.LastRunAt, &row.LastStatus)
	if err != nil {
		return row, err
	}
	row.TenantID, row.UserID = tenantID, userID
	if json.Unmarshal([]byte(idsJSON), &row.SessionIDs) != nil {
		return row, errInvalidSessionIDs
	}
	return row, nil
}

func (h *SuperAgentHandler) ListSchedules(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}
	rows, err := db.GlobalDBManager.Query(r.Context(), `
		SELECT id, tenant_id, COALESCE(name, ''), schedule, window_seconds,
		       session_ids::text, enabled, last_run_at, COALESCE(last_status, '')
		  FROM superagent_schedules
		 WHERE user_id = $1
		 ORDER BY created_at DESC`, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list super-agent schedules failed")
		return
	}
	defer rows.Close()

	out := make([]scheduleView, 0, 4)
	for rows.Next() {
		var row scheduleRow
		var idsJSON string
		if err := rows.Scan(&row.ID, &row.TenantID, &row.Name, &row.Schedule, &row.WindowSeconds,
			&idsJSON, &row.Enabled, &row.LastRunAt, &row.LastStatus); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan super-agent schedule failed")
			return
		}
		row.UserID = userID
		if json.Unmarshal([]byte(idsJSON), &row.SessionIDs) != nil {
			// 一行坏数据不该让整个列表失败：跳过它，其余照常返回。
			slog.Warn("super-agent schedule has invalid session_ids", "id", row.ID)
			continue
		}
		out = append(out, row.view())
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list super-agent schedules failed")
		return
	}
	OK(w, map[string]any{"schedules": out, "limit": maxSuperAgentSchedules})
}

func (h *SuperAgentHandler) CreateSchedule(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	tenantID := GetTenantID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body scheduleRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	req, msg := normalizeSchedule(body)
	if msg != "" {
		BadRequest(w, msg)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}

	ctx := r.Context()
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create super-agent schedule failed")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 配额判定必须与插入同一临界区：先查后插挡不住两个并发请求同时通过。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`,
		"superagent-schedules:"+userID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create super-agent schedule failed")
		return
	}
	var count int
	if err := tx.QueryRow(ctx,
		`SELECT count(*) FROM superagent_schedules WHERE user_id = $1`, userID).Scan(&count); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create super-agent schedule failed")
		return
	}
	if count >= maxSuperAgentSchedules {
		BadRequest(w, "schedule limit reached")
		return
	}
	if err := assertSessionsOwned(ctx, tx, req.SessionIDs, tenantID, userID); err != nil {
		h.respondStartErr(w, err)
		return
	}

	idsJSON, err := json.Marshal(req.SessionIDs)
	if err != nil {
		InternalError(w, "invalid session list")
		return
	}
	row := scheduleRow{
		ID: uuid.NewString(), TenantID: tenantID, UserID: userID,
		Name: req.Name, Schedule: req.Schedule, WindowSeconds: req.WindowSeconds,
		SessionIDs: req.SessionIDs, Enabled: req.Enabled,
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO superagent_schedules
		        (id, tenant_id, user_id, name, schedule, window_seconds, session_ids, enabled)
		VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)`,
		row.ID, row.TenantID, row.UserID, row.Name, row.Schedule,
		row.WindowSeconds, string(idsJSON), row.Enabled); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create super-agent schedule failed")
		return
	}
	if err := tx.Commit(ctx); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create super-agent schedule failed")
		return
	}
	Created(w, row.view())
}

func (h *SuperAgentHandler) UpdateSchedule(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	tenantID := GetTenantID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "schedule id is required")
		return
	}
	var body scheduleRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	req, msg := normalizeSchedule(body)
	if msg != "" {
		BadRequest(w, msg)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}

	ctx := r.Context()
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update super-agent schedule failed")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if err := assertSessionsOwned(ctx, tx, req.SessionIDs, tenantID, userID); err != nil {
		h.respondStartErr(w, err)
		return
	}
	idsJSON, err := json.Marshal(req.SessionIDs)
	if err != nil {
		InternalError(w, "invalid session list")
		return
	}
	tag, err := tx.Exec(ctx, `
		UPDATE superagent_schedules
		   SET name = $3, schedule = $4, window_seconds = $5, session_ids = $6::jsonb,
		       enabled = $7, updated_at = NOW()
		 WHERE id = $1 AND user_id = $2`,
		id, userID, req.Name, req.Schedule, req.WindowSeconds, string(idsJSON), req.Enabled)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update super-agent schedule failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "schedule not found")
		return
	}
	if err := tx.Commit(ctx); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update super-agent schedule failed")
		return
	}
	row, err := loadSchedule(ctx, id, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load super-agent schedule failed")
		return
	}
	OK(w, row.view())
}

func (h *SuperAgentHandler) DeleteSchedule(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "schedule id is required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM superagent_schedules WHERE id = $1 AND user_id = $2`, id, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete super-agent schedule failed")
		return
	}
	if tag.RowsAffected() == 0 {
		// 不存在与"不是我的"合并成同一个回答，不泄漏存在性。
		NotFound(w, "schedule not found")
		return
	}
	NoContent(w)
}

// TriggerSchedule 立即试跑一次：与到点触发走同一个执行函数，只是不等 cron。
func (h *SuperAgentHandler) TriggerSchedule(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "schedule id is required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}
	row, err := loadSchedule(r.Context(), id, userID)
	if err != nil {
		if isNoRows(err) {
			NotFound(w, "schedule not found")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "load super-agent schedule failed")
		return
	}

	// 试跑不抢调度槽位（那是给 cron 的"同一次调度"去重用的），但仍走同一个执行体，
	// 因此并发上限、会话锁与预算约束一致。
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 30*time.Second)
	defer cancel()
	opened, runErr := startScheduleTakeovers(ctx, row)
	if runErr != nil {
		logAndRespond(w, runErr, http.StatusInternalServerError, "run super-agent schedule failed")
		return
	}
	Accepted(w, map[string]any{"status": "accepted", "opened": opened, "id": id})
}

// executeSchedule cron 到点触发：先抢"本次调度"槽位（多副本同一分钟只有第一个执行），
// 再抢行级租约（同一任务不并发运行），然后给任务的每个会话开接管窗口。
func (s *CronScheduler) executeSchedule(ctx context.Context, j scheduleRow) {
	if !tryAcquireScheduleSlot(ctx, "sa:"+j.ID, time.Now()) {
		slog.Debug("super-agent schedule skipped: slot already claimed", "id", j.ID)
		return
	}
	token := newLeaseToken()
	tag, err := db.GlobalDBManager.Exec(ctx, `
		UPDATE superagent_schedules SET run_lease_at = NOW(), run_lease_token = $3
		 WHERE id = $1 AND enabled = true
		   AND (run_lease_at IS NULL OR run_lease_at < NOW() - make_interval(secs => $2))`,
		j.ID, cronLeaseStaleScheduledSecs, token)
	if err != nil {
		slog.Warn("super-agent schedule lease acquire failed", "id", j.ID, "error", err)
		return
	}
	if tag.RowsAffected() != 1 {
		slog.Debug("super-agent schedule skipped: lease held by another instance", "id", j.ID)
		return
	}

	// 归还脱离取消链：执行超时的那一刻 ctx 已失效，同 ctx 发 UPDATE 必然失败，
	// 租约就会挂到 stale 阈值之前谁都抢不到（与 runJob 的 returnLease 同一理由）。
	defer func() {
		relCtx, cancel := cronReleaseContext(ctx)
		defer cancel()
		if _, err := db.GlobalDBManager.Exec(relCtx,
			`UPDATE superagent_schedules SET run_lease_at = NULL, run_lease_token = NULL
			  WHERE id = $1 AND run_lease_token = $2`, j.ID, token); err != nil {
			slog.Error("super-agent schedule lease release failed", "id", j.ID, "error", err)
		}
	}()

	if _, err := startScheduleTakeovers(ctx, j); err != nil {
		slog.Warn("super-agent schedule run failed", "id", j.ID, "error", err)
		// startScheduleTakeovers 只在成功时写运行结果；失败要显式记下来，
		// 否则界面上这个任务永远停在"上次运行时间"上，看不出它一直在失败。
		recCtx, cancel := cronReleaseContext(ctx)
		defer cancel()
		_, _ = db.GlobalDBManager.Exec(recCtx,
			`UPDATE superagent_schedules SET last_run_at = NOW(), last_status = 'failed' WHERE id = $1`,
			j.ID)
	}
}
func assertSessionsOwned(ctx context.Context, tx pgx.Tx, sessionIDs []string, tenantID int16, userID string) error {
	var owned int
	if err := tx.QueryRow(ctx,
		`SELECT count(*) FROM sessions
		  WHERE id::text = ANY($1::text[]) AND tenant_id = $2 AND user_id = $3`,
		sessionIDs, tenantID, userID).Scan(&owned); err != nil {
		return err
	}
	if owned != len(sessionIDs) {
		return errSessionNotOwned
	}
	return nil
}

// startScheduleTakeovers 到点给该任务的每个会话开一个接管窗口。
//
// 返回实际开出的窗口数：已有接管在跑的会话会被跳过（同会话同时只允许一个未终结
// 接管），达到每用户并发上限时停止。跳过不是失败 —— 定时任务的价值在于"到点
// 尝试推进"，而不是与用户手动开的接管抢同一个会话。
//
// 预算取用户自己的 `superagent_takeover.budget.token_cap`：定时任务是无人值守的，
// 若在这里把 cap 写成 0（不限），它就成了绕过硬闸门的后门 —— 用户以为设了上限，
// 而定时任务开出的接管永远撞不到它。
func startScheduleTakeovers(ctx context.Context, sch scheduleRow) (int, error) {
	if db.GlobalDBManager == nil {
		return 0, errScheduleUnavailable
	}
	tokenCap := loadTakeoverConfig(ctx, sch.UserID).Budget.TokenCap
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 与 SaveConfig 用同一把锁：两者都在给同一用户开接管，配额必须共享同一个临界区，
	// 否则"先查后插"在并发下会各自看到 4 个而一起插到 6 个。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`,
		"superagent:"+sch.UserID); err != nil {
		return 0, err
	}

	tick := tickOfWindow(sch.WindowSeconds)
	opened := 0
	for _, sid := range sch.SessionIDs {
		var busy bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM superagent_takeovers
			                WHERE session_id = $1 AND status IN ('active', 'stopping'))`,
			sid).Scan(&busy); err != nil {
			return opened, err
		}
		if busy {
			continue
		}
		var active int
		if err := tx.QueryRow(ctx,
			`SELECT count(*) FROM superagent_takeovers
			  WHERE user_id = $1 AND status IN ('active', 'stopping')`, sch.UserID).Scan(&active); err != nil {
			return opened, err
		}
		if active >= maxActiveTakeovers {
			break
		}
		if _, err := tx.Exec(ctx, `
			INSERT INTO superagent_takeovers
			        (id, tenant_id, user_id, session_id, status, ends_at, token_cap,
			         tick_seconds, max_rounds)
			VALUES ($1, $2, $3, $4, 'active',
			        now() + make_interval(secs => $5), $6, $7, 0)`,
			uuid.NewString(), sch.TenantID, sch.UserID, sid, sch.WindowSeconds, tokenCap, tick); err != nil {
			// 唯一索引兜住并发（另一副本刚给同一会话开了窗口）：跳过，不算失败。
			if isUniqueViolation(err) {
				continue
			}
			return opened, err
		}
		opened++
	}

	// 一个窗口都没开成 = 这次触发被跳过了，不是失败：所有会话都已有接管在跑，
	// 或用户已达并发上限。用 `skipped_busy` 而不是 `failed`，验收第 10 条要求
	// 用户能分清"没跑"和"跑失败"。
	status := "skipped_busy"
	if opened > 0 {
		status = "success"
	}
	if _, err := tx.Exec(ctx,
		`UPDATE superagent_schedules SET last_run_at = NOW(), last_status = $2 WHERE id = $1`,
		sch.ID, status); err != nil {
		return opened, err
	}
	if err := tx.Commit(ctx); err != nil {
		return opened, err
	}
	return opened, nil
}
