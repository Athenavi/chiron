package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/engine"
)

// A1 的**独立 `thinking` 事件必须落库** —— 否则"实时看得到、刷新就没了"。
//
// 为什么值得单独一条：引擎自 A1 起不再把 native reasoning 包进 `text`，改为发独立
// `thinking` 事件；而网关的落库累计**只认 `evt.Type == "text"`**（`submit_handler.go`
// 里那条 `[thinking]` 前缀分支是**旧文本路径**）。2026-10-09 复核
// `docs/reasonix-gap-analysis.md` 的自家不一致清单时确认：新路径**没有落库** ⇒
// assistant 的 content 只剩正文，前端历史回放拿不到思考块。
//
// 手法：假引擎（httptest）按序发 `thinking` / `text` / `done`，**真 PG** 落库，
// 再把消息读回来断言 —— 这样验的是"用户刷新后真的还能看到思考"，而不是"代码里加了一行"。
func TestLiveSubmitPersistsThinkingEvent(t *testing.T) {
	mgr, pool := withLiveSessionManager(t)
	owner := liveUser(t, pool)
	sid := liveSession(t, mgr, owner)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		for _, ev := range []string{
			`{"type":"thinking","content":"先想一下"}`,
			`{"type":"text","content":"答案"}`,
			`{"type":"done","content":""}`,
		} {
			_, _ = fmt.Fprintf(w, "data: %s\n\n", ev)
		}
	}))
	t.Cleanup(srv.Close)

	h := NewSubmitHandler(engine.NewPythonClient(srv.URL), mgr, broadcast.NewHub(nil), nil)
	h.HandleSubmit(context.Background(), owner, sid, "hi", map[string]interface{}{}, nil)

	msgs, err := mgr.GetMessages(context.Background(), sid, 50)
	if err != nil {
		t.Fatalf("读回消息失败: %v", err)
	}
	var assistant string
	for _, m := range msgs {
		if m.Role == "assistant" {
			assistant = m.Content
		}
	}
	if assistant == "" {
		t.Fatal("没有落库 assistant 消息 —— 落库路径本身没跑通，本用例无效")
	}
	if !strings.Contains(assistant, "[thinking]先想一下[/thinking]") {
		t.Fatalf("思考没有落库（刷新后会丢）—— content=%q", assistant)
	}
	if !strings.Contains(assistant, "答案") {
		t.Fatalf("正文没落库 —— content=%q", assistant)
	}
}
