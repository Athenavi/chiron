package api

import (
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// ── 密码重置 ────────────────────────────────────────────

type passwordResetRequestBody struct {
	Email          string `json:"email"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

// RequestPasswordReset POST /v1/auth/password/reset/request（公开，须套 rlMW）
//
// 响应恒定（不区分邮箱是否存在），避免把本接口变成账号枚举工具；
// 真实原因只写审计日志。
func (h *MailHandler) RequestPasswordReset(w http.ResponseWriter, r *http.Request) {
	var req passwordResetRequestBody
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	email := auth.NormalizeMailAddress(req.Email)
	if !auth.ValidateMailAddress(email) {
		BadRequest(w, "invalid email address")
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
	if row == nil || !row.Enabled || !row.ResetEnabled {
		Forbidden(w, "密码重置未启用")
		return
	}
	base := h.appBaseURL(row)
	if base == "" {
		ServiceUnavailable(w, "未配置站点地址（app_base_url），无法生成重置链接")
		return
	}
	if !h.resetTokens.Available() {
		ServiceUnavailable(w, "密码重置需要 Redis 支持")
		return
	}
	if ok := h.enforceSendLimits(w, r, row, email); !ok {
		return
	}
	// 恒定响应：无论邮箱是否存在都回同一结果
	respond := func() {
		OK(w, map[string]any{"status": "sent", "expire_seconds": resetTTLSeconds(row)})
	}

	var userID string
	err = h.db.QueryRow(ctx,
		`SELECT id FROM users WHERE tenant_id = $1 AND lower(email) = lower($2)`,
		db.DefaultTenantID, email).Scan(&userID)
	if errors.Is(err, pgx.ErrNoRows) {
		db.AuditLog(ctx, "", db.DefaultTenantID, "password_reset_unknown_email",
			r.URL.Path, "email="+maskEmail(email), r.RemoteAddr, nil)
		respond()
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	token, err := auth.GenerateMailToken()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate token failed")
		return
	}
	ttl := time.Duration(resetTTLSeconds(row)) * time.Second
	// Redis 里只存令牌的 SHA-256：即便 Redis 被读走，也无法直接用于重置。
	if err := h.resetTokens.Put(ctx, hashToken(token), userID, ttl); err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}

	resetURL := base + "/reset-password?token=" + url.QueryEscape(token)
	subject, body, err := h.renderTemplate(row.ResetSubject, auth.DefaultMailResetSubject,
		row.ResetBody, auth.DefaultMailResetBody, h.codeVars(row, map[string]string{
			auth.MailVarEmail: email,
			auth.MailVarURL:   resetURL,
		}))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "邮件模板不可用")
		return
	}
	if err := h.sendMail(ctx, row, subject, body, []string{email}); err != nil {
		// 发送失败时撤销令牌，避免留下一个"永远收不到"的有效凭据
		_ = h.resetTokens.Del(ctx, hashToken(token))
		h.auditSendFailure(ctx, r, "password_reset_send_failed", email, err)
		respondMailSendError(w, err, false) // 面向终端用户：不回显服务商细节
		return
	}
	if err := h.store.MarkCooldown(ctx, email, time.Duration(row.SendIntervalSecs)*time.Second); err != nil {
		slog.Warn("mail mark cooldown failed", "error", err)
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "password_reset_requested", r.URL.Path,
		"email="+maskEmail(email), r.RemoteAddr, nil)
	respond()
}

type passwordResetConfirmBody struct {
	Token          string `json:"token"`
	Password       string `json:"password"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

// ConfirmPasswordReset POST /v1/auth/password/reset/confirm（公开，须套 rlMW）
// 校验一次性令牌后重设口令；令牌立即作废（防重放）。
func (h *MailHandler) ConfirmPasswordReset(w http.ResponseWriter, r *http.Request) {
	var req passwordResetConfirmBody
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	token := strings.TrimSpace(req.Token)
	if token == "" || len(token) > 256 {
		BadRequest(w, "重置链接无效")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.Password); !ok {
		BadRequest(w, msg)
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
	if !h.resetTokens.Available() {
		ServiceUnavailable(w, "密码重置需要 Redis 支持")
		return
	}

	ctx := r.Context()
	// 复核开关：管理员关闭密码重置后，此前已发出的链接不应继续可用。
	row, err := h.loadConfig(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil || !row.Enabled || !row.ResetEnabled {
		Forbidden(w, "密码重置未启用")
		return
	}

	userID, err := h.resetTokens.Take(ctx, hashToken(token))
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return
	}
	if userID == "" {
		BadRequest(w, "重置链接已失效，请重新申请")
		return
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "reset password failed")
		return
	}
	tag, err := h.db.Exec(ctx,
		`UPDATE users SET password_hash = $2, password_set = TRUE, updated_at = NOW()
		 WHERE id = $1 AND tenant_id = $3`,
		userID, string(hash), db.DefaultTenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		// 用户已被删除：令牌已在 Take 时作废，无需额外清理
		BadRequest(w, "重置链接已失效，请重新申请")
		return
	}
	db.AuditLog(ctx, userID, db.DefaultTenantID, "password_reset_completed",
		r.URL.Path, "", r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "reset"})
}
