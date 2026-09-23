package api

import (
	"regexp"
	"strings"
)

// 工具动作分级 —— **服务端独立判定**。
//
// 为什么服务端要自己判一遍：引擎（Python）侧的 ToolGuard 跑在**处理不可信内容的同一个进程**里。
// 一旦它被 prompt injection 影响，判定与执行就落在同一信任域 —— 等于没有边界。所以：
//
//	权威判定 = 本文件（服务端）
//	引擎侧判定 = 给前端展示级别 + 本地快速拒绝，**刻意保守**（宁可多判危险）
//
// 两侧不一致时只会出现"服务端拦得住"，不会出现"引擎放行了危险操作"。
//
// 与 python-engine/app/agent/tool_policy.py 同构（语言不同，无法共享代码），
// 该文件的默认表是这边的对照物；改动其一必须同步另一个。
//
// fail-closed：未声明的工具一律按 write 处理 —— 漏登记的后果是"多一次确认"，不是"少一次"。
type ToolLevel string

const (
	ToolLevelRead     ToolLevel = "read"
	ToolLevelWrite    ToolLevel = "write"
	ToolLevelDelete   ToolLevel = "delete"
	ToolLevelExternal ToolLevel = "external"
)

// 前缀规则：工具名随 action 变化（browser_navigate / browser_click …）时兜底，
// 避免"新增一个 browser_xxx 就漏登记"。
var toolLevelPrefixes = []struct {
	prefix string
	level  ToolLevel
}{
	{"browser", ToolLevelExternal},
	{"web_", ToolLevelExternal},
	{"mcp_", ToolLevelExternal},
}

// 命令类工具：级别取决于**命令内容**（既能 ls 也能 rm -rf），先按最严起步。
var commandTools = map[string]bool{
	"shell_exec":       true,
	"execute_command":  true,
	"persistent_shell": true,
	"execute_python":   true,
	"run_code":         true,
}

var toolLevelTable = func() map[string]ToolLevel {
	table := make(map[string]ToolLevel, 64)
	add := func(level ToolLevel, names ...string) {
		for _, name := range names {
			table[name] = level
		}
	}
	add(ToolLevelRead,
		"read_file", "read_image", "glob_files", "grep_files", "search_files", "file_analyzer",
		"git_status", "git_log", "git_diff",
		"kb_list", "kb_search", "rag_query", "knowledge",
		"memory_search", "recall",
		"skill_list", "skill_discover", "skill_run",
		"mode_list", "graph_templates", "agent_list", "agent_session_list",
		"read_subagent_result", "read_tool_result", "job_output", "stderr_drain",
		"requirement_validate", "task_decompose", "tech_design", "prd_generate",
		"vision_analyze", "speech_to_text",
		"tool",
	)
	add(ToolLevelWrite,
		"write_file", "edit_file",
		"git_commit", "git_branch",
		"graph_create", "agent_session_create",
		"remember", "mode_edit", "media_create",
		"image_generate", "text_to_speech", "skill_generate",
		// 委派类：内部会执行任意工具（子 Agent 有自己的栅栏），父层至少要确认一次
		"subagent", "agent_dispatch", "code_agent", "workflow_run", "graph_run",
	)
	add(ToolLevelDelete, "job_kill", "forget", "delete_file", "kb_delete")
	add(ToolLevelExternal, "web_fetch", "web_search", "skill_install")
	return table
}()

// 无害命令白名单：命中即把命令类工具降为 write（仍需确认，但不走二次校验）。
var safeCommandRe = regexp.MustCompile(`(?i)^\s*(ls|dir|cat|type|head|tail|wc|pwd|echo|which|where|whoami|git\s+(status|log|diff|show)|python\s+--version|go\s+version|node\s+--version|npm\s+--version)\b`)

// 破坏性命令：命中即升级为 delete（即使工具名中性）。
var destructiveCommandRe = regexp.MustCompile(`(?i)(\brm\b|\brmdir\b|\bdel\b|\berase\b|\bunlink\b|\bshutdown\b|\bkill\b|\bdrop\s+(table|database|schema)\b|\btruncate\b|\bdelete\s+from\b|git\s+(reset\s+--hard|clean\s+-[a-z]*f|push\s+--force)|format\b|mkfs\b|>\s*/dev/sd)`)

var commandArgKeys = []string{"command", "cmd", "script", "code", "source"}

// ToolLevelOf 返回工具 + 参数对应的动作级别。
//
// 未声明的工具返回 ToolLevelWrite（fail-closed）。命令类工具按**内容**判定：
// 破坏性命令升级为 delete，无害命令降为 write，判不出来（命令为空）保守停在 delete。
func ToolLevelOf(name string, args map[string]any) ToolLevel {
	if commandTools[name] {
		command := commandText(args)
		if command == "" {
			return ToolLevelDelete
		}
		if destructiveCommandRe.MatchString(command) {
			return ToolLevelDelete
		}
		if safeCommandRe.MatchString(command) {
			return ToolLevelWrite
		}
		return ToolLevelDelete
	}
	if level, ok := toolLevelTable[name]; ok {
		return level
	}
	for _, entry := range toolLevelPrefixes {
		if strings.HasPrefix(name, entry.prefix) {
			return entry.level
		}
	}
	return ToolLevelWrite
}

func commandText(args map[string]any) string {
	if args == nil {
		return ""
	}
	for _, key := range commandArgKeys {
		if value, ok := args[key].(string); ok && strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

// RequiresConfirmation 按会话授权模式决定是否需要人工确认。
//
//	ask  ：write / delete / external 都要确认
//	auto ：只有 delete / external（"仅危险工具"）
//	yolo ：都不确认（用户显式选择跳过）
//
// 注意：yolo 放过的是"是否要问用户"，**不放过**参数级硬拦截与二次校验。
func RequiresConfirmation(level ToolLevel, mode string) bool {
	switch mode {
	case "yolo":
		return false
	case "ask":
		return level == ToolLevelWrite || level == ToolLevelDelete || level == ToolLevelExternal
	default: // auto 及未知模式：保守按 auto
		return level == ToolLevelDelete || level == ToolLevelExternal
	}
}

// RequiresSecondCheck 是否需要二次校验（票据绑定 + 执行前复核）。
//
// 只有不可逆（delete）与外部触达（external）需要：write 有文件快照可回滚（见
// python-engine/app/agent/undo_stack.py），给每个写操作都加一道票据校验只会让正常流程变脆。
func RequiresSecondCheck(level ToolLevel) bool {
	return level == ToolLevelDelete || level == ToolLevelExternal
}

// ToolRollbackCapability 返回该副作用能否回滚，供审计与"事前明示"使用。
//
//	"auto" —— 文件写入：写入前有快照，可回滚
//	"none" —— 其余：删除拦不到、外部调用收不回来
//
// 与 python-engine/app/agent/side_effect_ledger.py 的 rollback_capability 同构。
func ToolRollbackCapability(tool string) string {
	switch tool {
	case "write_file", "edit_file":
		return "auto"
	default:
		return "none"
	}
}
