package api

import (
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/notify"
)

// NotificationHandler 提供通知中心的用户侧接口（P2-3）。
//
// 全部路由都只操作**当前登录用户自己的**通知：user_id 一律取自 claims，
// 不接受请求体或 query 传入的 user_id —— 否则任何人只要猜到一个 UUID 就能读
// 别人的通知（内含支付金额、套餐名这类敏感信息）。
type NotificationHandler struct {
	mgr *notify.Manager
}

// NewNotificationHandler 构造 handler。
func NewNotificationHandler(mgr *notify.Manager) *NotificationHandler {
	return &NotificationHandler{mgr: mgr}
}

// RegisterRoutes 挂载通知中心路由。
func (h *NotificationHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/notifications", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("GET /v1/notifications/unread_count", authMW(rlMW(http.HandlerFunc(h.UnreadCount))))
	mux.Handle("POST /v1/notifications/read", authMW(rlMW(http.HandlerFunc(h.MarkRead))))
	mux.Handle("POST /v1/notifications/read_all", authMW(rlMW(http.HandlerFunc(h.MarkAllRead))))
	mux.Handle("GET /v1/notifications/prefs", authMW(rlMW(http.HandlerFunc(h.GetPrefs))))
	mux.Handle("PUT /v1/notifications/prefs", authMW(rlMW(http.HandlerFunc(h.UpdatePrefs))))
}

// currentUserID 取当前登录用户；未登录返回空串。
func currentUserID(r *http.Request) string {
	if claims := auth.GetClaims(r.Context()); claims != nil {
		return claims.UserID
	}
	return ""
}

// notificationListMax 是单次列表返回上限。
//
// 硬上限而不是分页：通知是"最近发生了什么的摘要"，翻到第 50 页没有使用场景，
// 而游标分页会给前端增加不必要的状态。
const notificationListMax = 200

// notificationView 是返回给前端的通知形状。
//
// 不含 dedupe_key 与 tenant_id：前者是内部幂等实现细节，后者用户无需关心
// （他们只看得到自己租户内的通知）。
type notificationView struct {
	ID        string         `json:"id"`
	Kind      string         `json:"kind"`
	Title     string         `json:"title"`
	Body      string         `json:"body"`
	Link      string         `json:"link"`
	Payload   map[string]any `json:"payload,omitempty"`
	Read      bool           `json:"read"`
	ReadAt    *string        `json:"read_at"`
	CreatedAt string         `json:"created_at"`
}

// List GET /v1/notifications?limit=50&unread_only=false
func (h *NotificationHandler) List(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil || v <= 0 {
			BadRequest(w, "limit must be a positive integer")
			return
		}
		// 夹逼而不是报错：前端传大值是想"一次多拿点"，直接截到上限比让它 400 更友好。
		if v > notificationListMax {
			v = notificationListMax
		}
		limit = v
	}
	unreadOnly := r.URL.Query().Get("unread_only") == "true"

	items, err := h.mgr.List(r.Context(), userID, limit, unreadOnly)
	if err != nil {
		slog.Error("list notifications failed", "user_id", userID, "error", err)
		InternalError(w, "failed to list notifications")
		return
	}
	views := make([]notificationView, 0, len(items))
	for _, n := range items {
		views = append(views, toNotificationView(n))
	}
	// 同时返回未读数：前端渲染列表时几乎总要用它画角标，多一次请求没有意义。
	unread, err := h.mgr.UnreadCount(r.Context(), userID)
	if err != nil {
		// 未读数失败不应让整个列表失败 —— 通知内容本身是有效的。
		slog.Warn("count unread notifications failed", "user_id", userID, "error", err)
		unread = 0
	}
	OK(w, map[string]any{
		"items":        views,
		"unread_count": unread,
	})
}

func toNotificationView(n notify.Notification) notificationView {
	v := notificationView{
		ID:        n.ID,
		Kind:      n.Kind,
		Title:     n.Title,
		Body:      n.Body,
		Link:      n.Link,
		Payload:   n.Payload,
		Read:      n.ReadAt != nil,
		CreatedAt: n.CreatedAt.Format(rfc3339),
	}
	if n.ReadAt != nil {
		s := n.ReadAt.Format(rfc3339)
		v.ReadAt = &s
	}
	return v
}

// rfc3339 是统一的时间序列化格式（带时区，前端才能正确本地化）。
const rfc3339 = "2006-01-02T15:04:05Z07:00"

// UnreadCount GET /v1/notifications/unread_count
//
// 独立端点供顶栏角标轮询：只返回一个整数，比拉整个列表轻得多。
func (h *NotificationHandler) UnreadCount(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	n, err := h.mgr.UnreadCount(r.Context(), userID)
	if err != nil {
		slog.Error("count unread notifications failed", "user_id", userID, "error", err)
		InternalError(w, "failed to count notifications")
		return
	}
	OK(w, map[string]int{"unread_count": n})
}

// MarkRead POST /v1/notifications/read  body: {"ids": ["..."]}
func (h *NotificationHandler) MarkRead(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		IDs []string `json:"ids"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if len(body.IDs) == 0 {
		BadRequest(w, "ids is required")
		return
	}
	// 上限与列表上限一致：一次最多标记一屏能看到的条数，防止构造超大数组打库。
	if len(body.IDs) > notificationListMax {
		BadRequest(w, "too many ids")
		return
	}
	affected, err := h.mgr.MarkRead(r.Context(), userID, body.IDs)
	if err != nil {
		slog.Error("mark notifications read failed", "user_id", userID, "error", err)
		InternalError(w, "failed to mark read")
		return
	}
	// affected 是"这次真正改变的行数"：重复标记同一批返回 0 而不是错误，
	// 因为对调用方而言目标状态已经达成。
	OK(w, map[string]int{"marked": affected})
}

// MarkAllRead POST /v1/notifications/read_all
func (h *NotificationHandler) MarkAllRead(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	affected, err := h.mgr.MarkAllRead(r.Context(), userID)
	if err != nil {
		slog.Error("mark all notifications read failed", "user_id", userID, "error", err)
		InternalError(w, "failed to mark all read")
		return
	}
	OK(w, map[string]int{"marked": affected})
}

// GetPrefs GET /v1/notifications/prefs
//
// 返回全部事件类型的**实际生效**开关（未设置过的由 Manager 补默认开启），
// 前端因此不需要知道默认值是什么。
func (h *NotificationHandler) GetPrefs(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	prefs, err := h.mgr.Prefs(r.Context(), userID)
	if err != nil {
		slog.Error("load notification prefs failed", "user_id", userID, "error", err)
		InternalError(w, "failed to load preferences")
		return
	}
	// kinds 一并返回：前端据此渲染开关列表，新增事件类型时后端加一行即可生效，
	// 不必同步改前端常量。
	OK(w, map[string]any{
		"email_enabled": prefs,
		"kinds":         notify.AllKinds,
		"note":          "站内通知始终开启，这里只控制是否同时发送邮件",
	})
}

// UpdatePrefs PUT /v1/notifications/prefs  body: {"kind": "...", "email_enabled": true}
//
// 单条更新而不是整表覆盖：整表覆盖要求前端先 GET 再 PUT，中间若有别的端改过
// 偏好（比如用户在邮件里点了退订），会被静默改回去。
func (h *NotificationHandler) UpdatePrefs(w http.ResponseWriter, r *http.Request) {
	userID := currentUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		Kind         string `json:"kind"`
		EmailEnabled *bool  `json:"email_enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.EmailEnabled == nil {
		BadRequest(w, "email_enabled is required")
		return
	}
	if err := h.mgr.SetEmailEnabled(r.Context(), userID, body.Kind, *body.EmailEnabled); err != nil {
		if errors.Is(err, notify.ErrUnknownKind) {
			BadRequest(w, "unknown notification kind")
			return
		}
		slog.Error("update notification pref failed", "user_id", userID, "kind", body.Kind, "error", err)
		InternalError(w, "failed to save preference")
		return
	}
	prefs, err := h.mgr.Prefs(r.Context(), userID)
	if err != nil {
		// 已写入成功，只是回读失败：返回当前请求的结果即可，不让前端以为保存失败。
		slog.Warn("reload notification prefs failed", "user_id", userID, "error", err)
		OK(w, map[string]any{"kind": body.Kind, "email_enabled": *body.EmailEnabled})
		return
	}
	OK(w, map[string]any{
		"email_enabled": prefs,
		"kinds":         notify.AllKinds,
	})
}
