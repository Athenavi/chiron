package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/session"
)

// ConversationHandler handles CRUD for chat conversations (sessions).
type ConversationHandler struct {
	authenticator *auth.Authenticator
	sessionMgr    *session.Manager
}

func NewConversationHandler(a *auth.Authenticator, sm *session.Manager) *ConversationHandler {
	return &ConversationHandler{authenticator: a, sessionMgr: sm}
}

// Conversation is a chat session returned to the frontend.
type Conversation struct {
	ID        string     `json:"id"`
	Title     string     `json:"title"`
	Pinned    bool       `json:"pinned"`
	Tag       string     `json:"tag,omitempty"` // 会话标签（前端分类筛选；DB 持久化）
	CreatedAt time.Time  `json:"created_at"`
	UpdatedAt time.Time  `json:"updated_at"`
	Messages  []Message  `json:"messages,omitempty"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"` // S 修复：工具调用过程落库，刷新后还原
	Cursor    string     `json:"cursor,omitempty"`     // P 性能修复：分页游标（加载更早消息）
	HasMore   bool       `json:"has_more"`             // P 性能修复：是否还有更早的消息
}

// Message is a single chat message returned to the frontend.
type Message struct {
	ID        string    `json:"id"`
	Role      string    `json:"role"`
	Content   string    `json:"content"`
	ToolCalls string    `json:"tool_calls,omitempty"` // assistant 消息的 OpenAI 格式 tool_calls（S 修复）
	// TurnID 该消息所属回合（前端按回合分组渲染/锚定，缺失时前端按时间兜底）
	TurnID    string    `json:"turn_id,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

// ToolCall is a persisted tool invocation (input/output) for history rendering.
type ToolCall struct {
	ID        string    `json:"id"`
	ToolName  string    `json:"tool_name"`
	Input     string    `json:"input"`
	Output    string    `json:"output"`
	IsError   bool      `json:"is_error"`
	CreatedAt time.Time `json:"created_at"`
}

// List returns sessions for the current user.
func (h *ConversationHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	// 分页参数
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	perPage, _ := strconv.Atoi(r.URL.Query().Get("per_page"))

	sessions, err := h.sessionMgr.ListSessions(r.Context(), claims.UserID, page, perPage)
	if err != nil {
		// Fallback: return empty list
		OK(w, []Conversation{})
		return
	}

	convs := make([]Conversation, 0, len(sessions))
	for _, s := range sessions {
		convs = append(convs, Conversation{
			ID:        s.ID,
			Title:     s.Title,
			Pinned:    s.Pinned,
			Tag:       s.Tag,
			CreatedAt: s.CreatedAt,
			UpdatedAt: s.UpdatedAt,
		})
	}
	OK(w, convs)
}

// Get returns a single session with its messages (with optional pagination).
func (h *ConversationHandler) Get(w http.ResponseWriter, r *http.Request) {
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

	// 分页参数：默认返回最近 50 条（P 性能修复：首屏不加载全量，上滚加载更早）
	limit := 50
	if l := r.URL.Query().Get("limit"); l != "" {
		if parsed, err := strconv.Atoi(l); err == nil && parsed > 0 {
			limit = parsed
		}
	}
	before := r.URL.Query().Get("before")

	// P 性能：messages 与 tool_calls 两个查询并行（pgxpool 并发安全）
	// 使用 select 确保 context 取消时不泄漏 goroutine
	type pageResult struct {
		page session.MessagePage
		err  error
	}
	pageCh := make(chan pageResult, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("conversation message fetch panic", "session", id, "panic", r)
				pageCh <- pageResult{err: fmt.Errorf("panic: %v", r)}
			}
		}()
		p, e := h.sessionMgr.GetMessagesPage(r.Context(), id, limit, before)
		pageCh <- pageResult{p, e}
	}()
	toolCalls, _ := h.sessionMgr.GetToolCallsPage(r.Context(), id, limit, before)

	// Wait for messages or context cancellation
	var pr pageResult
	select {
	case pr = <-pageCh:
	case <-r.Context().Done():
		// Context cancelled; return what we have from toolCalls
		pr = pageResult{err: r.Context().Err()}
		slog.Warn("conversation fetch context cancelled", "session", id)
	}
	page := pr.page
	err = pr.err
	if err != nil {
		page = session.MessagePage{} // zero value, safe to access fields
	}

	conv := Conversation{
		ID:        sess.ID,
		Title:     sess.Title,
		Pinned:    sess.Pinned,
		Tag:       sess.Tag,
		CreatedAt: sess.CreatedAt,
		UpdatedAt: sess.UpdatedAt,
		Messages:  make([]Message, 0),
		Cursor:    page.Cursor,
		HasMore:   page.HasMore,
	}
	for _, m := range page.Messages {
		conv.Messages = append(conv.Messages, Message{
			ID:        m.ID,
			Role:      m.Role,
			Content:   m.Content,
			ToolCalls: m.ToolCalls, // S 修复：assistant 消息的 tool_calls 随详情返回
			TurnID:    m.TurnID,
			CreatedAt: m.CreatedAt,
		})
	}
	// S 修复：工具调用过程落库 — 随会话详情返回，前端刷新后还原工具卡片
	for _, tc := range toolCalls {
		conv.ToolCalls = append(conv.ToolCalls, ToolCall{
			ID:        tc.ID,
			ToolName:  tc.ToolName,
			Input:     tc.Input,
			Output:    tc.Output,
			IsError:   tc.IsError,
			CreatedAt: tc.CreatedAt,
		})
	}
	OK(w, conv)
}

// Create creates a new session. If authenticated, links to user account.
func (h *ConversationHandler) Create(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Title string `json:"title"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if body.Title == "" {
		body.Title = "新对话"
	}

	claims := auth.GetClaims(r.Context())
	userID := claims.UserID

	sessionID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	sess, err := h.sessionMgr.CreateSession(r.Context(), sessionID, userID, body.Title)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create session failed")
		return
	}

	OK(w, Conversation{
		ID:        sess.ID,
		Title:     sess.Title,
		CreatedAt: sess.CreatedAt,
		UpdatedAt: sess.UpdatedAt,
	})
}

// SaveMessages inserts user + assistant messages into the database.
// Called from the /submit goroutine after streaming completes.
func (h *ConversationHandler) SaveMessages(ctx context.Context, sessionID, userID, userContent, assistantContent string) {
	h.sessionMgr.SaveMessages(ctx, sessionID, userID, userContent, assistantContent)
}

// Delete removes a session and its messages (CASCADE).
func (h *ConversationHandler) Delete(w http.ResponseWriter, r *http.Request) {
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
	// 准则 3：从外部会话列表删除会话时，画布里的引用必须一起消失。
	// 先记下它出现在哪些画布 —— 删完之后 session_map_nodes 会被 CASCADE 清掉，查不到了。
	mapWorkspaceIDs := sessionMapWorkspaceIDsForSession(r.Context(), id)

	if err := h.sessionMgr.DeleteSession(r.Context(), id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete session failed")
		return
	}

	// 准则 3：PG 的 CASCADE 不会让 Redis 热层失效 —— 而热层是**跨实例**缓存，
	// 别的实例读到的仍是删除前的快照（表现为幽灵卡片）。必须显式清掉。
	invalidateSessionMapWorkspaces(r.Context(), mapWorkspaceIDs)

	OK(w, map[string]string{"status": "deleted"})
}

// Update updates a session's title / pinned flag / tag (session menu: 重命名/置顶/标签).
func (h *ConversationHandler) Update(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	var body struct {
		Title  *string `json:"title"`
		Pinned *bool   `json:"pinned"`
		Tag    *string `json:"tag"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if body.Title == nil && body.Pinned == nil && body.Tag == nil {
		BadRequest(w, "title, pinned or tag is required")
		return
	}
	if body.Title != nil && strings.TrimSpace(*body.Title) == "" {
		BadRequest(w, "title is required")
		return
	}
	if body.Tag != nil {
		// 长度与 sessions.tag 列（varchar(64)）一致；trim 后空串表示清除标签
		trimmed := strings.TrimSpace(*body.Tag)
		if len([]rune(trimmed)) > 64 {
			BadRequest(w, "tag is too long")
			return
		}
		body.Tag = &trimmed
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

	updated, err := h.sessionMgr.UpdateSession(r.Context(), id, session.SessionUpdate{
		Title:  body.Title,
		Pinned: body.Pinned,
		Tag:    body.Tag,
	})
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update session failed")
		return
	}

	OK(w, Conversation{
		ID:        updated.ID,
		Title:     updated.Title,
		Pinned:    updated.Pinned,
		Tag:       updated.Tag,
		CreatedAt: updated.CreatedAt,
		UpdatedAt: updated.UpdatedAt,
	})
}

func userIDFromClaims(claims *auth.Claims) string {
	if claims == nil {
		return ""
	}
	return claims.UserID
}
