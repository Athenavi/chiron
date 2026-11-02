package api

import (
	"testing"
	"time"
)

// ETag 的正确性只有两侧边界：**该失效时必须失效**、**不该失效时必须命中**。
// 前者错会让前端看到陈旧历史，后者错会让缓存永不复用（白做）。

func TestConversationETagIncludesRequestParams(t *testing.T) {
	// 同一个会话、同一份数据，但**翻到不同页** —— 必须是不同的 ETag。
	// 少了这一条，翻页会命中缓存，把第一页的内容当成第二页返回。
	base := time.Unix(1700000000, 0)
	same := conversationETag("s1", "", "", 50, base, 50, 10)

	for _, tc := range []struct {
		name string
		got  string
	}{
		{"different before cursor", conversationETag("s1", "2026-01-01T00:00:00Z|m1", "", 50, base, 50, 10)},
		{"different tool_before cursor", conversationETag("s1", "", "2026-01-01T00:00:00Z|tc1", 50, base, 50, 10)},
		{"different limit", conversationETag("s1", "", "", 20, base, 50, 10)},
		{"different session", conversationETag("s2", "", "", 50, base, 50, 10)},
	} {
		if tc.got == same {
			t.Errorf("%s must not share an ETag with the base page", tc.name)
		}
	}
}

func TestConversationETagTracksDataVersion(t *testing.T) {
	// 新消息到达 ⇒ 必须换 ETag，否则刷新后仍回 304，用户看不到新内容。
	base := time.Unix(1700000000, 0)
	later := base.Add(time.Nanosecond)
	if conversationETag("s1", "", "", 50, base, 50, 10) ==
		conversationETag("s1", "", "", 50, later, 50, 10) {
		t.Fatal("updated_at change must change the ETag")
	}
	// 条数变化同样要换：同一时刻写入的极端情况下 updated_at 可能相同。
	if conversationETag("s1", "", "", 50, base, 50, 10) ==
		conversationETag("s1", "", "", 50, base, 51, 10) {
		t.Fatal("message count change must change the ETag")
	}
	if conversationETag("s1", "", "", 50, base, 50, 10) ==
		conversationETag("s1", "", "", 50, base, 50, 11) {
		t.Fatal("tool call count change must change the ETag")
	}
}

func TestConversationETagIsStableForSameInput(t *testing.T) {
	// 同一份输入必须给出同一个 ETag，否则缓存永远不命中。
	base := time.Unix(1700000000, 0)
	a := conversationETag("s1", "c", "tc", 50, base, 50, 10)
	b := conversationETag("s1", "c", "tc", 50, base, 50, 10)
	if a != b {
		t.Fatalf("same input produced different ETags: %s vs %s", a, b)
	}
}

func TestConversationETagSeparatesFields(t *testing.T) {
	// 字段拼接必须无歧义：("ab","c") 与 ("a","bc") 不能撞成同一个 ETag。
	base := time.Unix(1700000000, 0)
	if conversationETag("s1", "ab", "c", 50, base, 1, 1) ==
		conversationETag("s1", "a", "bc", 50, base, 1, 1) {
		t.Fatal("field boundary is ambiguous: different inputs share an ETag")
	}
}

func TestETagMatches(t *testing.T) {
	etag := `W/"abc123"`

	for _, tc := range []struct {
		name   string
		header string
		want   bool
	}{
		{"empty header never matches", "", false},
		{"exact", etag, true},
		{"strong form of the same value", `"abc123"`, true},
		{"wildcard matches anything present", "*", true},
		{"single entry in a list", `W/"other", W/"abc123"`, true},
		{"list without a match", `W/"other", W/"another"`, false},
		{"different value", `W/"zzz"`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := etagMatches(tc.header, etag); got != tc.want {
				t.Errorf("etagMatches(%q) = %v, want %v", tc.header, got, tc.want)
			}
		})
	}
}
