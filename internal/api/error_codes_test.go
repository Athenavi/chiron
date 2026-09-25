package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// ── 稳定错误码契约（docs/error-codes.md）──────────────────────────────
//
// code 由 error 文案推断：knownMessageCodes 精确匹配（小写整串）→ messageCodeRules
// 关键字按序包含匹配 → request_failed 兜底；空文案不写 code 字段。
//
// 这些断言把「文案 → 错误码」的隐式契约钉成可回归的门禁：规则表失序、文案被改写、
// 常量被改动都会在这里亮红灯，而不是等到用户看到一句没本地化的报错。

func TestCodeForMessage_EmptyMessageOmitsCode(t *testing.T) {
	// 空文案必须返回空串：此时响应不应带 code 字段，交给前端的状态码兜底映射。
	if got := codeForMessage(""); got != "" {
		t.Fatalf("codeForMessage(%q) = %q, want empty (no code field)", "", got)
	}
}

func TestCodeForMessage_KnownMessagesExactMatch(t *testing.T) {
	// 与 docs/error-codes.md 的「错误码表」逐条对应。
	cases := []struct {
		msg  string
		want string
	}{
		{ErrAuthRequired, CodeAuthRequired},
		{ErrDBUnavailable, CodeServiceUnavailable},
		{ErrInvalidReq, CodeInvalidRequest},
		{ErrNotFound, CodeNotFound},
		{"rate limit exceeded", CodeRateLimited},
		{"tenant token quota exceeded", CodeQuotaExceeded},
		{"tenant concurrency quota exhausted", CodeQuotaExceeded},
		{"insufficient permissions", CodeForbidden},
		{"insufficient enterprise permissions", CodeForbidden},
		{"task already running for this session", CodeInvalidRequest},
	}
	for _, c := range cases {
		if got := codeForMessage(c.msg); got != c.want {
			t.Errorf("codeForMessage(%q) = %q, want %q", c.msg, got, c.want)
		}
	}
}

func TestCodeForMessage_IsCaseInsensitive(t *testing.T) {
	if got := codeForMessage("Tenant Token Quota Exceeded"); got != CodeQuotaExceeded {
		t.Errorf("known message matching must be case-insensitive: got %q, want %q",
			got, CodeQuotaExceeded)
	}
}

func TestCodeForMessage_KeywordRules(t *testing.T) {
	// 各 handler 自写文案只能靠关键字覆盖（无法穷举精确匹配）。
	cases := []struct {
		msg  string
		want string
	}{
		{"the quota for this tenant is exhausted", CodeQuotaExceeded},
		{"insufficient credits — please recharge in Billing", CodeInsufficientCredits},
		{"model gpt-4 rate limit exceeded for tenant t", CodeRateLimited},
		{"insufficient permissions to delete tenant-shared skills", CodeForbidden},
		{"resource not found", CodeNotFound},
		{"service temporarily unavailable", CodeServiceUnavailable},
		{"redis down", CodeServiceUnavailable},
		{"upstream timeout", CodeServiceUnavailable},
		{"internal database failure", CodeInternal},
		{"invalid request body", CodeInvalidRequest},
		// 未命中任何关键字 → 兜底码（注意：只要文案非空，code 就不会是空串）。
		{"session does not belong to the current user", CodeRequestFailed},
	}
	for _, c := range cases {
		if got := codeForMessage(c.msg); got != c.want {
			t.Errorf("codeForMessage(%q) = %q, want %q", c.msg, got, c.want)
		}
	}
}

func TestCodeForMessage_RuleOrderIsSpecificFirst(t *testing.T) {
	// messageCodeRules 必须从「更具体」排到「更宽泛」，否则宽泛的规则会截胡。
	// 这两条文案都同时命中多个关键字，断言的是「哪个先被写进表里」。
	cases := []struct {
		msg  string
		want string
		why  string
	}{
		{
			"insufficient credits: token quota exceeded",
			CodeQuotaExceeded,
			`"token quota" 必须排在 "insufficient credit" 之前`,
		},
		{
			"invalid request: authentication required",
			CodeAuthRequired,
			`"authentication required" 必须排在 "invalid" / "required" 之前`,
		},
	}
	for _, c := range cases {
		if got := codeForMessage(c.msg); got != c.want {
			t.Errorf("codeForMessage(%q) = %q, want %q（%s）", c.msg, got, c.want, c.why)
		}
	}
}

// ── JSON() 的自动补码行为（既有调用点零改动的依据）──────────────────────

func decodeResponse(t *testing.T, rec *httptest.ResponseRecorder) APIResponse {
	t.Helper()
	var resp APIResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (body=%s)", err, rec.Body.String())
	}
	return resp
}

func TestJSON_AutofillsCodeOnFailure(t *testing.T) {
	rec := httptest.NewRecorder()
	JSON(rec, http.StatusUnauthorized, APIResponse{Success: false, Error: ErrAuthRequired})

	resp := decodeResponse(t, rec)
	if resp.Code != CodeAuthRequired {
		t.Errorf("failure response must be auto-coded: got code %q, want %q", resp.Code, CodeAuthRequired)
	}
	if resp.Error != ErrAuthRequired {
		t.Errorf("error 原文必须保持兼容不变: got %q", resp.Error)
	}
}

func TestJSON_KeepsExplicitCodeAndParams(t *testing.T) {
	rec := httptest.NewRecorder()
	JSONWithCode(rec, http.StatusTooManyRequests, CodeQuotaExceeded,
		map[string]interface{}{"limit": 20}, "tenant token quota exceeded")

	resp := decodeResponse(t, rec)
	if resp.Code != CodeQuotaExceeded {
		t.Errorf("显式 code 不得被文案推断覆盖: got %q, want %q", resp.Code, CodeQuotaExceeded)
	}
	if got := resp.Params["limit"]; got != float64(20) {
		t.Errorf("params 必须原样透传: got %v, want 20", got)
	}
}

func TestJSON_SuccessCarriesNoCode(t *testing.T) {
	rec := httptest.NewRecorder()
	JSON(rec, http.StatusOK, APIResponse{Success: true, Data: map[string]interface{}{"ok": true}})

	if resp := decodeResponse(t, rec); resp.Code != "" {
		t.Errorf("成功响应不应带 code: got %q", resp.Code)
	}
}

func TestJSON_EmptyErrorLeavesCodeEmpty(t *testing.T) {
	// 前置组件（nginx 等）或调用点未给文案时要保留空 code，
	// 让前端落到状态码兜底映射，而不是被塞一个 request_failed。
	rec := httptest.NewRecorder()
	JSON(rec, http.StatusBadGateway, APIResponse{Success: false})

	if resp := decodeResponse(t, rec); resp.Code != "" {
		t.Errorf("空文案不应推断出 code: got %q", resp.Code)
	}
}
