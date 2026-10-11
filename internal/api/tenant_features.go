package api

import (
	"context"

	"github.com/athenavi/chiron/internal/auth"
)

// TenantFeatures 是租户级功能开关（目前只有 SSO 两项）。
//
// 它是 SSO 的**灰度载体**：平台 owner 在后台逐个租户放开，而不是"配了 provider 就等于开通"。
type TenantFeatures struct {
	SSOEnabled     bool `json:"sso_enabled"`
	SSOSelfService bool `json:"sso_self_service"`
}

// loadTenantFeatures 读取租户功能开关。
//
// **无记录 ⇒ 零值（全关）**，这是刻意的灰度默认；**读取失败也一律按全关**
// （fail-closed）—— 绝不能因为一次 DB 抖动或表未迁移就把 SSO 放开。
func loadTenantFeatures(ctx context.Context, db entQuerier, tenantID string) TenantFeatures {
	if tenantID == "" {
		return TenantFeatures{}
	}
	var f TenantFeatures
	if err := db.QueryRow(ctx,
		`SELECT sso_enabled, sso_self_service FROM tenant_features WHERE tenant_id = $1`,
		tenantID).Scan(&f.SSOEnabled, &f.SSOSelfService); err != nil {
		return TenantFeatures{}
	}
	return f
}

// saveTenantFeatures 写入（upsert）租户功能开关。
func saveTenantFeatures(ctx context.Context, db entQuerier, tenantID string, f TenantFeatures) error {
	_, err := db.Exec(ctx,
		`INSERT INTO tenant_features (tenant_id, sso_enabled, sso_self_service, updated_at)
		 VALUES ($1, $2, $3, NOW())
		 ON CONFLICT (tenant_id) DO UPDATE
		   SET sso_enabled = EXCLUDED.sso_enabled,
		       sso_self_service = EXCLUDED.sso_self_service,
		       updated_at = NOW()`,
		tenantID, f.SSOEnabled, f.SSOSelfService)
	return err
}

// isPlatformOwner 判断是否为**平台级** owner。
//
// 用于"租户开关只能由平台放开"这类判定 —— 与 admin.go 里"只有 owner 能授予 owner 角色"
// 同一口径（`claims.Role == "owner"`）。
func isPlatformOwner(claims *auth.Claims) bool {
	return claims != nil && claims.Role == "owner"
}
