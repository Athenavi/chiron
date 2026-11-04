package api

import (
	"encoding/json"
	"testing"
)

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

// 投递行的输入快照必须能原样往返（roadmap P1-1 接线）。
//
// 这一层是"入队时存什么、认领时还能拿到什么"的唯一契约：快照里少了哪个字段，
// drain 时就只能把那一项渲染成占位符，而模型看到的是"结果为空"，不是"字段丢了" ——
// 那种退化在真机上只会表现为 Agent 反复重试同一个子任务。
func TestFollowupSnapshotRoundTrip(t *testing.T) {
	cases := []struct {
		name string
		in   agentFollowupPayload
	}{
		{
			name: "子 Agent 全字段",
			in: agentFollowupPayload{
				RunID: "run-1", SessionID: "sess-1", TenantID: "3", UserID: "user-1",
				Status: "completed", Profile: "code", Depth: 2, Summary: "改完了 3 个文件",
				Kind: "subagent",
			},
		},
		{
			name: "只带必需字段",
			// 摘要为空是正常情形（子 Agent 没写 summary），不能因此丢整条事件。
			in: agentFollowupPayload{RunID: "run-2", Status: "failed"},
		},
		{
			// 空载荷不是编程错误：入队方可能只给了键。
			name: "零值",
			in:   agentFollowupPayload{},
		},
	}
	for _, c := range cases {
		raw, err := followupSnapshot(c.in)
		if err != nil {
			t.Fatalf("%s: followupSnapshot: %v", c.name, err)
		}
		// 直接拿解出来的结果比：threadDelivery 只带身份字段，
		// 解码方会用行上的权威值覆盖它们，所以这里给空行，只验快照本身。
		got, err := followupFromSnapshot(threadDelivery{Payload: raw})
		if err != nil {
			t.Fatalf("%s: followupFromSnapshot: %v", c.name, err)
		}
		// 身份字段被行覆盖：空行的租户是 0，`tenantStr(0)` 渲染成 "0" 而不是空串，
		// 其余三项在空行上就是零值。这里如实写出，不做"看起来更整齐"的断言。
		want := c.in
		want.SessionID, want.UserID, want.Kind = "", "", ""
		want.TenantID = tenantStr(0)
		if got != want {
			t.Errorf("%s: 往返后不一致\n got = %+v\nwant = %+v", c.name, got, want)
		}
	}
}

// 行上的权威字段必须压过快照里的旧值。
//
// 会话搬家 / 租户迁移之后，一条老快照里的 user_id 已经不代表当前所有者；配额与计费
// 认这个字段，信快照就会把额度记到别人头上。
func TestFollowupSnapshotRowWins(t *testing.T) {
	raw, err := followupSnapshot(agentFollowupPayload{
		RunID: "run-9", SessionID: "stale-session", UserID: "stale-user",
		TenantID: "1", Status: "completed", Summary: "ok", Kind: "job",
	})
	if err != nil {
		t.Fatalf("followupSnapshot: %v", err)
	}
	got, err := followupFromSnapshot(threadDelivery{
		SessionID:  "sess-live",
		UserID:     "user-live",
		TenantID:   7,
		SourceKind: "subagent",
		Payload:    raw,
	})
	if err != nil {
		t.Fatalf("followupFromSnapshot: %v", err)
	}
	if got.SessionID != "sess-live" || got.UserID != "user-live" {
		t.Errorf("身份字段未以行为准: session=%q user=%q", got.SessionID, got.UserID)
	}
	// 租户要按 int16 渲染回字符串，否则 "07" 这类前导零会流进计费键。
	if got.TenantID != "7" {
		t.Errorf("TenantID = %q, want %q", got.TenantID, "7")
	}
	if got.Kind != "subagent" {
		t.Errorf("Kind = %q, want source_kind 的 %q", got.Kind, "subagent")
	}
	// 快照里独有的内容不能被覆盖掉：不然 drain 时正文就是空的。
	if got.RunID != "run-9" || got.Summary != "ok" || got.Status != "completed" {
		t.Errorf("快照内容丢失: %+v", got)
	}
}

// 空载荷要解成零值而不是报错。
//
// 列默认值就是 `{}`（0022 迁移），而历史行可能正是这个形状；
// 报错会让 drain 卡在一条永远解不开的行上，队列再也前进不了。
func TestFollowupFromSnapshotEmptyIsZero(t *testing.T) {
	for _, raw := range []json.RawMessage{nil, {}, json.RawMessage(`{}`), json.RawMessage("  \n ")} {
		got, err := followupFromSnapshot(threadDelivery{Payload: raw, UserID: "u", TenantID: 2})
		if err != nil {
			t.Fatalf("payload %q: 意外报错 %v", string(raw), err)
		}
		if got.RunID != "" || got.Summary != "" || got.UserID != "u" || got.TenantID != "2" {
			t.Errorf("payload %q: got %+v", string(raw), got)
		}
	}
}
