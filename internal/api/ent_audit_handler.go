package api

import (
	"encoding/json"
	"fmt"
	"log/slog"
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
	permMW := RequireEntPerm("audit:read")
	mux.Handle("GET /v1/ent/audit", authMW(permMW(http.HandlerFunc(h.Query))))
	// 审计导出（M3）：与查询同一权限口径 —— 能看审计的人就能导，
	// 不给导出单独开一个更宽的权限点（导出只是"同一份数据的另一个出口"）。
	mux.Handle("GET /v1/ent/audit.csv", authMW(permMW(http.HandlerFunc(h.Export))))
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

// auditExportMaxSpan 是审计导出的时间跨度上限。
//
// 比查询的 7 天宽得多：查询是"在页面上看"，导出的用词是"把这段时间的流水
// 拿走对账"，沿用 7 天会让用户以为导出的内容被截断了。仍设上限是因为导出是
// **同步流式**端点，无边界的时间跨度等于让一个请求扫全表。
const auditExportMaxSpan = 366 * 24 * time.Hour

// auditExportMaxRows 是单次导出的行数上限（与交易/发票导出同一口径）。
const auditExportMaxRows = 50000

// parseAuditRange 解析并收敛 from/to（查询与导出共用，保证口径一致）。
//
// 两条容易做错的细节都在这里，且**只在这里**：
//   - 纯日期按**本地时区**解释：审计时间是给人看的"哪一天"，按 UTC 解析会让
//     本地 00:00–08:00 的记录落到前一天；
//   - `to` 是纯日期时表示"包含当天"，上界取次日 00:00 —— 否则前端默认的
//     `to=今天` 会被当成今天 00:00，今天发生的记录全部被排除。
//
// maxSpan 是跨度上限：查询传 7 天，导出传 366 天。
func parseAuditRange(q url.Values, now time.Time, maxSpan time.Duration) (from, to time.Time, err error) {
	parse := func(s string) (time.Time, error) {
		if t, err := time.ParseInLocation("2006-01-02", s, time.Local); err == nil {
			return t, nil
		}
		return time.Parse(time.RFC3339, s)
	}

	to = now.UTC()
	from = to.Add(-maxSpan)
	if toStr := q.Get("to"); toStr != "" {
		t, perr := parse(toStr)
		if perr != nil {
			return from, to, fmt.Errorf("invalid to: %w", perr)
		}
		if _, derr := time.ParseInLocation("2006-01-02", toStr, time.Local); derr == nil {
			t = t.AddDate(0, 0, 1)
		}
		to = t
	}
	if fromStr := q.Get("from"); fromStr != "" {
		t, perr := parse(fromStr)
		if perr != nil {
			return from, to, fmt.Errorf("invalid from: %w", perr)
		}
		from = t
	}
	if !from.Before(to) {
		return from, to, fmt.Errorf("from must be before to")
	}
	// 超跨度收敛为「to - maxSpan」而不是报错：用户选的是一段真实区间，
	// 直接拒绝会让"我就想导这一年"变成无从下手；收敛后至少能拿到最近一段。
	if to.Sub(from) > maxSpan {
		from = to.Add(-maxSpan)
	}
	return from, to, nil
}

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

	from, to, err := parseAuditRange(q, now, auditMaxRange)
	if err != nil {
		return f, err
	}
	f.From, f.To = from, to

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

// buildAuditWhere 构建审计查询的过滤条件（查询与导出共用）。
//
// 抽出来是为了让"页面看到的"与"导出的"永远是同一套口径：两处各写一份 WHERE，
// 迟早会出现"页面上 12 条、导出 3 条"这种无从解释的差异。
//
// created_at 是 `timestamp without time zone`（存的是写入时会话时区的墙钟时间），
// 而 f.From/f.To 是 UTC 时刻。直接把两者比较会差一个时区偏移 —— 实测本地
// (+08) 写入 09:47 的行，用 01:47Z 的窗口永远匹配不到，审计页恒为空。
// 显式 cast 成 timestamptz：让 PG 按会话时区把墙钟解释回绝对时刻再比较。
func buildAuditWhere(f auditQueryFilter) (string, []any) {
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
	return where, args
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
	where, args := buildAuditWhere(f)

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

// ── 导出（M3）────────────────────────────────────────────────────────────

// auditExportHeader 是审计导出的表头。
//
// 时间列标「服务器本地」而不是 UTC：`created_at` 是 `timestamp without time zone`，
// 存的是写入时会话时区的**墙钟**（实测 +08）。标成 UTC 会让跨时区对账差 8 小时，
// 而列里的值本身并没有时区信息 —— 标注错比不标更糟。
var auditExportHeader = []string{
	"时间(服务器本地)", "租户", "用户", "动作", "资源类型", "资源ID", "IP", "详情",
}

// formatAuditExportRow 把一条审计记录转成 CSV 行。
//
// 列顺序必须与 auditExportHeader 一致。文本列全部走 csvSafeText：
// action / resource_id / details 都可能带上用户可控内容（资源名、错误原文），
// 以 `=`/`+`/`-`/`@` 开头时会被 Excel 当公式执行。
func formatAuditExportRow(row auditLogRow) []string {
	details := ""
	if row.Details != nil {
		if b, err := json.Marshal(row.Details); err == nil {
			details = string(b)
		} else {
			// 不可序列化时退化到 fmt：导出里留一个可读的近似值，
			// 比整行报错或留空更有用。
			details = fmt.Sprint(row.Details)
		}
	}
	return []string{
		row.CreatedAt.Format("2006-01-02 15:04:05"),
		row.TenantID,
		csvSafeText(derefString(row.UserID)),
		csvSafeText(row.Action),
		csvSafeText(row.ResourceType),
		csvSafeText(derefString(row.ResourceID)),
		csvSafeText(derefString(row.IPAddress)),
		csvSafeText(details),
	}
}

// auditExportDisposition 生成 Content-Disposition。
//
// 文件名只用 ASCII：部分浏览器会把中文文件名按错误编码落盘，
// 用户下到的是一个乱码名字的文件。冒号在 Windows 上非法，故时间用 `T` 分隔。
func auditExportDisposition(now time.Time) string {
	name := "audit_" + now.UTC().Format("20060102T1504") + ".csv"
	return `attachment; filename="` + name + `"`
}

// Export GET /v1/ent/audit.csv?user_id=&action=&resource_type=&from=&to=
//
// 与 Query 共用 parseAuditRange 与 buildAuditWhere，保证"页面看到的"与
// "导出的"是同一套口径；但跨度上限放宽到 366 天（导出是拿走去对账，
// 沿用 7 天会让人以为内容被截断）。
//
// 租户隔离与查询完全一致：tenant_id 只来自 claims，不接受参数覆盖。
func (h *EntAuditHandler) Export(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	q := r.URL.Query()
	f := auditQueryFilter{
		UserID:       q.Get("user_id"),
		Action:       q.Get("action"),
		ResourceType: q.Get("resource_type"),
	}
	from, to, err := parseAuditRange(q, time.Now(), auditExportMaxSpan)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	f.From, f.To = from, to
	f.TenantID = entPolicyTenantID(claims)

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	where, args := buildAuditWhere(f)

	// 先数一遍再写。顺序很关键：一旦开始往 w 写字节，状态码就固定为 200，
	// 超限时就没法再返回 400 了（与交易/发票导出同一条约束）。
	var total int
	if err := pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM audit_logs`+where, args...).Scan(&total); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "count audit for export failed")
		return
	}
	if total > auditExportMaxRows {
		JSONWithCode(w, http.StatusBadRequest, CodeExportTooManyRows,
			map[string]interface{}{"max_rows": auditExportMaxRows},
			fmt.Sprintf("too many audit records (%d) in range, please narrow the range", total))
		return
	}

	rows, err := pool.Query(r.Context(),
		`SELECT id, tenant_id, user_id, action, resource_type, resource_id,
		        details, ip_address, created_at
		 FROM audit_logs`+where+`
		 ORDER BY created_at DESC`, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load audit for export failed")
		return
	}
	defer rows.Close()

	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", auditExportDisposition(time.Now()))
	w.WriteHeader(http.StatusOK)

	// 走到这里已经发出 200：后续任何扫描失败都只能中断下载并记日志，
	// 无法再改状态码（与交易导出同一条约束）。
	writeErr := writeCSVRows(w, auditExportHeader, func(yield func([]string) bool) {
		for rows.Next() {
			var row auditLogRow
			if err := rows.Scan(&row.ID, &row.TenantID, &row.UserID, &row.Action,
				&row.ResourceType, &row.ResourceID, &row.Details, &row.IPAddress,
				&row.CreatedAt); err != nil {
				slog.Error("scan audit for export failed", "tenant", f.TenantID, "error", err)
				return
			}
			if !yield(formatAuditExportRow(row)) {
				return
			}
		}
		if err := rows.Err(); err != nil {
			slog.Error("iterate audit for export failed", "tenant", f.TenantID, "error", err)
		}
	})
	if writeErr != nil {
		slog.Warn("write audit csv failed", "tenant", f.TenantID, "error", writeErr)
	}
}
