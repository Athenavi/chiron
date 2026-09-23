package billing

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
)

// AlipayClient 对接支付宝开放平台（当面付 trade.precreate + 异步通知验签）。
// 自研 RSA2 签名，不依赖第三方 SDK。
type AlipayClient struct {
	appID      string
	privateKey *rsa.PrivateKey
	publicKey  *rsa.PublicKey // 支付宝公钥（用于响应/回调验签）
	gateway    string
	notifyURL  string
	httpClient *http.Client
}

// NewAlipayClient 用 PEM 私钥/公钥构造客户端。gateway 为空时使用生产网关。
func NewAlipayClient(appID, privateKeyPEM, alipayPublicKeyPEM, gateway, notifyURL string) (*AlipayClient, error) {
	priv, err := parseRSAPrivateKey(privateKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse alipay private key: %w", err)
	}
	pub, err := parseRSAPublicKey(alipayPublicKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse alipay public key: %w", err)
	}
	if gateway == "" {
		gateway = "https://openapi.alipay.com/gateway.do"
	}
	return &AlipayClient{
		appID:      appID,
		privateKey: priv,
		publicKey:  pub,
		gateway:    gateway,
		notifyURL:  notifyURL,
		httpClient: &http.Client{Timeout: 15 * time.Second},
	}, nil
}

func parseRSAPrivateKey(pemStr string) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode([]byte(pemStr))
	if block == nil {
		// 兼容无 PEM 头的裸 base64 私钥
		der, err := base64.StdEncoding.DecodeString(strings.TrimSpace(pemStr))
		if err != nil {
			return nil, fmt.Errorf("decode private key: %w", err)
		}
		block = &pem.Block{Type: "PRIVATE KEY", Bytes: der}
	}
	if k, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		if rk, ok := k.(*rsa.PrivateKey); ok {
			return rk, nil
		}
	}
	if rk, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return rk, nil
	}
	return nil, fmt.Errorf("unsupported private key format")
}

func parseRSAPublicKey(pemStr string) (*rsa.PublicKey, error) {
	block, _ := pem.Decode([]byte(pemStr))
	if block == nil {
		der, err := base64.StdEncoding.DecodeString(strings.TrimSpace(pemStr))
		if err != nil {
			return nil, fmt.Errorf("decode public key: %w", err)
		}
		block = &pem.Block{Type: "PUBLIC KEY", Bytes: der}
	}
	if k, err := x509.ParsePKIXPublicKey(block.Bytes); err == nil {
		if rk, ok := k.(*rsa.PublicKey); ok {
			return rk, nil
		}
	}
	if k, err := x509.ParsePKCS1PublicKey(block.Bytes); err == nil {
		return k, nil
	}
	return nil, fmt.Errorf("unsupported public key format")
}

// buildSignContent 拼接待签名串：除 sign 外的非空参数按 key 字典序，key=value 用 & 连接。
//
// **sign_type 必须参与签名**：支付宝的规则是"除 sign 以外的全部请求参数都要参与"，
// 官方 SDK（AlipaySignature.getSignContent）也只排除 sign。此前这里把 sign_type 一并
// 排除，待签名串就与支付宝算出来的不一致 —— 网关直接返回
//
//	code=40002 Invalid Arguments（sub_msg 提示签名 / charset 参数不正确），
//
// 而且不论私钥配得多正确都不可能通过。
func buildSignContent(params map[string]string) string {
	keys := make([]string, 0, len(params))
	for k, v := range params {
		if k == "sign" || v == "" {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+params[k])
	}
	return strings.Join(parts, "&")
}

// sign 对参数做 RSA2（SHA256withRSA）签名，返回 base64。
func (c *AlipayClient) sign(params map[string]string) (string, error) {
	content := buildSignContent(params)
	digest := sha256.Sum256([]byte(content))
	sig, err := rsa.SignPKCS1v15(rand.Reader, c.privateKey, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(sig), nil
}

// verify 用支付宝公钥验签。
func (c *AlipayClient) verify(params map[string]string, signature string) error {
	content := buildSignContent(params)
	digest := sha256.Sum256([]byte(content))
	sig, err := base64.StdEncoding.DecodeString(signature)
	if err != nil {
		return fmt.Errorf("decode signature: %w", err)
	}
	return rsa.VerifyPKCS1v15(c.publicKey, crypto.SHA256, digest[:], sig)
}

// Precreate 支付宝当面付预下单，返回二维码内容与渠道订单号。
func (c *AlipayClient) Precreate(ctx context.Context, outTradeNo string, amountCents int64, subject string) (qrCode string, err error) {
	biz := map[string]any{
		"out_trade_no": outTradeNo,
		"total_amount": fmt.Sprintf("%.2f", float64(amountCents)/100),
		"subject":      subject,
	}
	bizJSON, err := json.Marshal(biz)
	if err != nil {
		return "", err
	}

	params := map[string]string{
		"app_id":      c.appID,
		"method":      "alipay.trade.precreate",
		"format":      "JSON",
		"charset":     "utf-8",
		"sign_type":   "RSA2",
		"timestamp":   time.Now().Format("2006-01-02 15:04:05"),
		"version":     "1.0",
		"biz_content": string(bizJSON),
	}
	if c.notifyURL != "" {
		params["notify_url"] = c.notifyURL
	}
	sign, err := c.sign(params)
	if err != nil {
		return "", fmt.Errorf("sign precreate: %w", err)
	}
	params["sign"] = sign

	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.gateway, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("alipay precreate request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("alipay precreate: 网关 %s 返回 HTTP %d：%s",
			c.gateway, resp.StatusCode, responseSnippet(body))
	}

	var r struct {
		Response struct {
			Code       string `json:"code"`
			Msg        string `json:"msg"`
			SubMsg     string `json:"sub_msg"`
			OutTradeNo string `json:"out_trade_no"`
			QRCode     string `json:"qr_code"`
		} `json:"alipay_trade_precreate_response"`
		Sign string `json:"sign"`
	}
	if err := json.Unmarshal([]byte(decodeBody(body)), &r); err != nil {
		// 支付宝 API 的响应恒为 JSON（失败时也带 code/msg/sub_msg）。拿到非 JSON 说明
		// 请求根本没到达 API 端点：常见于 ALIPAY_GATEWAY 漏写 /gateway.do、指向门户页，
		// 或中间代理拦截后返回了错误页。只报 "invalid character '<'" 会让人无从下手，
		// 因此把**实际使用的网关**与响应片段一并带出。
		return "", fmt.Errorf("alipay precreate: 网关 %s 返回了非 JSON 响应（HTTP %d）：%s；"+
			"请确认网关形如 https://openapi.alipay.com/gateway.do（沙箱为 https://openapi-sandbox.dl.alipaydev.com/gateway.do）",
			c.gateway, resp.StatusCode, responseSnippet(body))
	}
	if r.Response.Code != "10000" {
		return "", fmt.Errorf("alipay precreate failed: code=%s msg=%s sub_msg=%s",
			r.Response.Code, r.Response.Msg, r.Response.SubMsg)
	}
	if r.Response.QRCode == "" {
		return "", fmt.Errorf("alipay precreate returned empty qr_code")
	}
	return r.Response.QRCode, nil
}

// Query 查询订单支付状态。返回 (tradeNo, paid, err)。
func (c *AlipayClient) Query(ctx context.Context, outTradeNo string) (string, bool, error) {
	biz, _ := json.Marshal(map[string]any{"out_trade_no": outTradeNo})
	params := map[string]string{
		"app_id":      c.appID,
		"method":      "alipay.trade.query",
		"format":      "JSON",
		"charset":     "utf-8",
		"sign_type":   "RSA2",
		"timestamp":   time.Now().Format("2006-01-02 15:04:05"),
		"version":     "1.0",
		"biz_content": string(biz),
	}
	sign, err := c.sign(params)
	if err != nil {
		return "", false, err
	}
	params["sign"] = sign

	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.gateway, strings.NewReader(form.Encode()))
	if err != nil {
		return "", false, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", false, fmt.Errorf("alipay query request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", false, err
	}
	if resp.StatusCode >= 400 {
		return "", false, fmt.Errorf("alipay query: 网关 %s 返回 HTTP %d：%s",
			c.gateway, resp.StatusCode, responseSnippet(body))
	}
	var r struct {
		Response struct {
			Code       string `json:"code"`
			Msg        string `json:"msg"`
			SubMsg     string `json:"sub_msg"`
			TradeNo    string `json:"trade_no"`
			TradeState string `json:"trade_status"`
		} `json:"alipay_trade_query_response"`
	}
	if err := json.Unmarshal([]byte(decodeBody(body)), &r); err != nil {
		return "", false, fmt.Errorf("alipay query: 网关 %s 返回了非 JSON 响应（HTTP %d）：%s",
			c.gateway, resp.StatusCode, responseSnippet(body))
	}
	paid := r.Response.TradeState == "TRADE_SUCCESS" || r.Response.TradeState == "TRADE_FINISHED"
	return r.Response.TradeNo, paid, nil
}

// responseSnippet 截取响应体开头用于错误信息：足以判断返回的是 HTML 错误页还是 JSON，
// 又不会把整页内容灌进日志/前端提示。响应体先做字符集归一（见 decodeBody）。
func responseSnippet(body []byte) string {
	const maxLen = 200
	s := strings.Join(strings.Fields(decodeBody(body)), " ") // 折叠空白，避免多行 HTML 撑爆一行日志
	if len(s) > maxLen {
		s = s[:maxLen] + "..."
	}
	if s == "" {
		return "(空响应)"
	}
	return s
}

// decodeBody 把响应体归一为 UTF-8 文本。
//
// 支付宝在参数/charset 校验失败时会用它自身的默认字符集（GBK）返回错误文案，
// 此时按 UTF-8 读取会得到成片的 "��ǩ..." 乱码，sub_msg 完全不可读 ——
// 排查时只能靠猜（本次故障就是如此）。这里发现非法 UTF-8 时按 GBK 兜底解码。
func decodeBody(body []byte) string {
	if utf8.Valid(body) {
		return string(body)
	}
	if decoded, err := simplifiedchinese.GBK.NewDecoder().Bytes(body); err == nil {
		return string(decoded)
	}
	return string(body)
}

// VerifyCallback 校验支付宝异步通知参数（验签 + 交易成功状态）。
// 返回 (outTradeNo, tradeNo, ok)。
func (c *AlipayClient) VerifyCallback(params map[string]string) (string, string, bool) {
	sign := params["sign"]
	if sign == "" || params["app_id"] != c.appID {
		return "", "", false
	}
	if params["trade_status"] != "TRADE_SUCCESS" && params["trade_status"] != "TRADE_FINISHED" {
		return "", "", false
	}
	if err := c.verify(params, sign); err != nil {
		return "", "", false
	}
	return params["out_trade_no"], params["trade_no"], true
}
