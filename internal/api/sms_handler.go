package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// ── 短信验证码登录 + 手机号绑定 ─────────────────────────
//
// 防滥用四保险（复用既有设施）：
//  1. 人机验证栅栏：发码/登录均过 CaptchaHandler.Enforce（启用/失败升级强制）；
//  2. 发送冷却：同号冷却期内拒绝重发（Redis TTL 标记）；
//  3. 每日上限：同号每日发送次数受限（Redis 24h 计数）；
//  4. 验证码尝试次数：错 5 次作废，需重新获取；登录失败计入 IP 失败计数。

const (
	smsMaxTries      = 5                // 验证码最大尝试次数，超过作废
	smsCodeDigits    = 6                // 验证码位数
	smsMaxDailyLimit = 100              // 每日上限配置上限
	smsMaxCodeTTL    = 15 * time.Minute // 验证码有效期上限
)

// smsConfigRow 是 ent_sms_config 的内存形态（secret 保留密文）。
type smsConfigRow struct {
	Provider         string
	SignName         string
	TemplateID       string
	AccessKeyID      string
	SecretEnc        string
	Endpoint         string
	CodeTTLSeconds   int
	SendIntervalSecs int
	DailyLimit       int
	LoginEnabled     bool
	AutoRegister     bool
	Enabled          bool
}

// SmsHandler 提供短信验证码登录、手机号绑定/解绑与短信服务配置管理。
type SmsHandler struct {
	auth    *auth.Authenticator
	cfg     *config.Config
	db      entQuerier
	encKey  []byte
	sender  auth.SmsSender
	captcha *CaptchaHandler // 可选：nil 跳过人机验证（单测用）
	store   codeStore
}

// NewSmsHandler 构造短信 handler；加密密钥沿用 SSO 密钥，验证码存储依赖 Redis。
func NewSmsHandler(authenticator *auth.Authenticator, cfg *config.Config, captcha *CaptchaHandler) *SmsHandler {
	return &SmsHandler{
		auth:    authenticator,
		cfg:     cfg,
		db:      pgEntStore{},
		encKey:  auth.LoadOIDCEncryptionKey(),
		sender:  auth.NewHTTPSmsSender(),
		captcha: captcha,
		store:   newRedisCodeStore(db.Redis, "sms:"),
	}
}

// RegisterPublicRoutes 挂载公开路由（无 authMW；外层须套 rlMW）。
func (h *SmsHandler) RegisterPublicRoutes(mux *http.ServeMux, rlMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/auth/sms/status", rlMW(http.HandlerFunc(h.PublicStatus)))
	mux.Handle("POST /v1/auth/sms/code", rlMW(http.HandlerFunc(h.SendCode)))
	mux.Handle("POST /v1/auth/sms/login", rlMW(http.HandlerFunc(h.Login)))
}

// RegisterUserRoutes 挂载用户自助路由（authMW）：手机号查询 / 绑定 / 解绑。
func (h *SmsHandler) RegisterUserRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/auth/sms/bind", authMW(http.HandlerFunc(h.GetBind)))
	mux.Handle("POST /v1/auth/sms/bind", authMW(http.HandlerFunc(h.Bind)))
	mux.Handle("DELETE /v1/auth/sms/bind", authMW(http.HandlerFunc(h.Unbind)))
}

// RegisterAdminRoutes 挂载管理路由（authMW + RequireEntPerm("sso:manage")）。
func (h *SmsHandler) RegisterAdminRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	guard := func(hf http.HandlerFunc) http.Handler {
		return authMW(RequireEntPerm("sso:manage")(hf))
	}
	mux.Handle("GET /v1/ent/sms/config", guard(h.GetConfig))
	mux.Handle("PUT /v1/ent/sms/config", guard(h.UpdateConfig))
}

// ── 公开路由 ────────────────────────────────────────────

// PublicStatus GET /v1/auth/sms/status
// 前端据此决定是否展示"短信登录"标签页。
func (h *SmsHandler) PublicStatus(w http.ResponseWriter, r *http.Request) {
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled {
		OK(w, map[string]any{"enabled": false, "login_enabled": false})
		return
	}
	OK(w, map[string]any{"enabled": true, "login_enabled": row.LoginEnabled})
}

type sendSmsCodeRequest struct {
	Phone          string `json:"phone"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
	Purpose        string `json:"purpose"` // login（默认）| bind
}

// SendCode POST /v1/auth/sms/code（公开，须套 rlMW）
// 防滥用：人机验证 + 发送冷却 + 每日上限。
func (h *SmsHandler) SendCode(w http.ResponseWriter, r *http.Request) {
	var req sendSmsCodeRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	phone := auth.NormalizeSmsPhone(req.Phone)
	if !auth.ValidateSmsPhone(phone) {
		BadRequest(w, "invalid phone number")
		return
	}

	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled {
		Forbidden(w, "短信服务未启用")
		return
	}
	if req.Purpose == "login" || req.Purpose == "" {
		if !row.LoginEnabled {
			Forbidden(w, "短信登录未启用")
			return
		}
	}

	ctx := r.Context()
	cool, err := h.store.InCooldown(ctx, phone)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}
	if cool {
		TooManyRequests(w)
		return
	}
	daily, err := h.store.IncrDaily(ctx, phone)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}
	if daily > row.DailyLimit {
		TooManyRequests(w)
		return
	}

	code, err := auth.GenerateSmsCode(smsCodeDigits)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate code failed")
		return
	}

	secret, err := auth.DecryptAESGCM(h.encKey, row.SecretEnc)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sms secret unavailable")
		return
	}
	if err := h.sender.Send(ctx, &auth.SmsConfig{
		Provider:        row.Provider,
		SignName:        row.SignName,
		TemplateID:      row.TemplateID,
		AccessKeyID:     row.AccessKeyID,
		AccessKeySecret: secret,
		Endpoint:        row.Endpoint,
	}, phone, code); err != nil {
		if errors.Is(err, auth.ErrSmsUnreachable) {
			db.AuditLog(ctx, "", db.DefaultTenantID, "sms_send_unreachable", r.URL.Path, "phone="+phone, r.RemoteAddr, nil)
			logAndRespond(w, err, http.StatusBadGateway, "短信服务商不可达")
			return
		}
		db.AuditLog(ctx, "", db.DefaultTenantID, "sms_send_failed", r.URL.Path, "phone="+phone, r.RemoteAddr, nil)
		logAndRespond(w, err, http.StatusBadGateway, "短信发送失败")
		return
	}

	ttl := time.Duration(row.CodeTTLSeconds) * time.Second
	if err := h.store.SetCode(ctx, phone, code, ttl); err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}
	if err := h.store.ResetTries(ctx, phone); err != nil {
		slog.Warn("sms reset tries failed", "error", err)
	}
	interval := time.Duration(row.SendIntervalSecs) * time.Second
	if err := h.store.MarkCooldown(ctx, phone, interval); err != nil {
		slog.Warn("sms mark cooldown failed", "error", err)
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "sms_code_sent", r.URL.Path, "phone="+phone+" purpose="+req.Purpose, r.RemoteAddr, nil)
	OK(w, map[string]any{
		"status":         "sent",
		"expire_seconds": row.CodeTTLSeconds,
		"interval":       row.SendIntervalSecs,
	})
}

type smsLoginRequest struct {
	Phone          string `json:"phone"`
	Code           string `json:"code"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

// Login POST /v1/auth/sms/login（公开，须套 rlMW）
func (h *SmsHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req smsLoginRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	phone := auth.NormalizeSmsPhone(req.Phone)
	if !auth.ValidateSmsPhone(phone) {
		BadRequest(w, "invalid phone number")
		return
	}
	if strings.TrimSpace(req.Code) == "" {
		BadRequest(w, "验证码不能为空")
		return
	}

	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled || !row.LoginEnabled {
		Forbidden(w, "短信登录未启用")
		return
	}

	if !h.verifyCode(w, r, phone, req.Code) {
		return
	}

	ctx := r.Context()
	var user UserResponse
	err = h.db.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND phone = $2`,
		db.DefaultTenantID, phone).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		if !row.AutoRegister {
			NotFound(w, "该手机号未注册")
			return
		}
		user, err = h.provisionSmsUser(ctx, phone)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "auto register failed")
			return
		}
	} else if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	if h.captcha != nil {
		h.captcha.ClearFailures(ctx, r)
	}
	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, db.DefaultTenantID, auth.RolePermissions[user.Role])
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}
	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(ctx, "", db.DefaultTenantID, "login_success", "/v1/auth/sms/login", "phone="+phone, r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

// provisionSmsUser 自动建号：email 以手机号兜底、随机不可登录密码（password_set=FALSE）。
func (h *SmsHandler) provisionSmsUser(ctx context.Context, phone string) (UserResponse, error) {
	randomPassword := make([]byte, 32)
	if _, err := rand.Read(randomPassword); err != nil {
		return UserResponse{}, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(hex.EncodeToString(randomPassword)), auth.BcryptCost)
	if err != nil {
		return UserResponse{}, err
	}
	email := phone + "@sms.local"
	name := "用户" + phone[max(0, len(phone)-4):]
	var user UserResponse
	err = h.db.QueryRow(ctx,
		`INSERT INTO users (tenant_id, email, name, password_hash, role, phone, password_set)
		 VALUES ($1, $2, $3, $4, 'user', $5, FALSE)
		 ON CONFLICT DO NOTHING
		 RETURNING id, email, name, role`,
		db.DefaultTenantID, email, name, string(passwordHash), phone).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		// 并发建号：另一请求已用该手机号建号，直接复用
		err = h.db.QueryRow(ctx,
			`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND phone = $2`,
			db.DefaultTenantID, phone).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	}
	if err != nil {
		return UserResponse{}, err
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "sms_provision", "/v1/auth/sms/login", "phone="+phone, "", nil)
	return user, nil
}

// verifyCode 校验验证码；失败时已写响应并返回 false。
// 错误累计 smsMaxTries 次后作废验证码，并计入 IP 失败计数（触发人机验证升级）。
func (h *SmsHandler) verifyCode(w http.ResponseWriter, r *http.Request, phone, code string) bool {
	ctx := r.Context()
	stored, err := h.store.GetCode(ctx, phone)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return false
	}
	if stored == "" {
		BadRequest(w, "验证码已过期，请重新获取")
		return false
	}
	if stored != strings.TrimSpace(code) {
		tries, terr := h.store.IncrTries(ctx, phone)
		if terr != nil {
			slog.Warn("sms incr tries failed", "error", terr)
		}
		if h.captcha != nil {
			h.captcha.RecordFailure(ctx, r)
		}
		if tries >= smsMaxTries {
			_ = h.store.DelCode(ctx, phone)
			_ = h.store.ResetTries(ctx, phone)
			BadRequest(w, "验证码错误次数过多，请重新获取")
			return false
		}
		BadRequest(w, "验证码错误")
		return false
	}
	// 验证通过即作废（一次性），防重放
	_ = h.store.DelCode(ctx, phone)
	_ = h.store.ResetTries(ctx, phone)
	return true
}

// ── 用户自助：手机号绑定 ────────────────────────────────

// GetBind GET /v1/auth/sms/bind（authMW）返回当前绑定手机号。
func (h *SmsHandler) GetBind(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var phone *string
	err := h.db.QueryRow(r.Context(),
		`SELECT phone FROM users WHERE id = $1`, claims.UserID).Scan(&phone)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	phoneStr := ""
	if phone != nil {
		phoneStr = *phone
	}
	OK(w, map[string]any{"phone": phoneStr, "bound": phoneStr != ""})
}

type smsBindRequest struct {
	Phone string `json:"phone"`
	Code  string `json:"code"`
}

// Bind POST /v1/auth/sms/bind（authMW）验证码校验后绑定手机号。
func (h *SmsHandler) Bind(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req smsBindRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	phone := auth.NormalizeSmsPhone(req.Phone)
	if !auth.ValidateSmsPhone(phone) {
		BadRequest(w, "invalid phone number")
		return
	}
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled {
		Forbidden(w, "短信服务未启用")
		return
	}
	if !h.verifyCode(w, r, phone, req.Code) {
		return
	}

	ctx := r.Context()
	if _, err := h.db.Exec(ctx,
		`UPDATE users SET phone = $2, updated_at = NOW() WHERE id = $1`,
		claims.UserID, phone); err != nil {
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "该手机号已绑定其他账号"})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "sms_bind", "/v1/auth/sms/bind", "phone="+phone, r.RemoteAddr, nil)
	OK(w, map[string]any{"status": "bound", "phone": phone})
}

// Unbind DELETE /v1/auth/sms/bind（authMW）
// 守卫：无口令密码且无三方身份时拒绝解绑（保留至少一种登录方式）。
func (h *SmsHandler) Unbind(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	ctx := r.Context()
	var phone *string
	var passwordSet bool
	var identityCount int
	err := h.db.QueryRow(ctx,
		`SELECT u.phone, u.password_set,
		        (SELECT COUNT(*) FROM ent_user_identities ui WHERE ui.user_id = u.id)
		 FROM users u WHERE u.id = $1`, claims.UserID).
		Scan(&phone, &passwordSet, &identityCount)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if phone == nil || *phone == "" {
		BadRequest(w, "当前账号未绑定手机号")
		return
	}
	if !passwordSet && identityCount == 0 {
		Forbidden(w, "解绑后将无可用登录方式，请先设置密码或绑定其他登录方式")
		return
	}
	if _, err := h.db.Exec(ctx,
		`UPDATE users SET phone = NULL, updated_at = NOW() WHERE id = $1`, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "sms_unbind", "/v1/auth/sms/bind", "phone="+*phone, r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "unbound"})
}

// ── 管理端配置 ──────────────────────────────────────────

// GetConfig GET /v1/ent/sms/config（secret 脱敏）。
func (h *SmsHandler) GetConfig(w http.ResponseWriter, r *http.Request) {
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil {
		OK(w, h.configResponse(&smsConfigRow{
			Provider: auth.SmsAliyun, CodeTTLSeconds: 300, SendIntervalSecs: 60, DailyLimit: 10,
		}, false))
		return
	}
	OK(w, h.configResponse(row, true))
}

func (h *SmsHandler) configResponse(row *smsConfigRow, exists bool) map[string]any {
	return map[string]any{
		"provider":              row.Provider,
		"sign_name":             row.SignName,
		"template_id":           row.TemplateID,
		"access_key_id":         row.AccessKeyID,
		"secret":                maskedSecret,
		"endpoint":              row.Endpoint,
		"code_ttl_seconds":      row.CodeTTLSeconds,
		"send_interval_seconds": row.SendIntervalSecs,
		"daily_limit":           row.DailyLimit,
		"login_enabled":         row.LoginEnabled,
		"auto_register":         row.AutoRegister,
		"enabled":               row.Enabled,
		"exists":                exists,
	}
}

type updateSmsConfigRequest struct {
	Provider         *string `json:"provider"`
	SignName         *string `json:"sign_name"`
	TemplateID       *string `json:"template_id"`
	AccessKeyID      *string `json:"access_key_id"`
	Secret           *string `json:"secret"` // 空串/脱敏占位 = 保留原值
	Endpoint         *string `json:"endpoint"`
	CodeTTLSeconds   *int    `json:"code_ttl_seconds"`
	SendIntervalSecs *int    `json:"send_interval_seconds"`
	DailyLimit       *int    `json:"daily_limit"`
	LoginEnabled     *bool   `json:"login_enabled"`
	AutoRegister     *bool   `json:"auto_register"`
	Enabled          *bool   `json:"enabled"`
}

// UpdateConfig PUT /v1/ent/sms/config（单租户单行 upsert）。
func (h *SmsHandler) UpdateConfig(w http.ResponseWriter, r *http.Request) {
	var req updateSmsConfigRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	existing, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 以既有值（或缺省）为底，逐字段覆盖
	row := &smsConfigRow{
		Provider: auth.SmsAliyun, CodeTTLSeconds: 300, SendIntervalSecs: 60, DailyLimit: 10,
	}
	if existing != nil {
		*row = *existing
	}
	if req.Provider != nil {
		row.Provider = strings.TrimSpace(*req.Provider)
	}
	if !auth.IsKnownSmsProvider(row.Provider) {
		BadRequest(w, "unknown sms provider: "+row.Provider)
		return
	}
	if req.SignName != nil {
		row.SignName = strings.TrimSpace(*req.SignName)
	}
	if req.TemplateID != nil {
		row.TemplateID = strings.TrimSpace(*req.TemplateID)
	}
	if req.AccessKeyID != nil {
		row.AccessKeyID = strings.TrimSpace(*req.AccessKeyID)
	}
	if req.Endpoint != nil {
		row.Endpoint = strings.TrimSpace(*req.Endpoint)
	}
	if req.CodeTTLSeconds != nil {
		row.CodeTTLSeconds = *req.CodeTTLSeconds
	}
	if req.SendIntervalSecs != nil {
		row.SendIntervalSecs = *req.SendIntervalSecs
	}
	if req.DailyLimit != nil {
		row.DailyLimit = *req.DailyLimit
	}
	if req.LoginEnabled != nil {
		row.LoginEnabled = *req.LoginEnabled
	}
	if req.AutoRegister != nil {
		row.AutoRegister = *req.AutoRegister
	}
	if req.Enabled != nil {
		row.Enabled = *req.Enabled
	}

	// secret：新值加密；空串/占位保留原密文
	secretEnc := ""
	if existing != nil {
		secretEnc = existing.SecretEnc
	}
	if req.Secret != nil && *req.Secret != "" && *req.Secret != maskedSecret {
		if h.encKey == nil {
			ServiceUnavailable(w, "sms is not configured: "+auth.EnvOIDCSecretKey+" missing")
			return
		}
		enc, err := auth.EncryptAESGCM(h.encKey, *req.Secret)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "encrypt sms secret failed")
			return
		}
		secretEnc = enc
	}

	// 数值边界
	if row.CodeTTLSeconds < 60 || row.CodeTTLSeconds > int(smsMaxCodeTTL.Seconds()) {
		BadRequest(w, fmt.Sprintf("code_ttl_seconds 需在 60-%d 之间", int(smsMaxCodeTTL.Seconds())))
		return
	}
	if row.SendIntervalSecs < 0 || row.SendIntervalSecs > 3600 {
		BadRequest(w, "send_interval_seconds 需在 0-3600 之间")
		return
	}
	if row.DailyLimit < 1 || row.DailyLimit > smsMaxDailyLimit {
		BadRequest(w, fmt.Sprintf("daily_limit 需在 1-%d 之间", smsMaxDailyLimit))
		return
	}
	if len(row.SignName) > 64 || len(row.TemplateID) > 64 || len(row.AccessKeyID) > 256 || len(row.Endpoint) > 512 {
		BadRequest(w, "field too long")
		return
	}

	// 启用前置校验（fail-loud）
	if row.Enabled {
		if secretEnc == "" {
			BadRequest(w, "短信 AccessKeySecret 必须先配置才能启用")
			return
		}
		if row.Provider != auth.SmsCustom {
			if row.SignName == "" {
				BadRequest(w, "短信签名（sign_name）必须先配置才能启用")
				return
			}
			if row.TemplateID == "" {
				BadRequest(w, "短信模板 ID（template_id）必须先配置才能启用")
				return
			}
		}
		if row.Provider == auth.SmsCustom && row.Endpoint == "" {
			BadRequest(w, "custom 短信服务必须配置发送端点（endpoint）")
			return
		}
	}
	if row.LoginEnabled && !row.Enabled {
		BadRequest(w, "短信登录（login_enabled）依赖发送能力（enabled），请先启用短信服务")
		return
	}

	if _, err := h.db.Exec(r.Context(),
		`INSERT INTO ent_sms_config (tenant_id, provider, sign_name, template_id, access_key_id,
		                            secret_enc, endpoint, code_ttl_seconds, send_interval_seconds,
		                            daily_limit, login_enabled, auto_register, enabled)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
		 ON CONFLICT (tenant_id) DO UPDATE SET
		   provider = EXCLUDED.provider, sign_name = EXCLUDED.sign_name,
		   template_id = EXCLUDED.template_id, access_key_id = EXCLUDED.access_key_id,
		   secret_enc = EXCLUDED.secret_enc, endpoint = EXCLUDED.endpoint,
		   code_ttl_seconds = EXCLUDED.code_ttl_seconds,
		   send_interval_seconds = EXCLUDED.send_interval_seconds,
		   daily_limit = EXCLUDED.daily_limit, login_enabled = EXCLUDED.login_enabled,
		   auto_register = EXCLUDED.auto_register, enabled = EXCLUDED.enabled,
		   updated_at = NOW()`,
		db.DefaultTenantID, row.Provider, row.SignName, row.TemplateID, row.AccessKeyID,
		secretEnc, nullString(row.Endpoint), row.CodeTTLSeconds, row.SendIntervalSecs,
		row.DailyLimit, row.LoginEnabled, row.AutoRegister, row.Enabled); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	updated, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if updated == nil {
		logAndRespond(w, errors.New("sms config vanished after upsert"),
			http.StatusInternalServerError, "sms config unavailable")
		return
	}
	OK(w, h.configResponse(updated, true))
}

// ── 内部 ────────────────────────────────────────────────

func (h *SmsHandler) loadConfig(ctx context.Context) (*smsConfigRow, error) {
	var row smsConfigRow
	var endpoint *string
	err := h.db.QueryRow(ctx,
		`SELECT provider, sign_name, template_id, access_key_id, secret_enc, endpoint,
		        code_ttl_seconds, send_interval_seconds, daily_limit,
		        login_enabled, auto_register, enabled
		 FROM ent_sms_config WHERE tenant_id = $1`, db.DefaultTenantID).
		Scan(&row.Provider, &row.SignName, &row.TemplateID, &row.AccessKeyID, &row.SecretEnc,
			&endpoint, &row.CodeTTLSeconds, &row.SendIntervalSecs, &row.DailyLimit,
			&row.LoginEnabled, &row.AutoRegister, &row.Enabled)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if endpoint != nil {
		row.Endpoint = *endpoint
	}
	return &row, nil
}
