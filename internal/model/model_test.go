package model

import (
	"strings"
	"testing"
)

// WorkingMemory 是 agent 的短期草稿纸：GetRecent / Summarize 的实际输出会被喂回 LLM。
// 本包此前零测试。这里钉三件事：拷贝语义（外部改不动内部）、n 的边界、摘要截断。

func TestWorkingMemoryAddAndGetRecentCopiesOut(t *testing.T) {
	wm := NewWorkingMemory("s1", "task-A")
	first := wm.Add("thought", "thought-1", nil)
	wm.AddObservation("shell_exec", "output-1", true)

	if first.Type != "thought" || first.Content != "thought-1" || first.ID == "" {
		t.Fatalf("Add 返回的条目字段不对：%+v", first)
	}
	if first.CreatedAt.IsZero() {
		t.Fatal("CreatedAt 不应为零值")
	}

	all := wm.GetRecent(0)
	if len(all) != 2 {
		t.Fatalf("GetRecent(0) 应返回全部 2 条，实际 %d", len(all))
	}
	obs := all[1]
	if obs.Type != "observation" || obs.Content != "output-1" {
		t.Fatalf("第 2 条应是 observation，实际 %+v", obs)
	}
	if got := obs.Metadata["tool"]; got != "shell_exec" {
		t.Fatalf("observation 元数据应带 tool，实际 %v", got)
	}
	if got := obs.Metadata["success"]; got != true {
		t.Fatalf("observation 元数据应带 success=true，实际 %v", got)
	}

	// 返回的必须是**拷贝**：改它不能影响内部状态（否则调用方能悄悄篡改历史）
	all[0].Content = "tampered"
	if wm.GetRecent(0)[0].Content != "thought-1" {
		t.Fatal("GetRecent 返回的切片被外部修改后影响了内部条目（应返回拷贝）")
	}
}

func TestWorkingMemoryGetRecentBounds(t *testing.T) {
	wm := NewWorkingMemory("s1", "t")
	for i := 0; i < 5; i++ {
		wm.AddThought("t")
	}

	if got := wm.GetRecent(-3); len(got) != 5 {
		t.Fatalf("n<=0 应返回全部，实际 %d", len(got))
	}
	if got := wm.GetRecent(99); len(got) != 5 {
		t.Fatalf("n>len 应返回全部，实际 %d", len(got))
	}
	if got := wm.GetRecent(2); len(got) != 2 {
		t.Fatalf("n=2 应返回 2 条，实际 %d", len(got))
	}
}

func TestWorkingMemoryState(t *testing.T) {
	wm := NewWorkingMemory("s1", "t")
	if got := wm.GetState("missing"); got != nil {
		t.Fatalf("未设置的 key 应返回 nil，实际 %v", got)
	}
	wm.SetState("step", 3)
	if got := wm.GetState("step"); got != 3 {
		t.Fatalf("SetState/GetState 往返失败：%v", got)
	}
	wm.SetState("step", "overwritten")
	if got := wm.GetState("step"); got != "overwritten" {
		t.Fatalf("重复 SetState 应覆盖：%v", got)
	}
}

func TestWorkingMemorySummarizeTruncatesAndLimitsToThree(t *testing.T) {
	wm := NewWorkingMemory("s1", "my-task")
	long := strings.Repeat("x", 500)
	wm.AddThought("old-1")
	wm.AddThought("old-2")
	wm.AddThought("old-3")
	wm.AddThought("old-4")
	wm.AddThought(long) // 第 5 条：最近的 3 条 = old-3 / old-4 / long

	summary := wm.Summarize()
	if !strings.Contains(summary, "Task: my-task") {
		t.Fatalf("摘要应包含任务名，实际 %q", summary)
	}
	if !strings.Contains(summary, "Steps taken: 5") {
		t.Fatalf("摘要应包含总步数，实际 %q", summary)
	}
	// 只保留最近 3 条 ⇒ old-1 / old-2 必须被挤出
	if strings.Contains(summary, "old-1") || strings.Contains(summary, "old-2") {
		t.Fatalf("摘要应只保留最近 3 条，实际 %q", summary)
	}
	if !strings.Contains(summary, "old-3") || !strings.Contains(summary, "old-4") {
		t.Fatalf("最近 3 条应包含 old-3/old-4，实际 %q", summary)
	}
	// 超长内容按 200 字符截断并标 "..."
	if !strings.Contains(summary, long[:200]+"...") {
		t.Fatalf("超长条目应按 200 字符截断并标 ...，实际 %q", summary)
	}
	if strings.Contains(summary, long) {
		t.Fatal("超长条目被完整写进摘要（未截断）")
	}
}
