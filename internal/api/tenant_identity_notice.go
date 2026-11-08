package api

import (
	"fmt"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// TenantIdentityNotice GET /v1/tenant/identity-notice
//
// 租户管理员在 /tenant/identity 配置了身份能力（SSO 提供商 / 人机验证 / 短信
// 验证码）后，该租户的**普通用户**进入前端时看到一次通知。
//
// 只返回"是否启用 + 指纹"：指纹变化（例如新增一个 SSO 提供商）时前端会再提示
// 一次，而不是永远只提示一次。接口挂在 authMW 下（不限管理员）—— 通知的受众
// 正是普通租户用户，管理员自己不需要被提醒"你配了什么"。
func (h *AdminHandler) GetTenantIdentityNotice(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	tenantID := claims.TenantID
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, "database not available")
		return
	}

	var providers int
	_ = pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM ent_oidc_providers
		  WHERE tenant_id = $1 AND COALESCE(enabled, false)`,
		tenantID).Scan(&providers)

	var captcha bool
	_ = pool.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM ent_captcha_config
		                WHERE tenant_id = $1 AND COALESCE(enabled, false)
		                  AND COALESCE(provider, '') <> '')`,
		tenantID).Scan(&captcha)

	var sms bool
	_ = pool.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM ent_sms_config
		                WHERE tenant_id = $1 AND COALESCE(provider, '') <> ''
		                  AND COALESCE(access_key_id, '') <> '')`,
		tenantID).Scan(&sms)

	enabled := providers > 0 || captcha || sms
	fingerprint := fmt.Sprintf("%d|%d|%t|%t", tenantID, providers, captcha, sms)
	OK(w, map[string]interface{}{
		"enabled":     enabled,
		"providers":   providers,
		"captcha":     captcha,
		"sms":         sms,
		"fingerprint": fingerprint,
	})
}
