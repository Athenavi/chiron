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
	// CodeExportTooManyRows 表示导出范围内记录数超过单次上限。
	//
	// 单独成码而不是复用 invalid_request：这是一个**用户可自行解决**的情况
	// （缩小时间范围即可），文案必须带上实际条数与上限，否则用户只知道"失败了"。
	CodeExportTooManyRows = "export_too_many_rows"
	// CodeExportRangeTooWide 表示导出时间跨度超过上限，同样可由用户自行缩小范围解决。
	CodeExportRangeTooWide = "export_range_too_wide"
	// CodeInvoiceAlreadyRequested 表示同一笔支付已经提交过开票申请。
	//
	// 单独成码而不是复用 invalid_request：用户的下一步动作是明确的（去看已有
	// 申请的状态），而不是"改请求内容再试"。文案要能指向那张已有的申请。
	CodeInvoiceAlreadyRequested = "invoice_already_requested"
	// CodeInvoiceAlreadyReviewed 表示该申请已被审核过。
	//
	// 与上面的区别在**谁该行动**：前者由申请人处理，后者由管理员处理 ——
	// 通常是另一位管理员先审了，刷新列表即可。
	CodeInvoiceAlreadyReviewed = "invoice_already_reviewed"
	// CodeModelNotAllowed 表示请求的模型不在可用范围内（403）。
	//
	// 单独成码而不是复用 forbidden：模型被拒时用户的下一步动作取决于**是谁拦的**
	// （`params.reason`）—— 套餐权益不覆盖 → 去买/续套餐；租户策略不允许 →
	// 买套餐也没用，得找管理员。合成一句"没有权限"会让用户白花钱买套餐。
	CodeModelNotAllowed = "model_not_allowed"
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
	// 导出相关放在最前面：它们与"记录太多"有关，而下面的宽泛规则里
	// 有 "too many request"（限流）等，靠后的顺序会让文案被误判成别的语义。
	{"too many transactions", CodeExportTooManyRows},
	{"time range spans", CodeExportRangeTooWide},
	// 模型准入必须排在 "not found"/"invalid" 之前：原文 "model not allowed by
	// enterprise policy" 不含这些词，但引擎侧转发的措辞可能含 "not"。
	{"model not allowed", CodeModelNotAllowed},
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
//
// 不设 `retryable`：这是**有意的留空**（nil = 调用方自判），不是遗漏。只有确实掌握
// “重试有用/无用”依据的调用点才该走 `JSONWithRetryable` 显式表态。
func JSONWithCode(w http.ResponseWriter, status int, code string, params map[string]interface{}, msg string) {
	JSON(w, status, APIResponse{Success: false, Error: msg, Code: code, Params: params})
}

// JSONWithRetryable 与 `JSONWithCode` 相同，但额外携带服务端对可重试性的显式结论。
//
// 用在“我们确实知道重试有没有用”的地方 —— 例如 409 `session_busy`：上一轮跑完
// 这条会话就能再提交，重试是**对的策略**，客户端不该把它当成“请求内容有问题”而放弃。
func JSONWithRetryable(w http.ResponseWriter, status int, code string,
	params map[string]interface{}, msg string, retryable bool) {
	JSON(w, status, APIResponse{
		Success:   false,
		Error:     msg,
		Code:      code,
		Params:    params,
		Retryable: BoolPtr(retryable),
	})
}
