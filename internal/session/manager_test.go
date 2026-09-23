package session

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// 回归保护：tool_calls.input 是 jsonb 列，模型的 arguments 可能是空串（无参调用）
// 或被截断的半成品 JSON —— 直接 $4::jsonb 会让整条 INSERT 失败并只打日志，
// 表现为"工具调用历史与结果刷新后全部丢失"。normalizeToolInput 必须保证写入合法。
func TestNormalizeToolInput(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"空串降级为空对象", "", "{}"},
		{"仅空白降级为空对象", "   \n\t", "{}"},
		{"合法对象原样保留", `{"path":"a.txt"}`, `{"path":"a.txt"}`},
		{"合法空对象原样保留", `{}`, `{}`},
		{"合法数组原样保留", `[1,2]`, `[1,2]`},
		{"截断的半成品包一层 raw", `{"path":`, `{"raw":"{\"path\":"}`},
		{"非 JSON 文本包一层 raw", "not-json", `{"raw":"not-json"}`},
	}
	for _, c := range cases {
		if got := normalizeToolInput(c.in); got != c.want {
			t.Errorf("%s: normalizeToolInput(%q) = %q, want %q", c.name, c.in, got, c.want)
		}
	}

	// 无论输入如何，输出都必须是合法 JSON（jsonb 写入前提）
	for _, in := range []string{"", "   ", `{"a":1}`, `{"path":`, "x", "null-ish", `"str"`} {
		out := normalizeToolInput(in)
		if !json.Valid([]byte(out)) {
			t.Errorf("normalizeToolInput(%q) 输出非法 JSON: %q", in, out)
		}
	}
}

// 回归保护：会话标签必须能落库（此前 tag 仅存前端 localStorage，刷新即丢）。
// 置顶/标签都不该推进 updated_at（它们是列表偏好，不是会话活动），
// 空标签要写 NULL（而不是空串），否则读取端会拿到 "" 与 null 两种“无标签”状态。
func TestBuildSessionUpdate(t *testing.T) {
	const id = "11111111-2222-3333-4444-555555555555"
	now := time.Date(2026, 9, 11, 12, 0, 0, 0, time.UTC)
	title := "新标题"
	pinned := true
	tag := "工作"
	alias := "我的备注"
	blank := ""

	cases := []struct {
		name     string
		upd      SessionUpdate
		wantSQL  string
		wantArgs []interface{}
	}{
		{
			name:     "仅置顶：不推进 updated_at",
			upd:      SessionUpdate{Pinned: &pinned},
			wantSQL:  `UPDATE sessions SET pinned = $1 WHERE id = $2`,
			wantArgs: []interface{}{true, id},
		},
		{
			name:     "仅标签",
			upd:      SessionUpdate{Tag: &tag},
			wantSQL:  `UPDATE sessions SET tag = NULLIF($1, '') WHERE id = $2`,
			wantArgs: []interface{}{"工作", id},
		},
		{
			name:     "空标签走 NULLIF（清除标签）",
			upd:      SessionUpdate{Tag: &blank},
			wantSQL:  `UPDATE sessions SET tag = NULLIF($1, '') WHERE id = $2`,
			wantArgs: []interface{}{"", id},
		},
		{
			name:     "仅别名（用户给会话起的备注，展示时优先于 title）",
			upd:      SessionUpdate{Alias: &alias},
			wantSQL:  `UPDATE sessions SET alias = NULLIF($1, '') WHERE id = $2`,
			wantArgs: []interface{}{"我的备注", id},
		},
		{
			name:     "空别名走 NULLIF（清除别名）",
			upd:      SessionUpdate{Alias: &blank},
			wantSQL:  `UPDATE sessions SET alias = NULLIF($1, '') WHERE id = $2`,
			wantArgs: []interface{}{"", id},
		},
		{
			name:     "标题+置顶+标签+别名：占位符与参数严格对应",
			upd:      SessionUpdate{Title: &title, Pinned: &pinned, Tag: &tag, Alias: &alias},
			wantSQL:  `UPDATE sessions SET title = $1, updated_at = $2, pinned = $3, tag = NULLIF($4, ''), alias = NULLIF($5, '') WHERE id = $6`,
			wantArgs: []interface{}{"新标题", now, true, "工作", "我的备注", id},
		},
	}
	for _, c := range cases {
		gotSQL, gotArgs := buildSessionUpdate(id, c.upd, now)
		if gotSQL != c.wantSQL {
			t.Errorf("%s: SQL = %q, want %q", c.name, gotSQL, c.wantSQL)
		}
		if len(gotArgs) != len(c.wantArgs) {
			t.Fatalf("%s: args len = %d, want %d", c.name, len(gotArgs), len(c.wantArgs))
		}
		for i := range c.wantArgs {
			if gotArgs[i] != c.wantArgs[i] {
				t.Errorf("%s: args[%d] = %v, want %v", c.name, i, gotArgs[i], c.wantArgs[i])
			}
		}
	}

	if !(SessionUpdate{}).empty() {
		t.Error("零值 SessionUpdate 应判定为 empty")
	}
	if (SessionUpdate{Tag: &tag}).empty() {
		t.Error("只改 tag 不应判定为 empty")
	}
}

// 增量落库会在流式过程中高频调用 UpsertAssistantMessage：
// 无数据库连接或空 message id 时必须安全返回，不能 panic。
func TestUpsertAssistantMessageGuards(t *testing.T) {
	m := &Manager{}
	m.UpsertAssistantMessage(context.Background(), "sess", "", "content", "[]", "turn")
	m.UpsertAssistantMessage(context.Background(), "sess", "msg-id", "content", "[]", "turn")
	m.UpsertAssistantMessage(context.Background(), "sess", "msg-id", "", "", "")
}

// 回归（实测事故：每一次回合收尾都失败 → turns 永远停在 running → 用户看到"主 Agent 一直阻塞"）：
//
// `turns.cached_tokens` 是 bigint，而 `cache_hit = ($7 > 0)` 里的字面量 0 会让 Postgres
// 把**同一个参数**推断成 int4，prepare 阶段直接报
// `inconsistent types deduced for parameter $7 (SQLSTATE 42P08)`。
// 因此 $7 必须显式 cast，且两处必须指向同一个参数（语义一致）。
func TestFinishTurnCastsCachedTokensParam(t *testing.T) {
	if !strings.Contains(finishTurnSQL, "$7::bigint") {
		t.Fatalf("finishTurnSQL 缺少 $7::bigint —— PG 会用 int4 推断同一参数并报 42P08，"+
			"每一次回合收尾都会失败：\n%s", finishTurnSQL)
	}
	if got := strings.Count(finishTurnSQL, "$7"); got != 2 {
		t.Errorf("$7 应恰好出现两次（cached_tokens 与 cache_hit 同源，各带一次 cast），实际 %d 次", got)
	}
	for _, want := range []string{"cache_hit = ($7::bigint > 0)", "finished_at = NOW()", "WHERE id = $1"} {
		if !strings.Contains(finishTurnSQL, want) {
			t.Errorf("finishTurnSQL 缺少 %q", want)
		}
	}
}
