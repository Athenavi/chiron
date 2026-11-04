package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	ID     string `json:"id"`
	Title  string `json:"title"`
	Pinned bool   `json:"pinned"`
	Tag    string `json:"tag,omitempty"` // 会话标签（前端分类筛选；DB 持久化）
	// Alias 是用户给会话起的别名/备注（展示时优先于 title）；见 internal/model/model.go 的说明
	Alias string `json:"alias,omitempty"`
	// ── 分支血缘（P0：把 DB 里早已存在的列暴露给前端，用于列表/地图"辨识分支"）──
	ParentSessionID string `json:"parent_session_id,omitempty"`
	// ParentTitle 是父会话的展示名（alias || title）；父会话已删则为空
	ParentTitle   string     `json:"parent_title,omitempty"`
	BranchFromSeq int        `json:"branch_from_seq,omitempty"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
	Messages      []Message  `json:"messages,omitempty"`
	ToolCalls     []ToolCall `json:"tool_calls,omitempty"` // S 修复：工具调用过程落库，刷新后还原
	Cursor        string     `json:"cursor,omitempty"`     // P 性能修复：分页游标（加载更早消息）
	HasMore       bool       `json:"has_more"`             // P 性能修复：是否还有更早的消息
	// ToolCursor 是**工具调用自己**的分页游标：两张表的 created_at 分布不同，
	// 共用消息游标会漏取夹在两条消息之间的工具调用。
	ToolCursor string `json:"tool_cursor,omitempty"`
	// ToolHasMore 表示工具调用一侧还有更早的记录。
	ToolHasMore bool `json:"tool_has_more"`
	// ToolCallsPartial 表示本次工具调用查询失败，返回的列表不完整。
	// 与"确实没有工具调用"必须可区分，否则失败会伪装成空。
	ToolCallsPartial bool `json:"tool_calls_partial,omitempty"`
}

// Message is a single chat message returned to the frontend.
type Message struct {
	ID        string `json:"id"`
	Role      string `json:"role"`
	Content   string `json:"content"`
	ToolCalls string `json:"tool_calls,omitempty"` // assistant 消息的 OpenAI 格式 tool_calls（S 修复）
	// TurnID 该消息所属回合（前端按回合分组渲染/锚定，缺失时前端按时间兜底）
	TurnID string `json:"turn_id,omitempty"`
	// Source 消息来源：空 = 用户手动输入；"subagent_followup" = 子 Agent 自动轮注入。
	// 前端据它在用户气泡下加来源徽标，避免被误认成用户自己发的提问。
	Source string `json:"source,omitempty"`
	// Origin 是所属**回合**的发起方（turns.origin）：空 = 用户手动；
	// "superagent_takeover" = 接管巡检。一个回合只有一条注入消息，但该回合的助手回复
	// 也属于这次接管 —— 标在回合上才能让整轮都被认出来。
	Origin    string    `json:"origin,omitempty"`
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
	// OutputBytes 是库中 output 的完整字节数；Output 只是前若干字符的预览。
	// 这两个字段必须随列表返回：少了它，前端无法区分"结果本就这么小"与
	// "这里只是预览"，会把被截断的结果当成全部内容。
	OutputBytes     int  `json:"output_bytes,omitempty"`
	OutputTruncated bool `json:"output_truncated,omitempty"`
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
			ID:              s.ID,
			Title:           s.Title,
			Pinned:          s.Pinned,
			Tag:             s.Tag,
			Alias:           s.Alias,
			ParentSessionID: s.ParentSessionID,
			BranchFromSeq:   s.BranchFromSeq,
			CreatedAt:       s.CreatedAt,
			UpdatedAt:       s.UpdatedAt,
		})
	}
	// 分支会话补上父会话展示名（一次批量查询；失败也不影响列表本身）
	if titles := parentTitlesFor(r.Context(), h.sessionMgr, convs); len(titles) > 0 {
		for i := range convs {
			if convs[i].ParentSessionID != "" {
				convs[i].ParentTitle = titles[convs[i].ParentSessionID]
			}
		}
	}
	OK(w, convs)
}

// parentTitlesFor 查这批会话各自**父会话的展示名**（一次批量查询，避免 N+1）。
//
// 只返回查得到的那部分（父会话已删 → 该 id 缺失 → 调用方留空，前端显示"已删除的会话"）。
// 展示名是锦上添花：查询失败只告警，不影响列表/详情本身。
func parentTitlesFor(ctx context.Context, mgr *session.Manager, convs []Conversation) map[string]string {
	ids := make([]string, 0, len(convs))
	for _, c := range convs {
		if c.ParentSessionID != "" {
			ids = append(ids, c.ParentSessionID)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	titles, err := mgr.ParentTitles(ctx, ids)
	if err != nil {
		slog.Warn("query parent titles", "error", err)
		return nil
	}
	return titles
}

// ForkPoints 列出「自定义分叉」可选的落点（`GET /v1/conversations/{id}/fork-points`）。
//
// 为什么不复用 `GET /v1/conversations/{id}` 的消息数组：那条路径按 `seq DESC`
// 走（能用上 ix_messages_session_seq），而权威基线自述存量行的 `seq` 与
// `created_at` 顺序可能不一致；分叉的复制窗口却按 `ORDER BY created_at, id` 算。
// 前端拿列表下标当 `from_index` 会在存量数据上静默错位 —— 用户以为从第 10 条分叉，
// 实际复制的是另外 10 条。所以选点必须走与复制语句同口径的专用查询
// （`session.Manager.ListForkPoints`）。
func (h *ConversationHandler) ForkPoints(w http.ResponseWriter, r *http.Request) {
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
	// 与 Get 一致：不属于当前用户的会话一律 403（不泄露存在性细节）
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	points, err := h.sessionMgr.ListForkPoints(r.Context(), id)
	if err != nil {
		slog.Warn("list fork points failed", "session", id, "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{
			Success: false,
			Error:   "cannot list fork points: " + err.Error(),
		})
		return
	}

	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data: map[string]interface{}{
			"points": points,
			// total 显式给出：「全量分叉」与「压缩分叉」都要用"最后一条"当分叉点，
			// 让前端不必自己从数组长度推断（数组可能为空，长度 0 不是合法 from_index）。
			"total": len(points),
		},
	})
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
	// tool_before 独立于 before：两张表的 created_at 分布不同，用消息游标去筛
	// 工具调用会漏取本页该显示的工具调用（漏的正是夹在两条消息之间的那些）。
	// 缺省为空 = 取最新一页，这对首次打开与旧客户端都安全。
	toolBefore := r.URL.Query().Get("tool_before")

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
	tcPage, tcErr := h.sessionMgr.GetToolCallsPage(r.Context(), id, limit, toolBefore)
	// 工具调用查询失败过去被 `_` 丢掉：界面上卡片凭空消失，日志里什么都没有，
	// 用户报"我的工具记录没了"时无从排查。失败不阻断会话（消息才是主体），
	// 但必须留痕并告诉前端"这部分不完整"。
	toolCallsPartial := false
	if tcErr != nil {
		toolCallsPartial = true
		slog.Warn("tool calls page failed", "session", id, "error", tcErr)
	}

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
		ID:               sess.ID,
		Title:            sess.Title,
		Pinned:           sess.Pinned,
		Tag:              sess.Tag,
		Alias:            sess.Alias,
		ParentSessionID:  sess.ParentSessionID,
		BranchFromSeq:    sess.BranchFromSeq,
		CreatedAt:        sess.CreatedAt,
		UpdatedAt:        sess.UpdatedAt,
		Messages:         make([]Message, 0),
		Cursor:           page.Cursor,
		HasMore:          page.HasMore,
		ToolCursor:       tcPage.Cursor,
		ToolHasMore:      tcPage.HasMore,
		ToolCallsPartial: toolCallsPartial,
	}
	// 分支会话带上父会话展示名（单条查询；父会话已删则留空）
	if conv.ParentSessionID != "" {
		conv.ParentTitle = parentTitlesFor(r.Context(), h.sessionMgr, []Conversation{conv})[conv.ParentSessionID]
	}
	for _, m := range page.Messages {
		conv.Messages = append(conv.Messages, Message{
			ID:        m.ID,
			Role:      m.Role,
			Content:   m.Content,
			ToolCalls: m.ToolCalls, // S 修复：assistant 消息的 tool_calls 随详情返回
			TurnID:    m.TurnID,
			Source:    m.Source,
			Origin:    m.Origin,
			CreatedAt: m.CreatedAt,
		})
	}
	// S 修复：工具调用过程落库 — 随会话详情返回，前端刷新后还原工具卡片
	for _, tc := range tcPage.ToolCalls {
		conv.ToolCalls = append(conv.ToolCalls, ToolCall{
			ID:              tc.ID,
			ToolName:        tc.ToolName,
			Input:           tc.Input,
			Output:          tc.Output,
			IsError:         tc.IsError,
			CreatedAt:       tc.CreatedAt,
			OutputBytes:     tc.OutputBytes,
			OutputTruncated: tc.OutputTruncated,
		})
	}
	// ── 条件请求：本页内容未变时返回 304，省掉整段 JSON 的传输 ──
	//
	// 组成必须同时包含「请求参数」与「数据版本」：
	//   - 少请求参数 → 翻到不同页却命中同一 ETag，把不同内容误报成 304（错误）
	//   - 少数据版本 → 新消息到达后仍回 304，前端看到陈旧历史（错误）
	// 用弱 ETag：语义等价即可，不承诺逐字节相同。
	etag := conversationETag(id, before, toolBefore, limit, sess.UpdatedAt, len(conv.Messages), len(conv.ToolCalls))
	w.Header().Set("ETag", etag)
	// private：消息含用户内容，中间代理不得缓存。
	// no-cache：可存但每次必须回源校验 —— 这正是我们想要的（校验走 304，省传输）。
	w.Header().Set("Cache-Control", "private, no-cache")
	if etagMatches(r.Header.Get("If-None-Match"), etag) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	OK(w, conv)
}

// conversationETag 生成一页会话详情的内容标识。
//
// 用的是**弱 ETag**（W/ 前缀，RFC 9110 §8.8.3）：语义等价，不承诺逐字节相同。
// 这给了实现留出余量 —— 将来若把消息按不同顺序编码，缓存仍然正确。
func conversationETag(sessionID, before, toolBefore string, limit int, updatedAt time.Time, msgCount, toolCount int) string {
	h := sha256.New()
	// 字段间用 \x00 分隔：避免 "ab"+"c" 与 "a"+"bc" 拼出同一个串。
	fmt.Fprintf(h, "%s\x00%s\x00%s\x00%d\x00%d\x00%d\x00%d",
		sessionID, before, toolBefore, limit, updatedAt.UnixNano(), msgCount, toolCount)
	return `W/"` + hex.EncodeToString(h.Sum(nil)[:16]) + `"`
}

// etagMatches 判断客户端的 If-None-Match 是否命中。
//
// 按 RFC 9110 §13.1.2 处理：可以是 `*`、单值，或逗号分隔的列表；
// 弱前缀 W/ 在弱比较下不参与比对（两侧都剥掉）。
func etagMatches(ifNoneMatch, etag string) bool {
	ifNoneMatch = strings.TrimSpace(ifNoneMatch)
	if ifNoneMatch == "" {
		return false
	}
	if ifNoneMatch == "*" {
		return true
	}
	want := strings.TrimPrefix(etag, "W/")
	for _, candidate := range strings.Split(ifNoneMatch, ",") {
		if strings.TrimPrefix(strings.TrimSpace(candidate), "W/") == want {
			return true
		}
	}
	return false
}
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
		Alias  *string `json:"alias"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if body.Title == nil && body.Pinned == nil && body.Tag == nil && body.Alias == nil {
		BadRequest(w, "title, pinned, tag or alias is required")
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
	if body.Alias != nil {
		// 与 sessions.alias（varchar(64)）一致；trim 后空串表示清除别名。
		// 别名是"用户给会话起的名字/备注"：展示时优先于 title（displayName = alias || title），
		// 但不动 title 本身 —— 系统仍可用 title 描述会话内容。
		trimmed := strings.TrimSpace(*body.Alias)
		if len([]rune(trimmed)) > 64 {
			BadRequest(w, "alias is too long")
			return
		}
		body.Alias = &trimmed
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
		Alias:  body.Alias,
	})
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update session failed")
		return
	}

	// 分支会话一并回带血缘（前端就地更新列表项时不必重新拉取）
	out := Conversation{
		ID:              updated.ID,
		Title:           updated.Title,
		Pinned:          updated.Pinned,
		Tag:             updated.Tag,
		Alias:           updated.Alias,
		ParentSessionID: updated.ParentSessionID,
		BranchFromSeq:   updated.BranchFromSeq,
		CreatedAt:       updated.CreatedAt,
		UpdatedAt:       updated.UpdatedAt,
	}
	if out.ParentSessionID != "" {
		out.ParentTitle = parentTitlesFor(r.Context(), h.sessionMgr, []Conversation{out})[out.ParentSessionID]
	}
	OK(w, out)
}
