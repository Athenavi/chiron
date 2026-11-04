package api

import "testing"

// 队列准入的边界（roadmap P1-1.1）。
//
// 这里最值得钉死的是"上限 20"到底含不含第 20 条：写成 `>` 会变成最多排 21 条，
// 而 21 与 20 的差别没人会在代码评审时看出来，只会表现成"偶尔比预期多一条"。
func TestDecideThreadDelivery(t *testing.T) {
	cases := []struct {
		name    string
		pending int
		want    threadDeliveryDecision
	}{
		{"空队列可进", 0, threadDeliverQueued},
		{"未满可进", maxPendingThreadEvents - 1, threadDeliverQueued},
		// 上限的含义是"最多 20 条在等"，第 21 条必须被拒。
		{"恰好满则丢", maxPendingThreadEvents, threadDeliverDropped},
		{"已超限仍丢", maxPendingThreadEvents + 5, threadDeliverDropped},
		// 负数只可能是调用方读到不一致快照：此刻**不丢**是安全的一侧，
		// 丢事件不可逆，多排一条只是延迟。
		{"负数不丢", -1, threadDeliverQueued},
	}
	for _, c := range cases {
		if got := decideThreadDelivery(c.pending); got != c.want {
			t.Errorf("%s: decideThreadDelivery(%d) = %q, want %q", c.name, c.pending, got, c.want)
		}
	}
}
