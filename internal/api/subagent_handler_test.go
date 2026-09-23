package api

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

// 跨语言契约：Redis key 必须与引擎侧 app/subagent/runtime_cache.py 的 key 逐字一致，
// 否则前端会出现"查不到子 Agent 进度"这类难以定位的问题。
func TestSubagentKeyMatchesEngineConvention(t *testing.T) {
	cases := []struct {
		parts []string
		want  string
	}{
		{[]string{"t1", "run", "rs_1"}, "subagent:t1:run:rs_1"},
		{[]string{"t1", "ev", "rs_1"}, "subagent:t1:ev:rs_1"},
		{[]string{"t1", "children", "rs_0"}, "subagent:t1:children:rs_0"},
		{[]string{"t1", "tree", "s1"}, "subagent:t1:tree:s1"},
	}
	for _, c := range cases {
		if got := subagentKey(c.parts...); got != c.want {
			t.Errorf("subagentKey(%v) = %q, want %q", c.parts, got, c.want)
		}
	}
}

func TestSubagentTenantFallsBackToUserID(t *testing.T) {
	if got := subagentTenant(nil); got != "" {
		t.Errorf("nil claims → %q, want empty", got)
	}
	if got := subagentTenant(&auth.Claims{TenantID: "t1", UserID: "u1"}); got != "t1" {
		t.Errorf("tenant should win, got %q", got)
	}
	if got := subagentTenant(&auth.Claims{UserID: "u1"}); got != "u1" {
		t.Errorf("should fall back to user id, got %q", got)
	}
}

// 前端只认一份字段契约，Redis 与 DB 两条来源必须归一。
func TestNormalizeRunRowContract(t *testing.T) {
	row := map[string]interface{}{
		"id":             "rs_1",
		"parent_run_id":  "rs_0",
		"depth":          2,
		"profile_name":   "reviewer",
		"status":         "completed",
		"summary":        "结论",
		"input_tokens":   10,
		"output_tokens":  5,
		"steps":          3,
		"redacted_count": 2,
		"created_at":     time.Date(2026, 9, 21, 10, 0, 0, 0, time.UTC),
	}
	view := normalizeRunRow(row)

	if view["run_id"] != "rs_1" || view["status"] != "completed" {
		t.Fatalf("core fields wrong: %+v", view)
	}
	if view["depth"] != 2 {
		t.Errorf("depth = %v, want 2", view["depth"])
	}
	usage, ok := view["usage"].(map[string]interface{})
	if !ok || usage["steps"] != 3 || usage["input_tokens"] != 10 {
		t.Errorf("usage contract wrong: %+v", view["usage"])
	}
	if view["redacted_count"] != 2 {
		t.Errorf("redacted_count lost: %+v", view)
	}
	if view["created_at"] != "2026-09-21T10:00:00Z" {
		t.Errorf("created_at = %v", view["created_at"])
	}
	// 空字段不下发（保持帧精简）
	empty := normalizeRunRow(map[string]interface{}{"id": "rs_2", "status": "running"})
	if _, present := empty["parent_run_id"]; present {
		t.Error("empty parent_run_id should be omitted")
	}
	if empty["usage"].(map[string]interface{})["steps"] != 0 {
		t.Error("missing counters should default to 0")
	}
}

// 回放契约：DB steps 的 kind → 前端事件名。审批必须是**独立**事件名 ——
// 否则刷新页面后审批卡片退化成一行文字（看得到、批不了）。
func TestStepKindToEventTypeCoversApproval(t *testing.T) {
	cases := map[string]string{
		"message":     "subagent.text",
		"tool_call":   "subagent.status",
		"tool_result": "subagent.status",
		"approval":    "subagent.approval",
		"ask":         "subagent.ask",
		"unknown":     "subagent.notice",
	}
	for kind, want := range cases {
		if got := stepKindToEventType(kind); got != want {
			t.Errorf("stepKindToEventType(%q) = %q, want %q", kind, got, want)
		}
	}
}

func TestStringOfAndIntOfTolerateDriverTypes(t *testing.T) {
	if stringOf(nil) != "" || stringOf("x") != "x" {
		t.Error("stringOf basic cases failed")
	}
	if stringOf(int64(7)) != "7" || stringOf(3.5) != "3.5" {
		t.Error("stringOf should format scalars")
	}
	if !strings.HasPrefix(stringOf(time.Time{}), "0001-01-01") {
		t.Error("time should render as RFC3339")
	}
	for _, tc := range []struct {
		in   interface{}
		want int
	}{{int(1), 1}, {int32(2), 2}, {int64(3), 3}, {float64(4), 4}, {"5", 5}, {"bad", 0}, {nil, 0}} {
		if got := intOf(tc.in); got != tc.want {
			t.Errorf("intOf(%v) = %d, want %d", tc.in, got, tc.want)
		}
	}
}

// 无 Redis 时 handler 必须安全降级（回落 DB），不 panic。
func TestSubagentHandlerWithoutRedisFallsBack(t *testing.T) {
	h := NewSubagentHandler(nil)
	if runs, err := h.treeFromRedis(context.Background(), "t1", "s1"); err != nil || len(runs) != 0 {
		t.Errorf("treeFromRedis with nil redis: runs=%v err=%v", runs, err)
	}
	if view, err := h.runFromRedis(context.Background(), "t1", "rs_1"); err != nil || view != nil {
		t.Errorf("runFromRedis with nil redis: view=%v err=%v", view, err)
	}
	if events, err := h.eventsFromRedis(context.Background(), "t1", "rs_1", 10); err != nil || len(events) != 0 {
		t.Errorf("eventsFromRedis with nil redis: events=%v err=%v", events, err)
	}
}
