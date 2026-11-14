package api

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// 续跑前的判死逻辑。
//
// 这层的价值全在"哪些情况该判死、判成什么"上：判错的代价不对称 ——
// 把能跑的事件判成 dropped 是**不可逆**的信息损失，把坏行判成重试则会让它
// 一轮轮重占锁与信号量。所以下面逐条钉住每个分支的结论。
func TestPrepareThreadTurnRejects(t *testing.T) {
	deps := threadTurnDeps{} // billingMgr 为 nil ⇒ 余额闸门放行，专测结构性问题
	base := threadDelivery{
		ThreadID:   "th-1",
		EventKey:   "run-1",
		SessionID:  "sess-1",
		UserID:     "user-1",
		TenantID:   3,
		SourceKind: "subagent",
	}
	okPayload := mustSnapshot(t, agentFollowupPayload{RunID: "run-1", Status: "completed", Summary: "done"})

	cases := []struct {
		name string
		mut  func(*threadDelivery)
	}{
		{
			// 解不开的快照只可能来自编程错误或历史脏行：重试没有意义，
			// 而且会让 drain 永远卡在这条上。
			name: "快照解不开",
			mut:  func(d *threadDelivery) { d.Payload = json.RawMessage(`{"run_id":`) },
		},
		{
			// 表里可能留着更早版本写入的来源值；`kind.build` 为 nil 会 panic。
			name: "未注册来源",
			mut:  func(d *threadDelivery) { d.SourceKind = "webhook" },
		},
		{
			// 会话被删 / 行被搬走后投不进去，但这不是"等一会儿再来"的情形。
			name: "行为空会话",
			mut:  func(d *threadDelivery) { d.SessionID = "" },
		},
		{
			name: "行为空用户",
			mut:  func(d *threadDelivery) { d.UserID = "" },
		},
	}
	for _, c := range cases {
		d := base
		d.Payload = okPayload
		c.mut(&d)
		if prep, ok := prepareThreadTurn(context.Background(), deps, d); ok {
			t.Errorf("%s: 应当判死，却得到 %+v", c.name, prep)
		}
	}
}

// 判死的分支不应误伤正常投递 —— 尤其是"空摘要"，那是子 Agent 没写 summary 的常见情形。
func TestPrepareThreadTurnAccepts(t *testing.T) {
	deps := threadTurnDeps{}
	base := threadDelivery{
		ThreadID:   "th-2",
		EventKey:   "run-2",
		SessionID:  "sess-2",
		UserID:     "user-2",
		TenantID:   5,
		SourceKind: "subagent",
	}
	cases := []struct {
		name    string
		payload agentFollowupPayload
	}{
		{
			name:    "全字段",
			payload: agentFollowupPayload{RunID: "run-2", Status: "completed", Profile: "code", Depth: 1, Summary: "改了 2 个文件"},
		},
		{
			// 摘要为空是正常的：正文该渲染成占位符，而不是把整条事件丢掉。
			name:    "空摘要",
			payload: agentFollowupPayload{RunID: "run-2", Status: "failed"},
		},
	}
	for _, c := range cases {
		d := base
		d.Payload = mustSnapshot(t, c.payload)
		prep, ok := prepareThreadTurn(context.Background(), deps, d)
		if !ok {
			t.Fatalf("%s: 不该判死", c.name)
		}
		// 身份字段必须以行为准：配额与计费认这三个字段。
		if prep.body.SessionID != d.SessionID || prep.body.UserID != d.UserID || prep.body.TenantID != "5" {
			t.Errorf("%s: 身份字段未以行为准: %+v", c.name, prep.body)
		}
		if prep.body.Kind != "subagent" {
			t.Errorf("%s: Kind = %q, want 行上的 source_kind", c.name, prep.body.Kind)
		}
		// 正文必须已经拼好：快照里的内容要真的进到注入文本里，
		// 否则模型看到的是"结果为空"，而真正的原因是字段没取到。
		if !strings.Contains(prep.content, "run-2") {
			t.Errorf("%s: 正文里没有 run_id: %q", c.name, prep.content)
		}
		if c.payload.Summary != "" && !strings.Contains(prep.content, c.payload.Summary) {
			t.Errorf("%s: 正文里没有摘要: %q", c.name, prep.content)
		}
	}
}

// 空 kind 必须归一成 subagent：滚动升级期间旧引擎不带这个字段，
// 若归一成空串，入队时 `followupKinds[""]` 查不到就会把合法事件当未注册来源丢掉。
func TestNormalizedFollowupKind(t *testing.T) {
	cases := map[string]string{
		"":           "subagent",
		"   ":        "subagent",
		"subagent":   "subagent",
		" job ":      "job",
		"job":        "job",
		"job\n":      "job",
		"subagent\r": "subagent",
	}
	for in, want := range cases {
		if got := normalizedFollowupKind(in); got != want {
			t.Errorf("normalizedFollowupKind(%q) = %q, want %q", in, got, want)
		}
	}
	// 归一后的值必须真的能在注册表里查到 —— 这是它存在的唯一理由。
	for in := range cases {
		if _, ok := lookupFollowupKind(in); !ok {
			t.Errorf("lookupFollowupKind(%q) 查不到：归一到注册表之外的键了", in)
		}
	}
}

// 运行锁 token 必须互不相同：两次调用撞 token 就等于两轮共用一个锁身份，
// 释放时会互相把对方的锁删掉。
func TestNewThreadRunTokenUnique(t *testing.T) {
	seen := make(map[string]struct{}, 256)
	for i := 0; i < 256; i++ {
		tok := newThreadRunToken()
		if !strings.HasPrefix(tok, "thread-") {
			t.Fatalf("token 前缀不对: %q", tok)
		}
		if _, dup := seen[tok]; dup {
			t.Fatalf("token 重复: %q", tok)
		}
		seen[tok] = struct{}{}
	}
}

// mustSnapshot 把载荷编成快照，测试里出错就直接终止。
func mustSnapshot(t *testing.T, p agentFollowupPayload) json.RawMessage {
	t.Helper()
	raw, err := followupSnapshot(p)
	if err != nil {
		t.Fatalf("followupSnapshot: %v", err)
	}
	return raw
}
