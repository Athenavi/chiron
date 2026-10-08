package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// ── 公开路由 ────────────────────────────────────────────

// PublicStatus GET /v1/auth/email/status
// 前端据此决定是否展示"邮箱验证码登录"标签页 / 注册页验证码输入框 / 找回密码入口。
func (h *MailHandler) PublicStatus(w http.ResponseWriter, r *http.Request) {
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled {
		OK(w, map[string]any{
			"enabled": false, "login_enabled": false,
			"register_verify": false, "reset_enabled": false,
		})
		return
	}
	OK(w, map[string]any{
		"enabled":         true,
		"login_enabled":   row.LoginEnabled,
		"register_verify": row.RegisterVerify,
		"reset_enabled":   row.ResetEnabled,
	})
}

type sendMailCodeRequest struct {
	Email          string `json:"email"`
	Purpose        string `json:"purpose"` // login（默认）| register
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

// SendCode POST /v1/auth/email/code（公开，须套 rlMW）
// 防滥用：人机验证 + 发送冷却 + 每日上限。
func (h *MailHandler) SendCode(w http.ResponseWriter, r *http.Request) {
	var req sendMailCodeRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	email := auth.NormalizeMailAddress(req.Email)
	if !auth.ValidateMailAddress(email) {
		BadRequest(w, "invalid email address")
		return
	}
	purpose := strings.TrimSpace(req.Purpose)
	if purpose == "" {
		purpose = mailPurposeLogin
	}
	if purpose != mailPurposeLogin && purpose != mailPurposeRegister {
		BadRequest(w, "unsupported purpose")
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

	ctx := r.Context()
	row, err := h.loadConfig(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled {
		Forbidden(w, "邮件服务未启用")
		return
	}
	switch purpose {
	case mailPurposeLogin:
		if !row.LoginEnabled {
			Forbidden(w, "邮箱验证码登录未启用")
			return
		}
	case mailPurposeRegister:
		if !row.RegisterVerify {
			Forbidden(w, "邮箱注册验证未启用")
			return
		}
	}

	// 冷却与每日上限按邮箱统计（与用途无关，避免换用途绕过限流）
	if ok := h.enforceSendLimits(w, r, row, email); !ok {
		return
	}

	code, err := auth.GenerateMailCode(mailCodeDigits)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate code failed")
		return
	}
	subject, body, err := h.renderCodeMail(row, purpose, email, code)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "邮件模板不可用")
		return
	}
	if err := h.sendMail(ctx, row, subject, body, []string{email}); err != nil {
		h.auditSendFailure(ctx, r, "mail_code_send_failed", email, err)
		respondMailSendError(w, err, false) // 面向终端用户：不回显服务商细节
		return
	}

	ttl := time.Duration(row.CodeTTLSeconds) * time.Second
	if err := h.store.SetCode(ctx, mailCodeScope(purpose, email), code, ttl); err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}
	if err := h.store.ResetTries(ctx, mailCodeScope(purpose, email)); err != nil {
		slog.Warn("mail reset tries failed", "error", err)
	}
	if err := h.store.MarkCooldown(ctx, email, time.Duration(row.SendIntervalSecs)*time.Second); err != nil {
		slog.Warn("mail mark cooldown failed", "error", err)
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "mail_code_sent", r.URL.Path,
		"email="+maskEmail(email)+" purpose="+purpose, r.RemoteAddr, nil)
	OK(w, map[string]any{
		"status":         "sent",
		"expire_seconds": row.CodeTTLSeconds,
		"interval":       row.SendIntervalSecs,
	})
}

type mailLoginRequest struct {
	Email          string `json:"email"`
	Code           string `json:"code"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

// Login POST /v1/auth/email/login（公开，须套 rlMW）
func (h *MailHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req mailLoginRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	email := auth.NormalizeMailAddress(req.Email)
	if !auth.ValidateMailAddress(email) {
		BadRequest(w, "invalid email address")
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

	ctx := r.Context()
	row, err := h.loadConfig(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled || !row.LoginEnabled {
		Forbidden(w, "邮箱验证码登录未启用")
		return
	}

	if !h.verifyCode(w, r, mailPurposeLogin, email, req.Code) {
		return
	}

	var user UserResponse
	err = h.db.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND lower(email) = lower($2)`,
		db.DefaultTenantID, email).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		if !row.AutoRegister {
			NotFound(w, "该邮箱未注册")
			return
		}
		user, err = h.provisionMailUser(ctx, email)
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
	db.AuditLog(ctx, "", db.DefaultTenantID, "login_success", "/v1/auth/email/login",
		"email="+maskEmail(email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

// provisionMailUser 自动建号：随机不可登录密码（password_set=FALSE）。
func (h *MailHandler) provisionMailUser(ctx context.Context, email string) (UserResponse, error) {
	randomPassword := make([]byte, 32)
	if _, err := rand.Read(randomPassword); err != nil {
		return UserResponse{}, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(hex.EncodeToString(randomPassword)), auth.BcryptCost)
	if err != nil {
		return UserResponse{}, err
	}
	name := email
	if at := strings.Index(email, "@"); at > 0 {
		name = email[:at]
	}
	var user UserResponse
	err = h.db.QueryRow(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, password_set)
		 -- id 必须**显式**给：users.id 是 varchar(36) NOT NULL 且没有默认值（见 baseline 迁移）。
		 -- 曾漏掉这一列，导致"邮箱验证码登录 + 自动注册"直接 500
		 -- （null value in column "id" violates not-null constraint）。
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, 'user', FALSE)
		 ON CONFLICT DO NOTHING
		 RETURNING id, email, name, role`,
		db.DefaultTenantID, email, name, string(passwordHash)).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		// 并发建号：另一请求已用该邮箱建号，直接复用
		err = h.db.QueryRow(ctx,
			`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND lower(email) = lower($2)`,
			db.DefaultTenantID, email).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	}
	if err != nil {
		return UserResponse{}, err
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "mail_provision", "/v1/auth/email/login",
		"email="+maskEmail(email), "", nil)
	return user, nil
}
