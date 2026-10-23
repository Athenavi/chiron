package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
)

// M21 的网关半边：两条新边界的判据。
//
// 为什么把判据抽成函数再测，而不是连一条 SSE 实测：这段代码是**越权边界**
// （P0-S5 的会话隔离在用户维度上的对应物），而它长在长驻连接的读循环里 ——
// 直接测连接既慢又容易 flaky，变异验证也无从下手。

func TestSSEEventMatchesSessionChannel(t *testing.T) {
	ev := broadcast.Event{Type: "text", SessionID: "s1"}

	if !sseEventMatches(ev, "s1", "") {
		t.Fatal("同会话的事件必须转发")
	}
	if sseEventMatches(ev, "s2", "") {
		t.Fatal("别的会话的事件**绝不能**转发（会话隔离）")
	}
	// 无 session 的系统事件（connected / 系统通知）在会话通道上照发 —— 既有行为。
	if !sseEventMatches(broadcast.Event{Type: "connected"}, "s1", "") {
		t.Fatal("无会话归属的系统事件应照发")
	}
}

func TestSSEEventMatchesUserChannel(t *testing.T) {
	ev := broadcast.Event{Type: "interaction_pending", SessionID: "s9", UserID: "u1"}

	if !sseEventMatches(ev, "", "u1") {
		t.Fatal("挂在自己名下的事件必须转发（跨会话角标就靠它）")
	}
	if sseEventMatches(ev, "", "u2") {
		t.Fatal("别人的待办提醒**绝不能**转发到我的用户通道")
	}
	// 没有 UserID 的事件（普通会话事件）不进用户通道：它不属于"某个人"。
	if sseEventMatches(broadcast.Event{Type: "text", SessionID: "s1"}, "", "u1") {
		t.Fatal("无 UserID 的事件不该进用户通道")
	}
}

func TestSSEEventMatchesRejectsUnidentifiedSubscriber(t *testing.T) {
	ev := broadcast.Event{Type: "text", SessionID: "s1", UserID: "u1"}

	if sseEventMatches(ev, "", "") {
		t.Fatal("既没有会话也没有用户的订阅者不该收到任何事件（防全站放行）")
	}
}

func TestInteractionEventFromPayload(t *testing.T) {
	ev, ok := interactionEventFromPayload(map[string]interface{}{
		"type":           "interaction_pending",
		"session_id":     "s1",
		"user_id":        "u1",
		"interaction_id": "i1",
	})
	if !ok {
		t.Fatal("完整 payload 必须可路由")
	}
	if ev.Type != "interaction_pending" || ev.SessionID != "s1" || ev.UserID != "u1" {
		t.Fatalf("映射结果不对: %+v", ev)
	}

	// 缺 user_id ⇒ 路由不到用户通道；缺 type ⇒ 前端认不出。两者都必须丢弃而不是硬塞。
	if _, ok := interactionEventFromPayload(map[string]interface{}{"type": "x"}); ok {
		t.Fatal("缺 user_id 必须丢弃")
	}
	if _, ok := interactionEventFromPayload(map[string]interface{}{"user_id": "u1"}); ok {
		t.Fatal("缺 type 必须丢弃")
	}
}

// ── 跨会话汇总端点的代理半边 ──────────────────────────────────────────────

func TestInteractionListForUserForwardsClaimIdentity(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen,
		`{"ok":true,"count":1,"items":[{"id":"i1","session_id":"s9","kind":"approval"}]}`)
	defer closeSrv()

	// 客户端伪造别人的身份：只有 claims 那份能被转发。
	req := httptest.NewRequest(http.MethodGet,
		"/v1/agent/interactions?user_id=attacker&tenant_id=t-victim", nil)
	req = req.WithContext(auth.WithClaims(req.Context(),
		&auth.Claims{UserID: "u-owner", TenantID: 1}))
	rec := httptest.NewRecorder()

	h.ListForUser(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	if seen["path"] != "/v1/agent/interactions" {
		t.Fatalf("转发路径不对: %v", seen["path"])
	}
	q, _ := seen["query"].(string)
	if !strings.Contains(q, "user_id=u-owner") || !strings.Contains(q, "tenant_id=1") {
		t.Fatalf("未按 claims 注入身份: %s", q)
	}
	if strings.Contains(q, "attacker") || strings.Contains(q, "t-victim") {
		t.Fatalf("客户端自报身份被转发出去了: %s", q)
	}
	out := decodeAPI(t, rec)
	data, _ := out.Data.(map[string]any)
	items, _ := data["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("items 未透传: %+v", data)
	}
}

func TestInteractionListForUserRequiresAuthenticatedClaims(t *testing.T) {
	seen := map[string]any{}
	h, closeSrv := newInteractionTestHandlers(t, seen, "")
	defer closeSrv()

	req := httptest.NewRequest(http.MethodGet, "/v1/agent/interactions", nil)
	rec := httptest.NewRecorder()

	h.ListForUser(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("无 claims 必须 401，got %d", rec.Code)
	}
	if len(seen) != 0 {
		t.Fatalf("无身份时不该打引擎: %#v", seen)
	}
}
