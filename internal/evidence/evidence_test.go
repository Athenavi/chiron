package evidence

import (
	"testing"
)

func TestClassifyMutation(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go","content":"x"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)

	recs := c.Records()
	if len(recs) != 1 {
		t.Fatalf("want 1 record, got %d", len(recs))
	}
	if recs[0].Kind != KindMutation {
		t.Fatalf("want mutation, got %s", recs[0].Kind)
	}
	if recs[0].Target != "a.go" {
		t.Fatalf("want target a.go, got %q", recs[0].Target)
	}
	if !recs[0].OK {
		t.Fatalf("want OK")
	}
}

func TestClassifyVerificationSuccess(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "shell_exec", `{"command":"go test ./... -count=1"}`)
	c.ObserveToolResult("1", "", `{"stdout":"ok"}`)

	recs := c.Records()
	if recs[0].Kind != KindVerification {
		t.Fatalf("want verification, got %s", recs[0].Kind)
	}
	if !recs[0].OK {
		t.Fatalf("want OK")
	}
	if len(c.Verifications()) != 1 {
		t.Fatalf("want 1 verification")
	}
}

func TestNonVerificationCommandIsUnknown(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "shell_exec", `{"command":"echo hello"}`)
	c.ObserveToolResult("1", "", `{"stdout":"hello"}`)

	if got := c.Records()[0].Kind; got != KindUnknown {
		t.Fatalf("echo must not count as verification, got %s", got)
	}
	if len(c.Verifications()) != 0 {
		t.Fatalf("unknown tool must not produce verification")
	}
}

func TestReadToolIsRead(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "read_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"content":"..."}`)
	if got := c.Records()[0].Kind; got != KindRead {
		t.Fatalf("want read, got %s", got)
	}
}

func TestFailedVerificationDoesNotClearUnverifiedMutation(t *testing.T) {
	c := NewCollector()
	// 改文件
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	// 跑测试但**失败**
	c.ObserveToolCall("2", "shell_exec", `{"command":"pytest tests/"}`)
	c.ObserveToolResult("2", "", `{"error":"exit status 1"}`)

	un := c.UnverifiedMutations()
	if len(un) != 1 {
		t.Fatalf("failed verification must not clear the mutation; want 1 unverified, got %d", len(un))
	}
	if un[0].Target != "a.go" {
		t.Fatalf("want the mutation record, got %+v", un[0])
	}
}

func TestMutationFollowedByVerificationIsVerified(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	c.ObserveToolCall("2", "shell_exec", `{"command":"go test ./... "}`)
	c.ObserveToolResult("2", "", `{"ok":true}`)

	if got := c.UnverifiedMutations(); len(got) != 0 {
		t.Fatalf("a successful verification after the mutation clears it; got %+v", got)
	}
}

func TestMutationAfterVerificationIsUnverified(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "shell_exec", `{"command":"go test ./..."}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	// 验证之后又改了东西（顺序敏感）
	c.ObserveToolCall("2", "write_file", `{"path":"b.go"}`)
	c.ObserveToolResult("2", "", `{"ok":true}`)

	if got := c.UnverifiedMutations(); len(got) != 1 || got[0].Target != "b.go" {
		t.Fatalf("mutation after the last verification is unverified; got %+v", got)
	}
}

func TestErrorKeyMeansNotOK(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"error":"permission denied"}`)
	if c.Records()[0].OK {
		t.Fatalf("error field must mean OK=false")
	}
	// 失败的 mutation 不计入 unverified（它根本没成功）
	if len(c.UnverifiedMutations()) != 0 {
		t.Fatalf("failed mutation must not be counted as an unverified change")
	}
}

func TestEmptyErrorStringIsSuccess(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"error":"","path":"a.go"}`)
	if !c.Records()[0].OK {
		t.Fatalf(`empty error:"" should be treated as success`)
	}
}

func TestNonJSONResultFallsBackGracefully(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `permission denied: "error"`)
	if c.Records()[0].OK {
		t.Fatalf("non-JSON containing error should be failure")
	}
}

func TestEmptyResultIsFailClosed(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", "")
	if c.Records()[0].OK {
		t.Fatalf("empty result must be fail-closed (not proven)")
	}
}

func TestResultWithoutCallStillRecorded(t *testing.T) {
	c := NewCollector()
	c.ObserveToolResult("orphan", "read_file", `{"ok":true}`)
	if len(c.Records()) != 1 {
		t.Fatalf("a result without a prior call must still be recorded")
	}
}

func TestIsVerificationCommand(t *testing.T) {
	yes := []string{
		"go test ./...", "pytest tests/ -q", "npm test", "pnpm check",
		"ruff check app/", "mypy app/", "cargo clippy", "tsc --noEmit",
		"python -m pytest tests -q",
	}
	no := []string{
		"echo hello", "ls -la", "git status", "cat file.txt", "",
		"curl https://example.com",
	}
	for _, s := range yes {
		if !IsVerificationCommand(s) {
			t.Errorf("expected verification: %q", s)
		}
	}
	for _, s := range no {
		if IsVerificationCommand(s) {
			t.Errorf("must not be verification: %q", s)
		}
	}
}

func TestRecordsAreACopy(t *testing.T) {
	c := NewCollector()
	c.ObserveToolCall("1", "read_file", `{}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	got := c.Records()
	got[0].ToolName = "mutated"
	if c.Records()[0].ToolName == "mutated" {
		t.Fatalf("Records() must return a copy")
	}
}
