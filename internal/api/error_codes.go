package api

import (
	"net/http"
	"strings"
)

// ── 稳定错误码（i18n 基础）────────────────────────────────────────────
//
// 背景：`APIResponse.Error` 是给集成方看的英文/中文原文，不适合直接呈现给终端用户，
// 也无法随界面语言切换。本文件给面向用户的失败响应附加一个**稳定错误码**（`code`）
// 与可选的**结构化参数**（`params`），由客户端按当前语言渲染文案
// （前端落点：frontend-vue/src/utils/apiError.ts + src/locales/<lang>/errors.ts）。
//
// 兼容性：`error` 字段保持不变（旧客户端继续工作）；`code`/`params` 为新增字段。
// 内部日志（slog）不受本机制影响，仍记录原文，便于排障与日志检索。
//
// 接入方式：既有调用点**无需改动** —— `JSON()` 会对已知文案自动补码；
// 需要精确语义/参数的新代码使用 `JSONWithCode()`。

// 稳定错误码。新增时必须同步更新前端 locales/<lang>/errors.*，否则客户端只能回退 error 原文。
const (
	CodeAuthRequired        = "auth_required"
	CodeForbidden           = "forbidden"
	CodeInvalidRequest      = "invalid_request"
	CodeNotFound            = "not_found"
	CodeRateLimited         = "rate_limited"
	CodeQuotaExceeded       = "quota_exceeded"
	CodeInsufficientCredits = "insufficient_credits"
	CodeServiceUnavailable  = "service_unavailable"
	CodeInternal            = "internal_error"
	CodeRequestFailed       = "request_failed"
	// CodeSessionBusy 表示"同会话已有一轮在跑"。此前它被归到 invalid_request，
	// 客户端渲染成"请求内容不被接受，请检查后重试"—— 把用户引向完全错误的方向
	// （该改的是"等一下或修好卡住的会话"，不是改请求内容）。
	CodeSessionBusy = "session_busy"
)

// knownMessageCodes 已知文案 → 错误码（键为小写原文）。
// 覆盖 response.go 的统一常量与网关内高频用户可见文案。
var knownMessageCodes = map[string]string{
	strings.ToLower(ErrAuthRequired):        CodeAuthRequired,
	strings.ToLower(ErrDBUnavailable):       CodeServiceUnavailable,
	strings.ToLower(ErrInvalidReq):          CodeInvalidRequest,
	strings.ToLower(ErrNotFound):            CodeNotFound,
	"rate limit exceeded":                   CodeRateLimited,
	"tenant token quota exceeded":           CodeQuotaExceeded,
	"tenant concurrency quota exhausted":    CodeQuotaExceeded,
	"insufficient permissions":              CodeForbidden,
	"insufficient enterprise permissions":   CodeForbidden,
	"task already running for this session": CodeSessionBusy,
}

// messageCodeRules 关键词 → 错误码，按序匹配先到先得。
// 用于覆盖各 handler 自写的文案（无法穷举精确匹配），因此规则必须从"更具体"排到"更宽泛"。
var messageCodeRules = []struct {
	keyword string
	code    string
}{
	{"token quota", CodeQuotaExceeded},
	{"quota exceeded", CodeQuotaExceeded},
	{"quota", CodeQuotaExceeded},
	{"insufficient credit", CodeInsufficientCredits},
	{"credit", CodeInsufficientCredits},
	{"rate limit", CodeRateLimited},
	{"too many request", CodeRateLimited},
	{"enterprise permissions", CodeForbidden},
	{"insufficient permission", CodeForbidden},
	{"forbidden", CodeForbidden},
	{"not found", CodeNotFound},
	{"authentication required", CodeAuthRequired},
	{"unauthorized", CodeAuthRequired},
	{"invalid", CodeInvalidRequest},
	{"required", CodeInvalidRequest},
	{"unavailable", CodeServiceUnavailable},
	{"redis down", CodeServiceUnavailable},
	{"timeout", CodeServiceUnavailable},
	{"internal", CodeInternal},
}

// codeForMessage 推断失败文案对应的稳定错误码。
// 空文案返回空串（不写 code 字段）；未命中任何规则时返回 CodeRequestFailed。
func codeForMessage(msg string) string {
	if msg == "" {
		return ""
	}
	lower := strings.ToLower(msg)
	if code, ok := knownMessageCodes[lower]; ok {
		return code
	}
	for _, rule := range messageCodeRules {
		if strings.Contains(lower, rule.keyword) {
			return rule.code
		}
	}
	return CodeRequestFailed
}

// JSONWithCode 返回带显式错误码与结构化参数的失败响应（新代码推荐用法）。
// params 供客户端插值（如 {"limit": 20}）；msg 仍是兼容用的原文文案。
func JSONWithCode(w http.ResponseWriter, status int, code string, params map[string]interface{}, msg string) {
	JSON(w, status, APIResponse{Success: false, Error: msg, Code: code, Params: params})
}
