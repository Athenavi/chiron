package auth

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// ── 短信验证码（SMS）登录 ────────────────────────────────
//
// 支持的短信服务商：
//   - aliyun : 阿里云短信（dysmsapi，POP RPC 签名 V1）
//   - tencent: 腾讯云短信（sms.tencentcloudapi.com，TC3-HMAC-SHA256 签名）
//   - custom : 自定义 HTTP 端点（约定契约见 CustomSmsContract）
//
// 所有服务商的发送均为 HTTP 调用，不引入大 SDK。

// SmsProvider 是支持的短信服务商类型。
const (
	SmsAliyun  = "aliyun"
	SmsTencent = "tencent"
	SmsCustom  = "custom"
)


// IsKnownSmsProvider 判断 provider 类型是否受支持。
func IsKnownSmsProvider(p string) bool {
	switch p {
	case SmsAliyun, SmsTencent, SmsCustom:
		return true
	}
	return false
}

// 各服务商默认发送端点（cfg.Endpoint 非空时覆盖，供测试与代理场景）。
const (
	DefaultSmsAliyunEndpoint  = "https://dysmsapi.aliyuncs.com"
	DefaultSmsTencentEndpoint = "https://sms.tencentcloudapi.com"
)

// SmsConfig 是一次发送所需的完整配置（secret 已解密）。
type SmsConfig struct {
	Provider        string // aliyun/tencent/custom
	SignName        string
	TemplateID      string // 阿里云 TemplateCode / 腾讯云 TemplateId / custom template_id
	AccessKeyID     string
	AccessKeySecret string
	Endpoint        string // custom 必填；其余为覆盖项
}

// ErrSmsSendFailed 表示服务商拒绝发送（业务可回 502 + 具体原因）。
var ErrSmsSendFailed = errors.New("sms provider rejected the request")

// ErrSmsUnreachable 表示服务商不可达（业务可回 502，fail-loud）。
var ErrSmsUnreachable = errors.New("sms provider unreachable")

// SmsSender 抽象短信发送，测试可替换。
type SmsSender interface {
	// Send 发送验证码短信；成功返回 nil。
	Send(ctx context.Context, cfg *SmsConfig, phone, code string) error
}

// HTTPSmsSender 是真实实现：按 provider 分派请求/签名格式。
type HTTPSmsSender struct {
	client *http.Client
}

// NewHTTPSmsSender 构造发送器（10s 超时）。
func NewHTTPSmsSender() *HTTPSmsSender {
	return &HTTPSmsSender{client: &http.Client{Timeout: 10 * time.Second}}
}

// Send 按服务商协议发送验证码。
func (s *HTTPSmsSender) Send(ctx context.Context, cfg *SmsConfig, phone, code string) error {
	if cfg == nil {
		return errors.New("sms: nil config")
	}
	if !IsKnownSmsProvider(cfg.Provider) {
		return fmt.Errorf("sms: unknown provider %q", cfg.Provider)
	}
	if phone == "" || code == "" {
		return errors.New("sms: phone and code are required")
	}
	switch cfg.Provider {
	case SmsAliyun:
		return s.sendAliyun(ctx, cfg, phone, code)
	case SmsTencent:
		return s.sendTencent(ctx, cfg, phone, code)
	default:
		return s.sendCustom(ctx, cfg, phone, code)
	}
}

// ── 阿里云短信（POP RPC 签名 V1）────────────────────────

// aliyunPercentEncode 是阿里云 POP 协议的 RFC3986 百分号编码。
func aliyunPercentEncode(s string) string {
	encoded := url.QueryEscape(s)
	encoded = strings.ReplaceAll(encoded, "+", "%20")
	encoded = strings.ReplaceAll(encoded, "*", "%2A")
	encoded = strings.ReplaceAll(encoded, "%7E", "~")
	return encoded
}

// aliyunSign 计算 POP V1 签名：base64(HMAC-SHA1(AccessKeySecret+"&", stringToSign))。
func aliyunSign(secret, stringToSign string) string {
	mac := hmac.New(sha1.New, []byte(secret+"&"))
	mac.Write([]byte(stringToSign))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

func (s *HTTPSmsSender) sendAliyun(ctx context.Context, cfg *SmsConfig, phone, code string) error {
	endpoint := cfg.Endpoint
	if endpoint == "" {
		endpoint = DefaultSmsAliyunEndpoint
	}
	nonce := make([]byte, 16)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	templateParam, err := json.Marshal(map[string]string{"code": code})
	if err != nil {
		return err
	}
	params := map[string]string{
		"AccessKeyId":      cfg.AccessKeyID,
		"Action":           "SendSms",
		"Format":           "JSON",
		"PhoneNumbers":     phone,
		"RegionId":         "cn-hangzhou",
		"SignName":         cfg.SignName,
		"SignatureMethod":  "HMAC-SHA1",
		"SignatureNonce":   hex.EncodeToString(nonce),
		"SignatureVersion": "1.0",
		"TemplateCode":     cfg.TemplateID,
		"TemplateParam":    string(templateParam),
		"Timestamp":        time.Now().UTC().Format("2006-01-02T15:04:05Z"),
		"Version":          "2017-05-25",
	}

	// 规范化查询串：key 升序，k=percentEncode(v)
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	pairs := make([]string, 0, len(keys))
	for _, k := range keys {
		pairs = append(pairs, aliyunPercentEncode(k)+"="+aliyunPercentEncode(params[k]))
	}
	canonicalQuery := strings.Join(pairs, "&")
	stringToSign := "POST&" + aliyunPercentEncode("/") + "&" + aliyunPercentEncode(canonicalQuery)
	params["Signature"] = aliyunSign(cfg.AccessKeySecret, stringToSign)

	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}
	body, err := s.post(ctx, endpoint, "application/x-www-form-urlencoded", strings.NewReader(form.Encode()))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrSmsUnreachable, err)
	}
	var resp struct {
		Code      string `json:"Code"`
		Message   string `json:"Message"`
		BizID     string `json:"BizId"`
		RequestId string `json:"RequestId"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return fmt.Errorf("sms: malformed aliyun response: %w", err)
	}
	if resp.Code != "OK" {
		return fmt.Errorf("%w: aliyun Code=%s Message=%s", ErrSmsSendFailed, resp.Code, resp.Message)
	}
	return nil
}

// ── 腾讯云短信（TC3-HMAC-SHA256 签名）────────────────────

func hmacSHA256(key, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	return mac.Sum(nil)
}

func (s *HTTPSmsSender) sendTencent(ctx context.Context, cfg *SmsConfig, phone, code string) error {
	endpoint := cfg.Endpoint
	if endpoint == "" {
		endpoint = DefaultSmsTencentEndpoint
	}
	host := endpoint
	if u, err := url.Parse(endpoint); err == nil && u.Host != "" {
		host = u.Host
	}

	// 腾讯云要求 E.164 号码（无 "+" 前缀），如 8613800000000
	tel := strings.TrimPrefix(phone, "+")
	payload, err := json.Marshal(map[string]any{
		"PhoneNumberSet":   []string{tel},
		"SmsSdkAppId":      cfg.AccessKeyID, // 腾讯云侧 AppID 复用 AccessKeyID 字段传递
		"SignName":         cfg.SignName,
		"TemplateId":       cfg.TemplateID,
		"TemplateParamSet": []string{code},
	})
	if err != nil {
		return err
	}

	now := time.Now().UTC()
	timestamp := fmt.Sprintf("%d", now.Unix())
	date := now.Format("2006-01-02")
	action := strings.ToLower("SendSms")
	hashedPayload := fmt.Sprintf("%x", sha256.Sum256(payload))
	canonicalRequest := strings.Join([]string{
		"POST", "/", "",
		"content-type:application/json; charset=utf-8",
		"host:" + host,
		"x-tc-action:" + action,
		"",
		"content-type;host;x-tc-action",
		hashedPayload,
	}, "\n")
	canonicalHash := fmt.Sprintf("%x", sha256.Sum256([]byte(canonicalRequest)))
	stringToSign := strings.Join([]string{
		"TC3-HMAC-SHA256", timestamp,
		date + "/sms/tc3_request", canonicalHash,
	}, "\n")

	secretDate := hmacSHA256([]byte("TC3"+cfg.AccessKeySecret), []byte(date))
	secretService := hmacSHA256(secretDate, []byte("sms"))
	secretSigning := hmacSHA256(secretService, []byte("tc3_request"))
	signature := hex.EncodeToString(hmacSHA256(secretSigning, []byte(stringToSign)))
	authorization := fmt.Sprintf(
		"TC3-HMAC-SHA256 Credential=%s/%s/sms/tc3_request, SignedHeaders=content-type;host;x-tc-action, Signature=%s",
		cfg.AccessKeyID, date, signature)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(string(payload)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Host", host)
	req.Header.Set("X-TC-Action", "SendSms")
	req.Header.Set("X-TC-Timestamp", timestamp)
	req.Header.Set("X-TC-Version", "2021-01-11")
	req.Header.Set("Authorization", authorization)

	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrSmsUnreachable, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrSmsUnreachable, err)
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: tencent endpoint status %d", ErrSmsSendFailed, resp.StatusCode)
	}
	var tr struct {
		Response struct {
			Error *struct {
				Code    string `json:"Code"`
				Message string `json:"Message"`
			} `json:"Error"`
			SendStatusSet []struct {
				Code    string `json:"Code"`
				Message string `json:"Message"`
			} `json:"SendStatusSet"`
		} `json:"Response"`
	}
	if err := json.Unmarshal(data, &tr); err != nil {
		return fmt.Errorf("sms: malformed tencent response: %w", err)
	}
	if tr.Response.Error != nil {
		return fmt.Errorf("%w: tencent %s: %s", ErrSmsSendFailed,
			tr.Response.Error.Code, tr.Response.Error.Message)
	}
	for _, st := range tr.Response.SendStatusSet {
		if st.Code != "Ok" {
			return fmt.Errorf("%w: tencent send status %s: %s", ErrSmsSendFailed, st.Code, st.Message)
		}
	}
	if len(tr.Response.SendStatusSet) == 0 {
		return fmt.Errorf("%w: tencent empty SendStatusSet", ErrSmsSendFailed)
	}
	return nil
}

// CustomSmsContract 自定义短信端点契约：
//
//	POST {endpoint}
//	Content-Type: application/json
//	{"access_key_id": "...", "access_key_secret": "...", "phone": "...",
//	 "code": "...", "sign_name": "...", "template_id": "..."}
//
//	→ HTTP 200 且 {"success": true} 视为发送成功；其余一律拒绝。
//
// 该契约足以接入任意自建/第三方短信服务（网关侧做适配层即可）。
func (s *HTTPSmsSender) sendCustom(ctx context.Context, cfg *SmsConfig, phone, code string) error {
	if cfg.Endpoint == "" {
		return errors.New("sms: custom provider requires endpoint")
	}
	payload, err := json.Marshal(map[string]string{
		"access_key_id":     cfg.AccessKeyID,
		"access_key_secret": cfg.AccessKeySecret,
		"phone":             phone,
		"code":              code,
		"sign_name":         cfg.SignName,
		"template_id":       cfg.TemplateID,
	})
	if err != nil {
		return err
	}
	body, status, err := s.postStatus(ctx, cfg.Endpoint, "application/json", strings.NewReader(string(payload)))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrSmsUnreachable, err)
	}
	if status != http.StatusOK {
		return fmt.Errorf("%w: custom endpoint status %d", ErrSmsSendFailed, status)
	}
	var resp struct {
		Success bool   `json:"success"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return fmt.Errorf("sms: malformed custom response: %w", err)
	}
	if !resp.Success {
		return fmt.Errorf("%w: %s", ErrSmsSendFailed, resp.Message)
	}
	return nil
}

func (s *HTTPSmsSender) post(ctx context.Context, endpoint, contentType string, body io.Reader) ([]byte, error) {
	data, _, err := s.postStatus(ctx, endpoint, contentType, body)
	return data, err
}

func (s *HTTPSmsSender) postStatus(ctx context.Context, endpoint, contentType string, body io.Reader) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, body)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", contentType)
	resp, err := s.client.Do(req)
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

// ── 号码与验证码工具（纯函数，便于单测）─────────────────

// ValidateSmsPhone 校验手机号：可选 "+" 前缀 + 5-20 位数字。
// 归一化仅去除首尾空白；不做国家码推断（各服务商格式自定）。
func ValidateSmsPhone(phone string) bool {
	p := strings.TrimSpace(phone)
	if p == "" || len(p) > 21 {
		return false
	}
	if p[0] == '+' {
		p = p[1:]
	}
	if len(p) < 5 || len(p) > 20 {
		return false
	}
	for _, c := range p {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// NormalizeSmsPhone 去空白后返回原样（保留 "+"），供存储/查询统一形态。
func NormalizeSmsPhone(phone string) string {
	return strings.TrimSpace(phone)
}

// GenerateSmsCode 生成 n 位纯数字验证码（crypto/rand，首位可为 0）。
func GenerateSmsCode(n int) (string, error) {
	if n < 4 || n > 8 {
		return "", errors.New("sms: code length must be 4-8")
	}
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	out := make([]byte, n)
	for i, b := range buf {
		out[i] = '0' + b%10
	}
	return string(out), nil
}
