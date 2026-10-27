package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ── `retryable` 契约：服务端显式结论，缺失 = 调用方自判 ──────────────────
//
// 分层：`code` 说"发生了什么"（i18n / 分支），`retryable` 只说"要不要原样再发一次"。
// 因此这三条断言是硬约束：
//  1. 没表态时 JSON **里根本没有 retryable 这个键**（不能是 false —— "不知道" ≠ "不行"）；
//  2. 表态时是真的布尔，客户端可以 stringify 后直接读；
//  3. 429 与 409 session_busy 这些"有真实依据"的路径必须表态为 true。

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]interface{} {
	t.Helper()
	var body map[string]interface{}
	// 用 Bytes() 而非 Decoder：同一个 recorder 在一个用例里可能被断言多次，
	// 而 Decoder 会**消费** reader —— 第二次调用就只剩 EOF。
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON：%v", err)
	}
	return body
}

func TestJSON_OmitsRetryableWhenUnstated(t *testing.T) {
	rec := httptest.NewRecorder()
	BadRequest(rec, ErrInvalidReq)

	if _, present := decodeBody(t, rec)["retryable"]; present {
		t.Fatalf("服务端没表态时不该出现 retryable 键（留空 = 调用方自判），得到 %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"code":"`+CodeInvalidRequest+`"`) {
		t.Fatalf("code 仍须照常带上（负责 i18n 与分支），得到 %s", rec.Body.String())
	}
}

func TestJSONWithRetryable_EmitsExplicitValue(t *testing.T) {
	for _, want := range []bool{true, false} {
		rec := httptest.NewRecorder()
		JSONWithRetryable(rec, http.StatusConflict, CodeSessionBusy, nil, "session busy", want)

		body := decodeBody(t, rec)
		got, ok := body["retryable"]
		if !ok {
			t.Fatalf("want=%v：显式表态必须出现在 JSON 里，得到 %s", want, rec.Body.String())
		}
		if got != want {
			t.Fatalf("retryable = %v, want %v", got, want)
		}
		if body["code"] != CodeSessionBusy {
			t.Fatalf("code = %v, want %v", body["code"], CodeSessionBusy)
		}
	}
}

func TestTooManyRequests_IsRetryable(t *testing.T) {
	rec := httptest.NewRecorder()
	TooManyRequests(rec)

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("状态码 = %d, want 429", rec.Code)
	}
	if got := decodeBody(t, rec)["retryable"]; got != true {
		t.Fatalf("429 的唯一含义是\"窗口没开\"，必须 retryable=true，得到 %v（body=%s）", got, rec.Body.String())
	}
}

func TestLogAndRespond_LeavesRetryableUnstated(t *testing.T) {
	// 上游错误的性质网关读不出来 —— 不能替调用方猜，必须留空。
	rec := httptest.NewRecorder()
	logAndRespond(rec, http.ErrHandlerTimeout, http.StatusBadGateway, "上游不可达")

	if _, present := decodeBody(t, rec)["retryable"]; present {
		t.Fatalf("无法判定重试价值时必须留空，得到 %s", rec.Body.String())
	}
}

func TestInteractionEngineError_ReadsRetryableFromEnvelope(t *testing.T) {
	cases := []struct {
		name      string
		resp      map[string]interface{}
		wantHas   bool
		wantRetry bool
		wantCode  string
	}{
		{
			name:      "引擎说是暂时性写失败",
			resp:      map[string]interface{}{"ok": false, "error_code": "storage_error", "error": "db down", "retryable": true},
			wantHas:   true,
			wantRetry: true,
			wantCode:  CodeInternal,
		},
		{
			name:      "引擎说是确定性拒绝",
			resp:      map[string]interface{}{"ok": false, "error_code": "stale", "error": "gone", "retryable": false},
			wantHas:   true,
			wantRetry: false,
			wantCode:  CodeRequestFailed,
		},
		{
			name:    "引擎没表态",
			resp:    map[string]interface{}{"ok": false, "error_code": "not_found", "error": "nope"},
			wantHas: false,
			wantCode: CodeNotFound,
		},
		{
			name:    "非布尔一律当没表态（不猜 \"true\" / 1）",
			resp:    map[string]interface{}{"ok": false, "error_code": "not_found", "error": "nope", "retryable": "true"},
			wantHas: false,
			wantCode: CodeNotFound,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, code, _, retryable := interactionEngineError(tc.resp)
			if code != tc.wantCode {
				t.Fatalf("code = %q, want %q", code, tc.wantCode)
			}
			if tc.wantHas {
				if retryable == nil {
					t.Fatal("期望读到服务端结论，但拿到 nil")
				}
				if *retryable != tc.wantRetry {
					t.Fatalf("retryable = %v, want %v", *retryable, tc.wantRetry)
				}
			} else if retryable != nil {
				t.Fatalf("引擎没表态时必须留空，拿到 %v", *retryable)
			}
		})
	}
}

func TestRespondInteractionError_PassesThroughEngineRetryable(t *testing.T) {
	rec := httptest.NewRecorder()
	respondInteractionError(rec, map[string]interface{}{
		"ok": false, "error_code": "storage_error", "error": "db down", "retryable": true,
	})
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("状态码 = %d, want 500", rec.Code)
	}
	if got := decodeBody(t, rec)["retryable"]; got != true {
		t.Fatalf("引擎给的 true 必须原样透传（500 不该让客户端以为不可重试），得到 %v", got)
	}

	// 引擎没表态 ⇒ 网关也不许替它补 false。
	rec = httptest.NewRecorder()
	respondInteractionError(rec, map[string]interface{}{
		"ok": false, "error_code": "not_found", "error": "nope",
	})
	if _, present := decodeBody(t, rec)["retryable"]; present {
		t.Fatalf("引擎没表态时网关必须留空，得到 %s", rec.Body.String())
	}
}

func TestRejectSessionBusy_IsRetryable(t *testing.T) {
	rec := httptest.NewRecorder()
	rejectSessionBusy(rec, "this session already has a turn in progress")

	if got := decodeBody(t, rec)["retryable"]; got != true {
		t.Fatalf("会话忙是暂时性的，重试是正确策略，必须 retryable=true，得到 %v", got)
	}
	if got := decodeBody(t, rec)["code"]; got != CodeSessionBusy {
		t.Fatalf("code = %v, want %s", got, CodeSessionBusy)
	}
}

func TestRejectTenantQuota_IsRetryable(t *testing.T) {
	rec := httptest.NewRecorder()
	rejectTenantQuota(rec)

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("状态码必须仍是 429（客户端按它退避）: got %d", rec.Code)
	}
	if got := decodeBody(t, rec)["retryable"]; got != true {
		t.Fatalf("429 的意思是速率窗口没开，必须 retryable=true，得到 %v（body=%s）",
			got, rec.Body.String())
	}
	if got := decodeBody(t, rec)["code"]; got != CodeQuotaExceeded {
		t.Fatalf("code = %v, want %s", got, CodeQuotaExceeded)
	}
}
