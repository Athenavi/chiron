package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestConn(gateway string) (*conn, *bytes.Buffer) {
	c := newConn(gateway, "tok")
	buf := &bytes.Buffer{}
	c.out = bufio.NewWriter(buf)
	return c, buf
}

// decodeLines 把 stdout 侧写出的行解析成 JSON 对象列表。
func decodeLines(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("output line is not JSON: %q (%v)", line, err)
		}
		out = append(out, m)
	}
	return out
}

func TestInitializeReportsCapabilities(t *testing.T) {
	c, buf := newTestConn("http://127.0.0.1:1") // 不实际调用
	c.dispatch(nil, []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`))

	msgs := decodeLines(t, buf)
	if len(msgs) != 1 {
		t.Fatalf("want 1 message, got %d", len(msgs))
	}
	res, ok := msgs[0]["result"].(map[string]any)
	if !ok {
		t.Fatalf("initialize must return a result: %+v", msgs[0])
	}
	if res["protocolVersion"] != float64(protocolVersion) {
		t.Fatalf("protocolVersion mismatch: %v", res["protocolVersion"])
	}
}

func TestUnknownMethodIsExplicitlyRejected(t *testing.T) {
	c, buf := newTestConn("http://127.0.0.1:1")
	c.dispatch(nil, []byte(`{"jsonrpc":"2.0","id":7,"method":"session/teleport","params":{}}`))

	msgs := decodeLines(t, buf)
	errObj, ok := msgs[0]["error"].(map[string]any)
	if !ok {
		t.Fatalf("unknown method must produce an error, got %+v", msgs[0])
	}
	if errObj["code"] != float64(codeMethodNotFound) {
		t.Fatalf("want method-not-found, got %v", errObj["code"])
	}
}

func TestParseError(t *testing.T) {
	c, buf := newTestConn("http://127.0.0.1:1")
	c.dispatch(nil, []byte(`{not json`))
	msgs := decodeLines(t, buf)
	errObj, ok := msgs[0]["error"].(map[string]any)
	if !ok || errObj["code"] != float64(codeParseError) {
		t.Fatalf("want parse error, got %+v", msgs[0])
	}
}

func TestPromptRequiresNonEmptyText(t *testing.T) {
	c, buf := newTestConn("http://127.0.0.1:1")
	c.dispatch(nil, []byte(`{"jsonrpc":"2.0","id":2,"method":"session/prompt","params":{"sessionId":"s1","prompt":[]}}`))
	msgs := decodeLines(t, buf)
	if _, ok := msgs[0]["error"].(map[string]any); !ok {
		t.Fatalf("empty prompt must be rejected, got %+v", msgs[0])
	}
}

// TestStreamPromptMapsEvents 用一个假网关（SSE）验证事件 → ACP session/update 的映射。
func TestStreamPromptMapsEvents(t *testing.T) {
	sse := strings.Join([]string{
		`data: {"type":"thinking","content":"let me think"}`,
		`data: {"type":"text","content":"hello"}`,
		`data: {"type":"tool_call","tool_call_id":"c1","tool_name":"read_file","tool_arguments":"{}"}`,
		`data: {"type":"tool_result","tool_call_id":"c1","content":"{\"ok\":true}"}`,
		`data: {"type":"done"}`,
		"",
	}, "\n\n")

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/agent/submit" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		fmt.Fprint(w, sse)
	}))
	defer srv.Close()

	c, buf := newTestConn(srv.URL)
	stop, err := c.streamPrompt(t.Context(), "s1", "hi")
	if err != nil {
		t.Fatalf("streamPrompt failed: %v", err)
	}
	if stop != stopEndTurn {
		t.Fatalf("want end_turn, got %s", stop)
	}

	msgs := decodeLines(t, buf)
	var kinds []string
	for _, m := range msgs {
		if m["method"] != "session/update" {
			t.Fatalf("expected only session/update notifications, got %+v", m)
		}
		p := m["params"].(map[string]any)
		u := p["update"].(map[string]any)
		kinds = append(kinds, u["sessionUpdate"].(string))
	}
	want := []string{updateAgentThought, updateAgentMessage, updateToolCall, updateToolCallDone}
	if strings.Join(kinds, ",") != strings.Join(want, ",") {
		t.Fatalf("update kinds mismatch:\n got %v\nwant %v", kinds, want)
	}
}

func TestStreamPromptMarksFailedToolCall(t *testing.T) {
	sse := "data: {\"type\":\"tool_result\",\"tool_call_id\":\"c9\",\"content\":\"{\\\"error\\\":\\\"boom\\\"}\"}\n\ndata: {\"type\":\"done\"}\n\n"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, sse)
	}))
	defer srv.Close()

	c, buf := newTestConn(srv.URL)
	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err != nil {
		t.Fatalf("streamPrompt failed: %v", err)
	}
	msgs := decodeLines(t, buf)
	u := msgs[0]["params"].(map[string]any)["update"].(map[string]any)
	if u["status"] != "failed" {
		t.Fatalf("error result must map to failed status, got %v", u["status"])
	}
}

func TestStreamPromptSurfacesGatewayRejection(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	defer srv.Close()

	c, _ := newTestConn(srv.URL)
	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err == nil {
		t.Fatalf("gateway rejection must surface as an error")
	}
}
