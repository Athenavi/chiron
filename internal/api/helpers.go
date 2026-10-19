package api

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
)

// P0性能优化：集中管理超时常量，避免散落各处的魔法数字
const (
	DefaultAgentTimeout = 300 * time.Second
	DefaultMaxTurns     = 5
	// DefaultMaxOutputTokens 单次 LLM 调用的默认 max output tokens。
	//
	// M20：它不是 run 预算 —— run 全程的 token 上限在引擎侧（`AGENT_RUN_MAX_TOKENS`）。
	// 与后台「系统设置」的 `agent.max_tokens` 是同一个量；与引擎 `app/main.py` 的兜底
	// 8192 同值，所以"网关没配"时行为与改动前完全一致。
	DefaultMaxOutputTokens = 8192
)

// nullableStr returns nil for empty strings, useful for nullable DB columns.
func nullableStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// intVal returns the int value of a JSON field or a default value.
func intVal(m map[string]interface{}, key string, fallback int) int {
	if v, ok := m[key]; ok {
		if f, ok := v.(float64); ok {
			return int(f)
		}
	}
	return fallback
}

// newUUID generates a new UUID string.
func newUUID() string {
	return uuid.New().String()
}

// signHMACSHA256 signs body with secret using HMAC-SHA256.
func signHMACSHA256(body []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// bytesReader wraps []byte into io.Reader.
func bytesReader(b []byte) io.Reader {
	return bytes.NewReader(b)
}

// httpClient is the default HTTP client for outgoing webhook requests.
//
// SSRF 防护（P0 修复）：**不跟随重定向**。默认 client 会跟随最多 10 跳，而出站
// webhook 的初始 URL 虽经私网校验（validateWebhookURL），攻击者服务器仍可用
// 302 把请求引到 `http://169.254.169.254/latest/meta-data/` 或 `http://127.0.0.1:…`
// —— 校验只在创建/首次解析时做过，重定向后完全失效。
// `ErrUseLastResponse` 让 client 直接返回 3xx 响应（投递方只读 statusCode，语义不变）。
var httpClient = &http.Client{
	Timeout: 10 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:       100,
		IdleConnTimeout:    90 * time.Second,
		DisableCompression: false,
	},
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

// contextWithTimeout returns a context with timeout.
func contextWithTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}
