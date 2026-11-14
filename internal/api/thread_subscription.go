package api

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/google/uuid"

	"github.com/athenavi/chiron/internal/db"
)

// thread_subscription.go —— P1-1 订阅 CRUD。
//
// 订阅回答的是"这条事件该投给哪个会话"。明确：**无订阅则该事件到此为止**，
// 不回退给 Router 猜 —— 否则"有没有订阅"就不可观测，§6 要的那组对照埋点也就做不了。
// 因此这一层的正确性直接决定埋点能否成立，判据一律用 `user_id`（不是 `tenant_id`）：
// 订阅是个人授权行为，同租户的其他用户不该能改别人的投递去向。
//
// 三个动作都挂在 `/v1/threads/{id}/subscriptions`：
//   GET    列出该 thread 的订阅（含 paused，前端要能显示"已暂停"而不是消失）
//   POST   新增一条（同一 thread 对同一会话已有 active 订阅时由部分唯一索引挡住）
//   DELETE 删一条（`{sub_id}` 指订阅 id，不是 session id）

const (
	threadSubscriptionActive = "active"
	threadSubscriptionPaused = "paused"
)

type threadSubscriptionView struct {
	ID        string `json:"id"`
	ThreadID  string `json:"thread_id"`
	SessionID string `json:"session_id"`
	Status    string `json:"status"`
	CreatedAt string `json:"created_at"`
}

type createThreadSubscriptionRequest struct {
	SessionID string `json:"session_id"`
	Status    string `json:"status"`
}

func ListThreadSubscriptions(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	threadID := r.PathValue("id")
	if threadID == "" {
		BadRequest(w, "thread id is required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}
	// JOIN threads 而不是先查后比：归属判定写在**同一条查询**里，
	// "查到了但不是你的"与"根本不存在"对调用方必须是同一件事（都 404），
	// 分开写迟早会漏掉其中一条，变成可以枚举别人的 thread id。
	rows, err := db.GlobalDBManager.Query(r.Context(), `
		SELECT s.id, s.thread_id, s.session_id, s.status, s.created_at::text
		  FROM thread_subscriptions s
		  JOIN threads t ON t.id = s.thread_id
		 WHERE s.thread_id = $1 AND t.user_id = $2
		 ORDER BY s.created_at DESC`, threadID, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list thread subscriptions failed")
		return
	}
	defer rows.Close()

	out := make([]threadSubscriptionView, 0, 4)
	for rows.Next() {
		var v threadSubscriptionView
		if err := rows.Scan(&v.ID, &v.ThreadID, &v.SessionID, &v.Status, &v.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan thread subscription failed")
			return
		}
		out = append(out, v)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list thread subscriptions failed")
		return
	}
	OK(w, map[string]interface{}{"subscriptions": out})
}

func CreateThreadSubscription(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	tenantID := GetTenantID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	threadID := r.PathValue("id")
	if threadID == "" {
		BadRequest(w, "thread id is required")
		return
	}
	var body createThreadSubscriptionRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	sessionID := strings.TrimSpace(body.SessionID)
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	status := strings.TrimSpace(body.Status)
	if status == "" {
		status = threadSubscriptionActive
	}
	// 只接受两个值：订阅状态是"投不投"的开关，多出来的取值没有语义，
	// 静默存下去只会让 drain 时判不清该不该投。
	if status != threadSubscriptionActive && status != threadSubscriptionPaused {
		BadRequest(w, "status must be active or paused")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}

	ctx := r.Context()
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create thread subscription failed")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var threadTenant int16
	var threadUser string
	if err := tx.QueryRow(ctx,
		`SELECT tenant_id, user_id FROM threads WHERE id = $1`, threadID).
		Scan(&threadTenant, &threadUser); err != nil {
		// 不存在与不属于你同一个答复：见 List 的注释。
		NotFound(w, "thread not found")
		return
	}
	if threadUser != userID {
		NotFound(w, "thread not found")
		return
	}
	// 会话归属用既有那套判据（与接管定时任务同一个函数）：
	// 订阅一个不属于自己的会话等于把别人的会话变成自己事件的投递目标。
	if err := assertSessionsOwned(ctx, tx, []string{sessionID}, tenantID, userID); err != nil {
		BadRequest(w, "session not found or not owned by caller")
		return
	}

	var id string
	if err := tx.QueryRow(ctx, `
		INSERT INTO thread_subscriptions (id, thread_id, session_id, status)
		VALUES ($1, $2, $3, $4)
		RETURNING id`, uuid.NewString(), threadID, sessionID, status).Scan(&id); err != nil {
		// 部分唯一索引 `ux_thread_subscriptions_active` 挡住"同一 thread 对同一会话
		// 已有 active 订阅"。这不是服务端故障，是调用方重复提交 —— 409 而不是 500。
		slog.Warn("create thread subscription failed", "thread", threadID, "error", err)
		JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "subscription already exists"})
		return
	}
	if err := tx.Commit(ctx); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create thread subscription failed")
		return
	}
	Created(w, threadSubscriptionView{
		ID: id, ThreadID: threadID, SessionID: sessionID, Status: status,
	})
}

func DeleteThreadSubscription(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	threadID := r.PathValue("id")
	subID := r.PathValue("sub_id")
	if threadID == "" || subID == "" {
		BadRequest(w, "thread id and subscription id are required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, "database not available")
		return
	}
	// 归属同时校验在 WHERE 里：DELETE 只带 id 的话，猜到 id 就能删掉别人的订阅，
	// 而"删成功"与"没删掉"在响应上分辨不出来时，这种越权是最难发现的一类。
	res, err := db.GlobalDBManager.Exec(r.Context(), `
		DELETE FROM thread_subscriptions s
		 USING threads t
		 WHERE s.id = $1 AND s.thread_id = $2 AND t.id = s.thread_id AND t.user_id = $3`,
		subID, threadID, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete thread subscription failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "subscription not found")
		return
	}
	OK(w, map[string]string{"status": "deleted", "id": subID})
}
