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
var httpClient = &http.Client{
	Timeout: 10 * time.Second,
	Transport: &http.Transport{
		MaxIdleConns:       100,
		IdleConnTimeout:    90 * time.Second,
		DisableCompression: false,
	},
}

// contextWithTimeout returns a context with timeout.
func contextWithTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}
