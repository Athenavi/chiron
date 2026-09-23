package api

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
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
	r.HandleFunc("GET /database/backups", h.DatabaseBackups)
	r.HandleFunc("POST /database/backups", h.CreateDatabaseBackup)
	r.HandleFunc("POST /database/backups/{backupId}/restore", h.RestoreDatabaseBackup)
	r.HandleFunc("GET /database/status", h.DatabaseStatus)
	r.HandleFunc("POST /database/query", h.DatabaseQuery)
	r.HandleFunc("POST /database/optimize/{action}", h.DatabaseOptimize)

	// Redis 管理（单实例真实操作）
	r.HandleFunc("GET /redis/slow-log", h.RedisSlowLog)
	r.HandleFunc("POST /redis/flush-all", h.RedisFlushAll)

	// 模型注册表
	r.HandleFunc("GET /models", h.ListModels)
	r.HandleFunc("POST /models", h.CreateModel)
	r.HandleFunc("PUT /models/{id}", h.UpdateModel)
	r.HandleFunc("DELETE /models/{id}", h.DeleteModel)

	// 定时任务（DB 持久化；执行由调度器接入）
	r.HandleFunc("GET /cron-jobs", h.ListCronJobs)
	r.HandleFunc("POST /cron-jobs", h.CreateCronJob)
	r.HandleFunc("PUT /cron-jobs/{id}", h.UpdateCronJob)
	r.HandleFunc("DELETE /cron-jobs/{id}", h.DeleteCronJob)
	r.HandleFunc("POST /cron-jobs/{id}/trigger", h.HandleCronTrigger)
}

// ── 租户管理 ──

type tenantRow struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Status    string    `json:"status"`
	CreatedAt time.Time `json:"created_at"`
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
	if tenantID == "" {
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
	if tenantID != "" {
		// 多租户模式：校验域名归属
		var currentTenantID string
		if err := db.GlobalDBManager.QueryRow(r.Context(),
			`SELECT tenant_id FROM domains WHERE id = $1`, id).Scan(&currentTenantID); err != nil {
			NotFound(w, "domain not found")
			return
		}
		if currentTenantID != tenantID {
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

func backupDir() string {
	d := os.Getenv("BACKUP_DIR")
	if d == "" {
		d = os.Getenv("STORAGE_ROOT")
		if d == "" {
			d = config.LoadAllowUnconfigured().StorageRoot
		}
		if d != "" {
			d = filepath.Join(d, "backups")
			return d
		}
	}
	if d == "" {
		d = filepath.Join(config.GetDefaultDataDir(), "backups")
	}
	return d
}

func (h *AdminHandler) DatabaseBackups(w http.ResponseWriter, r *http.Request) {
	dir := backupDir()
	entries, err := os.ReadDir(dir)
	if err != nil {
		OK(w, map[string]interface{}{"backups": []interface{}{}, "total": 0})
		return
	}
	type backupInfo struct {
		Name string    `json:"name"`
		Size int64     `json:"size"`
		Time time.Time `json:"time"`
	}
	out := []backupInfo{}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".sql") {
			continue
		}
		info, _ := e.Info()
		out = append(out, backupInfo{Name: e.Name(), Size: info.Size(), Time: info.ModTime()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Time.After(out[j].Time) })
	OK(w, map[string]interface{}{"backups": out, "total": len(out)})
}

func (h *AdminHandler) CreateDatabaseBackup(w http.ResponseWriter, r *http.Request) {
	// 复用 CreateBackup 的 pg_dump 能力，落盘到备份目录
	if err := os.MkdirAll(backupDir(), 0o755); err != nil {
		InternalError(w, "backup dir create failed")
		return
	}
	name := fmt.Sprintf("chiron_backup_%s.sql", time.Now().Format("20060102_150405"))
	target := filepath.Join(backupDir(), name)
	if err := runPGDump(r.Context(), target); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "backup failed")
		return
	}
	OK(w, map[string]interface{}{"name": name, "status": "completed"})
}

func (h *AdminHandler) RestoreDatabaseBackup(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("backupId")
	if strings.Contains(name, "/") || strings.Contains(name, "..") {
		BadRequest(w, "invalid backup name")
		return
	}
	target := filepath.Join(backupDir(), name)
	if _, err := os.Stat(target); err != nil {
		NotFound(w, "backup not found")
		return
	}
	dsn := extractDSN()
	if dsn == "" {
		InternalError(w, "POSTGRES_DSN not configured")
		return
	}
	// P0 安全修复：密码通过 PGPASSWORD 环境变量传递，避免出现在命令行参数中
	host, port, user, dbname, password := parseDSNComponents(dsn)
	cmd := exec.CommandContext(r.Context(), "psql",
		"--host", host,
		"--port", port,
		"--username", user,
		"--dbname", dbname,
		"-f", target,
		"-v", "ON_ERROR_STOP=1",
	)
	cmd.Env = os.Environ()
	cmd.Env = append(cmd.Env, "PGPASSWORD="+password)
	output, err := cmd.CombinedOutput()
	if err != nil {
		slog.Error("restore failed", "error", err, "output", string(output))
		InternalError(w, "restore failed")
		return
	}
	OK(w, map[string]interface{}{"status": "restored", "backup": name})
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
	OK(w, map[string]interface{}{"slow_log": res})
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

// ── 工具 ──

// runPGDump 流式落盘 pg_dump（复用 extractDSN）。参数拆分传入防止注入。
// P0 安全修复：密码通过 PGPASSWORD 环境变量传递，避免出现在命令行参数中。
// 使用 stdout pipe 流式写入文件，避免整库缓冲入内存导致 OOM。
func runPGDump(ctx context.Context, target string) error {
	dsn := extractDSN()
	if dsn == "" {
		return fmt.Errorf("POSTGRES_DSN not configured")
	}
	host, port, user, dbname, password := parseDSNComponents(dsn)
	cmd := exec.CommandContext(ctx, "pg_dump",
		"--host", host,
		"--port", port,
		"--username", user,
		"--dbname", dbname,
	)
	cmd.Env = os.Environ()
	cmd.Env = append(cmd.Env, "PGPASSWORD="+password)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fmt.Errorf("stdout pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("pg_dump start: %w", err)
	}
	outFile, err := os.Create(target)
	if err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		return fmt.Errorf("create target file: %w", err)
	}
	if _, err := io.Copy(outFile, stdout); err != nil {
		outFile.Close()
		cmd.Process.Kill()
		cmd.Wait()
		return fmt.Errorf("write dump: %w", err)
	}
	outFile.Close()
	if err := cmd.Wait(); err != nil {
		return fmt.Errorf("pg_dump failed: %w", err)
	}
	return nil
}
