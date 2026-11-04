package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// ── 人机验证（CAPTCHA）防接口滥用 ─────────────────────────
//
// 支持的验证服务商：
//   - turnstile : Cloudflare Turnstile（默认，免费无感）
//   - recaptcha : Google reCAPTCHA v2/v3
//   - hcaptcha  : hCaptcha
//   - tencent   : 腾讯云验证码（TCaptcha，需 Ticket + Randstr）
//   - custom    : 自定义 HTTP 端点（约定契约见 CustomCaptchaContract）
//
// 所有服务商的服务端校验均为 HTTP 调用，不引入大 SDK。

// CaptchaProvider 是支持的验证码服务商类型。
const (
	CaptchaTurnstile = "turnstile"
	CaptchaRecaptcha = "recaptcha"
	CaptchaHCaptcha  = "hcaptcha"
	CaptchaTencent   = "tencent"
	CaptchaCustom    = "custom"
)


// IsKnownCaptchaProvider 判断 provider 类型是否受支持。
func IsKnownCaptchaProvider(p string) bool {
	switch p {
	case CaptchaTurnstile, CaptchaRecaptcha, CaptchaHCaptcha, CaptchaTencent, CaptchaCustom:
		return true
	}
	return false
}

// 各服务商默认服务端校验端点（cfg.VerifyURL 非空时覆盖，供测试与代理场景）。
var defaultCaptchaEndpoints = map[string]string{
	CaptchaTurnstile: "https://challenges.cloudflare.com/turnstile/v0/siteverify",
	CaptchaRecaptcha: "https://www.google.com/recaptcha/api/siteverify",
	CaptchaHCaptcha:  "https://api.hcaptcha.com/siteverify",
	CaptchaTencent:   "https://ssl.captcha.qq.com/ticket/verify",
}

// CaptchaConfig 是一次验证所需的完整配置（secret 已解密）。
type CaptchaConfig struct {
	Provider  string // turnstile/recaptcha/hcaptcha/tencent/custom
	SiteKey   string
	Secret    string
	VerifyURL string // custom 必填；其余为覆盖项
}

// CaptchaToken 是前端提交的验证凭据。
type CaptchaToken struct {
	Token   string // turnstile/recaptcha/hcaptcha 的 token；tencent 的 Ticket；custom 的 token
	Randstr string // 腾讯云验证码专用随机串
}

// ErrCaptchaFailed 表示验证码校验未通过（业务可回 400/403）。
var ErrCaptchaFailed = errors.New("captcha verification failed")

// ErrCaptchaUnreachable 表示验证服务商不可达（业务可回 502，fail-loud）。
var ErrCaptchaUnreachable = errors.New("captcha provider unreachable")

// CaptchaVerifier 抽象验证码服务端校验，测试可替换。
type CaptchaVerifier interface {
	// Verify 向服务商校验 token；通过返回 nil。
	Verify(ctx context.Context, cfg *CaptchaConfig, tok *CaptchaToken, remoteIP string) error
}

// HTTPCaptchaVerifier 是真实实现：按 provider 分派请求/响应格式。
type HTTPCaptchaVerifier struct {
	client *http.Client
}

// NewHTTPCaptchaVerifier 构造验证器（10s 超时）。
func NewHTTPCaptchaVerifier() *HTTPCaptchaVerifier {
	return &HTTPCaptchaVerifier{client: &http.Client{Timeout: 10 * time.Second}}
}

func (v *HTTPCaptchaVerifier) endpoint(cfg *CaptchaConfig) (string, error) {
	if cfg.VerifyURL != "" {
		return cfg.VerifyURL, nil
	}
	ep, ok := defaultCaptchaEndpoints[cfg.Provider]
	if !ok {
		return "", fmt.Errorf("captcha: no default endpoint for provider %q", cfg.Provider)
	}
	return ep, nil
}

// Verify 按服务商协议校验 token。
func (v *HTTPCaptchaVerifier) Verify(ctx context.Context, cfg *CaptchaConfig, tok *CaptchaToken, remoteIP string) error {
	if cfg == nil {
		return errors.New("captcha: nil config")
	}
	if !IsKnownCaptchaProvider(cfg.Provider) {
		return fmt.Errorf("captcha: unknown provider %q", cfg.Provider)
	}
	if cfg.Provider == CaptchaCustom && cfg.VerifyURL == "" {
		return errors.New("captcha: custom provider requires verify_url")
	}
	if tok == nil || strings.TrimSpace(tok.Token) == "" {
		return ErrCaptchaFailed
	}

	endpoint, err := v.endpoint(cfg)
	if err != nil {
		return err
	}

	switch cfg.Provider {
	case CaptchaTencent:
		return v.verifyTencent(ctx, endpoint, cfg, tok, remoteIP)
	case CaptchaCustom:
		return v.verifyCustom(ctx, endpoint, cfg, tok, remoteIP)
	default:
		// turnstile / recaptcha / hcaptcha 共用同一套 form 表单 + {"success": bool} 契约
		return v.verifyFormJSON(ctx, endpoint, cfg, tok, remoteIP)
	}
}

// verifyFormJSON 覆盖 turnstile / recaptcha / hcaptcha：
// POST application/x-www-form-urlencoded（secret/response/remoteip）→ {"success": bool}。
func (v *HTTPCaptchaVerifier) verifyFormJSON(ctx context.Context, endpoint string, cfg *CaptchaConfig, tok *CaptchaToken, remoteIP string) error {
	form := url.Values{}
	form.Set("secret", cfg.Secret)
	form.Set("response", tok.Token)
	if remoteIP != "" {
		form.Set("remoteip", remoteIP)
	}

	body, err := v.post(ctx, endpoint, "application/x-www-form-urlencoded", strings.NewReader(form.Encode()))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCaptchaUnreachable, err)
	}
	var resp struct {
		Success bool `json:"success"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return fmt.Errorf("captcha: malformed provider response: %w", err)
	}
	if !resp.Success {
		return ErrCaptchaFailed
	}
	return nil
}

// verifyTencent 腾讯云验证码（TCaptcha）：
// POST form（aid=site_key, AppSecretKey=secret, Ticket, Randstr, UserIP）→ {"response":"1"}。
func (v *HTTPCaptchaVerifier) verifyTencent(ctx context.Context, endpoint string, cfg *CaptchaConfig, tok *CaptchaToken, remoteIP string) error {
	if tok.Randstr == "" {
		return ErrCaptchaFailed // 腾讯验证码必须携带 Randstr
	}
	form := url.Values{}
	form.Set("aid", cfg.SiteKey)
	form.Set("AppSecretKey", cfg.Secret)
	form.Set("Ticket", tok.Token)
	form.Set("Randstr", tok.Randstr)
	form.Set("UserIP", remoteIP)

	body, err := v.post(ctx, endpoint, "application/x-www-form-urlencoded", strings.NewReader(form.Encode()))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCaptchaUnreachable, err)
	}
	var resp struct {
		Response string `json:"response"`
		ErrMSG   string `json:"err_msg"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return fmt.Errorf("captcha: malformed tencent response: %w", err)
	}
	if resp.Response != "1" {
		return fmt.Errorf("%w: tencent err_msg=%q", ErrCaptchaFailed, resp.ErrMSG)
	}
	return nil
}

// CustomCaptchaContract 自定义验证端点契约：
//
//	POST {verify_url}
//	Content-Type: application/json
//	{"secret": "...", "token": "...", "randstr": "...", "remote_ip": "..."}
//
//	→ HTTP 200 且 {"success": true} 视为通过；其余一律拒绝。
//
// 该契约足以接入任意自建/第三方验证服务（网关侧做适配层即可）。
func (v *HTTPCaptchaVerifier) verifyCustom(ctx context.Context, endpoint string, cfg *CaptchaConfig, tok *CaptchaToken, remoteIP string) error {
	payload, err := json.Marshal(map[string]string{
		"secret":    cfg.Secret,
		"token":     tok.Token,
		"randstr":   tok.Randstr,
		"remote_ip": remoteIP,
	})
	if err != nil {
		return err
	}
	body, status, err := v.postStatus(ctx, endpoint, "application/json", strings.NewReader(string(payload)))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrCaptchaUnreachable, err)
	}
	if status != http.StatusOK {
		return fmt.Errorf("%w: custom endpoint status %d", ErrCaptchaFailed, status)
	}
	var resp struct {
		Success bool `json:"success"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return fmt.Errorf("captcha: malformed custom response: %w", err)
	}
	if !resp.Success {
		return ErrCaptchaFailed
	}
	return nil
}

func (v *HTTPCaptchaVerifier) post(ctx context.Context, endpoint, contentType string, body io.Reader) ([]byte, error) {
	data, _, err := v.postStatus(ctx, endpoint, contentType, body)
	return data, err
}

func (v *HTTPCaptchaVerifier) postStatus(ctx context.Context, endpoint, contentType string, body io.Reader) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", contentType)
	resp, err := v.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	// 上限 64KB，防恶意端点拖垮网关
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return nil, resp.StatusCode, err
	}
	return data, resp.StatusCode, nil
}
