package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
)

// InputSanitizer 输入净化器，防止 Prompt Injection
type InputSanitizer struct {
	injectionPatterns []*regexp.Regexp
	homoglyphPattern  *regexp.Regexp
}

// NewInputSanitizer 创建输入净化器
func NewInputSanitizer() *InputSanitizer {
	patterns := []string{
		`(?i)ignore\s+(all\s+)?(previous|above|prior|earlier)\s+instructions?`,
		`(?i)forget\s+(everything|all|previous|prior)`,
		`(?i)you\s+are\s+(now|free|not\s+bound|unrestricted|no\s+longer)`,
		`(?i)system\s+prompt`,
		`(?i)new\s+instructions?`,
		`(?i)disregard\s+(all|previous|above|prior)`,
		`(?i)override\s+(safety|instructions?|rules?|policy)`,
		`(?i)act\s+as\s+if\s+you\s+(have|are)\s+no\s+restrictions?`,
		`(?i)pretend\s+you\s+are\s+(not|an?\s+unrestricted)`,
		`(?i)bypass\s+(all|safety|content)\s+(filters?|restrictions?|rules?|checks?)`,
		`(?i)dan\s+(mode|prompt|system)`,
		`(?i)jailbreak`,
		`(?i)developer\s+(mode|instructions?)`,
		`(?i)do\s+not\s+(follow|obey)\s+(the\s+)?(system\s+)?(instructions?|rules?)`,
		`(?i)I\s+am\s+(the\s+)?(system|admin|owner|root|superuser)`,
		`(?i)this\s+is\s+(a\s+)?(test|demo|simulation)`,
		`(?i)repeat\s+(after\s+)?(me|this):`,
		`(?i)translation\s+(mode|system|prompt)`,
		`(?i)markdown\s+(mode|system)`,
	}

	compiled := make([]*regexp.Regexp, 0, len(patterns))
	for _, p := range patterns {
		re, err := regexp.Compile(p)
		if err != nil {
			slog.Warn("编译注入模式失败", "pattern", p, "error", err)
			continue
		}
		compiled = append(compiled, re)
	}

	// 检测非 ASCII 同形异义词（Unicode 混淆攻击：西里尔/希腊/阿拉伯字母伪装成 ASCII）
	homoglyph := regexp.MustCompile("[\u0400-\u04FF\u0370-\u03FF\u0600-\u06FF]")

	return &InputSanitizer{
		injectionPatterns: compiled,
		homoglyphPattern:  homoglyph,
	}
}

// Sanitize 净化用户输入
// 使用 XML 标签包裹 + HTML 转义，防止用户输入被 LLM 解释为指令
func (s *InputSanitizer) Sanitize(input string) string {
	escaped := htmlEscape(input)
	return fmt.Sprintf("<user_input>\n%s\n</user_input>", escaped)
}

// DetectInjection 检测 Prompt Injection 攻击
// 返回 (是否检测到, 匹配的模式描述)
func (s *InputSanitizer) DetectInjection(input string) (bool, string) {
	normalized := normalizeInput(input)

	if s.homoglyphPattern.MatchString(input) {
		return true, "unicode homoglyph / zero-width characters detected"
	}

	for _, pattern := range s.injectionPatterns {
		if pattern.MatchString(normalized) {
			return true, pattern.String()
		}
	}
	return false, ""
}

// normalizeInput 归一化用户输入以规避混淆技术
// 将全角→半角、去除零宽字符、统一空白
func normalizeInput(input string) string {
	var b strings.Builder
	b.Grow(len(input))
	for _, r := range input {
		// 零宽字符 / 格式控制符（U+200B-200F, U+FE00-FE0F 变体选择符等）
		if (r >= '\u200B' && r <= '\u200F') || (r >= '\uFE00' && r <= '\uFE0F') || (r >= '\u2060' && r <= '\u2064') {
			continue
		}
		// 全角 ASCII → 半角
		if r >= '\uFF01' && r <= '\uFF5E' {
			b.WriteRune(r - 0xFEE0)
			continue
		}
		b.WriteRune(r)
	}
	lower := strings.ToLower(b.String())
	return strings.Join(strings.Fields(lower), " ")
}

// htmlEscape 转义用户输入中的特殊字符，防止 LLM 将
// 用户输入中的 XML/HTML 标签误解释为指令
func htmlEscape(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		switch r {
		case '<':
			b.WriteString("&#60;")
		case '>':
			b.WriteString("&#62;")
		case '&':
			b.WriteString("&#38;")
		case '"':
			b.WriteString("&#34;")
		case '\'':
			b.WriteString("&#39;")
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// SanitizeMiddleware 输入净化中间件
func SanitizeMiddleware(sanitizer *InputSanitizer) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 只对 POST 请求进行净化
			if r.Method != http.MethodPost {
				next.ServeHTTP(w, r)
				return
			}

			// 检查 Content-Type
			ct := r.Header.Get("Content-Type")
			if !strings.Contains(ct, "application/json") {
				next.ServeHTTP(w, r)
				return
			}

			// 读取请求体（保留所有字段）
			r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				BadRequest(w, "invalid or too large request body")
				return
			}
			defer r.Body.Close()

			// 只对非空 content 字段进行净化
			if content, ok := body["content"].(string); ok && content != "" {
				// 检测注入
				if detected, pattern := sanitizer.DetectInjection(content); detected {
					slog.Warn("检测到 Prompt Injection 攻击",
						"pattern", pattern,
						"content_preview", truncate(content, 100),
						"path", r.URL.Path,
						"ip", r.RemoteAddr,
					)
					BadRequest(w, "输入内容包含不允许的指令")
					return
				}
				// 净化 content
				body["content"] = sanitizer.Sanitize(content)
			}

			// 重建请求体（保留所有字段）
			newBody, err := json.Marshal(body)
			if err != nil {
				slog.Error("sanitize: marshal request body", "error", err)
				InternalError(w, "request processing failed")
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(newBody))
			r.ContentLength = int64(len(newBody))

			next.ServeHTTP(w, r)
		})
	}
}

// OutputScanner 输出扫描器，检测 LLM 响应中的敏感信息
type OutputScanner struct {
	// 系统提示关键词
	systemPromptKeywords []string
	// API Key 模式
	apiKeyPatterns []*regexp.Regexp
}


// Scan 扫描 LLM 输出
func (s *OutputScanner) Scan(response string) (safe bool, reason string) {
	// 检查是否泄露系统提示
	lower := strings.ToLower(response)
	for _, keyword := range s.systemPromptKeywords {
		if strings.Contains(lower, strings.ToLower(keyword)) {
			return false, "response may contain system prompt content"
		}
	}

	// 检查是否包含 API Key
	for _, pattern := range s.apiKeyPatterns {
		if pattern.MatchString(response) {
			return false, "response may contain API keys or secrets"
		}
	}

	return true, ""
}

// truncate 截断字符串
func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}
