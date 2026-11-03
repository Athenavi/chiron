package session

import (
	"context"
	"errors"
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
	//
	// seq（迁移 0016）**直接引用**，不走 jsonb：它是排序键，一旦包成表达式
	// `(to_jsonb(messages)->>'seq')::bigint`，外层的 ORDER BY 就用不上
	// ix_messages_session_seq，长会话分页会退化成全表排序 —— 比它要兼容的
	// 问题（迁移没跑）更糟。迁移与代码同批发布且迁移先执行（见
	// deploy/k8s/migrate-job.yaml），列一定先于本代码存在。
	query := `SELECT id, session_id, role, content, COALESCE(tool_calls::text, ''), COALESCE(turn_id::text, ''),
		          COALESCE(to_jsonb(messages)->>'source', ''), created_at, seq
		   FROM messages
		   WHERE session_id = $1`
	args := []interface{}{sessionID}

	if before != "" {
		// 游标格式 seq|id：seq 单调且无时区歧义，同毫秒并发下依然稳定。
		// 兼容旧游标（RFC3339Nano|id）：解析失败说明是升级前写入的游标，退回时间比较。
		parts := strings.SplitN(before, "|", 2)
		if len(parts) == 2 {
			if seq, err := strconv.ParseInt(parts[0], 10, 64); err == nil {
				query += ` AND seq < $2`
				args = append(args, seq)
			} else if t, err := time.Parse(time.RFC3339Nano, parts[0]); err == nil {
				query += ` AND (created_at < $2 OR (created_at = $2 AND id < $3))`
				args = append(args, t, parts[1])
			}
		}
	}

	// seq 做主序：与 (session_id, seq) 唯一索引同向，可保序扫描并提前终止。
	// created_at/id 作为二级排序只为让升级前后结果都确定，正常路径不会用到。
	query += ` ORDER BY seq DESC, created_at DESC, id DESC LIMIT $` + strconv.Itoa(len(args)+1)
	args = append(args, limit+1)

	rows, err := m.pool.Query(ctx, query, args...)
	if err != nil {
		return page, fmt.Errorf("query messages page: %w", err)
	}
	defer rows.Close()

	var msgs []model.Message
	var seqs []int64
	for rows.Next() {
		var msg model.Message
		var seq int64
		if err := rows.Scan(&msg.ID, &msg.SessionID, &msg.Role, &msg.Content, &msg.ToolCalls, &msg.TurnID, &msg.Source, &msg.CreatedAt, &seq); err != nil {
			slog.Warn("scan message row", "error", err)
			continue
		}
		msgs = append(msgs, msg)
		seqs = append(seqs, seq)
	}
	if err := rows.Err(); err != nil {
		return page, fmt.Errorf("iterate messages page: %w", err)
	}

	if len(msgs) > limit {
		page.HasMore = true
		msgs = msgs[:limit]
		seqs = seqs[:limit]
	}
	// 倒序翻转为正序（最早优先）
	for i, j := 0, len(msgs)-1; i < j; i, j = i+1, j-1 {
		msgs[i], msgs[j] = msgs[j], msgs[i]
		seqs[i], seqs[j] = seqs[j], seqs[i]
	}
	page.Messages = msgs
	if len(msgs) > 0 {
		page.Cursor = strconv.FormatInt(seqs[0], 10) + "|" + msgs[0].ID
	}
	m.attachTurnOrigins(ctx, msgs)
	return page, nil
}

// attachTurnOrigins 回填每条消息所属回合的发起来源（turns.origin）。
//
// 用一次批量补查而不是在主查询里 JOIN turns：主查询的 ORDER BY 用的是不带表名的
// seq/created_at，JOIN 后同名列会变成歧义引用，排序与索引选择都可能退化。
// 这里沿用 ParentTitles 的做法 —— 一次覆盖整页，不碰主查询。
//
// 查不到就留空（= 用户手动发起）：标记字段缺失只是少一个徽标，
// 不值得让整段对话历史失败。
func (m *Manager) attachTurnOrigins(ctx context.Context, msgs []model.Message) {
	turnIDs := make([]string, 0, len(msgs))
	seen := make(map[string]bool, len(msgs))
	for _, msg := range msgs {
		if msg.TurnID == "" || seen[msg.TurnID] {
			continue
		}
		seen[msg.TurnID] = true
		turnIDs = append(turnIDs, msg.TurnID)
	}
	if len(turnIDs) == 0 {
		return
	}

	rows, err := m.pool.Query(ctx,
		`SELECT id::text, COALESCE(origin, '')
		   FROM turns WHERE id::text = ANY($1::text[])`, turnIDs)
	if isMissingColumn(err, "origin") {
		// 迁移尚未执行（turns.origin 不存在）：没有标记可回填，历史照常返回。
		return
	}
	if err != nil {
		slog.Warn("query turn origins", "error", err)
		return
	}
	defer rows.Close()

	origins := make(map[string]string, len(turnIDs))
	for rows.Next() {
		var id, origin string
		if err := rows.Scan(&id, &origin); err != nil {
			slog.Warn("scan turn origin row", "error", err)
			continue
		}
		if origin != "" {
			origins[id] = origin
		}
	}
	if len(origins) == 0 {
		return
	}
	for i := range msgs {
		msgs[i].Origin = origins[msgs[i].TurnID]
	}
}

// ForkPoint 是「自定义分叉」选点用的一条消息。
type ForkPoint struct {
	// Index 是 1 基序号，与复制窗口的 ROW_NUMBER() 完全同口径
	Index     int       `json:"index"`
	Role      string    `json:"role"`
	Preview   string    `json:"preview"`
	CreatedAt time.Time `json:"created_at"`
}

// ListForkPoints 按**复制窗口同口径**列出可选的智能分叉点。
//
// 口径必须与 `BranchSession` 的复制语句一致：
//
//	ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
//	FROM messages WHERE session_id = $1 ORDER BY created_at, id
//
// 这里刻意**不用** `seq`：`GetMessagesPage` 按 `ORDER BY seq DESC` 走（那是分页的正确
// 选择，能用上 ix_messages_session_seq），但 0016 迁移自述「存量行的 seq 按物理顺序回填，
// 与 created_at 的相对顺序可能不一致」。若前端拿分页列表的下标当 `from_index`，
// 在存量数据上会**静默错位** —— 用户以为从第 10 条分叉，实际复制的是另外 10 条。
//
// 所以选点必须走这条与复制语句同源的查询，page 也不复用。
func (m *Manager) ListForkPoints(ctx context.Context, sessionID string) ([]ForkPoint, error) {
	if m.pool == nil {
		return nil, errors.New("database unavailable")
	}
	rows, err := m.pool.Query(ctx,
		`SELECT rn, role, content, created_at
		   FROM (SELECT role, content, created_at,
		                ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
		           FROM messages
		          WHERE session_id = $1) g
		  ORDER BY rn`, sessionID)
	if err != nil {
		return nil, fmt.Errorf("query fork points: %w", err)
	}
	defer rows.Close()

	points := make([]ForkPoint, 0, 32)
	for rows.Next() {
		var (
			rn   int64
			role string
			body string
			at   time.Time
		)
		if err := rows.Scan(&rn, &role, &body, &at); err != nil {
			return nil, fmt.Errorf("scan fork point: %w", err)
		}
		points = append(points, ForkPoint{
			Index:     int(rn),
			Role:      role,
			Preview:   previewOf(body),
			CreatedAt: at,
		})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate fork points: %w", err)
	}
	return points, nil
}

// forkPointPreviewRunes 是预览长度的上限。
//
// 预览只用于在下拉里辨认"是哪一条"，不是给用户读全文的；太长会把下拉撑成一面墙。
// 用 rune 而不是 byte 截断：中文与阿拉伯语按字节切会切出半个字符。
const forkPointPreviewRunes = 120

// previewOf 压平空白并截断，避免多行内容把下拉的排版撑破。
func previewOf(s string) string {
	flat := strings.Join(strings.Fields(s), " ")
	runes := []rune(flat)
	if len(runes) <= forkPointPreviewRunes {
		return flat
	}
	return string(runes[:forkPointPreviewRunes]) + "…"
}

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
