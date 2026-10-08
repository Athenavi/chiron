package monitor

import (
	"testing"
	"time"
)

// monitor 此前是"零测试包"之一。这里挑三块**会静默出错**的逻辑钉住：
// ① 环形直方图的"只保留最后 N 个样本"；② 计数器/快照的增量口径；
// ③ 会话成本表的容量淘汰与 TTL 过期。
//
// ⚠ 包级状态（Global / 环形缓冲 / maxSessionCosts / sessionCostTTL）是全局的，
// 每个用例都必须**先快照、后还原**（否则污染同进程的其它用例 —— 见 vendor/规划.md §4）。

func snapshotCostState() (map[string]*SessionCostWithTTL, []string, int, int, int, time.Duration) {
	buf := make([]string, len(sessionCostRing))
	copy(buf, sessionCostRing)
	cp := make(map[string]*SessionCostWithTTL, len(costBySession))
	for k, v := range costBySession {
		cp[k] = v
	}
	return cp, buf, sessionCostHead, sessionCostCount, maxSessionCosts, sessionCostTTL
}

func restoreCostState(cp map[string]*SessionCostWithTTL, ring []string, head, count, max int, ttl time.Duration) {
	costBySession = cp
	sessionCostRing = ring
	sessionCostHead = head
	sessionCostCount = count
	maxSessionCosts = max
	sessionCostTTL = ttl
}

func TestHistogramEmptySnapshot(t *testing.T) {
	h := NewHistogram("empty", 4)
	snap := h.Snapshot()
	if snap["name"] != "empty" || snap["count"] != 0 {
		t.Fatalf("空直方图快照不对：%v", snap)
	}
}

func TestHistogramKeepsOnlyLastSamples(t *testing.T) {
	h := NewHistogram("ring", 3)
	for i := 1; i <= 5; i++ {
		h.Record(time.Duration(i) * time.Millisecond)
	}
	snap := h.Snapshot()
	if snap["count"] != 3 {
		t.Fatalf("容量 3 只应保留 3 个样本，实际 %v", snap["count"])
	}
	// 环形缓冲应保留**最后**三个（3/4/5ms）⇒ 最大值必须是 5ms
	if snap["max"] != int64(5) {
		t.Fatalf("应保留最后三个样本（max=5ms），实际 max=%v", snap["max"])
	}
}

func TestHistogramPercentilesAreOrdered(t *testing.T) {
	h := NewHistogram("pct", 100)
	for i := 1; i <= 100; i++ {
		h.Record(time.Duration(i) * time.Millisecond)
	}
	snap := h.Snapshot()
	p50, p95, p99 := snap["p50"].(int64), snap["p95"].(int64), snap["p99"].(int64)
	mx := snap["max"].(int64)
	if !(p50 <= p95 && p95 <= p99 && p99 <= mx) {
		t.Fatalf("分位数必须单调：p50=%d p95=%d p99=%d max=%d", p50, p95, p99, mx)
	}
	if mx != 100 {
		t.Fatalf("最大值应为最后样本 100ms，实际 %d", mx)
	}
}

func TestCounterHelpersUseDeltas(t *testing.T) {
	before := Global.RequestsTotal.Load()
	beforeActive := Global.RequestsActive.Load()

	IncRequests()
	IncRequests()
	if got := Global.RequestsTotal.Load() - before; got != 2 {
		t.Fatalf("IncRequests 应 +2 total，实际 +%d", got)
	}
	if got := Global.RequestsActive.Load() - beforeActive; got != 2 {
		t.Fatalf("IncRequests 应 +2 active，实际 +%d", got)
	}
	DecRequests()
	if got := Global.RequestsActive.Load() - beforeActive; got != 1 {
		t.Fatalf("DecRequests 应 -1 active，实际 +%d", got)
	}

	// 配对语义：一次 Inc + 一次 Dec 净变化为 0（用增量而不是绝对值，避免依赖全局初值）
	wsBefore := Global.WebSocketConns.Load()
	IncWebSocketConn()
	DecWebSocketConn()
	if got := Global.WebSocketConns.Load(); got != wsBefore {
		t.Fatalf("Inc/DecWebSocketConn 应净变化为 0，实际 %d → %d", wsBefore, got)
	}
}

func TestSnapshotCarriesRuntimeAndUptimeKeys(t *testing.T) {
	snap := Snapshot()
	for _, key := range []string{
		"requests_total", "llm_calls", "tool_calls", "uptime_seconds", "started_at",
		"go_goroutines", "go_memory_alloc_bytes", "go_gc_runs",
	} {
		if _, ok := snap[key]; !ok {
			t.Fatalf("快照缺少键 %q", key)
		}
	}
	if _, ok := snap["uptime_seconds"].(float64); !ok {
		t.Fatalf("uptime_seconds 应是数值，实际 %T", snap["uptime_seconds"])
	}
	if _, err := time.Parse(time.RFC3339, snap["started_at"].(string)); err != nil {
		t.Fatalf("started_at 应是 RFC3339：%v", err)
	}
}

func TestRegisterExtraStatsMergesAndSkipsNil(t *testing.T) {
	RegisterExtraStats(func() map[string]interface{} {
		return map[string]interface{}{"extra_probe_value": 42}
	})
	RegisterExtraStats(func() map[string]interface{} { return nil }) // nil 应被跳过而不是 panic

	snap := Snapshot()
	if snap["extra_probe_value"] != 42 {
		t.Fatalf("外部统计未合并进快照：%v", snap["extra_probe_value"])
	}
}

func TestSessionUsageAccumulatesAndRefreshes(t *testing.T) {
	cp, ring, head, count, max, ttl := snapshotCostState()
	t.Cleanup(func() { restoreCostState(cp, ring, head, count, max, ttl) })

	sid := "s-accumulate"
	RecordSessionUsage(sid, 10, 5)
	RecordSessionUsage(sid, 1, 2)

	got := GetSessionCost(sid)
	if got.InputTokens != 11 || got.OutputTokens != 7 {
		t.Fatalf("token 应累加：%+v", got)
	}
	if got.TotalCalls != 2 {
		t.Fatalf("调用次数应为 2，实际 %d", got.TotalCalls)
	}
	if unknown := GetSessionCost("nope"); unknown != (SessionCost{}) {
		t.Fatalf("未知会话应返回零值，实际 %+v", unknown)
	}
}

func TestSessionCostExpiryIsDropped(t *testing.T) {
	cp, ring, head, count, max, ttl := snapshotCostState()
	t.Cleanup(func() { restoreCostState(cp, ring, head, count, max, ttl) })

	sid := "s-expired"
	RecordSessionUsage(sid, 3, 4)
	// 直接把过期时间拨到过去（等价于 TTL 到期），避免真的等 30 分钟
	costMu.Lock()
	costBySession[sid].ExpiresAt = time.Now().Add(-time.Second)
	costMu.Unlock()

	if got := GetSessionCost(sid); got != (SessionCost{}) {
		t.Fatalf("已过期会话应返回零值，实际 %+v", got)
	}
	if _, ok := AllSessionCosts()[sid]; ok {
		t.Fatal("AllSessionCosts 应剔除已过期会话")
	}
}

func TestSessionCostRingEvictsOldestAtCapacity(t *testing.T) {
	cp, ring, head, count, max, ttl := snapshotCostState()
	t.Cleanup(func() { restoreCostState(cp, ring, head, count, max, ttl) })

	// 把容量缩到 3，便于验证"满了淘汰最老"
	maxSessionCosts = 3
	sessionCostRing = make([]string, 3)
	sessionCostHead = 0
	sessionCostCount = 0
	costBySession = make(map[string]*SessionCostWithTTL)

	for _, sid := range []string{"a", "b", "c", "d"} {
		RecordSessionUsage(sid, 1, 1)
	}
	if len(costBySession) != 3 {
		t.Fatalf("容量为 3 时表内应只有 3 个会话，实际 %d", len(costBySession))
	}
	if _, ok := costBySession["a"]; ok {
		t.Fatal("最老的会话 a 应被淘汰")
	}
	for _, sid := range []string{"b", "c", "d"} {
		if _, ok := costBySession[sid]; !ok {
			t.Fatalf("会话 %s 不应被淘汰：%v", sid, costBySession)
		}
	}
}
