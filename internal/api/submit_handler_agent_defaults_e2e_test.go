package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
)

// M20 的端到端断言：后台默认值必须**出现在发给引擎的请求体里**。
//
// 纯函数测试证明"取值/注入逻辑对"，这条证明"那段逻辑真的接在提交链路上" ——
// 本批的缺陷形态正是"逻辑写了，但没人调用它"。
//
// 这里不需要 PG：session.Manager 的写路径在 `pool == nil` 时直接返回；
// 假引擎只回一条 `done`，足以让收尾路径跑完。
func TestHandleSubmitSendsAgentDefaultsToEngine(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/agent/submit" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("bad body: %v", err)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprint(w, "data: {\"type\":\"done\"}\n\n")
	}))
	t.Cleanup(srv.Close)

	h := NewSubmitHandler(
		engine.NewPythonClient(srv.URL),
		session.NewManager(nil, nil),
		broadcast.NewHub(nil),
		nil,
		&config.Config{AgentMaxOutputTokens: 2048, AgentDefaultMaxTurns: 7},
	)
	h.HandleSubmit(context.Background(), "u1", "s1", "hi",
		map[string]interface{}{}, nil, nil)

	llm, _ := got["llm_config"].(map[string]any)
	if llm == nil {
		t.Fatalf("请求体没有 llm_config: %#v", got)
	}
	// JSON 解出来是 float64；这里同时接受 int（若将来换成结构化编码）。
	switch v := llm["max_tokens"].(type) {
	case float64:
		if v != 2048 {
			t.Fatalf("agent.max_tokens 未进请求体: %v", v)
		}
	case int:
		if v != 2048 {
			t.Fatalf("agent.max_tokens 未进请求体: %v", v)
		}
	default:
		t.Fatalf("llm_config.max_tokens 缺失: %#v", llm)
	}
	if v, ok := got["max_turns"].(float64); !ok || v != 7 {
		t.Fatalf("agent.max_turns 未进请求体: %#v", got["max_turns"])
	}
}

// 显式给的值优先：后台默认值不能盖掉前端 / Agent 档案的选择。
func TestHandleSubmitKeepsExplicitMaxTokens(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("bad body: %v", err)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprint(w, "data: {\"type\":\"done\"}\n\n")
	}))
	t.Cleanup(srv.Close)

	h := NewSubmitHandler(
		engine.NewPythonClient(srv.URL),
		session.NewManager(nil, nil),
		broadcast.NewHub(nil),
		nil,
		&config.Config{AgentMaxOutputTokens: 2048, AgentDefaultMaxTurns: 7},
	)
	h.HandleSubmit(context.Background(), "u1", "s1", "hi",
		map[string]interface{}{"max_tokens": float64(1234), "max_turns": float64(3)}, nil, nil)

	llm, _ := got["llm_config"].(map[string]any)
	if llm == nil {
		t.Fatalf("请求体没有 llm_config: %#v", got)
	}
	if llm["max_tokens"] != float64(1234) {
		t.Fatalf("显式 max_tokens 被后台默认值覆盖: %#v", llm["max_tokens"])
	}
	if got["max_turns"] != float64(3) {
		t.Fatalf("显式 max_turns 被后台默认值覆盖: %#v", got["max_turns"])
	}
}
