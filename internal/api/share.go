package api

import (
	"crypto/rand"
	"encoding/base32"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/model"
	"github.com/athenavi/chiron/internal/session"
)

// ShareHandler manages public conversation shares (rendered at /share/{id}
// without authentication, like chat.deepseek.com/share/{id}).
type ShareHandler struct {
	authenticator *auth.Authenticator
	sessionMgr    *session.Manager
}

func NewShareHandler(a *auth.Authenticator, sm *session.Manager) *ShareHandler {
	return &ShareHandler{authenticator: a, sessionMgr: sm}
}

// shareToken generates a random unguessable share id (80 bits entropy, base32
// lowercase, no padding — 16 chars). Shares must not be enumerable, unlike
// snowflake ids, because the id is the whole access control.
func shareToken() (string, error) {
	var b [10]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("share: crypto/rand unavailable: %w", err)
	}
	return strings.ToLower(base32.HexEncoding.WithPadding(base32.NoPadding).EncodeToString(b[:])), nil
}

// userInputTagRe 匹配 InputSanitizer.Sanitize 添加的 <user_input> 包装
// （internal/api/security.go:67）。`(?s)` 让 `.` 跨行 —— 包装内容是整段用户输入。
var userInputTagRe = regexp.MustCompile(`(?s)^\s*<user_input>\s*(.*?)\s*</user_input>\s*$`)

// stripUserInputTag 剥掉上述包装；不符合该形态时只去首尾空白。
// 与前端 chat-types.ts 的 stripUserInputTag 同一口径。
//
// 只剥这一层：`[thinking]` 块**有意保留** —— 公开页前端会折叠展示它，
// 在这里一并剥掉会让访问者失去"这次回答想了什么"的信息。
func stripUserInputTag(content string) string {
	if m := userInputTagRe.FindStringSubmatch(content); m != nil {
		return strings.TrimSpace(m[1])
	}
	return strings.TrimSpace(content)
}

// Create shares a conversation (POST /v1/conversations/{id}/share).
// The body lists the message ids to expose; only text messages are rendered.
// Idempotent: an active share for the session is returned as-is.
func (h *ShareHandler) Create(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	var body struct {
		MessageIDs []string `json:"message_ids"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	messageIDs := dedupe(body.MessageIDs)
	if len(messageIDs) == 0 {
		BadRequest(w, "message_ids is required")
		return
	}
	// 限制单次分享最多 500 条消息，防止滥用
	if len(messageIDs) > 500 {
		BadRequest(w, "message_ids exceeds maximum of 500")
		return
	}

	// 消息归属校验：只允许分享本会话的消息
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id FROM messages WHERE session_id = $1 AND id = ANY($2::text[])`, id, messageIDs)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate messages failed")
		return
	}
	owned := make(map[string]bool)
	for rows.Next() {
		var mid string
		if rows.Scan(&mid) == nil {
			owned[mid] = true
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate messages failed")
		return
	}
	valid := make([]string, 0, len(messageIDs))
	for _, mid := range messageIDs {
		if owned[mid] {
			valid = append(valid, mid)
		}
	}
	if len(valid) == 0 {
		BadRequest(w, "message_ids does not match any message in this conversation")
		return
	}

	// 幂等：已有活跃分享直接返回
	existing, err := h.activeShare(r, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query share failed")
		return
	}
	if existing != nil {
		OK(w, map[string]interface{}{"share_id": existing.ID, "created_at": existing.CreatedAt})
		return
	}

	token, err := shareToken()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create share failed")
		return
	}
	// user_id 列是 varchar(36)（见 a0b3a964fb54 建表），直接传字符串与列类型一致。
	// 原写法 $3::uuid 会让 PG 走一次 I/O 转换而非类型化赋值 —— 在当前列类型下侥幸
	// 能过，但列类型一旦变动就会静默变形。
	// 真正致命的是 message_ids：[]string 被 pgx 编码成数组字面量写进 varchar(255)，
	// 超过 255 字符即报 value too long（修见迁移 d3e5f7a9b1c4）。
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO conversation_shares (id, session_id, user_id, title, message_ids, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		token, id, claims.UserID, sess.Title, valid, time.Now())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create share failed")
		return
	}

	OK(w, map[string]interface{}{"share_id": token, "created_at": time.Now()})
}

// GetActive returns the active share for a conversation (GET /v1/conversations/{id}/share).
func (h *ShareHandler) GetActive(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	share, err := h.activeShare(r, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query share failed")
		return
	}
	if share == nil {
		NotFound(w, "no active share")
		return
	}
	OK(w, map[string]interface{}{"share_id": share.ID, "created_at": share.CreatedAt})
}

// Revoke cancels the active share (DELETE /v1/conversations/{id}/share).
// The public link stops resolving afterwards.
func (h *ShareHandler) Revoke(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE conversation_shares SET revoked_at = NOW() WHERE session_id = $1 AND revoked_at IS NULL`, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "revoke share failed")
		return
	}
	OK(w, map[string]string{"status": "revoked"})
}

// List lists the caller's active shares (GET /v1/shares).
//
// 「数据管理 → 分享管理」需要它：现有接口都是按会话操作（Create/GetActive/Revoke），
// 要列出全部分享只能对每个会话各发一次请求，既慢又发现不了"哪个会话被分享过"。
// 已撤销的分享不返回 —— 在用户视角里它们已经不存在。
//
// 不分页：分享数天然有限，200 条上限兜底，避免为它引入一套分页参数。
func (h *ShareHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id, session_id, COALESCE(title, ''), COALESCE(array_length(message_ids, 1), 0), created_at
		 FROM conversation_shares
		 WHERE user_id = $1 AND revoked_at IS NULL
		 ORDER BY created_at DESC
		 LIMIT 200`, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list shares failed")
		return
	}
	defer rows.Close()

	items := make([]map[string]interface{}, 0, 16)
	for rows.Next() {
		var (
			shareID   string
			sessionID string
			title     string
			msgCount  int
			createdAt time.Time
		)
		if err := rows.Scan(&shareID, &sessionID, &title, &msgCount, &createdAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan share failed")
			return
		}
		items = append(items, map[string]interface{}{
			"share_id":      shareID,
			"session_id":    sessionID,
			"title":         title,
			"message_count": msgCount,
			"created_at":    createdAt,
		})
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate shares failed")
		return
	}
	OK(w, map[string]interface{}{"items": items})
}

// PublicGet renders a share without authentication (GET /v1/share/{id}).
// Returns 410 Gone once the owner revokes the share.
func (h *ShareHandler) PublicGet(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("id")
	if token == "" {
		NotFound(w, "share not found")
		return
	}

	var shareID, sessionID, title string
	var messageIDs []string
	var createdAt time.Time
	var revokedAt *time.Time
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, session_id, title, message_ids, created_at, revoked_at
		 FROM conversation_shares WHERE id = $1`, token).
		Scan(&shareID, &sessionID, &title, &messageIDs, &createdAt, &revokedAt)
	if err != nil {
		NotFound(w, "share not found")
		return
	}
	if revokedAt != nil {
		JSON(w, http.StatusGone, APIResponse{Success: false, Error: "share revoked"})
		return
	}

	// 只暴露用户选中的文本消息（user/assistant），按时间正序
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT role, content, created_at FROM messages
		 WHERE session_id = $1 AND id = ANY($2::text[]) AND role IN ('user', 'assistant')
		 ORDER BY created_at ASC, id ASC`, sessionID, messageIDs)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query messages failed")
		return
	}
	defer rows.Close()

	type sharedMessage struct {
		Role      string    `json:"role"`
		Content   string    `json:"content"`
		CreatedAt time.Time `json:"created_at"`
	}
	messages := make([]sharedMessage, 0, 16)
	for rows.Next() {
		var m sharedMessage
		if err := rows.Scan(&m.Role, &m.Content, &m.CreatedAt); err != nil {
			continue
		}
		// 剥离 InputSanitizer 添加的 <user_input> 包装：它是提示注入防线的内部标记，
		// 没有展示价值，不该随公开链接进到响应体里（前端再剥一层只是兜底）。
		m.Content = stripUserInputTag(m.Content)
		messages = append(messages, m)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate messages failed")
		return
	}

	OK(w, map[string]interface{}{
		"id":         shareID,
		"title":      title,
		"created_at": createdAt,
		"messages":   messages,
	})
}

// activeShare returns the session's non-revoked share, or nil.
func (h *ShareHandler) activeShare(r *http.Request, sessionID string) (*model.ConversationShare, error) {
	share := &model.ConversationShare{}
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, created_at FROM conversation_shares
		 WHERE session_id = $1 AND revoked_at IS NULL LIMIT 1`, sessionID).
		Scan(&share.ID, &share.CreatedAt)
	if err != nil {
		return nil, nil // no active share (or DB row missing)
	}
	return share, nil
}

func dedupe(ids []string) []string {
	seen := make(map[string]bool, len(ids))
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	return out
}
