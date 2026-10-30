package api

// 本文件由 sms_handler.go 按职责外移而来（短信通道配置：读取/回显/更新）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
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
