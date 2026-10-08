package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// 执行审计集中摄取（N4）的契约：**逐条校验、部分接受** —— 一条坏记录不该让整批审计丢失；
// 但空批次与超限批次要显式 400（否则调用方以为发出去了）。
//
// 这里直接调 handler（路由层的 internalTokenMW 由 routes_public.go 挂载，另有用例覆盖中间件本身）。

func postExecAudit(t *testing.T, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/audit/exec", bytes.NewReader([]byte(body)))
	rec := httptest.NewRecorder()
	ExecAuditIngestHandler(rec, req)

	var decoded map[string]any
	if rec.Body.Len() > 0 {
		_ = json.Unmarshal(rec.Body.Bytes(), &decoded)
	}
	return rec, decoded
}

func TestExecAuditIngestRejectsEmptyAndOversizedBatches(t *testing.T) {
	if rec, _ := postExecAudit(t, `{"records": []}`); rec.Code != http.StatusBadRequest {
		t.Fatalf("空批次应 400，实际 %d", rec.Code)
	}

	records := make([]string, 0, execAuditMaxBatch+1)
	for i := 0; i <= execAuditMaxBatch; i++ {
		records = append(records, `{"tool":"shell_exec","outcome":"ok"}`)
	}
	body := `{"records": [` + strings.Join(records, ",") + `]}`
	if rec, _ := postExecAudit(t, body); rec.Code != http.StatusBadRequest {
		t.Fatalf("超过单批上限应 400，实际 %d", rec.Code)
	}

	if rec, _ := postExecAudit(t, `{"records": "nope"}`); rec.Code != http.StatusBadRequest {
		t.Fatalf("非法形状应 400，实际 %d", rec.Code)
	}
}

func TestExecAuditIngestAcceptsValidBatch(t *testing.T) {
	body := `{"records": [
		{"tenant_id":"t1","user_id":"u1","session_id":"s1","tool":"shell_exec","command":"echo hi",
		 "outcome":"ok","exit_code":0,"duration_ms":12,"ts":"2026-10-08T12:00:00Z","instance":"engine-1"},
		{"tenant_id":"t1","user_id":"u1","tool":"run_code","command":"print(1)","outcome":"blocked",
		 "reason":"import not allowed","instance":"engine-1"}
	]}`
	rec, decoded := postExecAudit(t, body)
	if rec.Code != http.StatusOK {
		t.Fatalf("合法批次应 200，实际 %d body=%s", rec.Code, rec.Body.String())
	}
	data, _ := decoded["data"].(map[string]any)
	if got := data["accepted"]; got != float64(2) {
		t.Fatalf("应接受 2 条，实际 %v", got)
	}
	if got := data["skipped"]; got != float64(0) {
		t.Fatalf("不应跳过任何记录，实际 %v", got)
	}
}

func TestExecAuditIngestSkipsIncompleteRecordsInsteadOfFailingBatch(t *testing.T) {
	// 缺 tool / 缺 outcome 的记录跳过并计数；同批里的合法记录仍然落库
	body := `{"records": [
		{"tool":"","outcome":"ok"},
		{"tool":"shell_exec","outcome":""},
		{"tool":"shell_exec","outcome":"timeout","command":"sleep 99","instance":"engine-2"}
	]}`
	rec, decoded := postExecAudit(t, body)
	if rec.Code != http.StatusOK {
		t.Fatalf("部分坏记录不应整批 400，实际 %d", rec.Code)
	}
	data, _ := decoded["data"].(map[string]any)
	if data["accepted"] != float64(1) || data["skipped"] != float64(2) {
		t.Fatalf("应 1 接受 2 跳过，实际 %v", data)
	}
}

func TestTruncateRunesCutsByCharacters(t *testing.T) {
	// 按字节截断会把多字节字符切成乱码 —— 命令里中文/emoji 很常见
	long := strings.Repeat("命", 600)
	got := truncateRunes(long, execAuditCommandMaxChars)
	if r := []rune(got); len(r) != execAuditCommandMaxChars+1 { // +1 是省略号
		t.Fatalf("应按字符截断到 %d(+1)，实际 %d", execAuditCommandMaxChars, len(r))
	}
	if !strings.HasSuffix(got, "…") {
		t.Fatal("截断应有显式省略号（让人知道这里被截了）")
	}
	if short := truncateRunes("ok", 10); short != "ok" {
		t.Fatalf("短文本不应改动，实际 %q", short)
	}
}
