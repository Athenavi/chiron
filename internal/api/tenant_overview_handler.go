package api

import (
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// tenant_overview_handler.go —— 租户概览（一个请求聚合首页要看的几个数）。
//
// 所有查询都按 claims.tenant_id 过滤：成员/域名/用量/发票/权益各表都有自己的
// 租户归属或经 users 反查，概览层不再接受任何调用方传入的 tenant_id。

type tenantOverview struct {
	Members            int64 `json:"members"`
	MonthTokens        int64 `json:"month_tokens"`
	MonthCostCents     int64 `json:"month_cost_cents"`
	Domains            int64 `json:"domains"`
	VerifiedDomains    int64 `json:"verified_domains"`
	PendingInvoices    int64 `json:"pending_invoices"`
	ActiveEntitlements int64 `json:"active_entitlements"`
}

// GetTenantOverview GET /v1/tenant/overview
func (h *AdminHandler) GetTenantOverview(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	tenantID := claims.TenantID
	ctx := r.Context()
	var out tenantOverview

	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::bigint FROM tenant_members WHERE tenant_id = $1`, tenantID).
		Scan(&out.Members); err != nil {
		InternalError(w, "overview: members query failed")
		return
	}
	start := time.Now().AddDate(0, 0, -30)
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(input_tokens + output_tokens + cached_tokens), 0)::bigint,
		        COALESCE(SUM(COALESCE(cost_cents, 0)), 0)::bigint
		   FROM usage_events WHERE tenant_id = $1 AND created_at >= $2`, tenantID, start).
		Scan(&out.MonthTokens, &out.MonthCostCents); err != nil {
		InternalError(w, "overview: usage query failed")
		return
	}
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::bigint, COUNT(*) FILTER (WHERE verified)::bigint
		   FROM domains WHERE tenant_id = $1`, tenantID).
		Scan(&out.Domains, &out.VerifiedDomains); err != nil {
		InternalError(w, "overview: domains query failed")
		return
	}
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::bigint FROM invoice_requests ir
		   JOIN users u ON u.id = ir.user_id
		  WHERE u.tenant_id = $1 AND ir.status = 'pending'`, tenantID).
		Scan(&out.PendingInvoices); err != nil {
		InternalError(w, "overview: invoices query failed")
		return
	}
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::bigint FROM user_entitlements ue
		   JOIN users u ON u.id = ue.user_id
		  WHERE u.tenant_id = $1 AND ue.expires_at > now()`, tenantID).
		Scan(&out.ActiveEntitlements); err != nil {
		InternalError(w, "overview: entitlements query failed")
		return
	}
	OK(w, out)
}
