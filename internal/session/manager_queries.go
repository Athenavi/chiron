package session

import (
	"context"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/model"
)

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
	// source 用 jsonb 取键读取：该列由权威迁移建立（migrations/versions/0001_authoritative_baseline.py），
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

// ParentTitles 批量取"父会话展示名"（displayName = alias || title），
// 供分支会话在会话列表 / 会话地图上显示"分支自《谁》"。
//
// 为什么单独一个方法而不是 JOIN：会话列表与详情是两条独立查询路径，
// JOIN 会改动既有 SQL 的行数/列序语义；而这里**一次查询覆盖整页**，
// 既避免 N+1，又不碰主查询。
//
// 容错：
//   - 用 `id::text = ANY(...)` 而不是 `id = ANY($1::uuid[])`：调用方传来的
//     `parent_session_id` 理论上一定是 uuid，但**查询失败不该让列表 500**；
//     文本比较对非法值只是"查不到"，语义更稳。
//   - 父会话已被删除（或不在可见范围）时该 id 不出现在返回值里，调用方留空即可。
func (m *Manager) ParentTitles(ctx context.Context, ids []string) (map[string]string, error) {
	if m.pool == nil || len(ids) == 0 {
		return nil, nil
	}
	uniq := make([]string, 0, len(ids))
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		uniq = append(uniq, id)
	}
	if len(uniq) == 0 {
		return nil, nil
	}

	rows, err := m.pool.Query(ctx,
		`SELECT id::text, COALESCE(NULLIF(alias, ''), NULLIF(title, ''), '')
		   FROM sessions
		  WHERE id::text = ANY($1::text[])`, uniq)
	if err != nil {
		return nil, fmt.Errorf("query parent titles: %w", err)
	}
	defer rows.Close()

	out := make(map[string]string, len(uniq))
	for rows.Next() {
		var id, name string
		if err := rows.Scan(&id, &name); err != nil {
			slog.Warn("scan parent title row", "error", err)
			continue
		}
		out[id] = name
	}
	return out, nil
}
