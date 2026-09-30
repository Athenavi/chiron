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

// newInterruptTestHandler 起一个假引擎（httptest），把转发到的路径与 body 逐键写进 seen。
//
// seen 用 map 而非结构体：本组的重点就是"字段有没有丢 / 有没有打到对的路由"，逐键断言更直白。
func newInterruptTestHandler(t *testing.T, seen map[string]any) (*SubmitHandler, func()) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen["__path"] = r.URL.Path
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

func postInterrupt(t *testing.T, h *SubmitHandler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/agent/interrupt", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.SubmitInterrupt(rec, req)
	return rec
}

// 转发路径必须指向引擎的中断端点 —— 打到 approval/answer 上会变成"静默批准 / 答非所问"。
func TestSubmitInterruptForwardsToEngineInterruptRoute(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInterruptTestHandler(t, seen)
	defer closeSrv()

	rec := postInterrupt(t, h, `{"session_id":"s1"}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["__path"] != "/v1/agent/interrupt" {
		t.Errorf("path: want /v1/agent/interrupt, got %v", seen["__path"])
	}
	if seen["session_id"] != "s1" {
		t.Errorf("session_id 必须透传, got %v", seen["session_id"])
	}
}

func TestSubmitInterruptRequiresSessionID(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInterruptTestHandler(t, seen)
	defer closeSrv()

	rec := postInterrupt(t, h, `{}`)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code=%d, want 400", rec.Code)
	}
	if _, hit := seen["__path"]; hit {
		t.Error("缺 session_id 时不该转发到引擎")
	}
}

func TestSubmitInterruptRejectsBadJSON(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInterruptTestHandler(t, seen)
	defer closeSrv()

	rec := postInterrupt(t, h, `{not json`)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code=%d, want 400", rec.Code)
	}
}

// 归属映射不可用时**仍要转发**：中断与 approval 的关键差别就在这里 —— run 可能在别的副本，
// 引擎那边会走 Redis 信号那条路。本用例没有 Redis，正好验证"没拿到 run_token 也照发"。
func TestSubmitInterruptForwardsWithoutRunToken(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInterruptTestHandler(t, seen)
	defer closeSrv()

	rec := postInterrupt(t, h, `{"session_id":"s3"}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if v, has := seen["run_token"]; has {
		t.Errorf("没有归属映射时不该凭空造 run_token, got %v", v)
	}
}

// 身份字段确实会被转发（**权威来源是网关的 authMW**，见 gateway_router 的注册行：
// 从已验证的 JWT claims 覆盖 body 里的同名字段）。这里只钉住转发链路本身不丢字段。
func TestSubmitInterruptForwardsUserID(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInterruptTestHandler(t, seen)
	defer closeSrv()

	rec := postInterrupt(t, h, `{"session_id":"s2","user_id":"u-1"}`)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["user_id"] != "u-1" {
		t.Errorf("user_id 应被透传, got %v", seen["user_id"])
	}
}
