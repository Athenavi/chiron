package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// ─────────────────────────────────────────────────────────────
// /admin 全栈实装：租户 / 域名 / 数据库 / Redis / 模型 / 定时任务
// 所有数据均来自真实存储（无 mock），读写经 admin 权限路由（adminReadMW/adminWriteMW）。
// ─────────────────────────────────────────────────────────────

// registerOpsRoutes 挂载运维类管理端点（在 adminMux 内，经 StripPrefix /v1/admin）。
func (h *AdminHandler) registerOpsRoutes(r *http.ServeMux) {
	// 租户管理
	r.HandleFunc("GET /tenants", h.ListTenants)
	r.HandleFunc("POST /tenants", h.CreateTenant)
	r.HandleFunc("PUT /tenants/{id}", h.UpdateTenant)
	r.HandleFunc("DELETE /tenants/{id}", h.DeleteTenant)
	r.HandleFunc("POST /tenants/{id}/suspend", h.SuspendTenant)
	r.HandleFunc("GET /tenants/{id}/usage", h.TenantUsage)

	// 域名管理
	r.HandleFunc("GET /domains", h.ListDomains)
	r.HandleFunc("POST /domains", h.CreateDomain)
	r.HandleFunc("PUT /domains/{id}", h.UpdateDomain)
	r.HandleFunc("DELETE /domains/{id}", h.DeleteDomain)
	r.HandleFunc("POST /domains/{id}/verify", h.VerifyDomain)
	r.HandleFunc("POST /domains/{id}/renew-ssl", h.RenewDomainSSL)

	// 数据库管理
	r.HandleFunc("GET /database/configs", h.DatabaseConfigs)
	r.HandleFunc("GET /database/status", h.DatabaseStatus)
	r.HandleFunc("POST /database/query", h.DatabaseQuery)
	r.HandleFunc("POST /database/optimize/{action}", h.DatabaseOptimize)

	// Redis 管理（单实例真实操作）
	r.HandleFunc("GET /redis/slow-log", h.RedisSlowLog)
	r.HandleFunc("POST /redis/flush-all", h.RedisFlushAll)

	// 模型注册表
	r.HandleFunc("GET /models", h.ListModels)
	r.HandleFunc("POST /models", h.CreateModel)
	// 批量开关（/models 页的"组内全选 → 启用/禁用"）：字面量段比 /models/{id} 更具体，
	// Go 1.22+ 的 ServeMux 据此优先匹配，两者可共存。
	r.HandleFunc("PUT /models/batch", h.BatchUpdateModels)
	r.HandleFunc("PUT /models/{id}", h.UpdateModel)
	r.HandleFunc("DELETE /models/{id}", h.DeleteModel)

	// 定时任务（DB 持久化；执行由调度器接入）
	r.HandleFunc("GET /cron-jobs", h.ListCronJobs)
	r.HandleFunc("POST /cron-jobs", h.CreateCronJob)
	r.HandleFunc("PUT /cron-jobs/{id}", h.UpdateCronJob)
	r.HandleFunc("DELETE /cron-jobs/{id}", h.DeleteCronJob)
	r.HandleFunc("POST /cron-jobs/{id}/trigger", h.HandleCronTrigger)

	// 租户邀请码（F2）：注册时据邀请码决定新用户归属哪个租户
	r.HandleFunc("GET /invites", h.ListInvites)
	r.HandleFunc("POST /invites", h.CreateInvite)
	r.HandleFunc("DELETE /invites/{code}", h.DeleteInvite)
}

// ── 租户管理 ──

type tenantRow struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Status    string    `json:"status"`
	CreatedAt time.Time `json:"created_at"`
}

// ── 租户邀请码（F2）──

type inviteRow struct {
	Code      string     `json:"code"`
	TenantID  string     `json:"tenant_id"`
	MaxUses   int        `json:"max_uses"`
	Uses      int        `json:"uses"`
	ExpiresAt *time.Time `json:"expires_at"`
	CreatedAt time.Time  `json:"created_at"`
}

// ListInvites 列出全部租户邀请码。
func (h *AdminHandler) ListInvites(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT code, tenant_id::text, max_uses, uses, expires_at, created_at
		   FROM tenant_invites ORDER BY created_at DESC`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invites failed")
		return
	}
	defer rows.Close()
	out := []inviteRow{}
	for rows.Next() {
		var it inviteRow
		if err := rows.Scan(&it.Code, &it.TenantID, &it.MaxUses, &it.Uses, &it.ExpiresAt, &it.CreatedAt); err == nil {
			out = append(out, it)
		}
	}
	OK(w, map[string]interface{}{"invites": out, "total": len(out)})
}

// CreateInvite 为一个租户创建邀请码（未提供 code 时自动生成 16 位随机码）。
func (h *AdminHandler) CreateInvite(w http.ResponseWriter, r *http.Request) {
	var body struct {
		TenantID      string `json:"tenant_id"`
		Code          string `json:"code"`
		MaxUses       int    `json:"max_uses"`
		ExpiresInDays int    `json:"expires_in_days"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	tenantID := strings.TrimSpace(body.TenantID)
	if tenantID == "" {
		BadRequest(w, "tenant_id is required")
		return
	}
	var tenantExists bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM tenants WHERE id = $1)`, tenantID).Scan(&tenantExists); err != nil || !tenantExists {
		BadRequest(w, "tenant not found")
		return
	}

	code := strings.TrimSpace(body.Code)
	if code == "" {
		raw, err := id.UUID()
		if err != nil {
			InternalError(w, "generate code failed")
			return
		}
		code = strings.ReplaceAll(raw, "-", "")[:16]
	}

	var expiresAt interface{}
	if body.ExpiresInDays > 0 {
		expiresAt = time.Now().AddDate(0, 0, body.ExpiresInDays)
	}
	createdBy := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		createdBy = claims.UserID
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO tenant_invites (code, tenant_id, created_by, max_uses, expires_at)
		 VALUES ($1, $2, NULLIF($3, ''), $4, $5)`,
		code, tenantID, createdBy, body.MaxUses, expiresAt); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create invite failed")
		return
	}
	OK(w, map[string]interface{}{"code": code, "tenant_id": tenantID})
}

// DeleteInvite 撤销邀请码（撤销即删除该行）。
func (h *AdminHandler) DeleteInvite(w http.ResponseWriter, r *http.Request) {
	code := r.PathValue("code")
	if code == "" {
		BadRequest(w, "code is required")
		return
	}
	res, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM tenant_invites WHERE code = $1`, code)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete invite failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "invite not found")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

func (h *AdminHandler) ListTenants(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, name, status, created_at FROM tenants ORDER BY created_at DESC`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenants failed")
		return
	}
	defer rows.Close()
	out := []tenantRow{}
	for rows.Next() {
		var t tenantRow
		if err := rows.Scan(&t.ID, &t.Name, &t.Status, &t.CreatedAt); err == nil {
			out = append(out, t)
		}
	}
	OK(w, map[string]interface{}{"tenants": out, "total": len(out)})
}

func (h *AdminHandler) CreateTenant(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name string `json:"name"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || strings.TrimSpace(body.Name) == "" {
		BadRequest(w, "name is required")
		return
	}
	tenantID, err := id.UUID()
	if err != nil {
		InternalError(w, "generate id failed")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO tenants (id, name, status) VALUES ($1, $2, 'active')`, tenantID, strings.TrimSpace(body.Name)); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create tenant failed")
		return
	}
	OK(w, map[string]interface{}{"id": tenantID, "name": body.Name, "status": "active"})
}

func (h *AdminHandler) UpdateTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		Name   string `json:"name"`
		Status string `json:"status"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Name == "" && body.Status == "" {
		BadRequest(w, "no fields to update")
		return
	}
	if body.Status != "" && body.Status != "active" && body.Status != "suspended" {
		BadRequest(w, "status must be active or suspended")
		return
	}
	// S 安全修复：列名白名单，防止 SQL 注入
	tenantColumnMap := map[string]string{
		"name":   "name",
		"status": "status",
	}
	sets, args := []string{}, []interface{}{}
	idx := 1
	tenantFields := []struct {
		field string
		value string
	}{
		{"name", body.Name},
		{"status", body.Status},
	}
	for _, fv := range tenantFields {
		if fv.value != "" {
			col, ok := tenantColumnMap[fv.field]
			if !ok {
				continue
			}
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, fv.value)
			idx++
		}
	}
	args = append(args, id)
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE tenants SET %s, updated_at = NOW() WHERE id = $%d", strings.Join(sets, ", "), idx), args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update tenant failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM tenants WHERE id = $1`, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete tenant failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

func (h *AdminHandler) SuspendTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE tenants SET status = 'suspended', updated_at = NOW() WHERE id = $1`, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "suspend tenant failed")
		return
	}
	OK(w, map[string]string{"status": "suspended"})
}

func (h *AdminHandler) TenantUsage(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	type usage struct {
		Users          int `json:"users"`
		Sessions       int `json:"sessions"`
		AgentSessions  int `json:"agent_sessions"`
		KnowledgeBases int `json:"knowledge_bases"`
		Agents         int `json:"agents"`
		MediaAssets    int `json:"media_assets"`
	}
	var u usage
	ctx := r.Context()
	q := func(sql string, dst *int) {
		if *dst != 0 {
			return
		}
		_ = db.GlobalDBManager.QueryRow(ctx, sql, id).Scan(dst)
	}
	q(`SELECT COUNT(*) FROM users WHERE tenant_id = $1`, &u.Users)
	q(`SELECT COUNT(*) FROM sessions WHERE tenant_id = $1`, &u.Sessions)
	q(`SELECT COUNT(*) FROM agent_sessions WHERE tenant_id = $1`, &u.AgentSessions)
	q(`SELECT COUNT(*) FROM knowledge_bases WHERE tenant_id = $1`, &u.KnowledgeBases)
	q(`SELECT COUNT(*) FROM agents WHERE tenant_id = $1`, &u.Agents)
	q(`SELECT COUNT(*) FROM media_assets WHERE tenant_id = $1`, &u.MediaAssets)
	OK(w, u)
}

// ── 域名管理 ──

type domainRow struct {
	ID        string    `json:"id"`
	Domain    string    `json:"domain"`
	SSLStatus string    `json:"ssl_status"`
	Verified  bool      `json:"verified"`
	CreatedAt time.Time `json:"created_at"`
}

var validDomainRe = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$`)

func (h *AdminHandler) ListDomains(w http.ResponseWriter, r *http.Request) {
	// 支持分页查询
	page := 1
	perPage := 20
	if p := r.URL.Query().Get("page"); p != "" {
		fmt.Sscanf(p, "%d", &page)
		if page < 1 {
			page = 1
		}
	}
	if pp := r.URL.Query().Get("per_page"); pp != "" {
		fmt.Sscanf(pp, "%d", &perPage)
		if perPage < 1 || perPage > 100 {
			perPage = 100
		}
	}
	offset := (page - 1) * perPage

	// 先查询总数
	var total int
	_ = db.GlobalDBManager.QueryRow(r.Context(), `SELECT COUNT(*) FROM domains`).Scan(&total)

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, domain, ssl_status, verified, created_at FROM domains ORDER BY created_at DESC LIMIT $1 OFFSET $2`,
		perPage, offset)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list domains failed")
		return
	}
	defer rows.Close()
	out := []domainRow{}
	for rows.Next() {
		var d domainRow
		if err := rows.Scan(&d.ID, &d.Domain, &d.SSLStatus, &d.Verified, &d.CreatedAt); err == nil {
			out = append(out, d)
		}
	}
	OK(w, map[string]interface{}{
		"domains":  out,
		"total":    total,
		"page":     page,
		"per_page": perPage,
	})
}

func (h *AdminHandler) CreateDomain(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Domain string `json:"domain"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || !validDomainRe.MatchString(strings.ToLower(body.Domain)) {
		BadRequest(w, "valid domain is required")
		return
	}
	domain := strings.ToLower(strings.TrimSpace(body.Domain))
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO domains (tenant_id, domain) VALUES ($1, $2) ON CONFLICT (domain) DO NOTHING`,
		tenantID, domain); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create domain failed")
		return
	}
	OK(w, map[string]interface{}{"domain": domain, "ssl_status": "none", "verified": false})
}

func (h *AdminHandler) UpdateDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		Domain string `json:"domain"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || !validDomainRe.MatchString(strings.ToLower(body.Domain)) {
		BadRequest(w, "valid domain is required")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE domains SET domain = $1, verified = false, ssl_status = 'none', updated_at = NOW() WHERE id = $2`,
		strings.ToLower(body.Domain), id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update domain failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	// P0 安全修复：校验域名所有权，仅允许管理员删除（AdminHandler 已受 admin 权限保护）
	// 或校验 tenant_id 防止越权删除
	tenantID := GetTenantID(r)
	if tenantID != 0 {
		// 多租户模式：校验域名归属
		var currentTenantID string
		if err := db.GlobalDBManager.QueryRow(r.Context(),
			`SELECT tenant_id FROM domains WHERE id = $1`, id).Scan(&currentTenantID); err != nil {
			NotFound(w, "domain not found")
			return
		}
		if currentTenantID != tenantStr(tenantID) {
			Forbidden(w, "cannot delete domain from another tenant")
			return
		}
		if _, err := db.GlobalDBManager.Exec(r.Context(),
			`DELETE FROM domains WHERE id = $1 AND tenant_id = $2`, id, tenantID); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "delete domain failed")
			return
		}
	} else {
		// 管理员模式（无 tenant context）：直接删除
		if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM domains WHERE id = $1`, id); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "delete domain failed")
			return
		}
	}
	OK(w, map[string]string{"status": "deleted"})
}

// VerifyDomain 真实 DNS 校验：解析 A/AAAA 记录确认域名可达。
func (h *AdminHandler) VerifyDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var domain string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT domain FROM domains WHERE id = $1`, id).Scan(&domain); err != nil {
		NotFound(w, "domain not found")
		return
	}
	addrs, err := net.LookupHost(domain)
	verified := err == nil && len(addrs) > 0
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE domains SET verified = $1, updated_at = NOW() WHERE id = $2`, verified, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update domain failed")
		return
	}
	if !verified {
		OK(w, map[string]interface{}{"verified": false, "reason": "DNS 解析失败或无记录", "addresses": []string{}})
		return
	}
	OK(w, map[string]interface{}{"verified": true, "addresses": addrs})
}

// RenewDomainSSL 要求域名已通过验证后置 ssl_status=active（CA 签发由部署侧接入）。
func (h *AdminHandler) RenewDomainSSL(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var verified bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT verified FROM domains WHERE id = $1`, id).Scan(&verified); err != nil {
		NotFound(w, "domain not found")
		return
	}
	if !verified {
		BadRequest(w, "domain must be verified before SSL renewal")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE domains SET ssl_status = 'active', updated_at = NOW() WHERE id = $1`, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "renew ssl failed")
		return
	}
	OK(w, map[string]interface{}{"ssl_status": "active", "note": "证书签发由部署侧 CA 接入点处理"})
}

// ── 数据库管理 ──

func (h *AdminHandler) DatabaseConfigs(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT name, setting FROM pg_settings
		 WHERE name IN ('max_connections','shared_buffers','work_mem','maintenance_work_mem','effective_cache_size',
		   'wal_level','max_worker_processes','max_parallel_workers','statement_timeout','idle_in_transaction_session_timeout')
		 ORDER BY name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "read pg settings failed")
		return
	}
	defer rows.Close()
	configs := map[string]string{}
	for rows.Next() {
		var k, v string
		if rows.Scan(&k, &v) == nil {
			configs[k] = v
		}
	}
	OK(w, map[string]interface{}{"configs": configs})
}

func (h *AdminHandler) DatabaseStatus(w http.ResponseWriter, r *http.Request) {
	var version string
	err := db.GlobalDBManager.QueryRow(r.Context(), `SELECT version()`).Scan(&version)
	OK(w, map[string]interface{}{"version": version, "connected": err == nil && version != ""})
}

var selectOnlyRe = regexp.MustCompile(`(?i)^\s*select\b`)
var dangerousQueryRe = regexp.MustCompile(`(?i)\b(pg_read_file|lo_import|lo_export|lo_unlink|copy\s|copy\b|pg_write_file|pg_logdir_ls|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_sleep|generate_series)\b`)

func (h *AdminHandler) DatabaseQuery(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Query string `json:"query"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || strings.TrimSpace(body.Query) == "" {
		BadRequest(w, "query is required")
		return
	}
	// 仅允许 SELECT 查询
	if !selectOnlyRe.MatchString(body.Query) {
		BadRequest(w, "only SELECT queries are allowed")
		return
	}
	// 禁止危险函数调用
	if dangerousQueryRe.MatchString(body.Query) {
		BadRequest(w, "query not allowed")
		return
	}
	// 禁止多语句（防 stacked queries）
	if strings.Contains(strings.ToUpper(body.Query), ";") {
		BadRequest(w, "multiple statements not allowed")
		return
	}
	// 禁止 INTO OUTFILE 等导出操作
	if strings.Contains(strings.ToUpper(body.Query), "INTO OUTFILE") || strings.Contains(strings.ToUpper(body.Query), "INTO DUMPFILE") {
		BadRequest(w, "export operations not allowed")
		return
	}
	// P0 安全：添加查询超时，防止慢查询耗尽连接池
	queryCtx, queryCancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer queryCancel()
	rows, err := db.GlobalDBManager.Query(queryCtx, body.Query)
	if err != nil {
		BadRequest(w, "query failed")
		return
	}
	defer rows.Close()
	cols := make([]string, 0)
	for _, fd := range rows.FieldDescriptions() {
		cols = append(cols, string(fd.Name))
	}
	results := []map[string]interface{}{}
	count := 0
	for rows.Next() && count < 200 {
		vals := make([]interface{}, len(cols))
		ptrs := make([]interface{}, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if rows.Scan(ptrs...) != nil {
			break
		}
		row := map[string]interface{}{}
		for i, c := range cols {
			switch v := vals[i].(type) {
			case []byte:
				row[c] = string(v)
			default:
				row[c] = v
			}
		}
		results = append(results, row)
		count++
	}
	OK(w, map[string]interface{}{"columns": cols, "rows": results, "count": count, "truncated": count >= 200})
}

func (h *AdminHandler) DatabaseOptimize(w http.ResponseWriter, r *http.Request) {
	action := strings.ToLower(r.PathValue("action"))
	if action != "analyze" && action != "vacuum" {
		BadRequest(w, "action must be analyze or vacuum")
		return
	}
	var body struct {
		Table string `json:"table"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Table == "" {
		BadRequest(w, "table is required")
		return
	}
	// 表名白名单校验：仅允许 public schema 的常规表
	var exists bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=$1 AND table_type='BASE TABLE')`,
		body.Table).Scan(&exists); err != nil || !exists {
		BadRequest(w, "unknown table: "+body.Table)
		return
	}
	stmt := "ANALYZE " + quoteIdent(body.Table)
	if action == "vacuum" {
		stmt = "VACUUM " + quoteIdent(body.Table)
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(), stmt); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, action+" failed")
		return
	}
	OK(w, map[string]interface{}{"status": "completed", "action": action, "table": body.Table})
}

func quoteIdent(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, `""`) + `"`
}

// ── Redis 管理（单实例真实操作）──

func (h *AdminHandler) redisDo(ctx context.Context, args ...interface{}) (interface{}, error) {
	if db.Redis == nil {
		return nil, fmt.Errorf("redis unavailable")
	}
	cmd := db.Redis.Do(ctx, args...)
	if cmd.Err() != nil {
		return nil, cmd.Err()
	}
	return cmd.Result()
}

func (h *AdminHandler) RedisSlowLog(w http.ResponseWriter, r *http.Request) {
	res, err := h.redisDo(r.Context(), "SLOWLOG", "GET", 20)
	if err != nil {
		OK(w, map[string]interface{}{"slow_log": []interface{}{}, "error": "redis error"})
		return
	}
	OK(w, map[string]interface{}{"slow_log": normalizeSlowLog(res)})
}

// normalizeSlowLog 把 SLOWLOG GET 的结果规范成**具名字段**。
//
// 为什么需要这一层：Redis 的返回形状随协议与版本而变 ——
//   - RESP2（或 Redis < 7）：每条是**位置数组** `[id, timestamp, duration_us, [args…], client, name]`；
//   - RESP3 / Redis ≥ 7：每条是**映射** `{id, timestamp, duration, command, client-addr, client-name}`。
//
// 而前端 `RedisManagementView.vue` 的表格列是 `Object.keys(第一条)` 生成的 ⇒ 直接丢位置数组过去，
// 表头会变成 `0/1/2/…`（有数据、没列名）。前端已按
// `id / timestamp / duration_us / command / args / client / name` 备好列名与**微秒**单位
// （`SLOW_LABELS` + `common.elapsed_s` = 「耗时 (μs)」）⇒ 这里统一成那套键。
//
// 纯函数（喂 map / 喂数组都能测），与 DB 无关。
func normalizeSlowLog(raw interface{}) []map[string]interface{} {
	items, ok := raw.([]interface{})
	if !ok {
		return []map[string]interface{}{}
	}
	out := make([]map[string]interface{}, 0, len(items))
	for _, it := range items {
		var entry map[string]interface{}
		switch v := it.(type) {
		case []interface{}: // RESP2 / Redis < 7
			if len(v) < 4 {
				continue
			}
			cmd, args := splitSlowLogCommand(v[3])
			entry = map[string]interface{}{
				"id":          asInt64(v[0]),
				"timestamp":   asInt64(v[1]),
				"duration_us": asInt64(v[2]), // SLOWLOG 的耗时字段本身就是微秒
				"command":     cmd,
				"args":        args,
			}
			if len(v) >= 5 {
				entry["client"] = asString(v[4])
			}
			if len(v) >= 6 {
				entry["name"] = asString(v[5])
			}
		default: // RESP3 / Redis ≥ 7：映射
			m := slowLogStringMap(v)
			if len(m) == 0 {
				continue
			}
			cmd, args := splitSlowLogCommand(m["command"])
			entry = map[string]interface{}{
				"id":          asInt64(m["id"]),
				"timestamp":   asInt64(m["timestamp"]),
				"duration_us": asInt64(m["duration"]),
				"command":     cmd,
				"args":        args,
			}
			if c, ok := m["client-addr"]; ok {
				entry["client"] = asString(c)
			}
			if n, ok := m["client-name"]; ok {
				entry["name"] = asString(n)
			}
		}
		if entry != nil {
			out = append(out, entry)
		}
	}
	return out
}

// splitSlowLogCommand 把「参数数组」或「命令行字符串」统一拆成 (命令, 其余参数)。
func splitSlowLogCommand(v interface{}) (string, string) {
	switch t := v.(type) {
	case []interface{}:
		parts := make([]string, 0, len(t))
		for _, p := range t {
			parts = append(parts, asString(p))
		}
		if len(parts) == 0 {
			return "", ""
		}
		return parts[0], strings.Join(parts[1:], " ")
	default:
		fields := strings.Fields(asString(v))
		if len(fields) == 0 {
			return "", ""
		}
		return fields[0], strings.Join(fields[1:], " ")
	}
}

// slowLogStringMap 兼容 go-redis 在两种协议下给出的映射类型（键可能是 interface{}）。
//
// ⚠ 不能叫 `toStringMap`：`market_user.go:261` 已有同名函数（签名是 map[string]interface{} → map[string]string，
// 用途完全不同）—— 第一版就这么撞了，包内重名直接编译失败。
func slowLogStringMap(v interface{}) map[string]interface{} {
	switch t := v.(type) {
	case map[string]interface{}:
		return t
	case map[interface{}]interface{}:
		out := make(map[string]interface{}, len(t))
		for k, val := range t {
			out[fmt.Sprint(k)] = val
		}
		return out
	default:
		return nil
	}
}

func asString(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case []byte:
		return string(t)
	default:
		return fmt.Sprint(t)
	}
}

func asInt64(v interface{}) int64 {
	switch t := v.(type) {
	case nil:
		return 0
	case int64:
		return t
	case int:
		return int64(t)
	case uint64:
		return int64(t)
	case float64:
		return int64(t)
	case string:
		n, _ := strconv.ParseInt(strings.TrimSpace(t), 10, 64)
		return n
	case []byte:
		n, _ := strconv.ParseInt(strings.TrimSpace(string(t)), 10, 64)
		return n
	default:
		return 0
	}
}

func (h *AdminHandler) RedisFlushAll(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Confirm bool `json:"confirm"`
	}
	_ = DecodeJSON(w, r, &body)
	if !body.Confirm {
		BadRequest(w, "confirm=true is required to flush redis")
		return
	}
	if db.Redis == nil {
		logAndRespond(w, fmt.Errorf("redis unavailable"), http.StatusInternalServerError, "flush failed")
		return
	}
	// Cluster 下 FLUSHDB 只作用于被路由到的单个节点：必须逐 master 执行，
	// 否则其余节点数据残留（单机/哨兵只有一个 master，仅执行一次）。
	masters := 0
	if err := db.Redis.ForEachMaster(r.Context(), func(ctx context.Context, node db.RedisClient) error {
		if _, err := node.Do(ctx, "FLUSHDB").Result(); err != nil {
			return err
		}
		masters++
		return nil
	}); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "flush failed")
		return
	}
	slog.Info("redis flushed", "masters", masters)
	OK(w, map[string]interface{}{"status": "flushed", "masters": masters})
}

// ── 模型注册表 ──

type modelRow struct {
	ID            string    `json:"id"`
	Provider      string    `json:"provider"`
	Name          string    `json:"name"`
	DisplayName   string    `json:"display_name"`
	Enabled       bool      `json:"enabled"`
	ContextWindow int       `json:"context_window"`
	CreatedAt     time.Time `json:"created_at"`
}

func (h *AdminHandler) ListModels(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, provider, name, display_name, enabled, context_window, created_at FROM llm_models ORDER BY provider, name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list models failed")
		return
	}
	defer rows.Close()
	out := []modelRow{}
	for rows.Next() {
		var m modelRow
		if rows.Scan(&m.ID, &m.Provider, &m.Name, &m.DisplayName, &m.Enabled, &m.ContextWindow, &m.CreatedAt) == nil {
			out = append(out, m)
		}
	}
	OK(w, map[string]interface{}{"models": out, "total": len(out)})
}

func (h *AdminHandler) CreateModel(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider      string `json:"provider"`
		Name          string `json:"name"`
		DisplayName   string `json:"display_name"`
		Enabled       bool   `json:"enabled"`
		ContextWindow int    `json:"context_window"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Provider == "" || body.Name == "" {
		BadRequest(w, "provider and name are required")
		return
	}
	if body.ContextWindow <= 0 {
		body.ContextWindow = 8192
	}
	id, _ := id.UUID()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window)
		 VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (provider, name) DO UPDATE
		 SET display_name = EXCLUDED.display_name, enabled = EXCLUDED.enabled, context_window = EXCLUDED.context_window, updated_at = NOW()`,
		id, body.Provider, body.Name, body.DisplayName, body.Enabled, body.ContextWindow); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create model failed")
		return
	}
	OK(w, map[string]interface{}{"id": id, "provider": body.Provider, "name": body.Name})
}

func (h *AdminHandler) UpdateModel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		DisplayName   *string `json:"display_name"`
		Enabled       *bool   `json:"enabled"`
		ContextWindow *int    `json:"context_window"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// S 安全修复：列名白名单，防止 SQL 注入
	modelColumnMap := map[string]string{
		"display_name":   "display_name",
		"enabled":        "enabled",
		"context_window": "context_window",
	}
	sets, args := []string{}, []interface{}{}
	idx := 1
	if body.DisplayName != nil {
		if col, ok := modelColumnMap["display_name"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.DisplayName)
			idx++
		}
	}
	if body.Enabled != nil {
		if col, ok := modelColumnMap["enabled"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.Enabled)
			idx++
		}
	}
	if body.ContextWindow != nil {
		if col, ok := modelColumnMap["context_window"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.ContextWindow)
			idx++
		}
	}
	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	args = append(args, id)
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE llm_models SET %s, updated_at = NOW() WHERE id = $%d", strings.Join(sets, ", "), idx), args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update model failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

// maxBatchModelIDs 单次批量的 id 上限。存在的意义是防呆而非性能：这个端点是给
// "整组开关"用的，一次上千个 id 只可能来自前端选择逻辑出错或脚本滥用，
// 而 `unnest($2::text[])` 的参数量也不该无界。
const maxBatchModelIDs = 500

// normalizeBatchModelIDs 归一化批量模型 id：去空白、丢空串、去重。
//
// 去重是必须的：两级分组的"跨组全选"很容易把同一个 id 选两次（同一模型既在
// provider 组又在子组里），重复不影响 UPDATE 的结果，但会让返回的 updated
// 计数虚高 —— 前端拿它提示就会显示"已更新 12 个"而实际只有 10 个。
func normalizeBatchModelIDs(raw []string) ([]string, error) {
	seen := make(map[string]struct{}, len(raw))
	out := make([]string, 0, len(raw))
	for _, id := range raw {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		out = append(out, id)
	}
	if len(out) == 0 {
		return nil, errors.New("ids is empty")
	}
	if len(out) > maxBatchModelIDs {
		return nil, fmt.Errorf("too many ids (max %d)", maxBatchModelIDs)
	}
	return out, nil
}

// BatchUpdateModels 批量启用/禁用：`PUT /admin/models/batch`。
//
// 为什么需要它：在 /models 页把一个 provider 下的几十个模型一起关掉，逐条打
// `PUT /models/{id}` 会发出同样数量的请求，且中途失败会留下"一半开一半关"的
// 中间态、无从回滚。这里一条 `UPDATE ... WHERE id IN (...)` 原子完成。
func (h *AdminHandler) BatchUpdateModels(w http.ResponseWriter, r *http.Request) {
	var body struct {
		IDs []string `json:"ids"`
		// 用指针区分"没传 enabled"与"传了 false"——后者是合法且最常用的请求
		Enabled *bool `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Enabled == nil {
		BadRequest(w, "ids and enabled are required")
		return
	}
	ids, err := normalizeBatchModelIDs(body.IDs)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	// 参数写成 `$2::text[]`：`llm_models.id` 是 `character varying(36)`（见 0001 基线建表），
	// **不是** uuid 类型。真库用例 TestLiveBatchUpdateModels 在最初写成
	// `unnest($2::text[])::uuid` 时直接报 `operator does not exist: character varying = uuid`。
	// id 里存的就是 UUID 字符串，按文本数组比较即可，且 `= ANY(...)` 仍走主键索引。
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE llm_models SET enabled = $1, updated_at = NOW() WHERE id = ANY($2::text[])`,
		*body.Enabled, ids)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "batch update models failed")
		return
	}
	OK(w, map[string]interface{}{"updated": tag.RowsAffected(), "enabled": *body.Enabled})
}

func (h *AdminHandler) DeleteModel(w http.ResponseWriter, r *http.Request) {
	if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM llm_models WHERE id = $1`, r.PathValue("id")); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete model failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ListUserModels 用户侧可用模型（仅 enabled）：GET /v1/models
// 动态发现实现见 model_discovery.go（按已配置 provider 的 keyset 实时拉取并缓存到 llm_models）。
func ListUserModels(w http.ResponseWriter, r *http.Request) {
	ListModelsForUser(w, r)
}

// ── 定时任务 ──

type cronRow struct {
	ID           string     `json:"id"`
	Name         string     `json:"name"`
	Schedule     string     `json:"schedule"`
	Task         string     `json:"task"`
	Enabled      bool       `json:"enabled"`
	LastRunAt    *time.Time `json:"last_run_at"`
	LastStatus   string     `json:"last_status"`
	WebhookToken string     `json:"webhook_token"`
	CreatedAt    time.Time  `json:"created_at"`
}

func (h *AdminHandler) ListCronJobs(w http.ResponseWriter, r *http.Request) {
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, "database not available")
		return
	}
	rows, err := pool.Query(r.Context(),
		`SELECT id::text, name, schedule, task, enabled, last_run_at, last_status, webhook_token, created_at FROM cron_jobs ORDER BY created_at DESC`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list cron jobs failed")
		return
	}
	defer rows.Close()
	out := []cronRow{}
	for rows.Next() {
		var c cronRow
		if rows.Scan(&c.ID, &c.Name, &c.Schedule, &c.Task, &c.Enabled, &c.LastRunAt, &c.LastStatus, &c.WebhookToken, &c.CreatedAt) == nil {
			out = append(out, c)
		}
	}
	OK(w, map[string]interface{}{"jobs": out, "total": len(out)})
}

func (h *AdminHandler) CreateCronJob(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name     string `json:"name"`
		Schedule string `json:"schedule"`
		Task     string `json:"task"`
		Enabled  bool   `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Name == "" || body.Schedule == "" || body.Task == "" {
		BadRequest(w, "name, schedule and task are required")
		return
	}
	jobID, _ := id.UUID()
	token, _ := id.UUID()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO cron_jobs (id, name, schedule, task, enabled, webhook_token) VALUES ($1, $2, $3, $4, $5, $6)`,
		jobID, body.Name, body.Schedule, body.Task, body.Enabled, token); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create cron job failed")
		return
	}
	OK(w, map[string]interface{}{"id": jobID, "name": body.Name, "schedule": body.Schedule, "webhook_token": token})
}

func (h *AdminHandler) UpdateCronJob(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		Name     *string `json:"name"`
		Schedule *string `json:"schedule"`
		Task     *string `json:"task"`
		Enabled  *bool   `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// S 安全修复：列名白名单，防止 SQL 注入
	cronColumnMap := map[string]string{
		"name":     "name",
		"schedule": "schedule",
		"task":     "task",
		"enabled":  "enabled",
	}
	sets, args := []string{}, []interface{}{}
	idx := 1
	apply := func(col string, v *string) {
		if v != nil {
			if _, ok := cronColumnMap[col]; !ok {
				return
			}
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *v)
			idx++
		}
	}
	apply("name", body.Name)
	apply("schedule", body.Schedule)
	apply("task", body.Task)
	if body.Enabled != nil {
		if _, ok := cronColumnMap["enabled"]; ok {
			sets = append(sets, fmt.Sprintf("enabled = $%d", idx))
			args = append(args, *body.Enabled)
			idx++
		}
	}
	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	args = append(args, id)
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE cron_jobs SET %s, updated_at = NOW() WHERE id = $%d", strings.Join(sets, ", "), idx), args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update cron job failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteCronJob(w http.ResponseWriter, r *http.Request) {
	if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM cron_jobs WHERE id = $1`, r.PathValue("id")); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete cron job failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}
