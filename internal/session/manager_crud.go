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

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/model"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

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
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), COALESCE(alias, ''), COALESCE(parent_session_id::text, ''), COALESCE(branch_from_seq, 0), created_at, updated_at
		 FROM sessions WHERE id = $1`, id).
		Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.Alias, &s.ParentSessionID, &s.BranchFromSeq, &s.CreatedAt, &s.UpdatedAt)
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
		// 新会话：id 由调用方生成（UUID），撞车几乎不可能。仍带归属条件并检查影响行数 ——
		// 否则"撞上别人的 id"会静默覆盖对方标题（NULL 归属用 IS NOT DISTINCT FROM 匹配）。
		tag, err := m.pool.Exec(ctx,
			`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
			 VALUES ($1, $2, $3::uuid, $4, $5, $6)
			 ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, updated_at = EXCLUDED.updated_at
			  WHERE sessions.user_id IS NOT DISTINCT FROM EXCLUDED.user_id`,
			id, DefaultTenantID, uid, title, now, now)
		if err != nil {
			return nil, fmt.Errorf("create session: %w", err)
		}
		if tag.RowsAffected() == 0 {
			return nil, fmt.Errorf("create session: %w: %s", ErrSessionForbidden, id)
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
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), COALESCE(alias, ''), COALESCE(parent_session_id::text, ''), COALESCE(branch_from_seq, 0), created_at, updated_at
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
		if err := rows.Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.Alias, &s.ParentSessionID, &s.BranchFromSeq, &s.CreatedAt, &s.UpdatedAt); err != nil {
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
	Alias  *string
}

// empty 表示没有任何字段需要更新。
func (u SessionUpdate) empty() bool {
	return u.Title == nil && u.Pinned == nil && u.Tag == nil && u.Alias == nil
}

// buildSessionUpdate 组装 UPDATE sessions 的语句与参数。
// 只有 title 变更才推进 updated_at（置顶/标签/别名是列表偏好，不算会话活动）；
// tag / alias 传空串表示清除（写 NULL，避免留下空串）。
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
	if u.Alias != nil {
		sets = append(sets, fmt.Sprintf("alias = NULLIF($%d, '')", len(args)+1))
		args = append(args, *u.Alias)
	}
	args = append(args, id)
	return `UPDATE sessions SET ` + strings.Join(sets, ", ") + ` WHERE id = $` + strconv.Itoa(len(args)), args
}

// UpdateSession updates a session's title / pinned flag / tag / alias, then refreshes
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
