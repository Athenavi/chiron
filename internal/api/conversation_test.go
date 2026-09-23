package api

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/session"
)

// TestConversationJSONBranchContract 钉住分支血缘的**序列化契约**。
//
// 为什么值得单测：前端完全靠这三个键辨识分支（列表徽标、地图卡片、血缘连线）——
//   - `parent_session_id`：非空 = 这是分支；
//   - `parent_title`：显示"分支自《谁》"；
//   - `branch_from_seq`：显示分叉点第 N 条。
//
// 同时必须保证**普通会话不带这些键**（omitempty）：否则前端会把所有会话都当成分支。
func TestConversationJSONBranchContract(t *testing.T) {
	branchRaw, err := json.Marshal(Conversation{
		ID:              "22222222-2222-2222-2222-222222222222",
		Title:           "分支会话",
		ParentSessionID: "11111111-1111-1111-1111-111111111111",
		ParentTitle:     "父会话",
		BranchFromSeq:   12,
	})
	if err != nil {
		t.Fatalf("marshal branch conversation: %v", err)
	}
	branchJSON := string(branchRaw)
	for _, key := range []string{"parent_session_id", "parent_title", "branch_from_seq"} {
		if !strings.Contains(branchJSON, `"`+key+`"`) {
			t.Errorf("分支会话的 JSON 缺少 %s：%s", key, branchJSON)
		}
	}
	if !strings.Contains(branchJSON, `"branch_from_seq":12`) {
		t.Errorf("分叉点未按数字序列化：%s", branchJSON)
	}

	plainRaw, err := json.Marshal(Conversation{ID: "33333333-3333-3333-3333-333333333333", Title: "普通会话"})
	if err != nil {
		t.Fatalf("marshal plain conversation: %v", err)
	}
	plainJSON := string(plainRaw)
	for _, key := range []string{"parent_session_id", "parent_title", "branch_from_seq"} {
		if strings.Contains(plainJSON, `"`+key+`"`) {
			t.Errorf("普通会话不应出现 %s（前端会把所有会话误判为分支）：%s", key, plainJSON)
		}
	}
}

// TestParentTitlesForDegradesSafely 父会话展示名是"锦上添花"：
// 数据库不可用（无 pool）时只返回空，不 panic、不影响调用方的主流程。
func TestParentTitlesForDegradesSafely(t *testing.T) {
	mgr := &session.Manager{}

	if got := parentTitlesFor(context.Background(), mgr, nil); got != nil {
		t.Errorf("空列表应返回 nil，实际 %v", got)
	}
	if got := parentTitlesFor(context.Background(), mgr, []Conversation{{ID: "a"}}); got != nil {
		t.Errorf("没有分支会话时不应发起查询，实际 %v", got)
	}
	// 有分支会话、但查询不可用 → 返回 nil（调用方留空 ParentTitle）
	if got := parentTitlesFor(context.Background(), mgr, []Conversation{{ID: "b", ParentSessionID: "p"}}); len(got) != 0 {
		t.Errorf("数据库不可用时应返回空映射，实际 %v", got)
	}
}
