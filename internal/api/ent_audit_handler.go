package api

import (
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ── 企业审计日志查询 ─────────────────────────────────────────────────────

// EntAuditHandler 提供企业审计日志查询 API。
// 路由注册由集成任务统一接入（本任务不注册）：
//
//	auditHandler := api.NewEntAuditHandler()
//	auditHandler.RegisterRoutes(mux, authMW)
type EntAuditHandler struct{}

// NewEntAuditHandler 创建审计查询 handler。
func NewEntAuditHandler() *EntAuditHandler { return &EntAuditHandler{} }

// RegisterRoutes 挂载审计路由（authMW + RequireEntPerm("audit:read")）。
func (h *EntAuditHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/ent/audit", authMW(RequireEntPerm("audit:read")(http.HandlerFunc(h.Query))))
}

// auditQueryFilter 审计查询的归一化过滤条件（时间范围与分页已强制收敛）。
type auditQueryFilter struct {
	TenantID     string
	UserID       string
	Action       string
	ResourceType string
	From         time.Time
	To           time.Time
	Page         int
	PageSize     int
}

const auditMaxRange = 7 * 24 * time.Hour

// parseAuditQuery 解析并强制收敛查询参数（纯函数，独立可测）：
//   - 时间范围必填语义：from/to 缺省时强制最近 7 天；
//     显式范围超过 7 天时收敛为 to-7d ~ to；from >= to 报错；
//   - 强制分页：page>=1，page_size 缺省 50、上限 100。
func parseAuditQuery(q url.Values, now time.Time) (auditQueryFilter, error) {
	f := auditQueryFilter{
		UserID:       q.Get("user_id"),
		Action:       q.Get("action"),
		ResourceType: q.Get("resource_type"),
		Page:         1,
		PageSize:     50,
	}

	parse := func(s string) (time.Time, error) {
		// 纯日期按**本地时区**解释：审计时间是给人看的"哪一天"，
		// 按 UTC 解析会让本地 00:00–08:00 的记录落到前一天。
		if t, err := time.ParseInLocation("2006-01-02", s, time.Local); err == nil {
			return t, nil
		}
		return time.Parse(time.RFC3339, s)
	}

	f.To = now.UTC()
	f.From = f.To.Add(-auditMaxRange)
	if toStr := q.Get("to"); toStr != "" {
		t, err := parse(toStr)
		if err != nil {
			return f, fmt.Errorf("invalid to: %w", err)
		}
		// `to` 是纯日期时表示"包含当天"：窗口上界取次日 00:00。
		// 否则前端默认的 `to=今天` 会被当成今天 00:00，今天发生的记录全部被排除 ——
		// 这正是"审计页明明有记录却查不到"的第二个根因。
		if _, err := time.ParseInLocation("2006-01-02", toStr, time.Local); err == nil {
			t = t.AddDate(0, 0, 1)
		}
		f.To = t
	}
	if fromStr := q.Get("from"); fromStr != "" {
		t, err := parse(fromStr)
		if err != nil {
			return f, fmt.Errorf("invalid from: %w", err)
		}
		f.From = t
	}
	if !f.From.Before(f.To) {
		return f, fmt.Errorf("from must be before to")
	}
	// 时间范围上限 7 天（防止全表扫描，保证命中 idx_audit_logs_tenant_time）
	if f.To.Sub(f.From) > auditMaxRange {
		f.From = f.To.Add(-auditMaxRange)
	}

	if v := q.Get("page"); v != "" {
		n := 0
		if _, err := fmt.Sscanf(v, "%d", &n); err != nil || n < 1 {
			return f, fmt.Errorf("invalid page")
		}
		f.Page = n
	}
	if v := q.Get("page_size"); v != "" {
		n := 0
		if _, err := fmt.Sscanf(v, "%d", &n); err != nil || n < 1 {
			return f, fmt.Errorf("invalid page_size")
		}
		f.PageSize = n
	}
	if f.PageSize > 100 {
		f.PageSize = 100
	}
	return f, nil
}

// auditLogRow 审计查询响应行。
type auditLogRow struct {
	ID           string    `json:"id"`
	TenantID     string    `json:"tenant_id"`
	UserID       *string   `json:"user_id"`
	Action       string    `json:"action"`
	ResourceType string    `json:"resource_type"`
	ResourceID   *string   `json:"resource_id"`
	Details      any       `json:"details"`
	IPAddress    *string   `json:"ip_address"`
	CreatedAt    time.Time `json:"created_at"`
}

// Query GET /v1/ent/audit?user_id=&action=&resource_type=&from=&to=&page=&page_size=
// 租户隔离：claims.TenantID 优先，缺省回退默认租户；纯 SQL 走
// idx_audit_logs_tenant_time(tenant_id, created_at DESC) 索引。
func (h *EntAuditHandler) Query(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	f, err := parseAuditQuery(r.URL.Query(), time.Now())
	if err != nil {
		BadRequest(w, "invalid audit query parameters")
		return
	}
	f.TenantID = entPolicyTenantID(claims)

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}

	// created_at 是 `timestamp without time zone`（存的是写入时会话时区的墙钟时间），
	// 而 f.From/f.To 是 UTC 时刻。直接把两者比较会差一个时区偏移 —— 实测本地
	// (+08) 写入 09:47 的行，用 01:47Z 的窗口永远匹配不到，审计页恒为空。
	// 显式 cast 成 timestamptz：让 PG 按会话时区把墙钟解释回绝对时刻再比较。
	where := ` WHERE tenant_id = $1 AND created_at::timestamptz >= $2 AND created_at::timestamptz < $3`
	args := []any{f.TenantID, f.From, f.To}
	if f.UserID != "" {
		args = append(args, f.UserID)
		where += fmt.Sprintf(` AND user_id = $%d`, len(args))
	}
	if f.Action != "" {
		args = append(args, f.Action)
		where += fmt.Sprintf(` AND action = $%d`, len(args))
	}
	if f.ResourceType != "" {
		args = append(args, f.ResourceType)
		where += fmt.Sprintf(` AND resource_type = $%d`, len(args))
	}

	var total int
	if err := pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM audit_logs`+where, args...).Scan(&total); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "audit query failed")
		return
	}

	offset := (f.Page - 1) * f.PageSize
	rows, err := pool.Query(r.Context(),
		`SELECT id, tenant_id, user_id, action, resource_type, resource_id,
		        details, ip_address, created_at
		 FROM audit_logs`+where+`
		 ORDER BY created_at DESC
		 LIMIT $`+fmt.Sprint(len(args)+1)+` OFFSET $`+fmt.Sprint(len(args)+2),
		append(args, f.PageSize, offset)...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "audit query failed")
		return
	}
	defer rows.Close()

	out := []auditLogRow{}
	for rows.Next() {
		var row auditLogRow
		if err := rows.Scan(&row.ID, &row.TenantID, &row.UserID, &row.Action,
			&row.ResourceType, &row.ResourceID, &row.Details, &row.IPAddress,
			&row.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "audit query failed")
			return
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "audit query failed")
		return
	}

	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    out,
		Meta:    &Meta{Total: total, Page: f.Page, PerPage: f.PageSize},
	})
}
