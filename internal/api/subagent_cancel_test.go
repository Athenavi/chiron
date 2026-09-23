package api

import (
	"encoding/json"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
)

// 回执键与引擎侧 app/subagent/affinity.py 的 ack_key 必须逐字一致：
// 网关 BLPOP 的键和引擎 LPUSH 的键一旦不同，**每一次**取消都会退化成
// "无人认领 → 判 lost"（正常在跑的作业被误判失联）。
func TestCancelAckKeyMatchesEngineConvention(t *testing.T) {
	if got := cancelAckKey("rs_1"); got != "subagent:cancel:ack:rs_1" {
		t.Errorf("cancelAckKey = %q, want subagent:cancel:ack:rs_1", got)
	}
}

// 取消返回什么，由"有没有回执"决定 —— 这是"假成功"的边界，必须可单测。
func TestCancelStatusFor(t *testing.T) {
	if got := cancelStatusFor(true); got != "accepted" {
		t.Errorf("有回执应为 accepted，得到 %q", got)
	}
	if got := cancelStatusFor(false); got != "lost" {
		t.Errorf("无回执必须如实返回 lost（不能假装成功），得到 %q", got)
	}
}

// 跨语言契约：归属记录的字段名与引擎侧 _payload() 逐字对应。
// 字段名漂移会让网关把"有人在跑"读成"没人认领"（或反之），两种都是错的。
func TestSubagentOwnerContractMatchesEnginePayload(t *testing.T) {
	raw := `{"instance_id":"eng-1","url":"http://eng-1:8080","token":"tok",` +
		`"run_id":"rs_1","session_id":"s1","tenant_id":"t1",` +
		`"started_at":"2026-09-23T00:00:00Z","last_seen":"2026-09-23T00:00:00Z"}`
	var owner engine.SubagentOwner
	if err := json.Unmarshal([]byte(raw), &owner); err != nil {
		t.Fatalf("unmarshal engine payload: %v", err)
	}
	if owner.InstanceID != "eng-1" {
		t.Errorf("instance_id = %q", owner.InstanceID)
	}
	if owner.URL != "http://eng-1:8080" {
		t.Errorf("url = %q", owner.URL)
	}
	if owner.SessionID != "s1" || owner.TenantID != "t1" || owner.RunID != "rs_1" {
		t.Errorf("归属上下文丢失: %+v", owner)
	}
}

// 无 Redis 时查询归属必须给出"没有归属"的明确结论，而不是 panic。
func TestSubagentRunOwnerWithoutRedis(t *testing.T) {
	if _, ok := engine.SubagentRunOwner(t.Context(), "rs_1"); ok {
		t.Error("no redis → ok 应为 false")
	}
	if _, ok := engine.SubagentRunOwner(t.Context(), ""); ok {
		t.Error("empty run_id → ok 应为 false")
	}
}
