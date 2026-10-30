package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// sso_providers_admin.go —— SSO 的**管理端提供方配置**。
//
// 由 sso_handler.go 按职责拆出（L3-4 批次 A，同包纯移动，逐字未改）。
//
// 与留在原文件的部分怎么分：这里只有「管理员怎么配 IdP」——列表、创建、编辑、
// 删除、以及各提供方的配置指引；**登录链路**（Login / Callback / 绑定身份 /
// 设密码）仍在 sso_handler.go，它读的是本文件配出来的数据。
//
// 两个辅助函数 normalizeProviderInput / defaultStr 也在这里，因为它们只服务
// 本文件的创建/更新路径；登录链路不碰它们。

// ── 管理路由（authMW + sso:manage）──────────────────────

// ListProviders GET /v1/ent/sso/providers
func (h *SSOHandler) ListProviders(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.Query(r.Context(),
		`SELECT `+ssoProviderColumns+` FROM ent_oidc_providers ORDER BY sort_order, created_at`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]map[string]any, 0)
	for rows.Next() {
		provider, err := scanSSOProvider(rows)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		items = append(items, sanitizeProvider(provider))
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}

type createProviderRequest struct {
	Name          string            `json:"name"`
	Issuer        string            `json:"issuer"`
	ClientID      string            `json:"client_id"`
	ClientSecret  string            `json:"client_secret"`
	Scopes        []string          `json:"scopes"`
	Enabled       *bool             `json:"enabled"`
	AutoProvision *bool             `json:"auto_provision"`
	RoleMapping   map[string]string `json:"role_mapping"`
	// OAuth2 扩展
	Protocol     string            `json:"protocol"`
	ProviderType string            `json:"provider_type"`
	DisplayName  string            `json:"display_name"`
	Icon         string            `json:"icon"`
	SortOrder    *int              `json:"sort_order"`
	AuthURL      string            `json:"auth_url"`
	TokenURL     string            `json:"token_url"`
	UserinfoURL  string            `json:"userinfo_url"`
	Extra        map[string]string `json:"extra"`
}

// normalizeProviderInput 校验并补齐 provider 协议字段（模板端点自动填充）。
// 返回规范化的 issuer/protocol/providerType/authURL/tokenURL/userinfoURL/scopes。
func normalizeProviderInput(protocol, providerType, issuer, authURL, tokenURL, userinfoURL string, scopes []string) (
	string, string, string, string, string, string, []string, error) {
	// protocol 未显式指定时按 provider_type 模板推断（github/wechat 等原生 OAuth2）
	if protocol == "" {
		if profile, ok := auth.GetProviderProfile(providerType); ok {
			protocol = profile.Protocol
		} else {
			protocol = auth.ProtocolOIDC
		}
	}
	providerType = defaultStr(providerType, auth.ProviderCustom)
	if !auth.ValidProtocol(protocol) {
		return "", "", "", "", "", "", nil, errors.New("protocol must be oidc or oauth2")
	}
	if !auth.IsKnownProviderType(providerType) {
		return "", "", "", "", "", "", nil, errors.New("unknown provider_type: " + providerType)
	}
	issuer, authURL, tokenURL, userinfoURL = auth.ResolveEndpoints(providerType, issuer, authURL, tokenURL, userinfoURL)
	if len(scopes) == 0 {
		scopes = auth.DefaultScopes(providerType)
	}
	return issuer, protocol, providerType, authURL, tokenURL, userinfoURL, scopes, nil
}

func defaultStr(s, def string) string {
	if s == "" {
		return def
	}
	return s
}

// GetProviderGuide GET /v1/ent/sso/providers/guide —— 配置向导所需的信息。
//
// 存在的理由：配 SSO 最容易错的两件事是「**回调地址**填错」（IdP 侧校验失败，报错还很难懂）
// 和「端点 / scope 不知道该填什么」（只能翻文档）。这里一次性给出本实例的回调地址与
// 各内置 provider 的模板，前端据此渲染分步表单。
func (h *SSOHandler) GetProviderGuide(w http.ResponseWriter, r *http.Request) {
	tenantID := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		tenantID = tenantStr(claims.TenantID)
	}

	profiles := make([]map[string]any, 0, 8)
	for _, t := range auth.KnownProviderTypes() {
		p, ok := auth.GetProviderProfile(t)
		if !ok {
			continue
		}
		profiles = append(profiles, map[string]any{
			"type":         p.Type,
			"protocol":     p.Protocol,
			"issuer":       p.Issuer,
			"auth_url":     p.AuthURL,
			"token_url":    p.TokenURL,
			"userinfo_url": p.UserinfoURL,
			"scopes":       p.Scopes,
		})
	}

	OK(w, map[string]any{
		// 需要在 IdP 后台登记的 redirect_uri（本实例）
		"callback_url": h.redirectURL,
		"tenant_id":    tenantID,
		"providers":    profiles,
	})
}

// CreateProvider POST /v1/ent/sso/providers
// client_secret 以 AES-GCM 加密入库；密钥未配置时返回 503。
func (h *SSOHandler) CreateProvider(w http.ResponseWriter, r *http.Request) {
	var req createProviderRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Name == "" || req.ClientID == "" || req.ClientSecret == "" {
		BadRequest(w, "name, client_id and client_secret are required")
		return
	}
	if len(req.Name) > 64 || len(req.Issuer) > 512 || len(req.ClientID) > 256 ||
		len(req.DisplayName) > 64 || len(req.Icon) > 64 ||
		len(req.AuthURL) > 512 || len(req.TokenURL) > 512 || len(req.UserinfoURL) > 512 {
		BadRequest(w, "field too long")
		return
	}
	if h.encKey == nil {
		ServiceUnavailable(w, "SSO is not configured: "+auth.EnvOIDCSecretKey+" missing")
		return
	}

	// 租户归属：provider 落在**调用者所属租户**（与 ent_identity_handler.go 的角色/
	// 群组创建同口径，仅回退默认租户）。此前硬编码 db.DefaultTenantID ⇒ 多租户下
	// 无法为其它租户配置 IdP（只能手工改库），那些租户的用户永远用不上 SSO。
	// 登录链路本就读 provider.TenantID（见 Callback 的说明），修好创建端即两端一致。
	tenantID := db.DefaultTenantID
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.TenantID != 0 {
		tenantID = claims.TenantID
	}

	// 自助开通开关：`sso_self_service=false`（默认）时只有**平台 owner** 能建 provider，
	// 租户 admin 即便持有 `sso:manage` 也被拒 —— 灰度由平台逐个租户放开。
	if !loadTenantFeatures(r.Context(), h.db, tenantID).SSOSelfService &&
		!isPlatformOwner(auth.GetClaims(r.Context())) {
		Forbidden(w, "该租户尚未开放 SSO 自助配置，请联系平台管理员")
		return
	}

	issuer, protocol, providerType, authURL, tokenURL, userinfoURL, scopes, err :=
		normalizeProviderInput(req.Protocol, req.ProviderType, req.Issuer, req.AuthURL, req.TokenURL, req.UserinfoURL, req.Scopes)
	if err != nil {
		BadRequest(w, "sso provider configuration error")
		return
	}
	if protocol == auth.ProtocolOIDC && issuer == "" {
		BadRequest(w, "issuer is required for oidc providers")
		return
	}

	secretEnc, err := auth.EncryptAESGCM(h.encKey, req.ClientSecret)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "encrypt client_secret failed")
		return
	}

	enabled, autoProvision := true, true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	if req.AutoProvision != nil {
		autoProvision = *req.AutoProvision
	}
	sortOrder := 100
	if req.SortOrder != nil {
		sortOrder = *req.SortOrder
	}
	roleMapping := req.RoleMapping
	if roleMapping == nil {
		roleMapping = map[string]string{}
	}
	extra := req.Extra
	if extra == nil {
		extra = map[string]string{}
	}
	mappingJSON, err := json.Marshal(roleMapping)
	if err != nil {
		BadRequest(w, "invalid role_mapping")
		return
	}
	extraJSON, err := json.Marshal(extra)
	if err != nil {
		BadRequest(w, "invalid extra")
		return
	}

	var id string
	err = h.db.QueryRow(r.Context(),
		`INSERT INTO ent_oidc_providers
		   (tenant_id, name, issuer, client_id, client_secret_enc, scopes, enabled, auto_provision, role_mapping,
		    protocol, provider_type, display_name, icon, sort_order, auth_url, token_url, userinfo_url, extra)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) RETURNING id`,
		tenantID, req.Name, nullString(issuer), req.ClientID, secretEnc,
		scopes, enabled, autoProvision, mappingJSON,
		protocol, providerType, nullString(req.DisplayName), nullString(req.Icon), sortOrder,
		nullString(authURL), nullString(tokenURL), nullString(userinfoURL), extraJSON).Scan(&id)
	if err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "provider name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	provider, err := h.getProvider(r.Context(), id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	Created(w, sanitizeProvider(provider))
}

// GetProvider GET /v1/ent/sso/providers/{id}
func (h *SSOHandler) GetProvider(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid provider id")
		return
	}
	provider, err := h.getProvider(r.Context(), id)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	// 跨租户读防护：非 owner 只能读自己租户的 SSO Provider。
	if !isOwner && provider.TenantID != scope {
		NotFound(w, ErrNotFound)
		return
	}
	OK(w, sanitizeProvider(provider))
}

type updateProviderRequest struct {
	Name          *string            `json:"name"`
	Issuer        *string            `json:"issuer"`
	ClientID      *string            `json:"client_id"`
	ClientSecret  *string            `json:"client_secret"`
	Scopes        *[]string          `json:"scopes"`
	Enabled       *bool              `json:"enabled"`
	AutoProvision *bool              `json:"auto_provision"`
	RoleMapping   *map[string]string `json:"role_mapping"`
	// OAuth2 扩展
	Protocol     *string            `json:"protocol"`
	ProviderType *string            `json:"provider_type"`
	DisplayName  *string            `json:"display_name"`
	Icon         *string            `json:"icon"`
	SortOrder    *int               `json:"sort_order"`
	AuthURL      *string            `json:"auth_url"`
	TokenURL     *string            `json:"token_url"`
	UserinfoURL  *string            `json:"userinfo_url"`
	Extra        *map[string]string `json:"extra"`
}

// UpdateProvider PUT /v1/ent/sso/providers/{id}
// client_secret 为空串或脱敏占位符时保留原密文；提供新值时重新加密（需密钥）。
func (h *SSOHandler) UpdateProvider(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid provider id")
		return
	}
	var req updateProviderRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	ctx := r.Context()

	existing, err := h.getProvider(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	name, issuer, clientID := existing.Name, existing.Issuer, existing.ClientID
	secretEnc, scopes := existing.ClientSecretEnc, existing.Scopes
	enabled, autoProvision := existing.Enabled, existing.AutoProvision
	mapping := existing.RoleMapping
	protocol, providerType := existing.Protocol, existing.ProviderType
	displayName, icon, sortOrder := existing.DisplayName, existing.Icon, existing.SortOrder
	authURL, tokenURL, userinfoURL := existing.AuthURL, existing.TokenURL, existing.UserinfoURL
	extra := existing.Extra

	if req.Name != nil {
		if *req.Name == "" || len(*req.Name) > 64 {
			BadRequest(w, "invalid name")
			return
		}
		name = *req.Name
	}
	if req.Protocol != nil {
		protocol = *req.Protocol
	}
	if req.ProviderType != nil {
		providerType = *req.ProviderType
	}
	// 协议/模板变更后重新解析端点（显式覆盖优先）
	if req.Protocol != nil || req.ProviderType != nil || req.Issuer != nil ||
		req.AuthURL != nil || req.TokenURL != nil || req.UserinfoURL != nil {
		var iss, au, tu, uu string
		if req.Issuer != nil {
			iss = *req.Issuer
		}
		if req.AuthURL != nil {
			au = *req.AuthURL
		}
		if req.TokenURL != nil {
			tu = *req.TokenURL
		}
		if req.UserinfoURL != nil {
			uu = *req.UserinfoURL
		}
		// 未显式覆盖的字段沿用既有值（可能已是管理员自定义端点）
		if iss == "" {
			iss = issuer
		}
		if au == "" {
			au = authURL
		}
		if tu == "" {
			tu = tokenURL
		}
		if uu == "" {
			uu = userinfoURL
		}
		issuer, protocol, providerType, authURL, tokenURL, userinfoURL, _, err =
			normalizeProviderInput(protocol, providerType, iss, au, tu, uu, scopes)
		if err != nil {
			BadRequest(w, "sso provider configuration error")
			return
		}
	}
	if protocol == auth.ProtocolOIDC && issuer == "" {
		BadRequest(w, "issuer is required for oidc providers")
		return
	}
	if req.DisplayName != nil {
		displayName = *req.DisplayName
		if len(displayName) > 64 {
			BadRequest(w, "invalid display_name")
			return
		}
	}
	if req.Icon != nil {
		icon = *req.Icon
		if len(icon) > 64 {
			BadRequest(w, "invalid icon")
			return
		}
	}
	if req.SortOrder != nil {
		sortOrder = *req.SortOrder
	}
	if req.ClientID != nil {
		if *req.ClientID == "" || len(*req.ClientID) > 256 {
			BadRequest(w, "invalid client_id")
			return
		}
		clientID = *req.ClientID
	}
	if req.ClientSecret != nil && *req.ClientSecret != "" && *req.ClientSecret != maskedSecret {
		if h.encKey == nil {
			ServiceUnavailable(w, "SSO is not configured: "+auth.EnvOIDCSecretKey+" missing")
			return
		}
		secretEnc, err = auth.EncryptAESGCM(h.encKey, *req.ClientSecret)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "encrypt client_secret failed")
			return
		}
	}
	if req.Scopes != nil && len(*req.Scopes) > 0 {
		scopes = *req.Scopes
	}
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	if req.AutoProvision != nil {
		autoProvision = *req.AutoProvision
	}
	if req.Extra != nil {
		extra = *req.Extra
		if extra == nil {
			extra = map[string]string{}
		}
	}
	var mappingJSON, extraJSON []byte
	if req.RoleMapping != nil {
		mapping = *req.RoleMapping
		if mapping == nil {
			mapping = map[string]string{}
		}
	}
	mappingJSON, err = json.Marshal(mapping)
	if err != nil {
		BadRequest(w, "invalid role_mapping")
		return
	}
	extraJSON, err = json.Marshal(extra)
	if err != nil {
		BadRequest(w, "invalid extra")
		return
	}

	if _, err := h.db.Exec(ctx,
		`UPDATE ent_oidc_providers
		 SET name = $2, issuer = $3, client_id = $4, client_secret_enc = $5,
		     scopes = $6, enabled = $7, auto_provision = $8, role_mapping = $9,
		     protocol = $10, provider_type = $11, display_name = $12, icon = $13, sort_order = $14,
		     auth_url = $15, token_url = $16, userinfo_url = $17, extra = $18,
		     updated_at = NOW()
		 WHERE id = $1 AND ($19 = '' OR tenant_id::text = $19)`,
		id, name, nullString(issuer), clientID, secretEnc, scopes, enabled, autoProvision, mappingJSON,
		protocol, providerType, nullString(displayName), nullString(icon), sortOrder,
		nullString(authURL), nullString(tokenURL), nullString(userinfoURL), extraJSON, scope); err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "provider name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	updated, err := h.getProvider(ctx, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, sanitizeProvider(updated))
}

// DeleteProvider DELETE /v1/ent/sso/providers/{id}
func (h *SSOHandler) DeleteProvider(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid provider id")
		return
	}
	tag, err := h.db.Exec(r.Context(), `DELETE FROM ent_oidc_providers WHERE id = $1 AND ($2 = '' OR tenant_id::text = $2)`, id, scope)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}
