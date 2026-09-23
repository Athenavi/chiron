package api

import "testing"

// 与 python-engine/app/agent/tool_policy.py + tests/test_tool_policy.py 同构。
//
// 两份分级表刻意各存一份（语言不同，无法共享代码）。这组断言是"两侧不漂移"的守护，
// 而且方向是**单向的**：服务端可以比引擎更严（fail-closed），绝不能更松 ——
// 更松就意味着"引擎放行了危险操作，服务端也没拦住"。
func TestToolLevelOfRepresentativeTools(t *testing.T) {
	cases := []struct {
		name  string
		args  map[string]any
		level ToolLevel
	}{
		{"read_file", nil, ToolLevelRead},
		{"grep_files", nil, ToolLevelRead},
		{"read_tool_result", nil, ToolLevelRead},
		{"write_file", nil, ToolLevelWrite},
		{"edit_file", nil, ToolLevelWrite},
		{"web_fetch", nil, ToolLevelExternal},
		{"web_search", nil, ToolLevelExternal},
		{"skill_install", nil, ToolLevelExternal},
		{"job_kill", nil, ToolLevelDelete},
		{"forget", nil, ToolLevelDelete},
		// 前缀规则：工具名随 action 变化
		{"browser_click", nil, ToolLevelExternal},
		{"browser_navigate", nil, ToolLevelExternal},
		{"web_something_new", nil, ToolLevelExternal},
		// fail-closed：未声明 → write
		{"brand_new_tool_xyz", nil, ToolLevelWrite},
		// 命令类按内容判定
		{"shell_exec", map[string]any{"command": "rm -rf /tmp/x"}, ToolLevelDelete},
		{"shell_exec", map[string]any{"command": "DROP TABLE users"}, ToolLevelDelete},
		{"shell_exec", map[string]any{"command": "git push --force"}, ToolLevelDelete},
		{"run_code", map[string]any{"code": "shutil.rmtree('x')"}, ToolLevelDelete},
		{"shell_exec", map[string]any{"command": "ls -la"}, ToolLevelWrite},
		{"shell_exec", map[string]any{"command": "git status"}, ToolLevelWrite},
		// 判不出来（没有命令文本）时保守停在 delete
		{"shell_exec", map[string]any{}, ToolLevelDelete},
	}
	for _, c := range cases {
		if got := ToolLevelOf(c.name, c.args); got != c.level {
			t.Errorf("ToolLevelOf(%q, %v) = %q, want %q", c.name, c.args, got, c.level)
		}
	}
}

func TestRequiresConfirmationMatrix(t *testing.T) {
	for _, mode := range []string{"ask", "auto", "yolo"} {
		wantWrite := map[string]bool{"ask": true, "auto": false, "yolo": false}[mode]
		if got := RequiresConfirmation(ToolLevelWrite, mode); got != wantWrite {
			t.Errorf("write/%s = %v, want %v", mode, got, wantWrite)
		}
		wantDanger := map[string]bool{"ask": true, "auto": true, "yolo": false}[mode]
		for _, level := range []ToolLevel{ToolLevelDelete, ToolLevelExternal} {
			if got := RequiresConfirmation(level, mode); got != wantDanger {
				t.Errorf("%s/%s = %v, want %v", level, mode, got, wantDanger)
			}
		}
	}
	if RequiresConfirmation(ToolLevelRead, "ask") {
		t.Error("read should never require confirmation")
	}
}

func TestRequiresSecondCheckOnlyForIrreversible(t *testing.T) {
	if !RequiresSecondCheck(ToolLevelDelete) || !RequiresSecondCheck(ToolLevelExternal) {
		t.Error("delete/external must require the second check")
	}
	if RequiresSecondCheck(ToolLevelWrite) || RequiresSecondCheck(ToolLevelRead) {
		t.Error("write/read must not require the second check")
	}
}

func TestRollbackCapability(t *testing.T) {
	for _, tool := range []string{"write_file", "edit_file"} {
		if got := ToolRollbackCapability(tool); got != "auto" {
			t.Errorf("%s: got %q, want auto", tool, got)
		}
	}
	// 不猜、不承诺：删除拦不到、外部调用收不回来
	for _, tool := range []string{"shell_exec", "git_commit", "web_fetch", "job_kill"} {
		if got := ToolRollbackCapability(tool); got != "none" {
			t.Errorf("%s: got %q, want none", tool, got)
		}
	}
}

// 单向不变量：服务端判定不得比引擎更松。
//
// 这里挑的是引擎侧明确"要确认"的工具（write/delete/external 三档的代表）——
// 服务端若把它们判成 read，就等于两道关都放行。
func TestServerNeverMorePermissiveThanEngine(t *testing.T) {
	mustNotBeRead := []string{
		"write_file", "edit_file", "git_commit", "graph_create", "remember", "mode_edit",
		"subagent", "agent_dispatch", "workflow_run",
		"web_fetch", "web_search", "browser_click", "skill_install",
		"job_kill", "forget",
	}
	for _, name := range mustNotBeRead {
		if got := ToolLevelOf(name, nil); got == ToolLevelRead {
			t.Errorf("%s judged as read by the server — that is more permissive than the engine", name)
		}
	}
}
