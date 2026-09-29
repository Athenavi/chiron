package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
)

// newApprovalTestHandler 起一个假引擎（httptest），把转发到的 body 逐键写进 seen。
//
// seen 用 map 而非结构体：本组的重点就是"字段有没有丢"，逐键断言比结构体更直白。
func newApprovalTestHandler(t *testing.T, seen map[string]any) (*SubmitHandler, func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/agent/approval" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("bad body: %v", err)
		}
		for k, v := range body {
			seen[k] = v
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	}))
	h := NewSubmitHandler(engine.NewPythonClient(srv.URL), nil, nil, nil)
	return h, srv.Close
}

func postApproval(t *testing.T, h *SubmitHandler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/approval", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.SubmitApproval(rec, req)
	return rec
}

// C5：`decision=edit` 与编辑后的 `arguments` 必须**原样**透给引擎 —— 丢任何一个，
// 引擎侧就会退化成"批准原始参数"，即"批准的不是执行的那一次"。
func TestSubmitApprovalForwardsEditDecision(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newApprovalTestHandler(t, seen)
	defer closeSrv()

	rec := postApproval(t, h, `{"session_id":"s1","tool_call_id":"call_1","decision":"edit","arguments":"{\"cmd\":\"ls -la\"}"}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["decision"] != "edit" {
		t.Fatalf("decision 未透传: %v", seen)
	}
	if seen["arguments"] != `{"cmd":"ls -la"}` {
		t.Fatalf("arguments 未透传: %v", seen)
	}
}

// 旧前端（只发 approved）行为不变：字段被照常转发，且不需要 decision。
func TestSubmitApprovalLegacyApprovedStillForwards(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newApprovalTestHandler(t, seen)
	defer closeSrv()

	rec := postApproval(t, h, `{"session_id":"s1","tool_call_id":"call_1","approved":true}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["approved"] != true {
		t.Fatalf("approved 未透传: %v", seen)
	}
	if _, ok := seen["decision"]; ok {
		t.Fatalf("未提供 decision 时不应凭空补字段: %v", seen)
	}
}

// 非法决策值：网关当场 400，不必绕一圈才失败。
func TestSubmitApprovalRejectsUnknownDecision(t *testing.T) {
	h, closeSrv := newApprovalTestHandler(t, map[string]any{})
	defer closeSrv()

	rec := postApproval(t, h, `{"session_id":"s1","tool_call_id":"call_1","decision":"maybe"}`)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
}

// edit 却不给参数：同样 400（引擎侧还会再校验一次，见 parse_approval_payload）。
func TestSubmitApprovalRejectsEditWithoutArguments(t *testing.T) {
	h, closeSrv := newApprovalTestHandler(t, map[string]any{})
	defer closeSrv()

	for _, body := range []string{
		`{"session_id":"s1","tool_call_id":"call_1","decision":"edit"}`,
		`{"session_id":"s1","tool_call_id":"call_1","decision":"edit","arguments":"   "}`,
	} {
		rec := postApproval(t, h, body)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("body=%s code=%d", body, rec.Code)
		}
	}
}

// 既没 approved 也没 decision：字段级校验放行（它是引擎的语义校验），
// 但这里断言**不 panic**且仍被转发 —— 引擎会回 ok=false + 明确原因。
func TestSubmitApprovalWithoutAnyDecisionIsForwarded(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newApprovalTestHandler(t, seen)
	defer closeSrv()

	rec := postApproval(t, h, `{"session_id":"s1","tool_call_id":"call_1"}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if len(seen) == 0 {
		t.Fatal("请求应被转发（由引擎判定 ok=false）")
	}
}
