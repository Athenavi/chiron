package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"golang.org/x/crypto/bcrypt"
)

const tokenCookieName = "chiron_token"

// P0-4: Email 鑴辨晱鍑芥暟锛屼繚鐣欏墠3瀛楃+@+鍩熷悕棣栧瓧绗︼紝鍏朵綑鐢?**
func maskEmail(email string) string {
	at := strings.Index(email, "@")
	if at < 1 {
		return "***"
	}
	local := email[:at]
	domain := email[at+1:]
	dot := strings.Index(domain, ".")
	if dot < 1 {
		return "***@" + domain
	}
	if len(local) <= 3 {
		return local[:1] + "***@" + domain[:1] + "***" + domain[dot:]
	}
	return local[:3] + "***@" + domain[:1] + "***" + domain[dot:]
}

// DefaultTenantID is the default tenant ID for single-tenant mode.
const DefaultTenantID = db.DefaultTenantID

type AuthHandler struct {
	auth    *auth.Authenticator
	cfg     *config.Config
	captcha *CaptchaHandler
}

func NewAuthHandler(cfg *config.Config) *AuthHandler {
	return &AuthHandler{
		auth: auth.NewAuthenticator(cfg.JWTSecret, cfg.JWTExpiration),
		cfg:  cfg,
	}
}

// SetCaptchaHandler injects the captcha handler.
func (h *AuthHandler) SetCaptchaHandler(c *CaptchaHandler) {
	h.captcha = c
}

type LoginRequest struct {
	Email          string `json:"email"`
	Password       string `json:"password"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

type UserResponse struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Name  string `json:"name"`
	Role  string `json:"role"`
}

// SetTokenCookie sets the JWT as an HTTP-only secure cookie.
func SetTokenCookie(w http.ResponseWriter, token string, maxAge int, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   maxAge,
	})
}

func ClearTokenCookie(w http.ResponseWriter, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
	})
}

// logLoginFailure 统一的登录失败审计日志+验证码记录
func (h *AuthHandler) logLoginFailure(ctx context.Context, r *http.Request, email, tenantID, remoteAddr string) {
	db.AuditLog(ctx, "", tenantID, "login_failed", "/v1/auth/login", "email="+maskEmail(email), remoteAddr, nil)
	if h.captcha != nil {
		// 必须传真实请求：RecordFailure 会经 clientIP(r) 读 r.RemoteAddr，
		// 传 nil 会让任何登录失败都触发 nil 解引用 panic（表现为 500）。
		h.captcha.RecordFailure(ctx, r)
	}
}

func (h *AuthHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req LoginRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Email == "" || req.Password == "" {
		BadRequest(w, "email and password are required")
		return
	}
	if len(req.Email) > 255 {
		BadRequest(w, "email too long")
		return
	}
	if len(req.Password) > 128 {
		BadRequest(w, "password too long")
		return
	}

	// 浜烘満楠岃瘉鏍呮爮锛氬惎鐢?澶辫触鍗囩骇鏃跺己鍒舵牎楠岋紱杈惧埌纭笂闄愮洿鎺?429
	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	// No dev bypass — always validate against DB
	ctx := r.Context()

	// 设置租户上下文以绕过 RLS
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", DefaultTenantID); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}

	var user UserResponse
	var passwordHash string
	var tenantID string
	err = tx.QueryRow(ctx,
		`SELECT id, email, name, role, tenant_id, password_hash FROM users WHERE email = $1 AND tenant_id = $2`,
		req.Email, DefaultTenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role, &tenantID, &passwordHash)
	if err != nil {
		slog.Warn("login failed", "email", req.Email, "error", err)
		h.logLoginFailure(r.Context(), r, req.Email, DefaultTenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.Password)); err != nil {
		h.logLoginFailure(r.Context(), r, req.Email, tenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	if h.captcha != nil {
		h.captcha.ClearFailures(r.Context(), r)
	}

	// multi-tenant isolation
	// P1-5: tenant_id 为空直接拒绝登录
	if tenantID == "" {
		slog.Warn("login rejected: user has null tenant_id", "user_id", user.ID)
		Unauthorized(w, "user has no tenant binding; contact admin")
		return
	}
	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, tenantID, auth.RolePermissions[user.Role])
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(r.Context(), user.ID, tenantID, "login_success", "/v1/auth/login", "email="+maskEmail(req.Email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

type RegisterRequest struct {
	Email          string `json:"email"`
	Password       string `json:"password"`
	Name           string `json:"name"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

func (h *AuthHandler) Register(w http.ResponseWriter, r *http.Request) {
	if h.cfg.DisableRegistration {
		Forbidden(w, "registration is disabled on this instance")
		return
	}

	var req RegisterRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	// 注册接口防刷：人机验证标杆
	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	if req.Email == "" || req.Password == "" || req.Name == "" {
		BadRequest(w, "email, password, and name are required")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.Password); !ok {
		BadRequest(w, msg)
		return
	}
	if len(req.Email) > 255 || len(req.Name) > 128 {
		BadRequest(w, "email or name too long")
		return
	}

	ctx := r.Context()

	// 璁剧疆绉熸埛涓婁笅鏂囦互缁曡繃 RLS 鈥斺€?蹇呴』鍦ㄤ簨鍔′腑鎵嶈兘璁?SET LOCAL 鎸佺画鐢熸晥
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", DefaultTenantID); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}

	// Check for existing user
	var exists int
	if err := tx.QueryRow(ctx,
		`SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2`,
		req.Email, DefaultTenantID,
	).Scan(&exists); err != nil {
		slog.Error("check existing user", "error", err)
		InternalError(w, "registration failed")
		return
	}
	if exists > 0 {
		BadRequest(w, "email already registered")
		return
	}

	// 首个注册用户成为系统管理员（owner）：以 users 表是否为空判定；
	// 咨询锁保证并发注册时只有一个请求能成为 owner，其余按普通用户落库。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('chiron_first_user'))`); err != nil {
		slog.Error("acquire first-user lock", "error", err)
		InternalError(w, "registration failed")
		return
	}
	var userTotal int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM users`).Scan(&userTotal); err != nil {
		slog.Error("count users for first-user check", "error", err)
		InternalError(w, "registration failed")
		return
	}
	role := "user"
	if userTotal == 0 {
		role = "owner"
		slog.Info("first registered user becomes owner", "email", req.Email)
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "registration failed")
		return
	}

	// 浣跨敤 PostgreSQL 鐨?gen_random_uuid() 鐢熸垚 UUID
	var userID string
	err = tx.QueryRow(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, NOW(), NOW())
		 RETURNING id`,
		DefaultTenantID, req.Email, req.Name, string(hash), role,
	).Scan(&userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "registration failed")
		return
	}

	// 鎻愪氦浜嬪姟
	if err := tx.Commit(ctx); err != nil {
		slog.Error("commit tx", "error", err)
		InternalError(w, "registration failed")
		return
	}

	token, err := h.auth.GenerateToken(userID, req.Email, role, DefaultTenantID, auth.RolePermissions[role])
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	Created(w, map[string]interface{}{
		"token": token,
		"user":  UserResponse{ID: userID, Email: req.Email, Name: req.Name, Role: role},
	})
}

func (h *AuthHandler) Logout(w http.ResponseWriter, r *http.Request) {
	// 鈹€鈹€ JWT 榛戝悕鍗曪細灏嗚 token 鍔犲叆 Redis 榛戝悕鍗曪紝TTL 绛変簬鍓╀綑鏈夋晥鏈?鈹€鈹€
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.ID != "" && db.Redis != nil {
		remaining := time.Until(claims.ExpiresAt.Time)
		if remaining > 0 {
			db.Redis.Set(r.Context(), db.RedisKey("jwt:blacklist:")+claims.ID, "1", remaining)
			// P0-1: 璺ㄥ疄渚嬪悓姝ラ粦鍚嶅崟
			broadcastBlacklistSync(claims.ID)
		}
	}
	// 同步本地缓存，确保本实例后续请求立即拒绝该 token
	if claims := auth.GetClaims(r.Context()); claims != nil {
		markJWTBlacklisted(claims.ID)
	}
	ClearTokenCookie(w, h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "logged out"})
}

func (h *AuthHandler) Profile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var settings map[string]interface{}
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(settings::jsonb, '{}'::jsonb) FROM users WHERE id = $1`, claims.UserID).Scan(&settings); err != nil {
		settings = map[string]interface{}{}
	}
	OK(w, map[string]interface{}{
		"user_id":  claims.UserID,
		"email":    claims.Email,
		"role":     claims.Role,
		"perms":    claims.Perms,
		"settings": settings,
	})
}

// UpdateProfile updates the authenticated user's profile (name/email).
func (h *AuthHandler) UpdateProfile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var body struct {
		Email    string                 `json:"email"`
		Name     string                 `json:"name"`
		Settings map[string]interface{} `json:"settings"` // 鑷畾涔夋崲鑲ょ瓑鐢ㄦ埛璁剧疆锛堝眬閮ㄥ悎骞讹級
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Email == "" && body.Name == "" && body.Settings == nil {
		BadRequest(w, "no fields to update")
		return
	}

	setClauses := ""
	args := []interface{}{}
	argIdx := 1
	if body.Email != "" {
		setClauses += fmt.Sprintf("email = $%d, ", argIdx)
		args = append(args, body.Email)
		argIdx++
	}
	if body.Name != "" {
		setClauses += fmt.Sprintf("name = $%d, ", argIdx)
		args = append(args, body.Name)
		argIdx++
	}
	if body.Settings != nil {
		settingsJSON, err := json.Marshal(body.Settings)
		if err != nil {
			InternalError(w, "invalid settings")
			return
		}
		// 局部合并：settings = settings || $n
		setClauses += fmt.Sprintf("settings = COALESCE(settings::jsonb, '{}'::jsonb) || $%d::jsonb, ", argIdx)
		args = append(args, string(settingsJSON))
		argIdx++
	}
	setClauses = strings.TrimSuffix(setClauses, ", ")
	args = append(args, claims.UserID)

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE users SET %s, updated_at = NOW() WHERE id = $%d", setClauses, argIdx),
		args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update profile failed")
		return
	}

	OK(w, map[string]string{"status": "updated"})
}

type RefreshRequest struct {
	Token string `json:"token"`
}

// Session returns the current session from the httpOnly cookie.
func (h *AuthHandler) Session(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil || cookie.Value == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	claims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil || claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	ctx := r.Context()
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", DefaultTenantID); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}

	var user UserResponse
	if err := tx.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE id = $1 AND tenant_id = $2`,
		claims.UserID, DefaultTenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role); err != nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	OK(w, map[string]interface{}{
		"token": cookie.Value,
		"user":  user,
	})
}

func (h *AuthHandler) Refresh(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil {
		Unauthorized(w, "not authenticated")
		return
	}

	// 1) 校验 token 未被加入黑名单
	oldClaims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}
	if oldClaims.ID != "" && db.Redis != nil {
		if n, err := db.Redis.Exists(r.Context(), db.RedisKey("jwt:blacklist:")+oldClaims.ID).Result(); err == nil && n > 0 {
			Unauthorized(w, "token revoked")
			return
		}
	}

	// 2) 校验用户仍然存在
	var userExists bool
	err = db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM users WHERE id = $1)`, oldClaims.UserID).Scan(&userExists)
	if err != nil || !userExists {
		Unauthorized(w, "user not found")
		return
	}

	newToken, err := h.auth.RefreshToken(cookie.Value)
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}

	SetTokenCookie(w, newToken, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "token refreshed"})
}

func generateID() string {
	return id.NextID()
}
