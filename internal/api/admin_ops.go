package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
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

	// 定时任务管理面已下线（/admin/cron 页面移除）；调度器与 Webhook 触发保持运行。

	// 租户邀请码（F2）：注册时据邀请码决定新用户归属哪个租户
	r.HandleFunc("GET /invites", h.ListInvites)
	r.HandleFunc("POST /invites", h.CreateInvite)
	r.HandleFunc("DELETE /invites/{code}", h.DeleteInvite)
}

// ── 租户管理 ──

type tenantRow struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Status  string `json:"status"`
	// created_at 在基线里没有默认值，存量行可能是 NULL；用指针接收，
	// 让扫描不因 NULL 失败 —— 否则整行会被下面的循环静默跳过，列表恒为空。
	CreatedAt *time.Time `json:"created_at"`
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
	// COALESCE 兜住存量的 NULL：name/status 若为 NULL，Scan 到 string 会报错，
	// 而下面的循环对扫描失败是"跳过这一行"——列表就会莫名其妙地缺行。
	// created_at 用 DESC NULLS LAST：没有时间戳的行排在最后，而不是顶掉有时间的。
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, COALESCE(name, ''), COALESCE(status, 'active'), created_at
		   FROM tenants ORDER BY created_at DESC NULLS LAST, id ASC`)
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
	var tenantIDStr string
	// tenants.id 是 smallserial（基线 0001:735），必须让数据库自增。
	// 这里原先是 `id.UUID()` 生成的字符串，传给 smallint 会直接
	// "invalid input syntax for type smallint" —— 也就是说该端点此前从未成功过。
	// 改用 RETURNING id 取真实主键，后面的角色播种才有正确的 int16。
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO tenants (name, status, created_at) VALUES ($1, 'active', NOW()) RETURNING id::text`,
		strings.TrimSpace(body.Name)).Scan(&tenantIDStr); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create tenant failed")
		return
	}
	// 立刻播种 owner/admin/member：tenant_members.role_id 是外键，租户没有角色
	// 时后续任何加成员的操作都无从谈起。失败只告警 —— 注册与自动建号路径里
	// 也会幂等补一次（ensureTenantBuiltinRoles），不让建租户因此失败。
	if err := ensureTenantBuiltinRoles(r.Context(), db.GlobalDBManager, parseTenantID(tenantIDStr)); err != nil {
		slog.Warn("seed tenant builtin roles failed", "tenant", tenantIDStr, "error", err)
	}
	OK(w, map[string]interface{}{"id": tenantIDStr, "name": body.Name, "status": "active"})
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
	// 不写 updated_at：tenants 表没有这一列（基线只有 id/name/created_at/status）。
	// 之前的 `SET ..., updated_at = NOW()` 让「编辑租户」与「暂停」恒 500。
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE tenants SET %s WHERE id = $%d", strings.Join(sets, ", "), idx), args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update tenant failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	// 平台级端点（routes_admin.go 用 platformWriteMW 挂载），但平台 owner 也不该能把
	// 自己所在的默认租户整个删掉 —— 那是所有存量用户与会话的归属，删完平台即不可用。
	if id == "" || parseTenantID(id) == DefaultTenantID {
		BadRequest(w, "cannot delete the default tenant")
		return
	}
	ctx := r.Context()
	// 有用户就不许删：会话/消息/审计分散在十余张表，删租户必须是"清空之后"的显式动作，
	// 不能让一次点击级联掉整租户的数据。
	var users int
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*) FROM users WHERE tenant_id = $1`, id).Scan(&users); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "check tenant users failed")
		return
	}
	if users > 0 {
		BadRequest(w, fmt.Sprintf("tenant still has %d user(s); remove them first", users))
		return
	}
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "begin transaction failed")
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	// 租户级**配置**数据（角色/群组/域名/各类集成配置/配额池/上传与邀请）。
	// 顺序按外键依赖：先删引用方，再删被引用方。之前直接 DELETE tenants 会撞
	// ent_roles_tenant_id_fkey —— 新建的空租户都删不掉。
	cleanup := []string{
		`DELETE FROM ent_user_roles WHERE role_id IN (SELECT id FROM ent_roles WHERE tenant_id = $1)`,
		`DELETE FROM ent_group_roles WHERE group_id IN (SELECT id FROM ent_groups WHERE tenant_id = $1)`,
		`DELETE FROM ent_group_members WHERE group_id IN (SELECT id FROM ent_groups WHERE tenant_id = $1)`,
		`DELETE FROM ent_groups WHERE tenant_id = $1`,
		`DELETE FROM ent_roles WHERE tenant_id = $1`,
		`DELETE FROM ent_quota_allocations WHERE pool_id IN (SELECT id FROM ent_quota_pools WHERE tenant_id = $1)`,
		`DELETE FROM ent_quota_pools WHERE tenant_id = $1`,
		`DELETE FROM tenant_members WHERE tenant_id = $1`,
		`DELETE FROM tenant_invites WHERE tenant_id = $1`,
		`DELETE FROM domains WHERE tenant_id = $1`,
		`DELETE FROM ent_model_policies WHERE tenant_id = $1`,
		`DELETE FROM ent_tenant_policies WHERE tenant_id = $1`,
		`DELETE FROM ent_model_routes WHERE tenant_id = $1`,
		`DELETE FROM ent_oidc_providers WHERE tenant_id = $1`,
		`DELETE FROM ent_webhooks WHERE tenant_id = $1`,
		`DELETE FROM ent_captcha_config WHERE tenant_id = $1`,
		`DELETE FROM ent_mail_config WHERE tenant_id = $1`,
		`DELETE FROM ent_sms_config WHERE tenant_id = $1`,
		`DELETE FROM ent_catalog_installs WHERE tenant_id = $1`,
		`DELETE FROM cron_jobs WHERE tenant_id = $1`,
		`DELETE FROM uploads WHERE tenant_id = $1`,
		`DELETE FROM user_knowledge_settings WHERE tenant_id = $1`,
	}
	for _, q := range cleanup {
		if _, err := tx.Exec(ctx, q, id); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "cleanup tenant data failed")
			return
		}
	}
	tag, err := tx.Exec(ctx, `DELETE FROM tenants WHERE id = $1`, id)
	if err != nil {
		// 仍有外键引用（会话/知识库/审计…）⇒ 409：告诉调用方先清理，而不是 500。
		logAndRespond(w, err, http.StatusConflict, "tenant still has dependent data; clean it up first")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "tenant not found")
		return
	}
	if err := tx.Commit(ctx); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "commit transaction failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

func (h *AdminHandler) SuspendTenant(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	// 与 DeleteTenant 同口径：默认租户承载全部存量用户与会话，暂停它等于全平台停服，
	// 而这条路由只有平台 owner 能到 —— 没有"外部租户管理员来暂停"的正当场景。
	if id == "" || parseTenantID(id) == DefaultTenantID {
		BadRequest(w, "cannot suspend the default tenant")
		return
	}
	res, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE tenants SET status = 'suspended' WHERE id = $1`, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "suspend tenant failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "tenant not found")
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
	ID       string `json:"id"`
	TenantID int16  `json:"tenant_id"`
	// TenantName 便于平台直接在域名列表里看出归属；租户侧为空也可接受。
	TenantName string `json:"tenant_name,omitempty"`
	Domain     string `json:"domain"`
	SSLStatus  string `json:"ssl_status"`
	Verified   bool   `json:"verified"`
	// 同 tenantRow：SQL NULL 不能让整行被跳过。
	CreatedAt *time.Time `json:"created_at"`
}

var validDomainRe = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$`)

// normalizeDomain 归一化用户输入：去协议 / 路径 / 端口 / 尾点，统一小写。
// 域名比较必须忽略大小写与输入形态，否则 `https://Foo.com/` 与 `foo.com` 会被
// 当成两个域名，唯一约束形同虚设。
func normalizeDomain(raw string) string {
	d := strings.TrimSpace(strings.ToLower(raw))
	d = strings.TrimPrefix(d, "https://")
	d = strings.TrimPrefix(d, "http://")
	if i := strings.IndexAny(d, "/?#"); i >= 0 {
		d = d[:i]
	}
	if host, _, ok := strings.Cut(d, ":"); ok {
		d = host
	}
	return strings.TrimSuffix(d, ".")
}

// domainConflict 统一域名绑定冲突响应：前端据此展示具体原因。
func domainConflict(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusConflict, APIResponse{Success: false, Code: "domain_conflict", Error: msg})
}

// ── 域名的租户级语句 ──
//
// `domains` 决定**登录入口**：sso_handler.go 的 resolveTenantByHost 用
// `lower(domain) = Host AND verified` 反查 tenant_id，再据此决定登录页展示哪个
// 租户的 IdP。于是这条表上的任何跨租户读写都不只是"数据可见性"问题：
//
//   - 列表不带谓词 ⇒ 租户 admin 能看到全平台域名（等于拿到别家租户的入口清单）；
//   - 更新/删除/验证/续证不带谓词 ⇒ 改掉别人的域名后其 `verified=false`，
//     该租户的 SSO 入口立刻失效；反过来把别人的域名改成自己的，就劫持了它的
//     登录流量（等用户下次点 SSO 时被导向自己的 IdP）。
//
// CreateDomain 早已写入 GetTenantID(r)，说明这些端点的语义本来就是租户级；
// 下面六条语句因此统一带 `tenant_id = $n`，并把语句提为常量供单测断言形状。
const (
	domainCountQuery = `SELECT COUNT(*) FROM domains WHERE tenant_id = $1`
	domainListQuery  = `SELECT d.id::text, d.tenant_id, COALESCE(t.name, ''), COALESCE(d.domain, ''), COALESCE(d.ssl_status, 'none'), COALESCE(d.verified, false), d.created_at
		   FROM domains d LEFT JOIN tenants t ON t.id = d.tenant_id
		  WHERE d.tenant_id = $1 ORDER BY d.created_at DESC NULLS LAST, d.domain ASC LIMIT $2 OFFSET $3`
	// 平台 owner：可跨租户查看（可选 ?tenant_id= 收窄）。这两条不带租户谓词，
	// 仅由 ListDomains 在 `isPlatformOwner` 为真时使用。
	domainCountAllQuery = `SELECT COUNT(*) FROM domains`
	domainListAllQuery  = `SELECT d.id::text, d.tenant_id, COALESCE(t.name, ''), COALESCE(d.domain, ''), COALESCE(d.ssl_status, 'none'), COALESCE(d.verified, false), d.created_at
		   FROM domains d LEFT JOIN tenants t ON t.id = d.tenant_id
		  ORDER BY d.created_at DESC NULLS LAST, d.domain ASC LIMIT $1 OFFSET $2`
	domainCountAllByTenantQuery = `SELECT COUNT(*) FROM domains WHERE tenant_id = $1`
	domainListAllByTenantQuery  = `SELECT d.id::text, d.tenant_id, COALESCE(t.name, ''), COALESCE(d.domain, ''), COALESCE(d.ssl_status, 'none'), COALESCE(d.verified, false), d.created_at
		   FROM domains d LEFT JOIN tenants t ON t.id = d.tenant_id
		  WHERE d.tenant_id = $1 ORDER BY d.created_at DESC NULLS LAST, d.domain ASC LIMIT $2 OFFSET $3`
	// 改域名会重置 verified/ssl_status，因此必须限定归属，否则等于替别人下线入口。
	domainUpdateQuery = `UPDATE domains SET domain = $1, verified = false, ssl_status = 'none', updated_at = NOW()
		  WHERE id = $2 AND tenant_id = $3`
	domainDeleteQuery       = `DELETE FROM domains WHERE id = $1 AND tenant_id = $2`
	domainLoadQuery         = `SELECT domain FROM domains WHERE id = $1 AND tenant_id = $2`
	domainSetVerifiedQuery  = `UPDATE domains SET verified = $1, updated_at = NOW() WHERE id = $2 AND tenant_id = $3`
	domainLoadVerifiedQuery = `SELECT verified FROM domains WHERE id = $1 AND tenant_id = $2`
	domainSetSSLQuery       = `UPDATE domains SET ssl_status = 'active', updated_at = NOW() WHERE id = $1 AND tenant_id = $2`
)

func (h *AdminHandler) ListDomains(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	owner := isPlatformOwner(claims)
	tenantID := GetTenantID(r)
	if !owner && tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
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

	// 平台 owner 默认看全部（可用 ?tenant_id= 收窄）；租户 admin 只能看自己。
	filterTenant := int16(0)
	if owner {
		if t := strings.TrimSpace(r.URL.Query().Get("tenant_id")); t != "" {
			var v int
			if _, err := fmt.Sscanf(t, "%d", &v); err == nil && v > 0 {
				filterTenant = int16(v)
			}
		}
	}

	var (
		total int
		rows  pgx.Rows
		err   error
	)
	switch {
	case !owner:
		_ = db.GlobalDBManager.QueryRow(r.Context(), domainCountQuery, tenantID).Scan(&total)
		rows, err = db.GlobalDBManager.Query(r.Context(), domainListQuery, tenantID, perPage, offset)
	case filterTenant > 0:
		_ = db.GlobalDBManager.QueryRow(r.Context(), domainCountAllByTenantQuery, filterTenant).Scan(&total)
		rows, err = db.GlobalDBManager.Query(r.Context(), domainListAllByTenantQuery, filterTenant, perPage, offset)
	default:
		_ = db.GlobalDBManager.QueryRow(r.Context(), domainCountAllQuery).Scan(&total)
		rows, err = db.GlobalDBManager.Query(r.Context(), domainListAllQuery, perPage, offset)
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list domains failed")
		return
	}
	defer rows.Close()
	out := []domainRow{}
	for rows.Next() {
		var d domainRow
		if err := rows.Scan(&d.ID, &d.TenantID, &d.TenantName, &d.Domain, &d.SSLStatus, &d.Verified, &d.CreatedAt); err == nil {
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
		// TenantID 仅平台 owner 可指定（为某个租户绑定）；租户 admin 传了也忽略。
		TenantID int16 `json:"tenant_id"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	domain := normalizeDomain(body.Domain)
	if domain == "" || !validDomainRe.MatchString(domain) {
		BadRequest(w, "valid domain is required")
		return
	}
	claims := auth.GetClaims(r.Context())
	owner := isPlatformOwner(claims)
	tenantID := GetTenantID(r)
	if owner && body.TenantID > 0 {
		tenantID = body.TenantID
	}
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}

	// 一对一约束：一个域名只属于一个租户，一个租户只有一个域名。
	// 前置校验给出可读原因；唯一索引（迁移 0008）兜住并发窗口。
	var boundTenant int16
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT tenant_id FROM domains
		  WHERE lower(domain) = lower($1) AND domain IS NOT NULL AND domain <> '' LIMIT 1`,
		domain).Scan(&boundTenant); err == nil {
		if boundTenant == tenantID {
			domainConflict(w, "该域名已绑定到本租户")
		} else {
			domainConflict(w, "该域名已被其他租户绑定")
		}
		return
	}
	var hasDomain bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM domains WHERE tenant_id = $1 AND domain IS NOT NULL AND domain <> '')`,
		tenantID).Scan(&hasDomain); err == nil && hasDomain {
		domainConflict(w, "每个租户只能绑定一个域名；请先解绑现有域名")
		return
	}

	// id / 时间戳显式写入：基线表的这两列没有默认值（迁移 0003 才补上），
	// 漏写会直接撞 `null value in column "id"` 的非空约束。
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO domains (id, tenant_id, domain, ssl_status, verified, created_at, updated_at)
		 VALUES ($1, $2, $3, 'none', false, NOW(), NOW())`,
		newUUID(), tenantID, domain); err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			domainConflict(w, "域名或租户已有绑定，请刷新后重试")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create domain failed")
		return
	}
	OK(w, map[string]interface{}{
		"domain": domain, "tenant_id": tenantID, "ssl_status": "none", "verified": false,
	})
}

func (h *AdminHandler) UpdateDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	claims := auth.GetClaims(r.Context())
	owner := isPlatformOwner(claims)
	tenantID := GetTenantID(r)
	if !owner && tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var body struct {
		Domain string `json:"domain"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	domain := normalizeDomain(body.Domain)
	if domain == "" || !validDomainRe.MatchString(domain) {
		BadRequest(w, "valid domain is required")
		return
	}

	// 平台 owner 按 id 管理任意租户；租户 admin 必须带归属谓词。
	var (
		res pgconn.CommandTag
		err error
	)
	if owner {
		res, err = db.GlobalDBManager.Exec(r.Context(),
			`UPDATE domains SET domain = $1, verified = false, ssl_status = 'none', updated_at = NOW() WHERE id = $2`,
			domain, id)
	} else {
		res, err = db.GlobalDBManager.Exec(r.Context(), domainUpdateQuery, domain, id, tenantID)
	}
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "23505" {
			domainConflict(w, "该域名已被其他租户绑定")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update domain failed")
		return
	}
	// 0 行受影响 = 不存在或不属于本租户：统一 404，不泄漏别人的域名是否存在。
	if res.RowsAffected() == 0 {
		NotFound(w, "domain not found")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	claims := auth.GetClaims(r.Context())
	owner := isPlatformOwner(claims)
	tenantID := GetTenantID(r)
	if !owner && tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	// 归属谓词直接写在 DELETE 上：先 SELECT 再 DELETE 的两步写法在并发下仍可被
	// 抢先改归属，而这里一次语句就完成了"是我的才删"。平台 owner 按 id 删任意租户。
	var (
		res pgconn.CommandTag
		err error
	)
	if owner {
		res, err = db.GlobalDBManager.Exec(r.Context(),
			`DELETE FROM domains WHERE id = $1`, id)
	} else {
		res, err = db.GlobalDBManager.Exec(r.Context(), domainDeleteQuery, id, tenantID)
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete domain failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "domain not found")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// VerifyDomain 真实 DNS 校验：解析 A/AAAA 记录确认域名可达。
func (h *AdminHandler) VerifyDomain(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var domain string
	if err := db.GlobalDBManager.QueryRow(r.Context(), domainLoadQuery, id, tenantID).Scan(&domain); err != nil {
		NotFound(w, "domain not found")
		return
	}
	addrs, err := net.LookupHost(domain)
	verified := err == nil && len(addrs) > 0
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		domainSetVerifiedQuery, verified, id, tenantID); err != nil {
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
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var verified bool
	if err := db.GlobalDBManager.QueryRow(r.Context(), domainLoadVerifiedQuery, id, tenantID).Scan(&verified); err != nil {
		NotFound(w, "domain not found")
		return
	}
	if !verified {
		BadRequest(w, "domain must be verified before SSL renewal")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(), domainSetSSLQuery, id, tenantID); err != nil {
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
	// 只读校验统一走 validateReadOnlySQL（与 /v1/internal/db/* 同一套）：
	// 它额外拦 `SELECT ... INTO`、`WITH` 内嵌 DML 的 CTE、以及 `EXPLAIN ANALYZE
	// DELETE ...` 这类"首关键字是只读、实际会写"的形态 —— 本地这四段手工正则
	// 全都放行过（前面那版只校验 select 前缀 + 危险函数 + 分号）。
	if msg := validateReadOnlySQL(body.Query); msg != "" {
		BadRequest(w, msg)
		return
	}
	// 禁止危险函数调用
	if dangerousQueryRe.MatchString(body.Query) {
		BadRequest(w, "query not allowed")
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

// ── 模型注册表 ──

type modelRow struct {
	ID              string    `json:"id"`
	Provider        string    `json:"provider"`
	Name            string    `json:"name"`
	DisplayName     string    `json:"display_name"`
	Enabled         bool      `json:"enabled"`
	ContextWindow   int       `json:"context_window"`
	MaxOutputTokens *int      `json:"max_output_tokens"` // NULL = 未知 → 引擎回落默认值
	CreatedAt       time.Time `json:"created_at"`
}

func (h *AdminHandler) ListModels(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, provider, name, display_name, enabled, context_window, max_output_tokens, created_at FROM llm_models ORDER BY provider, name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list models failed")
		return
	}
	defer rows.Close()
	out := []modelRow{}
	for rows.Next() {
		var m modelRow
		if rows.Scan(&m.ID, &m.Provider, &m.Name, &m.DisplayName, &m.Enabled, &m.ContextWindow, &m.MaxOutputTokens, &m.CreatedAt) == nil {
			out = append(out, m)
		}
	}
	OK(w, map[string]interface{}{"models": out, "total": len(out)})
}

func (h *AdminHandler) CreateModel(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider        string `json:"provider"`
		Name            string `json:"name"`
		DisplayName     string `json:"display_name"`
		Enabled         bool   `json:"enabled"`
		ContextWindow   int    `json:"context_window"`
		MaxOutputTokens *int   `json:"max_output_tokens"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Provider == "" || body.Name == "" {
		BadRequest(w, "provider and name are required")
		return
	}
	if body.ContextWindow <= 0 {
		body.ContextWindow = 8192
	}
	if body.MaxOutputTokens != nil && *body.MaxOutputTokens <= 0 {
		// 0 / 负数在这里落成 NULL（= 未知），而不是写进库让引擎把它当成上限。
		body.MaxOutputTokens = nil
	}
	id, _ := id.UUID()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window, max_output_tokens)
		 VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (provider, name) DO UPDATE
		 SET display_name = EXCLUDED.display_name, enabled = EXCLUDED.enabled,
		     context_window = EXCLUDED.context_window,
		     max_output_tokens = COALESCE(EXCLUDED.max_output_tokens, llm_models.max_output_tokens),
		     updated_at = NOW()`,
		id, body.Provider, body.Name, body.DisplayName, body.Enabled, body.ContextWindow, body.MaxOutputTokens); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create model failed")
		return
	}
	OK(w, map[string]interface{}{"id": id, "provider": body.Provider, "name": body.Name})
}

func (h *AdminHandler) UpdateModel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		DisplayName     *string `json:"display_name"`
		Enabled         *bool   `json:"enabled"`
		ContextWindow   *int    `json:"context_window"`
		MaxOutputTokens *int    `json:"max_output_tokens"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// S 安全修复：列名白名单，防止 SQL 注入
	modelColumnMap := map[string]string{
		"display_name":      "display_name",
		"enabled":           "enabled",
		"context_window":    "context_window",
		"max_output_tokens": "max_output_tokens",
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
	if body.MaxOutputTokens != nil {
		if col, ok := modelColumnMap["max_output_tokens"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			// <=0 显式落成 NULL：管理端要能"改回未知"，否则填错一次就再也回不去。
			if *body.MaxOutputTokens > 0 {
				args = append(args, *body.MaxOutputTokens)
			} else {
				args = append(args, nil)
			}
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
	// 参数写成 `$2::text[]`：`llm_models.id` 是 `character varying(36)`（见权威基线），
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

