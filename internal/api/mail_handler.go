package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// ── 邮件（验证码登录 / 注册验证 / 密码重置 / 后台配置）──
//
// 与短信通道同构：同样的四道防滥用闸门（人机验证 → 发送冷却 → 每日上限 →
// 尝试次数），只是投递通道换成邮件。发信通道本身（SMTP / 晴辰云邮）在
// internal/auth/mail.go 里抽象，服务器地址、凭据、模板全部来自 ent_mail_config，
// **代码中不存在任何厂商地址**：后台改配置即换服务商。

const (
	mailCodeDigits    = 6                // 验证码位数
	mailMaxTries      = 5                // 验证码最大尝试次数，超过作废
	mailMaxDailyLimit = 100              // 每日发送上限的配置上限
	mailMaxCodeTTL    = 15 * time.Minute // 验证码有效期配置上限
	mailMinResetTTL   = 5 * time.Minute  // 重置链接最短有效期
)

// 邮件动作类型。同时作为 Redis 键的一部分：不同用途的验证码互不覆盖。
const (
	mailPurposeLogin    = "login"
	mailPurposeRegister = "register"
	mailPurposeReset    = "reset"
)

// mailResetKeyPrefix 重置令牌的 Redis 键前缀（值 = 用户 ID，TTL 即链接有效期）。
var mailResetKeyPrefix = db.RedisKey("mail:reset:")

// errCodeStoreUnavailable 验证码存储（Redis）不可用。
var errCodeStoreUnavailable = errors.New("验证码存储不可用")

// mailConfigRow 是 ent_mail_config 的内存形态（secret 保留密文）。
type mailConfigRow struct {
	Provider string
	Enabled  bool

	// SMTP
	SMTPHost        string
	SMTPPort        int
	SMTPUsername    string
	SMTPPasswordEnc string
	SMTPSecurity    string
	SMTPSkipVerify  bool

	// 晴辰云邮（HTTP API）
	APIBaseURL    string
	APIKeyEnc     string
	APIChannelID  int
	APITemplateID int

	// 发件身份
	FromAddress string
	FromName    string
	ReplyTo     string

	// 站点信息（用于模板变量与重置链接）
	SiteName   string
	AppBaseURL string

	// 能力开关
	LoginEnabled   bool
	RegisterVerify bool
	AutoRegister   bool
	ResetEnabled   bool
	WelcomeEnabled bool

	// 模板（留空回落内置默认）
	CodeSubject    string
	CodeBody       string
	WelcomeSubject string
	WelcomeBody    string
	ResetSubject   string
	ResetBody      string

	// 限流与超时
	CodeTTLSeconds   int
	SendIntervalSecs int
	DailyLimit       int
	TimeoutSeconds   int
}

// MailHandler 提供邮箱验证码登录、注册邮箱校验、密码重置与邮件服务配置管理。
type MailHandler struct {
	auth        *auth.Authenticator
	cfg         *config.Config
	db          entQuerier
	encKey      []byte
	sender      auth.MailSender
	captcha     *CaptchaHandler // 可选：nil 跳过人机验证（单测用）
	store       codeStore
	resetTokens resetTokenStore
}

// NewMailHandler 构造邮件 handler；加密密钥沿用 SSO 密钥，验证码存储依赖 Redis。
func NewMailHandler(authenticator *auth.Authenticator, cfg *config.Config, captcha *CaptchaHandler) *MailHandler {
	return &MailHandler{
		auth:        authenticator,
		cfg:         cfg,
		db:          pgEntStore{},
		encKey:      auth.LoadOIDCEncryptionKey(),
		sender:      auth.NewHTTPMailSender(),
		captcha:     captcha,
		store:       newRedisCodeStore(db.Redis, "mail:"),
		resetTokens: redisResetTokenStore{rdb: db.Redis},
	}
}

// RegisterPublicRoutes 挂载公开路由（无 authMW；外层须套 rlMW）。
func (h *MailHandler) RegisterPublicRoutes(mux *http.ServeMux, rlMW func(http.Handler) http.Handler) {
	mux.Handle("GET /v1/auth/email/status", rlMW(http.HandlerFunc(h.PublicStatus)))
	mux.Handle("POST /v1/auth/email/code", rlMW(http.HandlerFunc(h.SendCode)))
	mux.Handle("POST /v1/auth/email/login", rlMW(http.HandlerFunc(h.Login)))
	mux.Handle("POST /v1/auth/password/reset/request", rlMW(http.HandlerFunc(h.RequestPasswordReset)))
	mux.Handle("POST /v1/auth/password/reset/confirm", rlMW(http.HandlerFunc(h.ConfirmPasswordReset)))
}

// RegisterAdminRoutes 挂载管理路由（authMW + RequireEntPerm("sso:manage")）。
func (h *MailHandler) RegisterAdminRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	guard := func(hf http.HandlerFunc) http.Handler {
		return authMW(RequireEntPerm("sso:manage")(hf))
	}
	mux.Handle("GET /v1/ent/mail/config", guard(h.GetConfig))
	mux.Handle("PUT /v1/ent/mail/config", guard(h.UpdateConfig))
	mux.Handle("POST /v1/ent/mail/test", guard(h.SendTest))
}

// ── 供 AuthHandler（注册流程）复用的能力 ────────────────

// RegisterVerifyEnabled 报告"注册是否必须通过邮箱验证码"。
func (h *MailHandler) RegisterVerifyEnabled(ctx context.Context) (bool, error) {
	row, err := h.loadConfig(ctx)
	if err != nil {
		return false, err
	}
	return row != nil && row.Enabled && row.RegisterVerify, nil
}

// ConsumeCode 校验并一次性消费某用途的验证码；失败返回可直接展示给用户的错误。
func (h *MailHandler) ConsumeCode(ctx context.Context, purpose, email, code string) error {
	scoped := mailCodeScope(purpose, auth.NormalizeMailAddress(email))
	stored, err := h.store.GetCode(ctx, scoped)
	if err != nil {
		return errCodeStoreUnavailable
	}
	if stored == "" {
		return errors.New("验证码已过期，请重新获取")
	}
	if stored != strings.TrimSpace(code) {
		tries, terr := h.store.IncrTries(ctx, scoped)
		if terr != nil {
			slog.Warn("mail incr tries failed", "error", terr)
		}
		if tries >= mailMaxTries {
			_ = h.store.DelCode(ctx, scoped)
			_ = h.store.ResetTries(ctx, scoped)
			return errors.New("验证码错误次数过多，请重新获取")
		}
		return errors.New("验证码错误")
	}
	// 验证通过即作废（一次性），防重放
	_ = h.store.DelCode(ctx, scoped)
	_ = h.store.ResetTries(ctx, scoped)
	return nil
}

// SendWelcomeAsync 注册成功后异步发送欢迎邮件（不阻塞注册响应，失败只记日志）。
func (h *MailHandler) SendWelcomeAsync(email, name string) {
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		row, err := h.loadConfig(ctx)
		if err != nil {
			slog.Warn("welcome mail: load config failed", "error", err)
			return
		}
		if row == nil || !row.Enabled || !row.WelcomeEnabled {
			return
		}
		subject, body, err := h.renderTemplate(row.WelcomeSubject, auth.DefaultMailWelcomeSubject,
			row.WelcomeBody, auth.DefaultMailWelcomeBody, h.codeVars(row, map[string]string{
				auth.MailVarEmail: email,
				auth.MailVarName:  name,
				auth.MailVarURL:   h.appBaseURL(row),
			}))
		if err != nil {
			slog.Warn("welcome mail: render failed", "error", err)
			return
		}
		if err := h.sendMail(ctx, row, subject, body, []string{email}); err != nil {
			slog.Warn("welcome mail: send failed", "email", maskEmail(email), "error", err)
			db.AuditLog(ctx, "", db.DefaultTenantID, "mail_welcome_failed", "/v1/auth/register",
				"email="+maskEmail(email), "", nil)
			return
		}
		db.AuditLog(ctx, "", db.DefaultTenantID, "mail_welcome_sent", "/v1/auth/register",
			"email="+maskEmail(email), "", nil)
	}()
}

// ── 内部：发送与模板 ────────────────────────────────────

// sendMail 按当前配置渲染好的内容投递一封邮件。
func (h *MailHandler) sendMail(ctx context.Context, row *mailConfigRow, subject, body string, to []string) error {
	mc, err := h.senderConfig(row)
	if err != nil {
		return err
	}
	msg := &auth.MailMessage{
		To:      to,
		Subject: subject,
		HTML:    body,
		From:    row.FromAddress,
		ReplyTo: row.ReplyTo,
	}
	// 模板/通道参数只对 HTTP API 通道（晴辰云邮）生效；SMTP 侧自建 MIME。
	if row.Provider == auth.MailProviderQingchen {
		msg.TemplateID = row.APITemplateID
		msg.ChannelID = row.APIChannelID
	}
	return h.sender.Send(ctx, mc, msg)
}

// senderConfig 把库里的配置（含密文）映射为发送器配置。
func (h *MailHandler) senderConfig(row *mailConfigRow) (*auth.MailConfig, error) {
	mc := &auth.MailConfig{
		Provider:           row.Provider,
		Host:               row.SMTPHost,
		Port:               row.SMTPPort,
		Username:           row.SMTPUsername,
		Security:           row.SMTPSecurity,
		InsecureSkipVerify: row.SMTPSkipVerify,
		BaseURL:            row.APIBaseURL,
		From:               row.FromAddress,
		FromName:           row.FromName,
		ReplyTo:            row.ReplyTo,
		TimeoutSeconds:     row.TimeoutSeconds,
	}
	if row.SMTPPasswordEnc != "" {
		pw, err := h.decryptSecret(row.SMTPPasswordEnc)
		if err != nil {
			return nil, err
		}
		mc.Password = pw
	}
	if row.APIKeyEnc != "" {
		key, err := h.decryptSecret(row.APIKeyEnc)
		if err != nil {
			return nil, err
		}
		mc.APIKey = key
	}
	return mc, nil
}

func (h *MailHandler) encryptSecret(plain, what string) (string, error) {
	if h.encKey == nil {
		return "", fmt.Errorf("邮件 %s 无法保存：未配置 %s", what, auth.EnvOIDCSecretKey)
	}
	enc, err := auth.EncryptAESGCM(h.encKey, plain)
	if err != nil {
		return "", fmt.Errorf("encrypt mail %s: %w", what, err)
	}
	return enc, nil
}

func (h *MailHandler) decryptSecret(enc string) (string, error) {
	plain, err := auth.DecryptAESGCM(h.encKey, enc)
	if err != nil {
		return "", fmt.Errorf("邮件凭据解密失败（%s 是否变更？）: %w", auth.EnvOIDCSecretKey, err)
	}
	return plain, nil
}

// renderCodeMail 渲染验证码邮件（主题/正文可被后台覆盖）。
func (h *MailHandler) renderCodeMail(row *mailConfigRow, purpose, email, code string) (string, string, error) {
	return h.renderTemplate(row.CodeSubject, auth.DefaultMailCodeSubject,
		row.CodeBody, auth.DefaultMailCodeBody, h.codeVars(row, map[string]string{
			auth.MailVarCode:   code,
			auth.MailVarEmail:  email,
			auth.MailVarAction: mailActionLabel(purpose),
		}))
}

// renderTemplate 渲染模板对（空模板回落内置默认），并注入公共变量。
func (h *MailHandler) renderTemplate(subjectTpl, defaultSubject, bodyTpl, defaultBody string,
	vars map[string]string) (string, string, error) {
	if strings.TrimSpace(subjectTpl) == "" {
		subjectTpl = defaultSubject
	}
	if strings.TrimSpace(bodyTpl) == "" {
		bodyTpl = defaultBody
	}
	return auth.RenderMailTemplate(subjectTpl, bodyTpl, vars)
}

// codeVars 组装模板公共变量（站点名 / 验证码有效期）。
func (h *MailHandler) codeVars(row *mailConfigRow, extra map[string]string) map[string]string {
	vars := map[string]string{
		auth.MailVarSiteName:   h.siteName(row),
		auth.MailVarTTLMinutes: strconv.Itoa(ttlMinutes(row.CodeTTLSeconds)),
	}
	for k, v := range extra {
		vars[k] = v
	}
	return vars
}

func (h *MailHandler) siteName(row *mailConfigRow) string {
	if name := strings.TrimSpace(row.SiteName); name != "" {
		return name
	}
	return "Chiron"
}

// appBaseURL 返回站点地址：优先后台配置，其次部署期 FRONTEND_URL。
// 邮件里的链接（重置密码 / 回到站点）都以它拼接。
func (h *MailHandler) appBaseURL(row *mailConfigRow) string {
	base := strings.TrimSpace(row.AppBaseURL)
	if base == "" && h.cfg != nil {
		base = strings.TrimSpace(h.cfg.FrontendURL)
	}
	return strings.TrimRight(base, "/")
}

// enforceSendLimits 冷却 + 每日上限；超限时已写响应并返回 false。
func (h *MailHandler) enforceSendLimits(w http.ResponseWriter, r *http.Request, row *mailConfigRow, email string) bool {
	ctx := r.Context()
	cool, err := h.store.InCooldown(ctx, email)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return false
	}
	if cool {
		TooManyRequests(w)
		return false
	}
	daily, err := h.store.IncrDaily(ctx, email)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return false
	}
	if daily > row.DailyLimit {
		TooManyRequests(w)
		return false
	}
	return true
}

// verifyCode 校验验证码；失败时已写响应并返回 false。
func (h *MailHandler) verifyCode(w http.ResponseWriter, r *http.Request, purpose, email, code string) bool {
	err := h.ConsumeCode(r.Context(), purpose, email, code)
	if err == nil {
		return true
	}
	if errors.Is(err, errCodeStoreUnavailable) {
		logAndRespond(w, err, http.StatusServiceUnavailable, "验证码存储不可用")
		return false
	}
	// 错误计入 IP 失败计数（触发人机验证升级）
	if h.captcha != nil {
		h.captcha.RecordFailure(r.Context(), r)
	}
	BadRequest(w, err.Error())
	return false
}

func (h *MailHandler) auditSendFailure(ctx context.Context, r *http.Request, action, email string, err error) {
	slog.Error("mail send failed", "action", action, "email", maskEmail(email), "error", err)
	db.AuditLog(ctx, "", db.DefaultTenantID, action, r.URL.Path,
		"email="+maskEmail(email), r.RemoteAddr, nil)
}

// respondMailSendError 把发送器错误映射为 HTTP 状态。
//
// verbose=true（管理端「测试发信」）时把服务端原始原因一并透出，例如
// 「邮件发送失败：HTTP 404: 404 page not found」——否则管理员只看到「邮件发送失败」，
// 无从判断是地址写错、密钥无效还是服务商模板不存在。
// 面向终端用户的路径（验证码、重置邮件）保持通用文案，不泄露内部拓扑与服务商细节。
func respondMailSendError(w http.ResponseWriter, err error, verbose bool) {
	switch {
	case errors.Is(err, auth.ErrMailUnreachable):
		msg := "邮件服务不可达"
		if verbose {
			msg += "：" + err.Error()
		}
		logAndRespond(w, err, http.StatusBadGateway, msg)
	case errors.Is(err, auth.ErrMailConfigInvalid):
		// 配置类错误本身就是给管理员看的（如「HTTP 通道未配置服务地址（base_url）」）
		logAndRespond(w, err, http.StatusBadRequest, err.Error())
	default:
		msg := "邮件发送失败"
		if verbose {
			msg += "：" + err.Error()
			// 404 几乎总是"服务地址少了路径前缀"（如 docs/mail.md 的 /api/v1），
			// 直接给出可操作建议，省去一次来回排查。
			if strings.Contains(err.Error(), "HTTP 404") {
				msg += "（请确认服务地址与服务商文档完全一致，包含路径前缀，例如 /api/v1）"
			}
		}
		logAndRespond(w, err, http.StatusBadGateway, msg)
	}
}

// ── 内部：持久化 ────────────────────────────────────────

const mailConfigColumns = `provider, enabled, smtp_host, smtp_port, smtp_username, smtp_password_enc,
	smtp_security, smtp_skip_verify, api_base_url, api_key_enc, api_channel_id, api_template_id,
	from_address, from_name, reply_to, site_name, app_base_url,
	login_enabled, register_verify, auto_register, reset_enabled, welcome_enabled,
	code_subject, code_body, welcome_subject, welcome_body, reset_subject, reset_body,
	code_ttl_seconds, send_interval_seconds, daily_limit, timeout_seconds`

func (h *MailHandler) loadConfig(ctx context.Context) (*mailConfigRow, error) {
	var row mailConfigRow
	var smtpHost, smtpUser, smtpPassword, smtpSecurity, apiBaseURL, apiKey *string
	var fromAddress, fromName, replyTo, siteName, appBaseURL *string
	var codeSubject, codeBody, welcomeSubject, welcomeBody, resetSubject, resetBody *string
	err := h.db.QueryRow(ctx,
		`SELECT `+mailConfigColumns+` FROM ent_mail_config WHERE tenant_id = $1`,
		db.DefaultTenantID).
		Scan(&row.Provider, &row.Enabled, &smtpHost, &row.SMTPPort, &smtpUser, &smtpPassword,
			&smtpSecurity, &row.SMTPSkipVerify, &apiBaseURL, &apiKey, &row.APIChannelID, &row.APITemplateID,
			&fromAddress, &fromName, &replyTo, &siteName, &appBaseURL,
			&row.LoginEnabled, &row.RegisterVerify, &row.AutoRegister, &row.ResetEnabled, &row.WelcomeEnabled,
			&codeSubject, &codeBody, &welcomeSubject, &welcomeBody, &resetSubject, &resetBody,
			&row.CodeTTLSeconds, &row.SendIntervalSecs, &row.DailyLimit, &row.TimeoutSeconds)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	row.SMTPHost = derefString(smtpHost)
	row.SMTPUsername = derefString(smtpUser)
	row.SMTPPasswordEnc = derefString(smtpPassword)
	row.SMTPSecurity = derefString(smtpSecurity)
	if row.SMTPSecurity == "" {
		row.SMTPSecurity = auth.MailSecurityStartTLS
	}
	row.APIBaseURL = derefString(apiBaseURL)
	row.APIKeyEnc = derefString(apiKey)
	row.FromAddress = derefString(fromAddress)
	row.FromName = derefString(fromName)
	row.ReplyTo = derefString(replyTo)
	row.SiteName = derefString(siteName)
	row.AppBaseURL = derefString(appBaseURL)
	row.CodeSubject = derefString(codeSubject)
	row.CodeBody = derefString(codeBody)
	row.WelcomeSubject = derefString(welcomeSubject)
	row.WelcomeBody = derefString(welcomeBody)
	row.ResetSubject = derefString(resetSubject)
	row.ResetBody = derefString(resetBody)
	return &row, nil
}

func (h *MailHandler) saveConfig(ctx context.Context, row *mailConfigRow) error {
	args := pgx.NamedArgs{
		"id":                    newUUID(),
		"tenant_id":             db.DefaultTenantID,
		"provider":              row.Provider,
		"enabled":               row.Enabled,
		"smtp_host":             nullString(row.SMTPHost),
		"smtp_port":             row.SMTPPort,
		"smtp_username":         nullString(row.SMTPUsername),
		"smtp_password_enc":     nullString(row.SMTPPasswordEnc),
		"smtp_security":         row.SMTPSecurity,
		"smtp_skip_verify":      row.SMTPSkipVerify,
		"api_base_url":          nullString(row.APIBaseURL),
		"api_key_enc":           nullString(row.APIKeyEnc),
		"api_channel_id":        row.APIChannelID,
		"api_template_id":       row.APITemplateID,
		"from_address":          nullString(row.FromAddress),
		"from_name":             nullString(row.FromName),
		"reply_to":              nullString(row.ReplyTo),
		"site_name":             nullString(row.SiteName),
		"app_base_url":          nullString(row.AppBaseURL),
		"login_enabled":         row.LoginEnabled,
		"register_verify":       row.RegisterVerify,
		"auto_register":         row.AutoRegister,
		"reset_enabled":         row.ResetEnabled,
		"welcome_enabled":       row.WelcomeEnabled,
		"code_subject":          nullString(row.CodeSubject),
		"code_body":             nullString(row.CodeBody),
		"welcome_subject":       nullString(row.WelcomeSubject),
		"welcome_body":          nullString(row.WelcomeBody),
		"reset_subject":         nullString(row.ResetSubject),
		"reset_body":            nullString(row.ResetBody),
		"code_ttl_seconds":      row.CodeTTLSeconds,
		"send_interval_seconds": row.SendIntervalSecs,
		"daily_limit":           row.DailyLimit,
		"timeout_seconds":       row.TimeoutSeconds,
	}
	_, err := h.db.Exec(ctx,
		`INSERT INTO ent_mail_config (id, tenant_id, `+mailConfigColumns+`, created_at, updated_at)
		 VALUES (@id, @tenant_id, @provider, @enabled, @smtp_host, @smtp_port, @smtp_username,
		         @smtp_password_enc, @smtp_security, @smtp_skip_verify, @api_base_url, @api_key_enc,
		         @api_channel_id, @api_template_id, @from_address, @from_name, @reply_to, @site_name,
		         @app_base_url, @login_enabled, @register_verify, @auto_register, @reset_enabled,
		         @welcome_enabled, @code_subject, @code_body, @welcome_subject, @welcome_body,
		         @reset_subject, @reset_body, @code_ttl_seconds, @send_interval_seconds,
		         @daily_limit, @timeout_seconds, NOW(), NOW())
		 ON CONFLICT (tenant_id) DO UPDATE SET
		   provider = EXCLUDED.provider, enabled = EXCLUDED.enabled,
		   smtp_host = EXCLUDED.smtp_host, smtp_port = EXCLUDED.smtp_port,
		   smtp_username = EXCLUDED.smtp_username, smtp_password_enc = EXCLUDED.smtp_password_enc,
		   smtp_security = EXCLUDED.smtp_security, smtp_skip_verify = EXCLUDED.smtp_skip_verify,
		   api_base_url = EXCLUDED.api_base_url, api_key_enc = EXCLUDED.api_key_enc,
		   api_channel_id = EXCLUDED.api_channel_id, api_template_id = EXCLUDED.api_template_id,
		   from_address = EXCLUDED.from_address, from_name = EXCLUDED.from_name,
		   reply_to = EXCLUDED.reply_to, site_name = EXCLUDED.site_name,
		   app_base_url = EXCLUDED.app_base_url, login_enabled = EXCLUDED.login_enabled,
		   register_verify = EXCLUDED.register_verify, auto_register = EXCLUDED.auto_register,
		   reset_enabled = EXCLUDED.reset_enabled, welcome_enabled = EXCLUDED.welcome_enabled,
		   code_subject = EXCLUDED.code_subject, code_body = EXCLUDED.code_body,
		   welcome_subject = EXCLUDED.welcome_subject, welcome_body = EXCLUDED.welcome_body,
		   reset_subject = EXCLUDED.reset_subject, reset_body = EXCLUDED.reset_body,
		   code_ttl_seconds = EXCLUDED.code_ttl_seconds,
		   send_interval_seconds = EXCLUDED.send_interval_seconds,
		   daily_limit = EXCLUDED.daily_limit, timeout_seconds = EXCLUDED.timeout_seconds,
		   updated_at = NOW()`, args)
	return err
}

// ── 内部：纯函数 ────────────────────────────────────────

// mailCodeScope 把用途编进键：不同用途的验证码/尝试计数互不干扰。
func mailCodeScope(purpose, email string) string {
	return purpose + ":" + strings.ToLower(strings.TrimSpace(email))
}

func mailActionLabel(purpose string) string {
	switch purpose {
	case mailPurposeRegister:
		return "注册账号"
	case mailPurposeReset:
		return "重置密码"
	default:
		return "登录账号"
	}
}

func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

// resetTTLSeconds 返回重置链接有效期（下限 5 分钟，上限沿用验证码 TTL 上限）。
func resetTTLSeconds(row *mailConfigRow) int {
	ttl := row.CodeTTLSeconds
	if ttl < int(mailMinResetTTL.Seconds()) {
		ttl = int(mailMinResetTTL.Seconds())
	}
	if ttl > int(mailMaxCodeTTL.Seconds()) {
		ttl = int(mailMaxCodeTTL.Seconds())
	}
	return ttl
}

func ttlMinutes(seconds int) int {
	m := seconds / 60
	if m < 1 {
		m = 1
	}
	return m
}

func derefString(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// isHTTPURL 校验 http(s) URL（含主机名）。
func isHTTPURL(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return false
	}
	return (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

// validateMailConfigBounds 校验数值与长度边界，返回空串表示通过。
func validateMailConfigBounds(row *mailConfigRow) string {
	if row.CodeTTLSeconds < 60 || row.CodeTTLSeconds > int(mailMaxCodeTTL.Seconds()) {
		return fmt.Sprintf("code_ttl_seconds 需在 60-%d 之间", int(mailMaxCodeTTL.Seconds()))
	}
	if row.SendIntervalSecs < 0 || row.SendIntervalSecs > 3600 {
		return "send_interval_seconds 需在 0-3600 之间"
	}
	if row.DailyLimit < 1 || row.DailyLimit > mailMaxDailyLimit {
		return fmt.Sprintf("daily_limit 需在 1-%d 之间", mailMaxDailyLimit)
	}
	if row.TimeoutSeconds < 5 || row.TimeoutSeconds > 120 {
		return "timeout_seconds 需在 5-120 之间"
	}
	if row.SMTPPort < 0 || row.SMTPPort > 65535 {
		return "smtp_port 需在 0-65535 之间"
	}
	if row.APIChannelID < 0 || row.APITemplateID < 0 {
		return "api_channel_id / api_template_id 不能为负数"
	}
	if len(row.SMTPHost) > 255 || len(row.SMTPUsername) > 255 ||
		len(row.APIBaseURL) > 512 || len(row.FromAddress) > 255 ||
		len(row.AppBaseURL) > 512 || len(row.SiteName) > 128 {
		return "field too long"
	}
	if len(row.CodeSubject) > 255 || len(row.WelcomeSubject) > 255 || len(row.ResetSubject) > 255 {
		return "邮件主题过长（上限 255 字符）"
	}
	if len(row.CodeBody) > 20_000 || len(row.WelcomeBody) > 20_000 || len(row.ResetBody) > 20_000 {
		return "邮件正文过长（上限 20000 字符）"
	}
	return ""
}

// applyMailConfigPatch 以既有值为底，逐字段覆盖（nil 表示不改）。
func applyMailConfigPatch(row *mailConfigRow, req *updateMailConfigRequest) {
	setString := func(dst *string, src *string) {
		if src != nil {
			*dst = strings.TrimSpace(*src)
		}
	}
	setInt := func(dst *int, src *int) {
		if src != nil {
			*dst = *src
		}
	}
	setBool := func(dst *bool, src *bool) {
		if src != nil {
			*dst = *src
		}
	}
	setString(&row.Provider, req.Provider)
	setBool(&row.Enabled, req.Enabled)
	setString(&row.SMTPHost, req.SMTPHost)
	setInt(&row.SMTPPort, req.SMTPPort)
	setString(&row.SMTPUsername, req.SMTPUsername)
	setString(&row.SMTPSecurity, req.SMTPSecurity)
	setBool(&row.SMTPSkipVerify, req.SMTPSkipVerify)
	setString(&row.APIBaseURL, req.APIBaseURL)
	setInt(&row.APIChannelID, req.APIChannelID)
	setInt(&row.APITemplateID, req.APITemplateID)
	setString(&row.FromAddress, req.FromAddress)
	setString(&row.FromName, req.FromName)
	setString(&row.ReplyTo, req.ReplyTo)
	setString(&row.SiteName, req.SiteName)
	setString(&row.AppBaseURL, req.AppBaseURL)
	setBool(&row.LoginEnabled, req.LoginEnabled)
	setBool(&row.RegisterVerify, req.RegisterVerify)
	setBool(&row.AutoRegister, req.AutoRegister)
	setBool(&row.ResetEnabled, req.ResetEnabled)
	setBool(&row.WelcomeEnabled, req.WelcomeEnabled)
	// 模板正文保留原文（空白敏感），只做首尾裁剪
	if req.CodeSubject != nil {
		row.CodeSubject = strings.TrimSpace(*req.CodeSubject)
	}
	if req.CodeBody != nil {
		row.CodeBody = strings.TrimSpace(*req.CodeBody)
	}
	if req.WelcomeSubject != nil {
		row.WelcomeSubject = strings.TrimSpace(*req.WelcomeSubject)
	}
	if req.WelcomeBody != nil {
		row.WelcomeBody = strings.TrimSpace(*req.WelcomeBody)
	}
	if req.ResetSubject != nil {
		row.ResetSubject = strings.TrimSpace(*req.ResetSubject)
	}
	if req.ResetBody != nil {
		row.ResetBody = strings.TrimSpace(*req.ResetBody)
	}
	setInt(&row.CodeTTLSeconds, req.CodeTTLSeconds)
	setInt(&row.SendIntervalSecs, req.SendIntervalSecs)
	setInt(&row.DailyLimit, req.DailyLimit)
	setInt(&row.TimeoutSeconds, req.TimeoutSeconds)
}
