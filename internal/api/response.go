package api

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
)

// Unified error message constants — single source of truth for all handlers.
const (
	ErrAuthRequired  = "authentication required"
	ErrDBUnavailable = "service temporarily unavailable"
	ErrInvalidReq    = "invalid request body"
	ErrNotFound      = "resource not found"
)

type APIResponse struct {
	Success bool        `json:"success"`
	Data    interface{} `json:"data,omitempty"`
	Error   string      `json:"error,omitempty"`
	// Code 稳定错误码（i18n）：客户端据此渲染本地化文案；Error 保留为兼容兜底。
	// 见 error_codes.go 的常量表与前端 src/utils/apiError.ts。
	Code string `json:"code,omitempty"`
	// Params 错误码的结构化参数（如 {"limit": 20}），供客户端插值。
	Params map[string]interface{} `json:"params,omitempty"`
	// Retryable 是**服务端对"这次失败值得不值得原样重来"的显式结论**。
	//
	// 分层：`code` 负责 i18n 与分支（"发生了什么事"），`retryable` 只负责重试决策
	// （"要不要再发一次"）。此前客户端只能靠 HTTP 状态码猜（429/503 才退避），
	// 于是 409 session_busy（等上一轮跑完就好）被判成不可重试，而 500 里的
	// provider 暂时性故障又被判成不可重试。
	//
	// **留空语义**：nil = "服务端没有结论，调用方自行判断" —— 此时客户端回退到
	// 既有的状态码逻辑（后向兼容）。因此**不要为了"填满字段"而默认填 false**：
	// 显式 false 的语义是"服务端确认重试无用"（如参数非法），与"不知道"不同。
	Retryable *bool `json:"retryable,omitempty"`
	Meta      *Meta `json:"meta,omitempty"`
}

// BoolPtr 取布尔字面量的地址（用于填 `APIResponse.Retryable` 这种三态字段）。
// Go 没有布尔字面量取址语法，各调用点都写一遍局部变量太啰嗦。
func BoolPtr(v bool) *bool { return &v }

type Meta struct {
	Total   int `json:"total,omitempty"`
	Page    int `json:"page,omitempty"`
	PerPage int `json:"per_page,omitempty"`
}

func JSON(w http.ResponseWriter, status int, resp APIResponse) {
	// i18n：失败响应未显式指定 code 时按文案自动补码（既有调用点零改动）。
	if !resp.Success && resp.Code == "" {
		resp.Code = codeForMessage(resp.Error)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		slog.Error("json encode response failed", "error", err, "status", status)
	}
}

func OK(w http.ResponseWriter, data interface{}) {
	JSON(w, http.StatusOK, APIResponse{Success: true, Data: data})
}

func Created(w http.ResponseWriter, data interface{}) {
	JSON(w, http.StatusCreated, APIResponse{Success: true, Data: data})
}

func Accepted(w http.ResponseWriter, data interface{}) {
	JSON(w, http.StatusAccepted, APIResponse{Success: true, Data: data})
}

func NoContent(w http.ResponseWriter) {
	w.WriteHeader(http.StatusNoContent)
}

func BadRequest(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: msg})
}

func NotFound(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: msg})
}

func InternalError(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: msg})
}

func logAndRespond(w http.ResponseWriter, err error, statusCode int, userMsg string) {
	logAndRespondRetryable(w, err, statusCode, userMsg, nil)
}

// logAndRespondRetryable 同 `logAndRespond`，但显式携带可重试性结论。
//
// `retryable` 传 nil 表示"服务端无结论"（留空，客户端自判）—— 这正是绝大多数错误的
// 实际情形：一个上游 502 究竟是对方重启还是我们发错了参数，网关从错误串里读不出来。
// 只有调用方**确实知道**才传 non-nil。
func logAndRespondRetryable(w http.ResponseWriter, err error, statusCode int, userMsg string, retryable *bool) {
	slog.Error(userMsg, "error", err)
	JSON(w, statusCode, APIResponse{Success: false, Error: userMsg, Retryable: retryable})
}

func Unauthorized(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusUnauthorized, APIResponse{Success: false, Error: msg})
}

func Forbidden(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusForbidden, APIResponse{Success: false, Error: msg})
}

func ServiceUnavailable(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: msg})
}

// NotImplemented 返回 501：端点已暴露但功能尚未实现。
// 用于避免调用方把"未实现"当作成功（如 chaos 故障注入此前会返回 created）。
func NotImplemented(w http.ResponseWriter, msg string) {
	JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: msg})
}

// TooManyRequests 返回 429。
//
// `retryable=true` 对**速率闸门**有真实依据：窗口一开同一个请求就能成 —— 这正是
// "原样重来有效"的定义。
//
// 注意 429 并非只有一种语义：租户配额池耗尽也走 429（`rejectTenantQuota`，为兼容既有
// 集成方保留状态码），但按周期（月/日）重置，秒级重试没有意义，因此那里显式给
// `retryable=false`。区分靠 `code`，不靠状态码。
func TooManyRequests(w http.ResponseWriter) {
	JSON(w, http.StatusTooManyRequests, APIResponse{
		Success:   false,
		Error:     "rate limit exceeded",
		Retryable: BoolPtr(true),
	})
}

// TooManyRequestsAfterSeconds 同 `TooManyRequests`，但把"还要等多久"写进两个位置：
// `Retry-After` 头（标准）与 body 的 `params.retry_after_seconds`（代理常会吃掉响应头，
// body 不会被改；前端 `rateLimitBackoff` 两处都认）。
//
// 不给等待时长时仍用 `TooManyRequests`：`Retry-After: 60` 是**猜的**，只该由确实知道
// 窗口长度的调用点给出（令牌桶按 rpm 补充，窗口上限就是 60s）。
func TooManyRequestsAfterSeconds(w http.ResponseWriter, seconds int) {
	if seconds <= 0 {
		TooManyRequests(w)
		return
	}
	w.Header().Set("Retry-After", strconv.Itoa(seconds))
	JSON(w, http.StatusTooManyRequests, APIResponse{
		Success:   false,
		Error:     "rate limit exceeded",
		Code:      CodeRateLimited,
		Params:    map[string]interface{}{"retry_after_seconds": seconds},
		Retryable: BoolPtr(true),
	})
}

func DecodeJSON(w http.ResponseWriter, r *http.Request, v interface{}) error {
	// Enforce 1MB limit at the read level (not just Content-Length header)
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	return json.NewDecoder(r.Body).Decode(v)
}
