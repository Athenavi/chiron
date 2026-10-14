package agentgoal

import (
	"testing"

	"github.com/athenavi/chiron/internal/evidence"
)

func collectorWith(calls [][2]string, results [][3]string) *evidence.Collector {
	c := evidence.NewCollector()
	for i, call := range calls {
		id := call[0]
		res := results[i]
		c.ObserveToolCall(id, call[1], res[1])
		c.ObserveToolResult(id, "", res[2])
	}
	return c
}

// 说明：calls 里第二项是工具名，results 里第二项是参数、第三项是结果 JSON。
// 为可读性，下面用显式的三元素构造 calls/results。

func TestPureQAIsCompleted(t *testing.T) {
	// 没改任何东西、有回答 ⇒ 完成。
	rep := Evaluate(nil, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("want completed, got %s (%v)", rep.Outcome, rep.Reasons)
	}
	if !rep.Complete() {
		t.Fatalf("Complete() must be true for completed")
	}
}

func TestErrorIsFailed(t *testing.T) {
	rep := Evaluate(nil, Signals{HasAnswer: true, HasError: true, ErrorMessage: "boom"})
	if rep.Outcome != Failed {
		t.Fatalf("want failed, got %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("failed must not be Complete()")
	}
}

func TestMutationWithoutVerificationIsIncomplete(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("want incomplete (unverified change), got %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("incomplete must not be Complete()")
	}
	// 应当点名是 verified-changes 欠着。
	found := false
	for _, f := range rep.Findings {
		if f.Obligation.ID == "verified-changes" && f.Verdict == Owed {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected verified-changes owed; findings=%+v", rep.Findings)
	}
}

func TestMutationThenVerificationIsCompleted(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	c.ObserveToolCall("2", "shell_exec", `{"command":"go test ./..."}`)
	c.ObserveToolResult("2", "", `{"ok":true}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("want completed, got %s (%v)", rep.Outcome, rep.Reasons)
	}
}

func TestNoAnswerIsOwed(t *testing.T) {
	rep := Evaluate(nil, Signals{HasAnswer: false})
	if rep.Outcome != Incomplete {
		t.Fatalf("want incomplete (no answer), got %s", rep.Outcome)
	}
}

func TestCancelledDoesNotOweAnswer(t *testing.T) {
	// 取消的回合不该因为"没有回答"被算作欠 —— 那是 Blocked 语义，answered 义务不派生。
	rep := Evaluate(nil, Signals{Cancelled: true})
	if rep.Outcome != Completed {
		t.Fatalf("cancelled turn with no other obligations should not be incomplete; got %s", rep.Outcome)
	}
}

func TestReadOnlyTurnIsCompleted(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "read_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"content":"x"}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("read-only turn must not require verification; got %s", rep.Outcome)
	}
}

func TestNoObligationsIsNone(t *testing.T) {
	// 取消 + 无其他义务 + 无变更 ⇒ 无义务（None），Complete() 为真（不强制继续）。
	rep := Evaluate(nil, Signals{Cancelled: true})
	if len(rep.Findings) == 0 {
		if rep.Outcome != None {
			t.Fatalf("want none, got %s", rep.Outcome)
		}
	}
}

func TestFailedVerificationStillOwed(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	c.ObserveToolCall("2", "shell_exec", `{"command":"pytest"}`)
	c.ObserveToolResult("2", "", `{"error":"1 failed"}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("a failing verification must keep the change unverified; got %s", rep.Outcome)
	}
}

func TestModelClaimNeverSatisfies(t *testing.T) {
	// 直接构造一条 model-claim 义务，确认它永远是 owed（claim 自身不满足义务）。
	f := judge(Obligation{ID: "c1", Required: true, Class: classModelClaim}, nil, Signals{})
	if f.Verdict != Owed {
		t.Fatalf("model claim must be owed until host evidence exists; got %s", f.Verdict)
	}
}

func TestBlockedReport(t *testing.T) {
	rep := BlockedReport("deadline exceeded")
	if rep.Outcome != Blocked {
		t.Fatalf("want blocked, got %s", rep.Outcome)
	}
}

func TestSameInputSameResult(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	a := Evaluate(c, Signals{HasAnswer: true})
	b := Evaluate(c, Signals{HasAnswer: true})
	if a.Outcome != b.Outcome || len(a.Findings) != len(b.Findings) {
		t.Fatalf("Evaluate must be deterministic")
	}
}
