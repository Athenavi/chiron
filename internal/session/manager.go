package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/model"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// redisKeyPrefix 统一前缀(N2):由 db.RedisKey 注入 REDIS_KEY_PREFIX。
var redisKeyPrefix = db.RedisKey("session:")

const (
	redisTTL = 2 * time.Hour
)

// ErrSessionNotFound 表示会话不存在（SSE 端点据此放行尚未创建的新会话连接）。
var ErrSessionNotFound = errors.New("session not found")

// Manager provides session CRUD with Redis hot cache + PostgreSQL persistence.
// All methods degrade gracefully when Redis or PG is unavailable.
type Manager struct {
	pool *pgxpool.Pool
	rdb  db.RedisClient
}

func NewManager(pool *pgxpool.Pool, rdb db.RedisClient) *Manager {
	return &Manager{pool: pool, rdb: rdb}
}

// ── Session CRUD ──────────────────────────────────────────────────────────

// GetSession retrieves a session by ID. Checks Redis first, falls back to PG.
func (m *Manager) GetSession(ctx context.Context, id string) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}

	// 1. Redis hot path
	if m.rdb != nil {
		data, err := m.rdb.Get(ctx, redisKeyPrefix+id).Bytes()
		if err == nil {
			var entry sessionCacheEntry
			if json.Unmarshal(data, &entry) == nil && entry.D != nil {
				return entry.D, nil
			}
			// 旧格式（升级前的裸 Session JSON）或损坏条目：删除后回退 PG 自愈
			m.rdb.Del(ctx, redisKeyPrefix+id)
		}
	}

	// 2. PG cold path
	if m.pool == nil {
		return nil, fmt.Errorf("database not available")
	}

	var s model.Session
	err := m.pool.QueryRow(ctx,
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), created_at, updated_at
		 FROM sessions WHERE id = $1`, id).
		Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.CreatedAt, &s.UpdatedAt)
	if err == pgx.ErrNoRows {
		return nil, fmt.Errorf("%w: %s", ErrSessionNotFound, id)
	} else if err != nil {
		// 非法 uuid 格式（如前端旧版 fallback id "session_xxx"）：会话必然不存在，
		// 按 not found 处理（否则 SSE 端点会 500、前端触发"连接已断开"）
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "22P02" {
			return nil, fmt.Errorf("%w: %s", ErrSessionNotFound, id)
		}
		return nil, fmt.Errorf("query session: %w", err)
	}

	// Warm Redis cache
	m.cacheSession(ctx, &s)
	return &s, nil
}

// DefaultTenantID is the default tenant for single-tenant deployments.
// 单一来源见 internal/db/seed.go。
const DefaultTenantID = db.DefaultTenantID

// CreateSession inserts a new session into PG and caches in Redis.
// If id is empty, returns an error.
func (m *Manager) CreateSession(ctx context.Context, id, userID, title string) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}

	if title == "" {
		title = "New Chat"
	}

	now := time.Now()
	s := &model.Session{
		ID:        id,
		UserID:    userID,
		Title:     title,
		CreatedAt: now,
		UpdatedAt: now,
	}

	var uid *string
	if userID != "" {
		uid = &userID
	}

	if m.pool != nil {
		_, err := m.pool.Exec(ctx,
			`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
			 VALUES ($1, $2, $3::uuid, $4, $5, $6)
			 ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, updated_at = EXCLUDED.updated_at`,
			id, DefaultTenantID, uid, title, now, now)
		if err != nil {
			return nil, fmt.Errorf("create session: %w", err)
		}
	}

	m.cacheSession(ctx, s)
	return s, nil
}

// ListSessions returns sessions for a given user, newest first.
// page 从 1 开始，perPage 默认为 100（最大 200）。
func (m *Manager) ListSessions(ctx context.Context, userID string, page, perPage int) ([]model.Session, error) {
	if m.pool == nil {
		return nil, nil
	}
	if page < 1 {
		page = 1
	}
	if perPage < 1 || perPage > 200 {
		perPage = 100
	}
	offset := (page - 1) * perPage

	rows, err := m.pool.Query(ctx,
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), created_at, updated_at
		 FROM sessions
		 WHERE user_id = $1
		 ORDER BY pinned DESC, updated_at DESC
		 LIMIT $2 OFFSET $3`, userID, perPage, offset)
	if err != nil {
		return nil, fmt.Errorf("list sessions: %w", err)
	}
	defer rows.Close()

	var sessions []model.Session
	for rows.Next() {
		var s model.Session
		if err := rows.Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.CreatedAt, &s.UpdatedAt); err != nil {
			slog.Warn("scan session row", "error", err)
			continue
		}
		sessions = append(sessions, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate sessions: %w", err)
	}

	if sessions == nil {
		sessions = []model.Session{}
	}
	return sessions, nil
}

// DeleteSession removes a session from PG and Redis cache.
// Evict cache first so a subsequent GetSession falls through to PG (source of truth)
// even if Redis eviction fails silently.
//
// 依赖表清理：messages / turns / billing_records 均以 session_id 外键引用 sessions，
// 且迁移（a0b3a964fb54、c1f5a83e6b90）未配置 ON DELETE CASCADE，直接
// `DELETE FROM sessions` 会触发外键违反 → 接口 500 "服务器错误"。
// 故在同一事务内按依赖顺序清理：先删引用行，再删会话本身。
func (m *Manager) DeleteSession(ctx context.Context, id string) error {
	if id == "" {
		return fmt.Errorf("session id is required")
	}

	m.evictCache(ctx, id)

	if m.pool == nil {
		return nil
	}

	tx, err := m.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("delete session: begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	stmts := []string{
		`DELETE FROM turns WHERE session_id = $1`,
		`DELETE FROM messages WHERE session_id = $1`,
		`DELETE FROM tool_calls WHERE session_id = $1`,
		// 计费记录属财务数据：只解除会话引用（置 NULL）保留流水，不随会话删除
		`UPDATE billing_records SET session_id = NULL WHERE session_id = $1`,
		`DELETE FROM sessions WHERE id = $1`,
	}
	for _, q := range stmts {
		if _, err := tx.Exec(ctx, q, id); err != nil {
			return fmt.Errorf("delete session: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("delete session: commit: %w", err)
	}

	return nil
}

// SessionUpdate 描述一次会话属性更新：nil 表示不修改该字段。
// 单独抽出类型是为了让 SQL 组装（buildSessionUpdate）可脱离 DB 单测。
type SessionUpdate struct {
	Title  *string
	Pinned *bool
	Tag    *string
}

// empty 表示没有任何字段需要更新。
func (u SessionUpdate) empty() bool {
	return u.Title == nil && u.Pinned == nil && u.Tag == nil
}

// buildSessionUpdate 组装 UPDATE sessions 的语句与参数。
// 只有 title 变更才推进 updated_at（置顶/标签是列表偏好，不算会话活动）；
// tag 传空串表示清除标签（写 NULL，避免留下空串标签）。
func buildSessionUpdate(id string, u SessionUpdate, now time.Time) (string, []interface{}) {
	var sets []string
	var args []interface{}
	if u.Title != nil {
		sets = append(sets, fmt.Sprintf("title = $%d", len(args)+1))
		args = append(args, *u.Title)
		sets = append(sets, fmt.Sprintf("updated_at = $%d", len(args)+1))
		args = append(args, now)
	}
	if u.Pinned != nil {
		sets = append(sets, fmt.Sprintf("pinned = $%d", len(args)+1))
		args = append(args, *u.Pinned)
	}
	if u.Tag != nil {
		sets = append(sets, fmt.Sprintf("tag = NULLIF($%d, '')", len(args)+1))
		args = append(args, *u.Tag)
	}
	args = append(args, id)
	return `UPDATE sessions SET ` + strings.Join(sets, ", ") + ` WHERE id = $` + strconv.Itoa(len(args)), args
}

// UpdateSession updates a session's title / pinned flag / tag, then refreshes
// the Redis cache so a subsequent GetSession returns the fresh row.
// At least one field must be non-nil.
func (m *Manager) UpdateSession(ctx context.Context, id string, upd SessionUpdate) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}
	if upd.empty() {
		return nil, fmt.Errorf("nothing to update")
	}

	if m.pool != nil {
		query, args := buildSessionUpdate(id, upd, time.Now())
		if _, err := m.pool.Exec(ctx, query, args...); err != nil {
			return nil, fmt.Errorf("update session: %w", err)
		}
	}

	m.evictCache(ctx, id)
	return m.GetSession(ctx, id)
}

// ── Message helpers ───────────────────────────────────────────────────────

// SaveMessage inserts a single message into the session's message log.
// The user_id is set to NULL; SaveUserMessage/SaveAssistantMessage should be
// used for user-facing message persistence.
func (m *Manager) SaveMessage(ctx context.Context, sessionID, role, content string) error {
	if m.pool == nil {
		return nil
	}

	_, err := m.pool.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, NULL::uuid, '', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET updated_at = NOW()`,
		sessionID, DefaultTenantID)
	if err != nil {
		return fmt.Errorf("ensure session: %w", err)
	}

	msgID, err := genID()
	if err != nil {
		return fmt.Errorf("generate message id: %w", err)
	}
	_, err = m.pool.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, created_at) VALUES ($1, $2, $3, $4, NOW())`,
		msgID, sessionID, role, content)
	if err != nil {
		return fmt.Errorf("save message: %w", err)
	}

	m.evictCache(ctx, sessionID)
	return nil
}

// isMissingColumn 判断 err 是否为"列不存在"（PG 42703 / undefined_column）。
// 用途：新增可选列时，迁移尚未执行的库仍能走旧路径写入 —— 核心消息不能因为一个
// 展示用字段而写不进去。
func isMissingColumn(err error, column string) bool {
	if err == nil {
		return false
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code == "42703"
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "column") && strings.Contains(msg, strings.ToLower(column))
}

// SaveUserMessage persists the user message immediately at submit time
// (S 修复：上下文丢失 — SSE 中断/停止时不再丢失用户消息，历史可续).
//
// source 标记消息来源：空串 = 用户正常输入；FollowupSource = 子 Agent 自动轮注入
// （见 internal/api/agent_followup.go），前端据此区分渲染。
func (m *Manager) SaveUserMessage(ctx context.Context, sessionID, userID, userContent, turnID, source string) {
	if m.pool == nil || userContent == "" {
		return
	}
	_, err := m.pool.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, NULLIF($3, '')::uuid, '', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET updated_at = NOW()`,
		sessionID, DefaultTenantID, userID)
	if err != nil {
		slog.Warn("ensure session", "error", err)
		return
	}
	msgID, err := genID()
	if err != nil {
		slog.Warn("generate message id", "error", err)
		return
	}
	_, err = m.pool.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, turn_id, source, created_at)
		 VALUES ($1, $2, 'user', $3, NULLIF($4, ''), $5, NOW())`,
		msgID, sessionID, userContent, turnID, source)
	if isMissingColumn(err, "source") {
		// 迁移尚未执行（messages.source 不存在）时退化为旧写入：用户消息绝不能因为
		// 一个来源标记而丢失（丢一条就等于刷新后对话缺头）。
		_, err = m.pool.Exec(ctx,
			`INSERT INTO messages (id, session_id, role, content, turn_id, created_at)
			 VALUES ($1, $2, 'user', $3, NULLIF($4, ''), NOW())`,
			msgID, sessionID, userContent, turnID)
	}
	if err != nil {
		// 失败不再静默（000.md 第 14 条）：用户消息丢失会导致刷新后对话缺头
		slog.Error("save user message", "session", sessionID, "turn", turnID, "error", err)
	}
	_, err = m.pool.Exec(ctx,
		`UPDATE sessions SET title = LEFT($1, 255), updated_at = NOW()
		 WHERE id = $2 AND (title = '' OR title IS NULL)`,
		truncateTitle(userContent), sessionID)
	if err != nil {
		slog.Warn("update session title", "error", err)
	}
	m.evictCache(ctx, sessionID)
}

// SaveAssistantMessage persists the assistant reply (with optional OpenAI-format
// tool_calls JSON) after streaming completes. 分配新 id（一次性写入场景）。
func (m *Manager) SaveAssistantMessage(ctx context.Context, sessionID, assistantContent, toolCallsJSON, turnID string) {
	if m.pool == nil {
		return
	}
	msgID, err := genID()
	if err != nil {
		slog.Warn("generate message id", "error", err)
		return
	}
	m.UpsertAssistantMessage(ctx, sessionID, msgID, assistantContent, toolCallsJSON, turnID)
}

// UpsertAssistantMessage 以固定 messageID 写入/更新一条 assistant 消息。
//
// 用于"增量落库"：回合进行中按节流反复写入同一行（思考块 + 已产生正文 + 工具调用 id 集合），
// 使刷新 / 断线 / 网关重启后仍能看到已产生的部分；回合结束时用同一 id 定型，不产生重复消息。
// 此前只在回合结束才落库，长回合（多轮工具调用，常达数分钟）进行中刷新必然看到"什么都没有"。
func (m *Manager) UpsertAssistantMessage(ctx context.Context, sessionID, messageID, assistantContent, toolCallsJSON, turnID string) {
	if m.pool == nil || messageID == "" {
		return
	}
	if toolCallsJSON == "" {
		toolCallsJSON = "[]"
	}
	// 尚无任何内容（既无正文也无工具调用）：不创建空消息行
	if assistantContent == "" && toolCallsJSON == "[]" {
		return
	}
	_, err := m.pool.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, tool_calls, turn_id, created_at)
		 VALUES ($1, $2, 'assistant', $3, $4::jsonb, NULLIF($5, ''), NOW())
		 ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content,
		   tool_calls = EXCLUDED.tool_calls,
		   turn_id = COALESCE(EXCLUDED.turn_id, messages.turn_id)`,
		messageID, sessionID, assistantContent, toolCallsJSON, turnID)
	if err != nil {
		// 失败不再静默（000.md 第 14 条）：模型已输出但不落库 => 历史与计费不一致
		slog.Error("upsert assistant message", "session", sessionID, "message", messageID, "turn", turnID, "error", err)
	}
	m.evictCache(ctx, sessionID)
}

// pgUUIDOrNil 把字符串转成 *string 供 uuid 列使用：非 uuid 格式返回 nil（写 NULL）。
// 历史数据存在 "session_xxx" 这类非 uuid 会话 ID，直接 ::uuid 强转会让整条 INSERT
// 报 22P02，导致该会话的 turn 记录全部丢失（A5）。
func pgUUIDOrNil(v string) *string {
	if len(v) != 36 {
		return nil
	}
	for i := 0; i < len(v); i++ {
		c := v[i]
		if i == 8 || i == 13 || i == 18 || i == 23 {
			if c != '-' {
				return nil
			}
			continue
		}
		isHex := (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
		if !isHex {
			return nil
		}
	}
	return &v
}

// CreateTurn 记录一次回合的开始（000.md 第 14 条）：turns.created -> running。
// turnID 由调用方生成并贯穿该回合的消息/工具调用/计费落库，便于幂等与故障排查。
// 会话不存在时先补建（与 SaveUserMessage 相同的 upsert），避免 FK 失败。
func (m *Manager) CreateTurn(ctx context.Context, turnID, sessionID, userID string) {
	if m.pool == nil || turnID == "" || sessionID == "" {
		return
	}
	_, err := m.pool.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, NULLIF($3, '')::uuid, '', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET updated_at = NOW()`,
		sessionID, DefaultTenantID, userID)
	if err != nil {
		slog.Error("ensure session for turn", "session", sessionID, "error", err)
	}
	_, err = m.pool.Exec(ctx,
		`INSERT INTO turns (id, session_id, user_id, status, started_at, created_at)
		 VALUES ($1, $2, $3, 'running', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET status = 'running', started_at = NOW()`,
		turnID, pgUUIDOrNil(sessionID), pgUUIDOrNil(userID))
	if err != nil {
		// 回合状态写失败不阻断对话（SSE 照常），但必须可见（不再静默）
		slog.Error("create turn", "turn", turnID, "session", sessionID, "error", err)
	}
}

// FinishTurn 收敛回合终态：completed / failed / cancelled，并记录 token 用量。
// status 为非法值时回退 completed；失败原因写入 turns.error 便于排查。
func (m *Manager) FinishTurn(ctx context.Context, turnID, status, errMsg string, inputTokens, outputTokens int) {
	if m.pool == nil || turnID == "" {
		return
	}
	switch status {
	case "completed", "failed", "cancelled":
	default:
		status = "completed"
	}
	_, err := m.pool.Exec(ctx,
		`UPDATE turns
		 SET status = $2, error = NULLIF($3, ''), input_tokens = $4, output_tokens = $5,
		     finished_at = NOW()
		 WHERE id = $1`,
		turnID, status, errMsg, inputTokens, outputTokens)
	if err != nil {
		slog.Error("finish turn", "turn", turnID, "status", status, "error", err)
	}
}

// SaveToolCall persists a tool call record (S 修复：工具调用过程落库，刷新后显示一致).
func (m *Manager) SaveToolCall(ctx context.Context, sessionID, toolCallID, toolName, inputJSON, turnID string) {
	if m.pool == nil || toolCallID == "" {
		return
	}
	_, err := m.pool.Exec(ctx,
		`INSERT INTO tool_calls (id, session_id, tool_name, input, output, turn_id, created_at)
		 VALUES ($1, $2, $3, $4::jsonb, '', NULLIF($5, ''), NOW())
		 ON CONFLICT (id) DO UPDATE SET tool_name = EXCLUDED.tool_name, input = EXCLUDED.input,
		   turn_id = COALESCE(EXCLUDED.turn_id, tool_calls.turn_id)`,
		toolCallID, sessionID, toolName, normalizeToolInput(inputJSON), turnID)
	if err != nil {
		slog.Error("save tool call", "session", sessionID, "turn", turnID, "error", err)
	}
}

// normalizeToolInput 保证写入 jsonb 列（tool_calls.input / messages.tool_calls）的内容合法。
// 模型的 arguments 可能为空串（无参调用）或被截断的半成品 JSON，直接 $4::jsonb 会因
// invalid input syntax for type json 让整条 INSERT 失败 —— 记录只打日志，表现为
// "工具调用历史与结果刷新后全部丢失"。此处降级为合法 JSON，宁可少字段也不丢记录。
func normalizeToolInput(inputJSON string) string {
	trimmed := strings.TrimSpace(inputJSON)
	if trimmed == "" {
		return "{}"
	}
	if json.Valid([]byte(trimmed)) {
		return trimmed
	}
	// 非 JSON（截断/半成品）：包一层保留原文，供前端展示
	if b, err := json.Marshal(map[string]string{"raw": trimmed}); err == nil {
		return string(b)
	}
	return "{}"
}

// UpdateToolCall stores the tool result on the matching record (S 修复).
func (m *Manager) UpdateToolCall(ctx context.Context, toolCallID, output string, isError bool, turnID string) {
	if m.pool == nil || toolCallID == "" {
		return
	}
	_, err := m.pool.Exec(ctx,
		`UPDATE tool_calls
		 SET output = $2, is_error = $3, turn_id = COALESCE(NULLIF($4, ''), turn_id)
		 WHERE id = $1`,
		toolCallID, output, isError, turnID)
	if err != nil {
		slog.Error("update tool call", "call", toolCallID, "error", err)
	}
}

// GetToolCallsPage retrieves a page of tool call records older than the cursor,
// aligned with GetMessagesPage pagination (created_at|id cursor).
func (m *Manager) GetToolCallsPage(ctx context.Context, sessionID string, limit int, before string) ([]model.ToolCall, error) {
	if m.pool == nil || limit <= 0 {
		return nil, nil
	}
	query := `SELECT id, session_id, tool_name, input, output, is_error, created_at
		 FROM tool_calls WHERE session_id = $1`
	args := []interface{}{sessionID}
	if before != "" {
		parts := strings.SplitN(before, "|", 2)
		if len(parts) == 2 {
			t, err := time.Parse(time.RFC3339Nano, parts[0])
			if err == nil {
				query += ` AND (created_at < $2 OR (created_at = $2 AND id < $3))`
				args = append(args, t, parts[1])
			}
		}
	}
	query += ` ORDER BY created_at DESC, id DESC LIMIT $` + strconv.Itoa(len(args)+1)
	args = append(args, limit+1)

	rows, err := m.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.ToolCall
	for rows.Next() {
		var tc model.ToolCall
		var inputJSON []byte
		var created time.Time
		if err := rows.Scan(&tc.ID, &tc.SessionID, &tc.ToolName, &inputJSON, &tc.Output, &tc.IsError, &created); err != nil {
			return nil, err
		}
		tc.Input = string(inputJSON)
		tc.CreatedAt = created
		out = append(out, tc)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(out) > limit {
		out = out[:limit]
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, nil
}

// GetToolCalls returns all tool call records for a session, oldest first.
func (m *Manager) GetToolCalls(ctx context.Context, sessionID string) ([]model.ToolCall, error) {
	if m.pool == nil {
		return nil, nil
	}
	rows, err := m.pool.Query(ctx,
		`SELECT id, session_id, tool_name, input, output, is_error, created_at
		 FROM tool_calls WHERE session_id = $1 ORDER BY created_at ASC, id ASC`,
		sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.ToolCall
	for rows.Next() {
		var tc model.ToolCall
		var inputJSON []byte
		var created time.Time
		if err := rows.Scan(&tc.ID, &tc.SessionID, &tc.ToolName, &inputJSON, &tc.Output, &tc.IsError, &created); err != nil {
			return nil, err
		}
		tc.Input = string(inputJSON)
		tc.CreatedAt = created
		out = append(out, tc)
	}
	return out, rows.Err()
}

// SaveMessages saves user + assistant messages and updates the session title.
func (m *Manager) SaveMessages(ctx context.Context, sessionID, userID, userContent, assistantContent string) {
	if m.pool == nil {
		return
	}

	_, err := m.pool.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, NULLIF($3, '')::uuid, '', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET updated_at = NOW()`,
		sessionID, DefaultTenantID, userID)
	if err != nil {
		slog.Warn("ensure session", "error", err)
		return
	}

	if userContent != "" {
		msgID, err := genID()
		if err != nil {
			slog.Warn("generate message id", "error", err)
			return
		}
		_, err = m.pool.Exec(ctx,
			`INSERT INTO messages (id, session_id, role, content, created_at) VALUES ($1, $2, 'user', $3, NOW())`,
			msgID, sessionID, userContent)
		if err != nil {
			slog.Warn("save user message", "error", err)
		}
	}
	if assistantContent != "" {
		msgID, err := genID()
		if err != nil {
			slog.Warn("generate message id", "error", err)
			return
		}
		_, err = m.pool.Exec(ctx,
			`INSERT INTO messages (id, session_id, role, content, created_at) VALUES ($1, $2, 'assistant', $3, NOW())`,
			msgID, sessionID, assistantContent)
		if err != nil {
			slog.Warn("save assistant message", "error", err)
		}
	}

	if userContent != "" {
		_, err := m.pool.Exec(ctx,
			`UPDATE sessions SET title = LEFT($1, 255), updated_at = NOW()
			 WHERE id = $2 AND (title = '' OR title IS NULL)`,
			truncateTitle(userContent), sessionID)
		if err != nil {
			slog.Warn("update session title", "error", err)
		}
	}
	m.evictCache(ctx, sessionID)
}

// ── Message query ─────────────────────────────────────────────────────────

// MessagePage is a page of messages plus the cursor for loading earlier pages.
type MessagePage struct {
	Messages []model.Message
	HasMore  bool
	// Cursor 指向本页最早一条（created_at|id），前端用它请求更早一页
	Cursor string
}

// GetMessagesPage retrieves a page of messages (newest-first internally, returned
// oldest-first). When before is empty, returns the newest `limit` messages; when
// before is set (format "RFC3339Nano|msgID"), returns the `limit` messages older
// than that cursor. HasMore reports whether even older messages exist.
func (m *Manager) GetMessagesPage(ctx context.Context, sessionID string, limit int, before string) (MessagePage, error) {
	page := MessagePage{}
	if m.pool == nil || limit <= 0 {
		return page, nil
	}
	// 多取 1 条用于判断是否还有更早的数据
	// source 用 jsonb 取键读取：该列可能尚未迁移（见 migrations/versions/c1a7d3f92b04_*.py），
	// 而"列不存在"会让整个历史查询失败 —— 一个展示用字段不值得拿对话历史去赌。
	query := `SELECT id, session_id, role, content, COALESCE(tool_calls::text, ''), COALESCE(turn_id::text, ''),
		          COALESCE(to_jsonb(messages)->>'source', ''), created_at
		   FROM messages
		   WHERE session_id = $1`
	args := []interface{}{sessionID}

	if before != "" {
		// 游标格式 created_at|id（复合游标，同 created_at 也稳定分页）
		parts := strings.SplitN(before, "|", 2)
		if len(parts) == 2 {
			t, err := time.Parse(time.RFC3339Nano, parts[0])
			if err == nil {
				query += ` AND (created_at < $2 OR (created_at = $2 AND id < $3))`
				args = append(args, t, parts[1])
			}
		}
	}

	query += ` ORDER BY created_at DESC, id DESC LIMIT $` + strconv.Itoa(len(args)+1)
	args = append(args, limit+1)

	rows, err := m.pool.Query(ctx, query, args...)
	if err != nil {
		return page, fmt.Errorf("query messages page: %w", err)
	}
	defer rows.Close()

	var msgs []model.Message
	for rows.Next() {
		var msg model.Message
		if err := rows.Scan(&msg.ID, &msg.SessionID, &msg.Role, &msg.Content, &msg.ToolCalls, &msg.TurnID, &msg.Source, &msg.CreatedAt); err != nil {
			slog.Warn("scan message row", "error", err)
			continue
		}
		msgs = append(msgs, msg)
	}
	if err := rows.Err(); err != nil {
		return page, fmt.Errorf("iterate messages page: %w", err)
	}

	if len(msgs) > limit {
		page.HasMore = true
		msgs = msgs[:limit]
	}
	// 倒序翻转为正序（最早优先）
	for i, j := 0, len(msgs)-1; i < j; i, j = i+1, j-1 {
		msgs[i], msgs[j] = msgs[j], msgs[i]
	}
	page.Messages = msgs
	if len(msgs) > 0 {
		first := msgs[0]
		page.Cursor = first.CreatedAt.UTC().Format(time.RFC3339Nano) + "|" + first.ID
	}
	return page, nil
}

// GetMessages retrieves messages for a session, oldest first, with optional limit.
// If limit <= 0, returns all messages (legacy behavior).
func (m *Manager) GetMessages(ctx context.Context, sessionID string, limit ...int) ([]model.Message, error) {
	if m.pool == nil {
		return nil, nil
	}

	query := `SELECT id, session_id, role, content, COALESCE(tool_calls::text, ''), COALESCE(turn_id::text, ''),
		          COALESCE(to_jsonb(messages)->>'source', ''), created_at
		   FROM messages
		   WHERE session_id = $1
		   ORDER BY created_at ASC`
	args := []interface{}{sessionID}

	if len(limit) > 0 && limit[0] > 0 {
		// 子查询：先取最新的 N 条，再按正序排列，保持"最早优先"的返回契约
		query = `SELECT id, session_id, role, content, COALESCE(tool_calls::text, ''), COALESCE(turn_id::text, ''),
		          COALESCE(source, ''), created_at FROM (
			   SELECT id, session_id, role, content, tool_calls, turn_id, created_at,
			          to_jsonb(messages)->>'source' AS source
			   FROM messages
			   WHERE session_id = $1
			   ORDER BY created_at DESC
			   LIMIT $2
		   ) sub ORDER BY created_at ASC`
		args = append(args, limit[0])
	}

	rows, err := m.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("query messages: %w", err)
	}
	defer rows.Close()

	var msgs []model.Message
	for rows.Next() {
		var msg model.Message
		if err := rows.Scan(&msg.ID, &msg.SessionID, &msg.Role, &msg.Content, &msg.ToolCalls, &msg.TurnID, &msg.Source, &msg.CreatedAt); err != nil {
			slog.Warn("scan message row", "error", err)
			continue
		}
		msgs = append(msgs, msg)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate messages: %w", err)
	}

	if msgs == nil {
		msgs = []model.Message{}
	}
	return msgs, nil
}

// ForkSession 从 srcSessionID 的第 fromIndex 条消息处分叉出一个新会话。
//
// 为什么需要它：会话地图要按**真实分支**连线。参照实现 `vendor/dsh-synapse`
// （DSH 的可视化对话工作台）是按 DSH 原生 `session.header.parentSession` 画边的，
// 而我们的 `sessions` 表此前**完全没有父子关系** —— 地图画出来只有孤立方块。
//
// 语义：
//   - 复制**前 fromIndex 条**消息（含第 fromIndex 条）到新会话；
//   - 新会话记 `parent_session_id` + `branch_from_seq = fromIndex`，这是"这条分支
//     从对话的哪一步长出来"的唯一依据（地图连线与详情面板都读它）；
//   - 复制出来的消息时间戳按 `now() - (n - 序号) ms` 重排：既保持与源会话一致的
//     相对顺序，又保证新会话之后的真实消息时间必然更大（否则历史列表排序会乱）。
//
// 依赖 PG13+ 的 `gen_random_uuid()`（PG12 需要 pgcrypto 扩展）。
func (m *Manager) ForkSession(ctx context.Context, srcSessionID, userID, title string, fromIndex int) (string, int, error) {
	if m.pool == nil {
		return "", 0, errors.New("database unavailable")
	}
	if fromIndex <= 0 {
		return "", 0, errors.New("from_index must be greater than 0")
	}
	newID, err := genID()
	if err != nil {
		return "", 0, err
	}
	tx, err := m.pool.Begin(ctx)
	if err != nil {
		return "", 0, fmt.Errorf("begin fork tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 源会话：校验归属（同租户语义由 tenant 过滤保证），并继承 agent/tenant
	var srcTitle string
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(title, '') FROM sessions
		  WHERE id = $1 AND (user_id = NULLIF($2, '')::uuid OR user_id IS NULL)`,
		srcSessionID, userID).Scan(&srcTitle); err != nil {
		return "", 0, fmt.Errorf("source session not found: %w", err)
	}
	if strings.TrimSpace(title) == "" {
		title = strings.TrimSpace(srcTitle) + " · 分支"
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, agent_id, title, status, created_at, updated_at,
		                       parent_session_id, branch_from_seq)
		 SELECT $1, tenant_id, user_id, agent_id, $2, 'active', NOW(), NOW(), id, $3
		   FROM sessions WHERE id = $4`,
		newID, title, fromIndex, srcSessionID); err != nil {
		return "", 0, fmt.Errorf("insert forked session: %w", err)
	}

	// 复制消息：source（用户输入 / 子 Agent 自动轮）一并复制，前端靠它区分渲染
	tag, err := tx.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, tool_calls, turn_id, source, created_at)
		 SELECT gen_random_uuid()::text, $1, g.role, g.content, g.tool_calls, g.turn_id, g.source,
		        NOW() - ((g.n - g.rn) * INTERVAL '1 millisecond')
		   FROM (SELECT role, content, tool_calls, turn_id, source,
		                ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn,
		                $2::int AS n
		           FROM messages
		          WHERE session_id = $3
		          ORDER BY created_at, id
		          LIMIT $2) g`,
		newID, fromIndex, srcSessionID)
	if err != nil {
		return "", 0, fmt.Errorf("copy messages for fork: %w", err)
	}
	copied := int(tag.RowsAffected())
	if err := tx.Commit(ctx); err != nil {
		return "", 0, fmt.Errorf("commit fork tx: %w", err)
	}
	return newID, copied, nil
}

// ── Cache helpers ─────────────────────────────────────────────────────────

// sessionCacheEntry 缓存条目：带版本（updated_at 纳秒）以便写入时比较。
// 目的：并发交错下「读路径回填的旧快照」不应覆盖新值（000.md 第 13 条）。
type sessionCacheEntry struct {
	V int64          `json:"v"`
	D *model.Session `json:"d"`
}

// sessionCacheSetLua 带版本比较的写入：缓存缺失、或新值版本不早于缓存值时写入。
// 返回 1=已写入，0=拒绝（缓存中已有更新的版本）。旧格式条目（无 v 字段）视为可覆盖。
const sessionCacheSetLua = `
local cur = redis.call('GET', KEYS[1])
if cur then
  local ok, old = pcall(cjson.decode, cur)
  if ok and old['v'] then
    local oldv = tonumber(old['v'])
    local newv = tonumber(ARGV[2])
    if oldv and newv and newv < oldv then
      return 0
    end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
`

func (m *Manager) cacheSession(ctx context.Context, s *model.Session) {
	if m.rdb == nil {
		return
	}
	payload, err := json.Marshal(sessionCacheEntry{V: s.UpdatedAt.UnixNano(), D: s})
	if err != nil {
		return
	}
	res, err := m.rdb.Eval(ctx, sessionCacheSetLua,
		[]string{redisKeyPrefix + s.ID},
		payload,
		strconv.FormatInt(s.UpdatedAt.UnixNano(), 10),
		int(redisTTL.Seconds()),
	).Result()
	if err != nil {
		slog.Warn("session cache set", "error", err)
		return
	}
	if n, ok := res.(int64); ok && n == 0 {
		slog.Debug("session cache set skipped (cached version is newer)", "session", s.ID)
	}
}

func (m *Manager) evictCache(ctx context.Context, id string) {
	if m.rdb == nil {
		return
	}
	m.rdb.Del(ctx, redisKeyPrefix+id)
}

// ── Helpers ───────────────────────────────────────────────────────────────

func genID() (string, error) {
	return id.UUID()
}

func truncateTitle(s string) string {
	s = strings.TrimSpace(s)
	idx := strings.Index(s, "\n")
	if idx >= 0 {
		s = s[:idx]
	}
	if utf8.RuneCountInString(s) > 120 {
		runes := []rune(s)
		s = string(runes[:120])
	}
	if s == "" {
		s = "New Chat"
	}
	return s
}

func nullableStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
