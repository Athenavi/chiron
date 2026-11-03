package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
)

// superagent.go —— Super-agent 的「受限接管」：用户显式开启、限时、限额、可随时打断。
//
// 分两层：配置（用户想要什么）存在 `users.settings.superagent_takeover`，运行态
// （此刻正在发生什么）落在 `superagent_takeovers`。分开是因为二者生命周期不同 ——
// 改配置不该悄悄改掉已在进行的接管的到期时间。

const (
	takeoverStatusActive   = "active"
	takeoverStatusStopping = "stopping"
	takeoverStatusStopped  = "stopped"

	// maxActiveTakeovers 是每用户同时生效的接管上限（判据取 user_id，不是 tenant_id）。
	maxActiveTakeovers = 5
	minTakeoverSeconds = 60
	maxTakeoverSeconds = 86400

	// 节拍默认 3 分钟：接管的价值在周期性推进，比这更密只会让账单涨得更快。
	defaultTickSeconds = 180
	minTickSeconds     = 60
	maxTickSeconds     = 3600
)

// windowConfig 描述一次接管的时长与节拍。
type windowConfig struct {
	Kind            string `json:"kind"` // countdown | schedule
	DurationSeconds int    `json:"duration_seconds"`
	// TickSeconds 是两拍之间的间隔；0 表示用驱动侧默认值。
	TickSeconds int `json:"tick_seconds"`
	// MaxRounds 是单窗口回合数上限；0 表示不设限（只受 token 预算约束）。
	MaxRounds int `json:"max_rounds"`
}

// budgetConfig 是硬上限：提示是软的，这里到点就停。
type budgetConfig struct {
	TokenCap int64 `json:"token_cap"`
}

// takeoverConfig 是存进 users.settings 的用户意图。
type takeoverConfig struct {
	Enabled    bool         `json:"enabled"`
	SessionIDs []string     `json:"session_ids"`
	Window     windowConfig `json:"window"`
	Budget     budgetConfig `json:"budget"`
}

// takeover 是运行态的对外投影。
type takeover struct {
	ID         string    `json:"id"`
	SessionID  string    `json:"session_id"`
	Status     string    `json:"status"`
	StartedAt  time.Time `json:"started_at"`
	EndsAt     time.Time `json:"ends_at"`
	StopReason string    `json:"stop_reason,omitempty"`
	// LastStatus 是上一拍的结果（如 skipped_busy）。与 StopReason 分开：
	// 它说的是"这一拍没跑成"，接管本身仍在进行，写进 stop_reason 会被读成已终止。
	LastStatus string `json:"last_status,omitempty"`
	TokenSpent int64  `json:"token_spent"`
	// TokenCap == 0 表示不限量（此时不按预算自动停）。
	TokenCap int64 `json:"token_cap"`
}

type SuperAgentHandler struct {
	sessionMgr *session.Manager
	hub        *broadcast.Hub
}

func NewSuperAgentHandler(sessionMgr *session.Manager, hub *broadcast.Hub) *SuperAgentHandler {
	return &SuperAgentHandler{sessionMgr: sessionMgr, hub: hub}
}

// 接管状态事件。前端遮罩靠它实时收敛，但**快照仍是权威来源**：接管态不属于
// 某一轮的 token 流，断线重连期间的事件要靠重放补齐，只信事件会让刷新后的遮罩错位。
const (
	eventTakeoverStarted  = "superagent.takeover_started"
	eventTakeoverStopping = "superagent.takeover_stopping"
	eventTakeoverEnded    = "superagent.takeover_ended"
)

// publishTakeoverEvent 同时带 SessionID 与 UserID：会话流让正开着该会话的页面立刻
// 弹遮罩，用户通道则让没打开会话的人也能看到"我的某个会话正在被接管"。
func publishTakeoverEvent(hub *broadcast.Hub, eventType, takeoverID, sessionID, userID, status string) {
	if hub == nil {
		return
	}
	hub.Publish(broadcast.Event{
		Type:      eventType,
		SessionID: sessionID,
		UserID:    userID,
		Data: map[string]string{
			"takeover_id": takeoverID,
			"session_id":  sessionID,
			"status":      status,
		},
	})
}

// loadTakeoverConfig 读用户意图。任何异常（无行、非 JSON、字段缺失）都当"未开启"：
// 接管会花钱，读不出来时必须朝安全方向失败。
func loadTakeoverConfig(ctx context.Context, userID string) takeoverConfig {
	cfg := takeoverConfig{Window: windowConfig{Kind: "countdown"}}
	if db.GlobalDBManager == nil || userID == "" {
		return cfg
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT settings::text AS settings FROM users WHERE id = $1`, userID)
	if err != nil || row == nil {
		return cfg
	}
	var raw struct {
		SuperAgent *takeoverConfig `json:"superagent_takeover"`
	}
	if json.Unmarshal([]byte(stringOf(row["settings"])), &raw) != nil || raw.SuperAgent == nil {
		return cfg
	}
	return *raw.SuperAgent
}

// Snapshot 返回当前用户的接管配置与运行态。前端以快照为准、SSE 只做增量。
//
// 支持 `?session_id=`：打开某个会话时只关心这一个会话有没有被接管，
// 否则页面要拉全部接管再自己过滤，白白多传一份别的会话的运行态。
func (h *SuperAgentHandler) Snapshot(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	cfg := loadTakeoverConfig(r.Context(), userID)
	items, err := listTakeovers(r.Context(), userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list takeovers failed")
		return
	}
	if sid := r.URL.Query().Get("session_id"); sid != "" {
		filtered := make([]takeover, 0, 1)
		for _, t := range items {
			if t.SessionID == sid {
				filtered = append(filtered, t)
			}
		}
		items = filtered
	}
	OK(w, map[string]any{"config": cfg, "takeovers": items})
}

// listTakeovers 只返回未终结的接管：已 stopped/expired 的行是历史，留给审计。
//
// 只按 user_id 收窄、不带 tenant_id：users.id 是全局主键，加了租户条件反而会因
// claims 里 tenant_id 缺失（withTenantCtx 在 TenantID==0 时不注入）而静默查空 ——
// 前端据此显示"没有接管在进行"，而实际上它正在花钱，这是最危险的误导方向。
func listTakeovers(ctx context.Context, userID string) ([]takeover, error) {
	if db.GlobalDBManager == nil {
		return nil, nil
	}
	rows, err := db.GlobalDBManager.Query(ctx, `
		SELECT id, session_id, status, started_at, ends_at,
		       COALESCE(stop_reason, ''), COALESCE(last_status, ''), token_spent, token_cap
		  FROM superagent_takeovers
		 WHERE user_id = $1
		   AND status IN ('active', 'stopping')
		 ORDER BY started_at DESC`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make([]takeover, 0, 4)
	for rows.Next() {
		var t takeover
		if err := rows.Scan(&t.ID, &t.SessionID, &t.Status, &t.StartedAt, &t.EndsAt,
			&t.StopReason, &t.LastStatus, &t.TokenSpent, &t.TokenCap); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

type startTakeoverRequest struct {
	SessionIDs      []string `json:"session_ids"`
	DurationSeconds int      `json:"duration_seconds"`
	TokenCap        int64    `json:"token_cap"`
	TickSeconds     int      `json:"tick_seconds"`
	MaxRounds       int      `json:"max_rounds"`
}

// SaveConfig 写用户意图；`enabled=true` 时在同一请求里开出接管窗口。
//
// 合并成一次写入是有意的：分成"改配置"和"开窗口"两步，中间若失败就会留下
// "开关已打开、却没有任何接管在跑"的状态，前端据此显示"接管中"而实际没有 ——
// 用户会放心离开电脑。宁可整体失败让他重试。
func (h *SuperAgentHandler) SaveConfig(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	tenantID := GetTenantID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		Enabled         bool     `json:"enabled"`
		SessionIDs      []string `json:"session_ids"`
		DurationSeconds int      `json:"duration_seconds"`
		TokenCap        int64    `json:"token_cap"`
		TickSeconds     int      `json:"tick_seconds"`
		MaxRounds       int      `json:"max_rounds"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}

	ids := dedupeNonEmpty(body.SessionIDs)
	// 全部校验先做：任何一项不合规就整体拒绝，绝不留下"开关已打开、窗口没开成"的半成品。
	if body.Enabled && len(ids) == 0 {
		BadRequest(w, "session_ids is required when enabling")
		return
	}
	if body.Enabled && (body.DurationSeconds < minTakeoverSeconds ||
		body.DurationSeconds > maxTakeoverSeconds) {
		BadRequest(w, "duration_seconds out of range")
		return
	}
	if len(ids) > maxActiveTakeovers {
		BadRequest(w, "too many sessions in one request")
		return
	}

	// 节拍：0 表示用默认值；给定则必须在可解释的区间内 —— 太快会让账单涨得
	// 没有意义，太慢则接管窗口结束前根本跑不到一拍。
	tick := body.TickSeconds
	if tick == 0 {
		tick = defaultTickSeconds
	}
	if tick < minTickSeconds || tick > maxTickSeconds {
		BadRequest(w, "tick_seconds out of range")
		return
	}
	// 轮数上限：0 = 不限（已定），负数没有意义，直接拒绝而不是静默当 0。
	if body.MaxRounds < 0 {
		BadRequest(w, "max_rounds must not be negative")
		return
	}
	if body.Enabled && body.DurationSeconds < tick {
		// 窗口比一个节拍还短，等于开了一个永远跑不到第一拍的接管：
		// 用户会看到"已开启"却什么都不发生。
		BadRequest(w, "duration_seconds must be at least tick_seconds")
		return
	}

	cfg := takeoverConfig{Enabled: body.Enabled, SessionIDs: ids}
	cfg.Window = windowConfig{
		Kind:            "countdown",
		DurationSeconds: body.DurationSeconds,
		TickSeconds:     tick,
		MaxRounds:       body.MaxRounds,
	}
	cfg.Budget = budgetConfig{TokenCap: body.TokenCap}

	payload, err := json.Marshal(map[string]any{"superagent_takeover": cfg})
	if err != nil {
		InternalError(w, "invalid config")
		return
	}

	if !body.Enabled || len(ids) == 0 {
		// 只关不开：意图单独落库即可，不涉及运行态。
		if _, err := db.GlobalDBManager.Exec(r.Context(),
			`UPDATE users SET settings = COALESCE(settings::jsonb,'{}'::jsonb) || $1::jsonb,
			                  updated_at = NOW() WHERE id = $2`,
			string(payload), userID); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "save super-agent config failed")
			return
		}
		OK(w, map[string]any{"config": cfg, "takeovers": []takeover{}})
		return
	}

	settingsJSON := string(payload)
	// 必须传归一化后的 tick 与 cfg 里的一致：body.TickSeconds 为 0 时若原样落库，
	// 驱动循环的 `make_interval(secs => 0)` 会让每一轮扫描都判定"该跑了"，
	// 每 30 秒拉一整个回合 —— 用户没配节拍，账单却按最密的方式走。
	created, err := h.startTx(r.Context(), userID, tenantID, ids, startTakeoverRequest{
		SessionIDs:      ids,
		DurationSeconds: body.DurationSeconds,
		TokenCap:        body.TokenCap,
		TickSeconds:     tick,
		MaxRounds:       body.MaxRounds,
	}, &settingsJSON)
	if err != nil {
		h.respondStartErr(w, err)
		return
	}
	for _, t := range created {
		publishTakeoverEvent(h.hub, eventTakeoverStarted, t.ID, t.SessionID, userID, t.Status)
	}
	Created(w, map[string]any{"config": cfg, "takeovers": created})
}

// startTx 在一个事务里写用户意图（settingsJSON 非 nil 时）与接管记录。
//
// 两者同事务是必须的：分开写会留下"开关已打开、却没有任何接管在跑"的半成品，
// 前端据此显示"接管中"而实际没有 —— 用户会放心离开电脑。
func (h *SuperAgentHandler) startTx(
	ctx context.Context, userID string, tenantID int16, ids []string,
	body startTakeoverRequest, settingsJSON *string,
) ([]takeover, error) {
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 配额判定必须与插入同一临界区：先查后插挡不住两个并发请求同时通过。
	// 按 user_id 取事务级 advisory lock，锁随提交/回滚自动释放。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, "superagent:"+userID); err != nil {
		return nil, err
	}

	var active int
	if err := tx.QueryRow(ctx,
		`SELECT count(*) FROM superagent_takeovers
		  WHERE user_id = $1 AND status IN ('active', 'stopping')`, userID).Scan(&active); err != nil {
		return nil, err
	}
	if active+len(ids) > maxActiveTakeovers {
		return nil, errTooManyTakeovers
	}

	if settingsJSON != nil {
		if _, err := tx.Exec(ctx,
			`UPDATE users SET settings = COALESCE(settings::jsonb,'{}'::jsonb) || $1::jsonb,
			                  updated_at = NOW() WHERE id = $2`,
			*settingsJSON, userID); err != nil {
			return nil, err
		}
	}

	out := make([]takeover, 0, len(ids))
	for _, sid := range ids {
		// 归属校验在写表之前：跨租户/跨用户的行一旦落库，后面的每个查询都得靠它兜底。
		var exists bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM sessions
			                WHERE id = $1 AND tenant_id = $2 AND user_id = $3)`,
			sid, tenantID, userID).Scan(&exists); err != nil {
			return nil, err
		}
		if !exists {
			return nil, errSessionNotOwned
		}

		t := takeover{ID: uuid.NewString(), SessionID: sid, Status: takeoverStatusActive, TokenCap: body.TokenCap}
		// ends_at 由 PG 计算：进程时钟偏一秒，"到点自动停"就成了可争议的行为。
		// tick_seconds / max_rounds 随行落库，是因为它们属于**这一次接管**的运行参数：
		// 用户之后改配置不该改变已在进行的接管的节拍。
		if err := tx.QueryRow(ctx, `
			INSERT INTO superagent_takeovers
			        (id, tenant_id, user_id, session_id, status, ends_at, token_cap,
			         tick_seconds, max_rounds)
			VALUES ($1, $2, $3, $4, 'active',
			        now() + make_interval(secs => $5), $6, $7, $8)
			RETURNING started_at, ends_at`,
			t.ID, tenantID, userID, sid, body.DurationSeconds, body.TokenCap,
			body.TickSeconds, body.MaxRounds,
		).Scan(&t.StartedAt, &t.EndsAt); err != nil {
			// 部分唯一索引是并发的第一道闸：同一会话已有未终结接管时由 DB 拒掉。
			if isUniqueViolation(err) {
				return nil, errTakeoverConflict
			}
			return nil, err
		}
		out = append(out, t)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return out, nil
}

var (
	errTooManyTakeovers = errors.New("too many active takeovers")
	errSessionNotOwned  = errors.New("session does not belong to the current user")
	errTakeoverConflict = errors.New("session is already under takeover")

	// 定时任务专用：一行坏掉的 session_ids 不该让整个列表 500，调用方据它跳过该行。
	errInvalidSessionIDs = errors.New("invalid session_ids payload")
	// 数据库不可用时定时任务无从执行（与 superagent 其余路径一致的 fail-closed）。
	errScheduleUnavailable = errors.New("super-agent schedule storage unavailable")
)

func (h *SuperAgentHandler) respondStartErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, errTooManyTakeovers):
		BadRequest(w, "active takeover limit reached")
	case errors.Is(err, errSessionNotOwned):
		Forbidden(w, "session does not belong to the current user")
	case errors.Is(err, errTakeoverConflict):
		BadRequest(w, "session is already under takeover")
	default:
		logAndRespond(w, err, http.StatusInternalServerError, "start takeover failed")
	}
}

// Stop 打断接管。顺序不可颠倒：先把事实写成 stopping 并提交，再取消回合 ——
// 反过来会留一个窗口，驱动循环仍看到 active，于是下一拍又把回合拉起来。
func (h *SuperAgentHandler) Stop(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "takeover id is required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}

	var sessionID string
	err := db.GlobalDBManager.QueryRow(r.Context(), `
		UPDATE superagent_takeovers
		   SET status = 'stopping', stop_reason = 'user_stop', stopping_at = now()
		 WHERE id = $1 AND user_id = $2 AND status = 'active'
		RETURNING session_id`, id, userID).Scan(&sessionID)
	if err != nil {
		if isNoRows(err) {
			// 不存在、已停止、或是别人的 —— 一律当作"没有可停的接管"，不泄漏存在性。
			NotFound(w, "active takeover not found")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "stop takeover failed")
		return
	}

	status, cancelErr := cancelSessionRun(r.Context(), h.sessionMgr, sessionID, userID)
	if cancelErr != nil {
		// 取消不了不等于没停成：事实已写成 stopping，驱动循环不会再拉起新回合。
		// 但会话归属异常说明数据不一致（接管行是我的、会话不是），必须留痕。
		slog.Error("superagent stop: cancel running turn failed",
			"takeover_id", id, "session_id", sessionID, "error", cancelErr)
	}
	// 没有在跑的回合 ⇒ 无需等收口，直接终结；否则留给驱动循环在回合结束时收口。
	if status == "no_active_task" {
		_, _ = db.GlobalDBManager.Exec(r.Context(), `
			UPDATE superagent_takeovers
			   SET status = 'stopped'
			 WHERE id = $1 AND user_id = $2 AND status = 'stopping'`, id, userID)
	}
	// 只广播 stopping：终态 stopped 由驱动循环在回合真正收口后广播，
	// 提前报 ended 会让遮罩先消失、而回合还在跑。
	publishTakeoverEvent(h.hub, eventTakeoverStopping, id, sessionID, userID, takeoverStatusStopping)
	OK(w, map[string]string{"status": "stopping", "id": id, "session_id": sessionID})
}

func dedupeNonEmpty(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, v := range in {
		v = strings.TrimSpace(v)
		if v == "" {
			continue
		}
		if _, dup := seen[v]; dup {
			continue
		}
		seen[v] = struct{}{}
		out = append(out, v)
	}
	return out
}

// isNoRows 判断 pgx 的"没有返回行"。单独包一层是为了让调用点读起来是业务语义。
func isNoRows(err error) bool {
	return errors.Is(err, pgx.ErrNoRows)
}
