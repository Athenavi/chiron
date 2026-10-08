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

	"github.com/athenavi/chiron/internal/model"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

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
// ensureSessionOwned 确保会话存在、且**属于 userID** —— fail-closed。
//
// 所有"按 session_id 写入"的路径都必须经此，三种结果：
//   * 会话不存在 → 顺带创建（首条消息创建会话的正常路径）；
//   * 已存在且属于该用户 → 只刷新 updated_at；
//   * 已存在但归属不符 → `ON CONFLICT ... DO UPDATE ... WHERE` 既不更新也不返回行，
//     函数返回 ErrSessionForbidden，调用方**必须放弃写入**。
//
// 背景：此前 4 处写入（SaveUserMessage / SaveMessages / CreateTurn / CreateSession）各自
// `INSERT ... ON CONFLICT (id) DO UPDATE`，**不比对归属** —— 任何登录用户只要知道别人的
// session_id，就能往那个会话里塞消息（CreateSession 那处还会把对方的标题一起覆盖）。
func (m *Manager) ensureSessionOwned(ctx context.Context, sessionID, userID string) error {
	if m.pool == nil {
		return nil
	}
	if sessionID == "" {
		return errors.New("session id is required")
	}
	var ensured string
	err := m.pool.QueryRow(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
		 VALUES ($1, $2, NULLIF($3, '')::uuid, '', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET updated_at = NOW()
		  WHERE sessions.user_id = EXCLUDED.user_id
		 RETURNING id`,
		sessionID, DefaultTenantID, userID).Scan(&ensured)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("%w: %s", ErrSessionForbidden, sessionID)
	}
	return err
}

func (m *Manager) SaveUserMessage(ctx context.Context, sessionID, userID, userContent, turnID, source string) {
	if m.pool == nil || userContent == "" {
		return
	}
	// 归属校验内聚在 ensureSessionOwned（fail-closed）：归属不符时不写任何消息
	if err := m.ensureSessionOwned(ctx, sessionID, userID); err != nil {
		slog.Warn("save user message refused", "session", sessionID, "error", err)
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
	if ensureErr := m.ensureSessionOwned(ctx, sessionID, userID); ensureErr != nil {
		slog.Error("ensure session for turn", "session", sessionID, "error", ensureErr)
	}
	_, err := m.pool.Exec(ctx,
		`INSERT INTO turns (id, session_id, user_id, status, started_at, created_at)
		 VALUES ($1, $2, $3, 'running', NOW(), NOW())
		 ON CONFLICT (id) DO UPDATE SET status = 'running', started_at = NOW()`,
		turnID, pgUUIDOrNil(sessionID), pgUUIDOrNil(userID))
	if err != nil {
		// 回合状态写失败不阻断对话（SSE 照常），但必须可见（不再静默）
		slog.Error("create turn", "turn", turnID, "session", sessionID, "error", err)
	}
}

// finishTurnSQL 收敛回合终态：completed / failed / cancelled。
//
// ⚠️ `$7::bigint` 这两个显式 cast **不是可选的**（实测事故）：
// `turns.cached_tokens` 是 bigint，而 `cache_hit = ($7 > 0)` 里的字面量 `0` 让
// Postgres 在 prepare 阶段把**同一个参数**推断成 int4 —— 两处类型不一致，直接报
//
//	ERROR: inconsistent types deduced for parameter $7 (SQLSTATE 42P08)
//
// 于是**每一次**回合收尾都写不进库，turns 永远停在 `running`：会话地图/侧边栏
// 永久显示"运行中"，刷新也不会变，用户看到的就是"主 Agent 一直阻塞"。
// 显式 cast 让两处推断到同一个类型。
const finishTurnSQL = `
UPDATE turns
   SET status = $2, error = NULLIF($3, ''), input_tokens = $4, output_tokens = $5,
       model = NULLIF($6, ''), cached_tokens = $7::bigint, cache_hit = ($7::bigint > 0),
       finished_at = NOW()
 WHERE id = $1`

// FinishTurn 收敛回合终态：completed / failed / cancelled，并记录 token 用量与所用模型。
//
// status 为非法值时回退 completed；失败原因写入 turns.error 便于排查。
// model / cachedTokens 由引擎随 done 事件回传 —— 会话地图详情页据此展示"每轮用了哪个
// 模型"与"缓存命中率"。取不到时分别写空值与 0：统计少算，但不阻断回合收尾。
func (m *Manager) FinishTurn(ctx context.Context, turnID, status, errMsg, model string, inputTokens, outputTokens, cachedTokens int) {
	if m.pool == nil || turnID == "" {
		return
	}
	switch status {
	case "completed", "failed", "cancelled":
	default:
		status = "completed"
	}
	_, err := m.pool.Exec(ctx, finishTurnSQL,
		turnID, status, errMsg, inputTokens, outputTokens, model, cachedTokens)
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

	if err := m.ensureSessionOwned(ctx, sessionID, userID); err != nil {
		slog.Warn("save messages refused", "session", sessionID, "error", err)
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
