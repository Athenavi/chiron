package api

import (
	"errors"
	"html/template"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ── 管理端配置 ──────────────────────────────────────────

// GetConfig GET /v1/ent/mail/config（secret 脱敏）。
func (h *MailHandler) GetConfig(w http.ResponseWriter, r *http.Request) {
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil {
		OK(w, h.configResponse(defaultMailConfigRow(), false))
		return
	}
	OK(w, h.configResponse(row, true))
}

// defaultMailConfigRow 返回管理端表单缺省值（不含任何厂商域名）。
func defaultMailConfigRow() *mailConfigRow {
	return &mailConfigRow{
		Provider:         auth.MailProviderSMTP,
		SMTPSecurity:     auth.MailSecurityStartTLS,
		SMTPPort:         587,
		SiteName:         "Chiron",
		WelcomeEnabled:   true,
		CodeTTLSeconds:   300,
		SendIntervalSecs: 60,
		DailyLimit:       10,
		TimeoutSeconds:   30,
	}
}

func (h *MailHandler) configResponse(row *mailConfigRow, exists bool) map[string]any {
	secret := ""
	if row.SMTPPasswordEnc != "" {
		secret = maskedSecret
	}
	apiKey := ""
	if row.APIKeyEnc != "" {
		apiKey = maskedSecret
	}
	return map[string]any{
		"provider":              row.Provider,
		"enabled":               row.Enabled,
		"smtp_host":             row.SMTPHost,
		"smtp_port":             row.SMTPPort,
		"smtp_username":         row.SMTPUsername,
		"smtp_password":         secret,
		"smtp_security":         row.SMTPSecurity,
		"smtp_skip_verify":      row.SMTPSkipVerify,
		"api_base_url":          row.APIBaseURL,
		"api_key":               apiKey,
		"api_channel_id":        row.APIChannelID,
		"api_template_id":       row.APITemplateID,
		"from_address":          row.FromAddress,
		"from_name":             row.FromName,
		"reply_to":              row.ReplyTo,
		"site_name":             row.SiteName,
		"app_base_url":          h.appBaseURL(row),
		"login_enabled":         row.LoginEnabled,
		"register_verify":       row.RegisterVerify,
		"auto_register":         row.AutoRegister,
		"reset_enabled":         row.ResetEnabled,
		"welcome_enabled":       row.WelcomeEnabled,
		"code_subject":          row.CodeSubject,
		"code_body":             row.CodeBody,
		"welcome_subject":       row.WelcomeSubject,
		"welcome_body":          row.WelcomeBody,
		"reset_subject":         row.ResetSubject,
		"reset_body":            row.ResetBody,
		"code_ttl_seconds":      row.CodeTTLSeconds,
		"send_interval_seconds": row.SendIntervalSecs,
		"daily_limit":           row.DailyLimit,
		"timeout_seconds":       row.TimeoutSeconds,
		"exists":                exists,
	}
}

type updateMailConfigRequest struct {
	Provider *string `json:"provider"`
	Enabled  *bool   `json:"enabled"`

	SMTPHost       *string `json:"smtp_host"`
	SMTPPort       *int    `json:"smtp_port"`
	SMTPUsername   *string `json:"smtp_username"`
	SMTPPassword   *string `json:"smtp_password"` // 空串/脱敏占位 = 保留原值
	SMTPSecurity   *string `json:"smtp_security"`
	SMTPSkipVerify *bool   `json:"smtp_skip_verify"`

	APIBaseURL    *string `json:"api_base_url"`
	APIKey        *string `json:"api_key"` // 空串/脱敏占位 = 保留原值
	APIChannelID  *int    `json:"api_channel_id"`
	APITemplateID *int    `json:"api_template_id"`

	FromAddress *string `json:"from_address"`
	FromName    *string `json:"from_name"`
	ReplyTo     *string `json:"reply_to"`

	SiteName   *string `json:"site_name"`
	AppBaseURL *string `json:"app_base_url"`

	LoginEnabled   *bool `json:"login_enabled"`
	RegisterVerify *bool `json:"register_verify"`
	AutoRegister   *bool `json:"auto_register"`
	ResetEnabled   *bool `json:"reset_enabled"`
	WelcomeEnabled *bool `json:"welcome_enabled"`

	CodeSubject    *string `json:"code_subject"`
	CodeBody       *string `json:"code_body"`
	WelcomeSubject *string `json:"welcome_subject"`
	WelcomeBody    *string `json:"welcome_body"`
	ResetSubject   *string `json:"reset_subject"`
	ResetBody      *string `json:"reset_body"`

	CodeTTLSeconds   *int `json:"code_ttl_seconds"`
	SendIntervalSecs *int `json:"send_interval_seconds"`
	DailyLimit       *int `json:"daily_limit"`
	TimeoutSeconds   *int `json:"timeout_seconds"`
}

// UpdateConfig PUT /v1/ent/mail/config（单租户单行 upsert）。
func (h *MailHandler) UpdateConfig(w http.ResponseWriter, r *http.Request) {
	var req updateMailConfigRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	existing, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	row := defaultMailConfigRow()
	if existing != nil {
		*row = *existing
	}
	applyMailConfigPatch(row, &req)

	if !auth.IsKnownMailProvider(row.Provider) {
		BadRequest(w, "unknown mail provider: "+row.Provider)
		return
	}
	if !auth.IsKnownMailSecurity(row.SMTPSecurity) {
		BadRequest(w, "unknown smtp security mode: "+row.SMTPSecurity)
		return
	}
	if msg := validateMailConfigBounds(row); msg != "" {
		BadRequest(w, msg)
		return
	}
	if row.FromAddress != "" && !auth.ValidateMailAddress(row.FromAddress) {
		BadRequest(w, "发件地址格式不正确")
		return
	}
	if row.ReplyTo != "" && !auth.ValidateMailAddress(row.ReplyTo) {
		BadRequest(w, "回复地址格式不正确")
		return
	}
	if row.AppBaseURL != "" && !isHTTPURL(row.AppBaseURL) {
		BadRequest(w, "站点地址需为 http(s):// 开头的 URL")
		return
	}
	if row.APIBaseURL != "" && !isHTTPURL(row.APIBaseURL) {
		BadRequest(w, "邮件服务地址需为 http(s):// 开头的 URL")
		return
	}
	for name, tpl := range map[string]string{
		"code_subject": row.CodeSubject, "code_body": row.CodeBody,
		"welcome_subject": row.WelcomeSubject, "welcome_body": row.WelcomeBody,
		"reset_subject": row.ResetSubject, "reset_body": row.ResetBody,
	} {
		if err := auth.ValidateMailTemplate(tpl); err != nil {
			BadRequest(w, name+" 模板语法错误: "+err.Error())
			return
		}
	}

	// secret：新值加密；空串/占位保留原密文
	smtpPasswordEnc := ""
	apiKeyEnc := ""
	if existing != nil {
		smtpPasswordEnc = existing.SMTPPasswordEnc
		apiKeyEnc = existing.APIKeyEnc
	}
	if req.SMTPPassword != nil && *req.SMTPPassword != "" && *req.SMTPPassword != maskedSecret {
		enc, err := h.encryptSecret(*req.SMTPPassword, "smtp password")
		if err != nil {
			logAndRespond(w, err, http.StatusServiceUnavailable, err.Error())
			return
		}
		smtpPasswordEnc = enc
	}
	if req.APIKey != nil && *req.APIKey != "" && *req.APIKey != maskedSecret {
		enc, err := h.encryptSecret(*req.APIKey, "api key")
		if err != nil {
			logAndRespond(w, err, http.StatusServiceUnavailable, err.Error())
			return
		}
		apiKeyEnc = enc
	}
	row.SMTPPasswordEnc = smtpPasswordEnc
	row.APIKeyEnc = apiKeyEnc

	// 启用前置校验（fail-loud：不允许"看起来启用、实际发不出去"）
	if row.Enabled {
		switch row.Provider {
		case auth.MailProviderSMTP:
			if row.SMTPHost == "" {
				BadRequest(w, "SMTP 服务器地址（smtp_host）必须先配置才能启用")
				return
			}
			if row.FromAddress == "" && row.SMTPUsername == "" {
				BadRequest(w, "请至少配置发件地址（from_address）或 SMTP 用户名")
				return
			}
		case auth.MailProviderQingchen:
			if row.APIBaseURL == "" {
				BadRequest(w, "邮件服务地址（api_base_url）必须先配置才能启用")
				return
			}
			if row.APIKeyEnc == "" {
				BadRequest(w, "API Key 必须先配置才能启用")
				return
			}
		}
	}
	if !row.Enabled {
		if row.LoginEnabled || row.RegisterVerify || row.ResetEnabled {
			BadRequest(w, "邮箱登录/注册验证/密码重置都依赖发送能力（enabled），请先启用邮件服务")
			return
		}
	}

	if err := h.saveConfig(r.Context(), row); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	updated, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if updated == nil {
		logAndRespond(w, errors.New("mail config vanished after upsert"),
			http.StatusInternalServerError, "mail config unavailable")
		return
	}
	OK(w, h.configResponse(updated, true))
}

type mailTestRequest struct {
	To string `json:"to"`
}

// SendTest POST /v1/ent/mail/test —— 用当前配置真实发一封邮件，
// 让管理员在启用前就能确认"地址/端口/凭据/发件人"是否真的能通。
func (h *MailHandler) SendTest(w http.ResponseWriter, r *http.Request) {
	var req mailTestRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	to := auth.NormalizeMailAddress(req.To)
	if !auth.ValidateMailAddress(to) {
		BadRequest(w, "invalid email address")
		return
	}
	row, err := h.loadConfig(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if row == nil {
		BadRequest(w, "邮件服务尚未配置")
		return
	}
	ctx := r.Context()
	subject := "[" + h.siteName(row) + "] 邮件配置测试"
	body := `<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#333">` +
		`<p>这是一封来自 <b>` + template.HTMLEscapeString(h.siteName(row)) + `</b> 的测试邮件。</p>` +
		`<p>收到它说明当前发信配置（通道：` + template.HTMLEscapeString(row.Provider) + `）可以正常工作。</p>` +
		`<p style="color:#999;font-size:12px">发送时间：` + time.Now().Format(time.RFC3339) + `</p></div>`
	if err := h.sendMail(ctx, row, subject, body, []string{to}); err != nil {
		h.auditSendFailure(ctx, r, "mail_test_failed", to, err)
		// 管理端排障：把服务端原始原因带出来（如 HTTP 404 / 401 与服务商提示）
		respondMailSendError(w, err, true)
		return
	}
	db.AuditLog(ctx, "", db.DefaultTenantID, "mail_test_sent", r.URL.Path,
		"email="+maskEmail(to), r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "sent", "to": to})
}
