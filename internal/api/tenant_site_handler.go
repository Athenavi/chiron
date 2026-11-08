package api

import (
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
)

// tenant_site_handler.go —— 租户站点品牌配置（标题 / 图标 / 描述）。
//
// 存储复用 system_settings 的租户维度（category = "site"，tenant_id = claims.tenant_id）：
// 与支付/SMTP 同一套隔离机制，不新增表。租户未配置时前端保持平台默认。
//
// 读取接口挂在 authMW（不限管理员）：品牌要在**每个登录用户**的页面上生效，
// 普通成员也必须能读到本租户的标题/图标；写入仍要求管理员权限。

const settingsCategorySite = "site"

type tenantSiteConfig struct {
	Title       string `json:"title"`
	IconURL     string `json:"icon_url"`
	Description string `json:"description"`
}

// GetTenantSiteConfig GET /v1/tenant/site-config
func (h *AdminHandler) GetTenantSiteConfig(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	cfg := tenantSiteConfig{}
	if store := h.ensureSettingsStore(); store != nil {
		if saved, err := store.LoadTenantConfig(r.Context(), int(claims.TenantID), settingsCategorySite); err == nil {
			cfg.Title, _ = saved["site_title"].(string)
			cfg.IconURL, _ = saved["site_icon_url"].(string)
			cfg.Description, _ = saved["site_description"].(string)
		}
	}
	OK(w, map[string]interface{}{
		"config": cfg,
		// using_default=true：本租户尚未配置，前端保持平台默认标题/图标。
		"using_default": cfg.Title == "" && cfg.IconURL == "" && cfg.Description == "",
	})
}

// SaveTenantSiteConfig PUT /v1/tenant/site-config
func (h *AdminHandler) SaveTenantSiteConfig(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var body tenantSiteConfig
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	title := strings.TrimSpace(body.Title)
	icon := strings.TrimSpace(body.IconURL)
	desc := strings.TrimSpace(body.Description)

	if len([]rune(title)) > 128 {
		BadRequest(w, "title must be at most 128 characters")
		return
	}
	if len([]rune(desc)) > 512 {
		BadRequest(w, "description must be at most 512 characters")
		return
	}
	if icon != "" && !isHTTPURL(icon) {
		BadRequest(w, "icon_url must be an http(s) URL")
		return
	}

	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	if err := store.SaveTenantConfig(r.Context(), int(claims.TenantID), settingsCategorySite, map[string]interface{}{
		"site_title":       title,
		"site_icon_url":    icon,
		"site_description": desc,
	}, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save site config failed")
		return
	}
	OK(w, map[string]interface{}{
		"config":        tenantSiteConfig{Title: title, IconURL: icon, Description: desc},
		"using_default": title == "" && icon == "" && desc == "",
	})
}
