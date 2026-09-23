package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Broker 的核心职责有两条：**服务端独立判定**、**可以收紧用户的模式选择**。
// 这组断言主要钉住后者 —— 模式选择能表达"我愿意多放手"，不能表达"我愿意承受不可撤销的
// 后果"。所以 yolo（跳过全部确认）下的 delete / external，服务端仍然要求人工确认。
//
// 顺带钉住"服务端不代替引擎执行"：它只回答 allowed + 该走哪些关。

func authorize(t *testing.T, body string) toolAuthorizeResponse {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(body))
	rec := httptest.NewRecorder()
	ToolAuthorizeHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var envelope struct {
		Data toolAuthorizeResponse `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v (%s)", err, rec.Body.String())
	}
	return envelope.Data
}

func TestToolAuthorizeTightensYoloForIrreversible(t *testing.T) {
	got := authorize(t, `{"tenant_id":"t","user_id":"u","tool_name":"shell_exec",
		"arguments":{"command":"rm -rf /tmp"},"tools_mode":"yolo"}`)

	if !got.Allowed {
		t.Error("allowed should stay true —— 服务端是判定者，不是默认否决者")
	}
	if got.Level != ToolLevelDelete {
		t.Errorf("level = %q, want delete", got.Level)
	}
	if !got.RequiresUserApproval {
		t.Error("yolo 不能免除不可逆操作的人工确认")
	}
	if !got.Enforced {
		t.Error("覆盖了用户模式选择时必须标记 enforced（前端要能解释为什么还要确认）")
	}
	if !got.RequiresSecondCheck {
		t.Error("delete 需要二次校验")
	}
	if got.Rollback != "none" {
		t.Errorf("rollback = %q, want none", got.Rollback)
	}
}

func TestToolAuthorizeTightensYoloForExternal(t *testing.T) {
	got := authorize(t, `{"tool_name":"web_fetch","arguments":{"url":"https://x"},"tools_mode":"yolo"}`)
	if got.Level != ToolLevelExternal {
		t.Errorf("level = %q, want external", got.Level)
	}
	if !got.RequiresUserApproval || !got.Enforced {
		t.Error("外部触达在 yolo 下同样需要确认，且要标记 enforced")
	}
}

func TestToolAuthorizeLeavesReadAndWriteAlone(t *testing.T) {
	// 只读：不确认、不二次校验、不收紧
	read := authorize(t, `{"tool_name":"read_file","arguments":{"path":"a.txt"},"tools_mode":"yolo"}`)
	if read.Level != ToolLevelRead || read.RequiresUserApproval || read.Enforced {
		t.Errorf("read 不应被加任何关：%+v", read)
	}
	// 普通写入：auto 模式下不确认（与引擎判定一致），也不算"被收紧"
	write := authorize(t, `{"tool_name":"write_file","arguments":{"path":"a.txt"},"tools_mode":"auto"}`)
	if write.Level != ToolLevelWrite {
		t.Errorf("level = %q, want write", write.Level)
	}
	if write.Enforced {
		t.Error("write 未被收紧，不应标记 enforced")
	}
	if write.Rollback != "auto" {
		t.Errorf("write_file 的副作用可回滚，rollback = %q", write.Rollback)
	}
}

func TestToolAuthorizeDefaultsModeToAuto(t *testing.T) {
	// 缺省模式按 auto：delete 仍要确认（而不是像 ask 那样把所有 write 也拦下）
	got := authorize(t, `{"tool_name":"shell_exec","arguments":{"command":"rm -rf /"}}`)
	if !got.RequiresUserApproval {
		t.Error("缺省应保守按 auto，delete 仍要确认")
	}
	if got.Enforced {
		t.Error("auto 下 delete 本来就要确认，不算收紧")
	}
}

func TestToolAuthorizeRejectsMissingToolName(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	ToolAuthorizeHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}
