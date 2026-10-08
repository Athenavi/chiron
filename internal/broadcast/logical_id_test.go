package broadcast

import "testing"

// 多实例去重的判据就是"逻辑身份"：同一条逻辑事件无论被几个实例各写一次，key 必须相同；
// 不同事件必须不同。这里不需要 Redis（纯函数）。

func TestLogicalEventIDPrefersEventID(t *testing.T) {
	a := Event{Type: "subagent_status", Data: map[string]any{"event_id": "abc", "seq": 1}}
	b := Event{Type: "subagent_status", Data: map[string]any{"event_id": "abc", "seq": 999}}
	if logicalEventID(a) != logicalEventID(b) {
		t.Fatalf("同 event_id 应得到同一逻辑身份：%q vs %q", logicalEventID(a), logicalEventID(b))
	}
	c := Event{Type: "subagent_status", Data: map[string]any{"event_id": "other"}}
	if logicalEventID(a) == logicalEventID(c) {
		t.Fatal("不同 event_id 不应同身份")
	}
	if got := logicalEventID(a); got != "subagent_status|abc" {
		t.Fatalf("身份应带类型前缀：%q", got)
	}
}

func TestLogicalEventIDFallsBackToPayloadHash(t *testing.T) {
	a := Event{Type: "subagent_status", Data: map[string]any{"run_id": "r1", "status": "running"}}
	b := Event{Type: "subagent_status", Data: map[string]any{"run_id": "r1", "status": "running"}}
	c := Event{Type: "subagent_status", Data: map[string]any{"run_id": "r1", "status": "done"}}
	if logicalEventID(a) == "" {
		t.Fatal("无 event_id 时应退化为哈希，而不是空身份")
	}
	if logicalEventID(a) != logicalEventID(b) {
		t.Fatal("内容相同的两条应同身份（退化路径的已知精度上限）")
	}
	if logicalEventID(a) == logicalEventID(c) {
		t.Fatal("内容不同不应同身份")
	}
}
