package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// maskedSecret 是 provider 读响应中 client_secret 的唯一脱敏形态。
const maskedSecret = "********"

// ssoStateTTL 是 SSO state 令牌有效期（防重放窗口）。
const ssoStateTTL = 10 * time.Minute

// ── 数据模型 ────────────────────────────────────────────

type ssoProvider struct {
	ID              string
	TenantID        string
	Name            string
	Issuer          string
	ClientID        string
	ClientSecretEnc string
	Scopes          []string
	Enabled         bool
	AutoProvision   bool
	RoleMapping     map[string]string
	// OAuth2 扩展
	Protocol     string
	ProviderType string
	DisplayName  string
	Icon         string
	SortOrder    int
	AuthURL      string
	TokenURL     string
	UserinfoURL  string
	Extra        map[string]string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

type ssoUser struct {
	ID    string
	Email string
	Name  string
	Role  string
}

// sanitizeProvider 构造 provider 的对外响应：client_secret 一律脱敏。
func sanitizeProvider(p *ssoProvider) map[string]any {
	roleMapping := p.RoleMapping
	if roleMapping == nil {
		roleMapping = map[string]string{}
	}
	scopes := p.Scopes
	if scopes == nil {
		scopes = []string{}
	}
	extra := p.Extra
	if extra == nil {
		extra = map[string]string{}
	}
	return map[string]any{
		"id":             p.ID,
		"tenant_id":      p.TenantID,
		"name":           p.Name,
		"issuer":         p.Issuer,
		"client_id":      p.ClientID,
		"client_secret":  maskedSecret,
		"scopes":         scopes,
		"enabled":        p.Enabled,
		"auto_provision": p.AutoProvision,
		"role_mapping":   roleMapping,
		"protocol":       p.Protocol,
		"provider_type":  p.ProviderType,
		"display_name":   p.DisplayName,
		"icon":           p.Icon,
		"sort_order":     p.SortOrder,
		"auth_url":       p.AuthURL,
		"token_url":      p.TokenURL,
		"userinfo_url":   p.UserinfoURL,
		"extra":          extra,
		"created_at":     p.CreatedAt,
		"updated_at":     p.UpdatedAt,
	}
}

type scanner interface{ Scan(dest ...any) error }

// 可空列统一 COALESCE 为空串：未覆盖（”）即"用模板缺省值"，语义与 OAuth2 端点覆盖一致。
const ssoProviderColumns = `id, tenant_id, name, COALESCE(issuer, ''), client_id, client_secret_enc,
       scopes, enabled, auto_provision, role_mapping,
       COALESCE(protocol, 'oidc'), COALESCE(provider_type, 'custom'),
       COALESCE(display_name, ''), COALESCE(icon, ''), COALESCE(sort_order, 100),
       COALESCE(auth_url, ''), COALESCE(token_url, ''), COALESCE(userinfo_url, ''),
       extra, created_at, updated_at`

func scanSSOProvider(row scanner) (*ssoProvider, error) {
	var p ssoProvider
	var scopes []string
	var roleMappingRaw []byte
	var extraRaw []byte
	if err := row.Scan(&p.ID, &p.TenantID, &p.Name, &p.Issuer, &p.ClientID, &p.ClientSecretEnc,
		&scopes, &p.Enabled, &p.AutoProvision, &roleMappingRaw,
		&p.Protocol, &p.ProviderType, &p.DisplayName, &p.Icon, &p.SortOrder,
		&p.AuthURL, &p.TokenURL, &p.UserinfoURL, &extraRaw, &p.CreatedAt, &p.UpdatedAt); err != nil {
		return nil, err
	}
	p.Scopes = scopes
	if p.Scopes == nil {
		p.Scopes = []string{}
	}
	p.RoleMapping = map[string]string{}
	if len(roleMappingRaw) > 0 {
		if err := json.Unmarshal(roleMappingRaw, &p.RoleMapping); err != nil {
			return nil, err
		}
	}
	p.Extra = map[string]string{}
	if len(extraRaw) > 0 {
		if err := json.Unmarshal(extraRaw, &p.Extra); err != nil {
			return nil, err
		}
	}
	if p.Protocol == "" {
		p.Protocol = auth.ProtocolOIDC
	}
	if p.ProviderType == "" {
		p.ProviderType = auth.ProviderCustom
	}
	return &p, nil
}

// ── Handler ─────────────────────────────────────────────

// SSOHandler 负责 OIDC/OAuth2 SSO 公开流程、用户自助绑定与管理端 CRUD。
// db 抽象为 entQuerier 便于测试注入 fake；exchanger 抽象 IdP 交互。
type SSOHandler struct {
	auth        *auth.Authenticator
	cfg         *config.Config
	db          entQuerier
	exchanger   auth.OIDCExchanger // OIDC 协议交换器（go-oidc）
	oauth2      auth.OIDCExchanger // OAuth2 协议交换器（github/微信/钉钉/飞书/qq）
	codec       *auth.StateCodec
	encKey      []byte
	redirectURL string // OAuth2 redirect_uri（授权回调地址）
	successURL  string // 登录成功后 302 回前端的目标
	bindURL     string // 绑定成功后 302 回前端的目标
}

func NewSSOHandler(authenticator *auth.Authenticator, cfg *config.Config) *SSOHandler {
	// 前端独立部署（dev 5173 / 独立域名）时 302 到绝对地址；同源部署保持相对路径
	feBase := strings.TrimRight(cfg.FrontendURL, "/")
	h := &SSOHandler{
		auth:        authenticator,
		cfg:         cfg,
		db:          pgEntStore{},
		exchanger:   auth.NewRemoteOIDCExchanger(),
		oauth2:      auth.NewOAuth2Exchanger(),
		encKey:      auth.LoadOIDCEncryptionKey(),
		redirectURL: cfg.PublicBaseURL + "/v1/auth/sso/callback",
		successURL:  feBase + "/?sso=ok",
		bindURL:     feBase + "/profile?bind=ok",
	}
	if h.encKey != nil {
		h.codec = auth.NewStateCodec(h.encKey, ssoStateTTL)
	}
	return h
}

// RegisterPublicRoutes 挂载公开 SSO 路由（无 authMW；外层须套 rlMW 限流）：
// 发现列表 / 登录跳转 / IdP 回调。bind 模式的登录态从 JWT cookie 解析。
func (h *SSOHandler) RegisterPublicRoutes(mux *http.ServeMux, rlMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/auth/sso/providers", rlMW(http.HandlerFunc(h.ListPublicProviders)))
	mux.Handle("GET /v1/auth/sso/login/{providerID}", rlMW(http.HandlerFunc(h.Login)))
	mux.Handle("GET /v1/auth/sso/callback", rlMW(http.HandlerFunc(h.Callback)))
}

// RegisterUserRoutes 挂载用户自助路由（authMW）：绑定列表 / 解绑 / 设置密码。
func (h *SSOHandler) RegisterUserRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/auth/sso/identities", authMW(http.HandlerFunc(h.ListIdentities)))
	mux.Handle("DELETE /v1/auth/sso/identities/{id}", authMW(http.HandlerFunc(h.DeleteIdentity)))
	mux.Handle("POST /v1/auth/password", authMW(http.HandlerFunc(h.SetPassword)))
}

// RegisterAdminRoutes 挂载 SSO 管理路由：authMW + RequireEntPerm("sso:manage")。
func (h *SSOHandler) RegisterAdminRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	sso := func(hf http.HandlerFunc) http.Handler {
		return authMW(RequireEntPerm("sso:manage")(hf))
	}
	mux.Handle("GET /v1/ent/sso/providers", sso(h.ListProviders))
	mux.Handle("POST /v1/ent/sso/providers", sso(h.CreateProvider))
	mux.Handle("GET /v1/ent/sso/providers/{id}", sso(h.GetProvider))
	mux.Handle("PUT /v1/ent/sso/providers/{id}", sso(h.UpdateProvider))
	mux.Handle("DELETE /v1/ent/sso/providers/{id}", sso(h.DeleteProvider))
}

// ── 公开路由 ────────────────────────────────────────────

// ListPublicProviders GET /v1/auth/sso/providers
// 返回 enabled provider 的展示字段（不含任何敏感信息），按 sort_order 排序。
func (h *SSOHandler) ListPublicProviders(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.Query(r.Context(),
		`SELECT id, name, display_name, provider_type, icon, sort_order, protocol
		 FROM ent_oidc_providers WHERE enabled = TRUE ORDER BY sort_order, name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, name, providerType, protocol string
		var displayName, icon *string
		var sortOrder int
		if err := rows.Scan(&id, &name, &displayName, &providerType, &icon, &sortOrder, &protocol); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		display := name
		if displayName != nil && *displayName != "" {
			display = *displayName
		}
		iconKey := providerType
		if icon != nil && *icon != "" {
			iconKey = *icon
		}
		items = append(items, map[string]any{
			"id":            id,
			"name":          name,
			"display_name":  display,
			"provider_type": providerType,
			"icon":          iconKey,
			"sort_order":    sortOrder,
			"protocol":      protocol,
		})
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}

// Login GET /v1/auth/sso/login/{providerID}[?mode=bind]
// 生成 state+nonce → 302 到 IdP 授权页。
// mode=bind 需登录态（authMW 保证），uid 写入 HMAC 签名 state。
func (h *SSOHandler) Login(w http.ResponseWriter, r *http.Request) {
	providerID := r.PathValue("providerID")
	if !isValidUUID(providerID) {
		BadRequest(w, "invalid provider id")
		return
	}
	if h.encKey == nil || h.codec == nil {
		ServiceUnavailable(w, "SSO is not configured: "+auth.EnvOIDCSecretKey+" missing")
		return
	}

	provider, err := h.getEnabledProvider(r.Context(), providerID)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, "provider not found or disabled")
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	clientSecret, err := auth.DecryptAESGCM(h.encKey, provider.ClientSecretEnc)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sso provider secret unavailable")
		return
	}

	// bind 模式：必须携带登录态（从 JWT cookie 解析，公开路由无 authMW）
	mode := auth.StateModeLogin
	uid := ""
	if r.URL.Query().Get("mode") == auth.StateModeBind {
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			if c, cookieErr := r.Cookie(tokenCookieName); cookieErr == nil && c.Value != "" {
				if parsed, tokenErr := h.auth.ValidateToken(c.Value); tokenErr == nil {
					claims = parsed
				}
			}
		}
		if claims == nil || claims.UserID == "" {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		mode = auth.StateModeBind
		uid = claims.UserID
	}

	nonce, err := auth.RandomNonce()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sso login failed")
		return
	}
	state, err := h.codec.IssueMode(providerID, nonce, mode, uid)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sso login failed")
		return
	}

	exchanger, cfg, err := h.exchangerFor(provider, clientSecret)
	if err != nil {
		BadRequest(w, "sso provider configuration error")
		return
	}
	authURL, err := exchanger.AuthURL(r.Context(), cfg, state, nonce)
	if err != nil {
		logAndRespond(w, err, http.StatusBadGateway, "sso provider unreachable")
		return
	}
	http.Redirect(w, r, authURL, http.StatusFound)
}

// Callback GET /v1/auth/sso/callback?code=&state=
// 校验 state → 授权码换身份 → 登录绑定/自动建号（或 bind 模式绑定当前账号）→ 颁发会话。
func (h *SSOHandler) Callback(w http.ResponseWriter, r *http.Request) {
	state := r.URL.Query().Get("state")
	code := r.URL.Query().Get("code")
	if state == "" || code == "" {
		BadRequest(w, "missing code or state")
		return
	}
	if h.encKey == nil || h.codec == nil {
		ServiceUnavailable(w, "SSO is not configured: "+auth.EnvOIDCSecretKey+" missing")
		return
	}

	// 1. state 校验（HMAC 防 CSRF/篡改，TTL 防重放）——失败一律 400，且不触碰 DB
	payload, err := h.codec.Verify(state)
	if err != nil {
		logAndRespond(w, err, http.StatusBadRequest, "invalid sso state")
		return
	}

	// 2. 加载 provider（必须 enabled）
	provider, err := h.getEnabledProvider(r.Context(), payload.ProviderID)
	if errors.Is(err, pgx.ErrNoRows) {
		BadRequest(w, "unknown sso provider in state")
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	clientSecret, err := auth.DecryptAESGCM(h.encKey, provider.ClientSecretEnc)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sso provider secret unavailable")
		return
	}

	// 3. 授权码交换 + 身份校验（OIDC 含 nonce 比对；OAuth2 由 state HMAC 防重放）
	exchanger, cfg, err := h.exchangerFor(provider, clientSecret)
	if err != nil {
		BadRequest(w, "sso provider configuration error")
		return
	}
	idToken, err := exchanger.ExchangeAndVerify(r.Context(), cfg, code, payload.Nonce)
	if err != nil {
		logAndRespond(w, err, http.StatusUnauthorized, "sso authentication failed")
		return
	}
	if idToken.Subject == "" {
		BadRequest(w, "id_token missing subject")
		return
	}

	// 3.5 bind 模式：把该三方身份绑定到 state.uid 账号
	if payload.Mode == auth.StateModeBind {
		h.handleBindCallback(w, r, provider, idToken, payload)
		return
	}

	// 4. 按 (provider_id, subject) 查绑定
	user, err := h.findIdentityUser(r.Context(), provider.ID, idToken.Subject)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 5. 未绑定：auto_provision 决定建号或拒绝
	if errors.Is(err, pgx.ErrNoRows) {
		if !provider.AutoProvision {
			logAndRespond(w, errors.New("sso subject not bound and auto_provision disabled"),
				http.StatusForbidden, "account not provisioned for this SSO provider")
			return
		}
		user, err = h.provisionAndBind(r, provider, idToken)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "sso provisioning failed")
			return
		}
	}

	// 6. 复用现有登录的会话颁发逻辑（GenerateToken + SetTokenCookie）
	// 多租户隔离：SSO 用户的 tenant_id 来自 sso_provider 表配置（ent_oidc_providers.tenant_id），
	// 与自动建号 provisionAndBind 使用的 provider.TenantID 一致，保证 JWT 与 DB 行为对齐
	if provider.TenantID == "" {
		InternalError(w, "sso provider missing tenant_id configuration")
		return
	}
	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, provider.TenantID, auth.RolePermissions[user.Role])
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}
	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(r.Context(), user.ID, provider.TenantID, "sso_login", "/v1/auth/sso/callback", "provider="+provider.Name, r.RemoteAddr, nil)
	http.Redirect(w, r, h.successURL, http.StatusFound)
}

// handleBindCallback bind 模式回调：该三方身份 → state.uid 本地账号。
// 冲突（已绑定他人）→ 409；已绑定本人 → 幂等成功。
func (h *SSOHandler) handleBindCallback(w http.ResponseWriter, r *http.Request, provider *ssoProvider, idToken *auth.IDTokenResult, payload *auth.StatePayload) {
	ctx := r.Context()

	existing, err := h.findIdentityUser(ctx, provider.ID, idToken.Subject)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if existing != nil {
		if existing.ID != payload.UID {
			db.AuditLog(ctx, payload.UID, provider.TenantID, "sso_bind_conflict", "/v1/auth/sso/callback",
				"provider="+provider.Name, r.RemoteAddr, nil)
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "该三方账号已绑定其他用户，请改用其登录",
			})
			return
		}
		// 已绑定本人 → 幂等
		http.Redirect(w, r, h.bindURL, http.StatusFound)
		return
	}

	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_user_identities (user_id, provider_id, subject, email)
		 VALUES ($1, $2, $3, $4)`,
		payload.UID, provider.ID, idToken.Subject, idToken.Email); err != nil {
		if isUniqueViolation(err) {
			// 并发绑定冲突
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "该三方账号已绑定其他用户，请改用其登录",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	db.AuditLog(ctx, payload.UID, provider.TenantID, "sso_bind", "/v1/auth/sso/callback",
		"provider="+provider.Name, r.RemoteAddr, nil)
	http.Redirect(w, r, h.bindURL, http.StatusFound)
}

// ── 用户自助：绑定列表 / 解绑 / 设置密码 ────────────────

// ListIdentities GET /v1/auth/sso/identities（authMW）
func (h *SSOHandler) ListIdentities(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	rows, err := h.db.Query(r.Context(),
		`SELECT ui.id, p.name, COALESCE(p.display_name, p.name), p.provider_type,
		        ui.subject, ui.email, ui.created_at
		 FROM ent_user_identities ui
		 JOIN ent_oidc_providers p ON p.id = ui.provider_id
		 WHERE ui.user_id = $1
		 ORDER BY ui.created_at`, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, name, display, providerType, subject string
		var email *string
		var createdAt time.Time
		if err := rows.Scan(&id, &name, &display, &providerType, &subject, &email, &createdAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		emailStr := ""
		if email != nil {
			emailStr = *email
		}
		items = append(items, map[string]any{
			"id":            id,
			"provider_name": display,
			"provider_type": providerType,
			"subject":       subject,
			"email":         emailStr,
			"created_at":    createdAt,
		})
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}

// DeleteIdentity DELETE /v1/auth/sso/identities/{id}（authMW）
// 守卫：用户无可口令密码（password_set=false）且这是最后一个三方身份 → 403。
func (h *SSOHandler) DeleteIdentity(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid identity id")
		return
	}
	ctx := r.Context()

	// 守卫：确保解绑后至少保留一种登录方式
	var passwordSet bool
	var count int
	err := h.db.QueryRow(ctx,
		`SELECT u.password_set,
		        (SELECT COUNT(*) FROM ent_user_identities ui WHERE ui.user_id = u.id)
		 FROM users u WHERE u.id = $1`, claims.UserID).Scan(&passwordSet, &count)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 目标身份属于本人？
	var subject string
	err = h.db.QueryRow(ctx,
		`SELECT subject FROM ent_user_identities WHERE id = $1 AND user_id = $2`,
		id, claims.UserID).Scan(&subject)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	if !passwordSet && count <= 1 {
		Forbidden(w, "无法解绑：请先设置密码，保证至少保留一种登录方式")
		return
	}

	tag, err := h.db.Exec(ctx,
		`DELETE FROM ent_user_identities WHERE id = $1 AND user_id = $2`, id, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	db.AuditLog(ctx, claims.UserID, claims.TenantID, "sso_unbind", "/v1/auth/sso/identities/"+id,
		"subject="+subject, r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "deleted"})
}

type setPasswordRequest struct {
	CurrentPassword string `json:"current_password"`
	NewPassword     string `json:"new_password"`
}

// SetPassword POST /v1/auth/password（authMW）
// SSO 建号用户（password_set=false）首设密码免旧密码；已设置者必须校验旧密码。
func (h *SSOHandler) SetPassword(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req setPasswordRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.NewPassword); !ok {
		BadRequest(w, msg)
		return
	}
	ctx := r.Context()

	var passwordHash string
	var passwordSet bool
	err := h.db.QueryRow(ctx,
		`SELECT password_hash, password_set FROM users WHERE id = $1`, claims.UserID).
		Scan(&passwordHash, &passwordSet)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	if passwordSet {
		if req.CurrentPassword == "" {
			BadRequest(w, "current_password is required")
			return
		}
		if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.CurrentPassword)); err != nil {
			Unauthorized(w, "invalid current password")
			return
		}
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.NewPassword), auth.BcryptCost)
	if err != nil {
		InternalError(w, "set password failed")
		return
	}
	if _, err := h.db.Exec(ctx,
		`UPDATE users SET password_hash = $2, password_set = TRUE, updated_at = NOW() WHERE id = $1`,
		claims.UserID, string(hash)); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	db.AuditLog(ctx, claims.UserID, claims.TenantID, "password_set", "/v1/auth/password", "", r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "updated"})
}

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
		db.DefaultTenantID, req.Name, nullString(issuer), req.ClientID, secretEnc,
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
		 WHERE id = $1`,
		id, name, nullString(issuer), clientID, secretEnc, scopes, enabled, autoProvision, mappingJSON,
		protocol, providerType, nullString(displayName), nullString(icon), sortOrder,
		nullString(authURL), nullString(tokenURL), nullString(userinfoURL), extraJSON); err != nil {
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
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid provider id")
		return
	}
	tag, err := h.db.Exec(r.Context(), `DELETE FROM ent_oidc_providers WHERE id = $1`, id)
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

// ── 内部辅助 ────────────────────────────────────────────

func (h *SSOHandler) getProvider(ctx context.Context, id string) (*ssoProvider, error) {
	return scanSSOProvider(h.db.QueryRow(ctx,
		`SELECT `+ssoProviderColumns+` FROM ent_oidc_providers WHERE id = $1`, id))
}

func (h *SSOHandler) getEnabledProvider(ctx context.Context, id string) (*ssoProvider, error) {
	return scanSSOProvider(h.db.QueryRow(ctx,
		`SELECT `+ssoProviderColumns+` FROM ent_oidc_providers WHERE id = $1 AND enabled = TRUE`, id))
}

// exchangerFor 按 provider 协议选择交换器并构造完整配置（含模板端点解析）。
func (h *SSOHandler) exchangerFor(p *ssoProvider, clientSecret string) (auth.OIDCExchanger, *auth.OIDCProviderConfig, error) {
	protocol := p.Protocol
	if protocol == "" {
		protocol = auth.ProtocolOIDC
	}
	// 模板端点补齐：显式覆盖（DB 列非空）优先，其次 provider 模板缺省值
	issuer, authURL, tokenURL, userinfoURL := auth.ResolveEndpoints(
		p.ProviderType, p.Issuer, p.AuthURL, p.TokenURL, p.UserinfoURL)
	scopes := p.Scopes
	if len(scopes) == 0 {
		scopes = auth.DefaultScopes(p.ProviderType)
	}

	cfg := &auth.OIDCProviderConfig{
		Issuer:       issuer,
		ClientID:     p.ClientID,
		ClientSecret: clientSecret,
		Scopes:       scopes,
		RedirectURL:  h.redirectURL,
		Protocol:     protocol,
		ProviderType: p.ProviderType,
		AuthURL:      authURL,
		TokenURL:     tokenURL,
		UserinfoURL:  userinfoURL,
		Extra:        p.Extra,
	}

	if protocol == auth.ProtocolOAuth2 {
		if h.oauth2 == nil {
			return nil, nil, errors.New("oauth2 exchanger not configured")
		}
		if cfg.AuthURL == "" || cfg.TokenURL == "" || cfg.UserinfoURL == "" {
			return nil, nil, errors.New("oauth2 provider missing endpoints (auth_url/token_url/userinfo_url)")
		}
		return h.oauth2, cfg, nil
	}
	if cfg.Issuer == "" {
		return nil, nil, errors.New("oidc provider missing issuer")
	}
	return h.exchanger, cfg, nil
}

// findIdentityUser 按 (provider_id, subject) 查绑定用户；未绑定返回 pgx.ErrNoRows。
func (h *SSOHandler) findIdentityUser(ctx context.Context, providerID, subject string) (*ssoUser, error) {
	var user ssoUser
	err := h.db.QueryRow(ctx,
		`SELECT u.id, u.email, u.name, u.role
		 FROM ent_user_identities ui JOIN users u ON u.id = ui.user_id
		 WHERE ui.provider_id = $1 AND ui.subject = $2`, providerID, subject).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if err != nil {
		return nil, err
	}
	return &user, nil
}

// provisionAndBind 自动建号并写入外部身份绑定。
// 租户取 provider 配置；email 取身份信息（缺失时以 subject 兜底）；
// password_hash 写入随机不可碰撞 bcrypt 且 password_set=FALSE（不可口令登录）；
// role 按 role_mapping 匹配、缺省 "user"；携带手机号时回填 users.phone。
func (h *SSOHandler) provisionAndBind(r *http.Request, provider *ssoProvider, idToken *auth.IDTokenResult) (*ssoUser, error) {
	ctx := r.Context()

	email := idToken.Email
	if email == "" {
		email = idToken.Subject + "@sso.local"
	}
	name := idToken.Name
	if name == "" {
		name = email
	}
	role := resolveRole(provider.RoleMapping, idToken.Roles)

	// 随机 32 字节密码 → bcrypt；SSO 建号用户不可用口令登录，直到主动设置密码
	randomPassword := make([]byte, 32)
	if _, err := rand.Read(randomPassword); err != nil {
		return nil, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(hex.EncodeToString(randomPassword)), auth.BcryptCost)
	if err != nil {
		return nil, err
	}

	// 同租户同 email 已有本地账号时直接绑定，避免重复建号
	var user ssoUser
	err = h.db.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND email = $2`,
		provider.TenantID, email).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		err = h.db.QueryRow(ctx,
			`INSERT INTO users (id, tenant_id, email, name, password_hash, role, password_set)
			 -- id 必须**显式**给：users.id 是 varchar(36) NOT NULL 且没有默认值（见 baseline 迁移）。
			 -- 与邮箱/短信自动注册同一个坑：漏这一列 ⇒ null value in column "id"。
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, FALSE) RETURNING id`,
			provider.TenantID, email, name, string(passwordHash), role).Scan(&user.ID)
		if err != nil {
			return nil, err
		}
		user.Email, user.Name, user.Role = email, name, role
	} else if err != nil {
		return nil, err
	}

	// 携带手机号（微信/钉钉等）时回填；失败仅记录不阻断
	if idToken.Phone != "" {
		if _, err := h.db.Exec(ctx,
			`UPDATE users SET phone = $2, updated_at = NOW() WHERE id = $1 AND (phone IS NULL OR phone = '')`,
			user.ID, idToken.Phone); err != nil {
			// 手机号可能与他人冲突（唯一索引），不阻断登录主流程
			_ = err
		}
	}

	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_user_identities (user_id, provider_id, subject, email)
		 VALUES ($1, $2, $3, $4)`,
		user.ID, provider.ID, idToken.Subject, email); err != nil {
		if isUniqueViolation(err) {
			// 并发场景：另一请求已完成绑定，直接复用既有绑定用户
			if bound, lookupErr := h.findIdentityUser(ctx, provider.ID, idToken.Subject); lookupErr == nil {
				return bound, nil
			}
		}
		return nil, err
	}
	db.AuditLog(ctx, user.ID, provider.TenantID, "sso_provision", "/v1/auth/sso/callback",
		"provider="+provider.Name+" email="+email, r.RemoteAddr, nil)
	return &user, nil
}

// resolveRole 按 provider.role_mapping 将 IdP "roles" claim 映射为本地角色；
// 无命中时缺省 "user"。纯函数，便于单元测试。
func resolveRole(mapping map[string]string, idpRoles []string) string {
	for _, r := range idpRoles {
		if mapped, ok := mapping[r]; ok && mapped != "" {
			return mapped
		}
	}
	return "user"
}
