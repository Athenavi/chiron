package api

// 本文件由 mail_handler.go 按职责外移而来（邮件配置行的边界校验与补丁应用，纯函数）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"fmt"
	"net/url"
	"strings"
)

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
