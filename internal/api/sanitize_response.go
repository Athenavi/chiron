package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
)

// SensitiveFields 需要脱敏的字段名列表
//
// ⚠ **不要放 `token`**：它是**本来就要交给客户端的凭据**（登录/注册的响应体里就是这个字段），
// 脱敏它等于**打破认证** —— 而 `/v1/auth/login` 与 `/v1/auth/register` 恰恰在下面的
// `sanitizeSensitivePaths` 白名单里（意图是"脱敏密码传输"），于是 body 里的 `token` 被换成
// `********`。症状极具迷惑性：**浏览器端一切正常**（走 `SetTokenCookie` 的响应头，不经 body
// 脱敏），而**任何读 body 的 API/SDK 客户端一律 401 "invalid or expired token"**
// （SaaS 端到端用例 test_chiron_client_sdk_live.py 就是这么红了很久）。
// ⚠ 注意 `isSensitiveField` 用的是**子串**匹配 ⇒ 名单里任何一项都会连带命中包含它的字段名。
// ⚠ 同族待查：`api_key` 也在名单里，而"新建 API key"同样要把明文交给客户端 —— 未在本轮改动。
var SensitiveFields = []string{
	"password", "secret", "key", "dsn", "credential",
	"access_key", "secret_key", "private_key", "api_key",
}

// sanitizeSensitivePaths 需要脱敏的路径前缀白名单。
// 只有匹配这些前缀的响应才会被全量缓冲和脱敏，其他路径零开销透传。
var sanitizeSensitivePaths = []string{
	"/v1/admin/", "/admin/", // 管理后台（含租户/用户敏感配置）
	"/v1/ent/", "/ent/", // 企业 SSO/策略配置
	"/v1/auth/register", // 注册（密码传输）
	"/v1/auth/login",    // 登录（密码传输）
	"/v1/auth/sso/",     // SSO OIDC 配置
}

// sanitizePathAllowed 判断路径是否需要脱敏缓冲。
func sanitizePathAllowed(path string) bool {
	for _, prefix := range sanitizeSensitivePaths {
		if strings.HasPrefix(path, prefix) {
			return true
		}
	}
	return false
}

// SanitizeResponseMiddleware 脱敏响应中间件 — 路径白名单模式。
// 只有匹配 sanitizeSensitivePaths 前缀的路径才进行全量缓冲+脱敏，
// 其他路径零开销直接透传，避免大 JSON 响应（如对话列表）的额外内存分配和延迟。
func SanitizeResponseMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 非敏感路径零开销直通
		if !sanitizePathAllowed(r.URL.Path) {
			next.ServeHTTP(w, r)
			return
		}

		// P0-性能：流式响应（SSE/text/event-stream）跳过缓冲，直接透传
		if strings.Contains(r.Header.Get("Accept"), "text/event-stream") {
			next.ServeHTTP(w, r)
			return
		}

		// 包装ResponseWriter以捕获响应体
		buf := &bytes.Buffer{}
		rw := &responseBufferWriter{
			ResponseWriter: w,
			buffer:         buf,
		}

		next.ServeHTTP(rw, r)

		// 如果响应是JSON，进行脱敏处理
		if rw.status < 300 && buf.Len() > 0 {
			var data interface{}
			if err := json.Unmarshal(buf.Bytes(), &data); err == nil {
				sanitizeMap(data)
				if sanitized, err := json.Marshal(data); err == nil {
					w.Header().Set("Content-Length", strconv.Itoa(len(sanitized)))
					w.Write(sanitized)
					return
				}
			}
		}

		// 如果不是JSON或解析失败，直接返回原始响应
		w.Write(buf.Bytes())
	})
}

type responseBufferWriter struct {
	http.ResponseWriter
	buffer *bytes.Buffer
	status int
}

func (rw *responseBufferWriter) WriteHeader(status int) {
	rw.status = status
	rw.ResponseWriter.WriteHeader(status)
}

func (rw *responseBufferWriter) Write(b []byte) (int, error) {
	return rw.buffer.Write(b)
}

// sanitizeMap 递归脱敏map中的数据
func sanitizeMap(data interface{}) {
	switch v := data.(type) {
	case map[string]interface{}:
		for key, val := range v {
			if isSensitiveField(key) {
				if str, ok := val.(string); ok && str != "" {
					v[key] = "********"
				}
			} else {
				sanitizeMap(val)
			}
		}
	case []interface{}:
		for _, item := range v {
			sanitizeMap(item)
		}
	}
}

// isSensitiveField 判断字段名是否敏感
func isSensitiveField(fieldName string) bool {
	lower := strings.ToLower(fieldName)
	for _, sensitive := range SensitiveFields {
		if strings.Contains(lower, sensitive) {
			return true
		}
	}
	return false
}
