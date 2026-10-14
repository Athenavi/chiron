// Package evidence 采集 **host 侧**对一次 agent 回合的客观证据。
//
// 为什么需要它：Chiron 此前判断"这一轮做完了"完全依赖模型的自我表述 ——
// 模型不再调工具（runtime.py:2000 `break`）就被当作完成，没有任何外部信号复核。
// 于是"改了文件但没验证""声称通过但没有验证跟随"这类情况**在证据层面无从发现**。
//
// 本包只做**采集**（把网关已观测到的工具事件归一化成记录），不做判定 ——
// 判定在 internal/agentgoal（P1b）。采集与判定分离，是为了让判定成为**纯函数**、
// 可单测，且**不读取任何由模型写下的状态**（对齐 Reasonix verdict 的 I2 约束：
// 一条 claim 可以触发验证，但永远不能自己满足义务）。
//
// 数据来源（唯一的采集点：internal/api/submit_handler.go 的引擎事件循环）：
//   - `tool_call`    事件 → 工具名 + 参数（JSON 字符串）
//   - `tool_result`  事件 → 结果 JSON（`error` 字段非空即失败，与网关既有口径一致）
package evidence

import (
	"encoding/json"
	"regexp"
	"sort"
	"strings"
)

// Kind 是证据能证明的东西。刻意只有三种取值 —— 判定逻辑保持可推理。
type Kind string

const (
	// KindRead：只读取了信息，未改动工作区。
	KindRead Kind = "read"
	// KindMutation：改动了工作区（写 / 编辑 / 删除文件）。
	KindMutation Kind = "mutation"
	// KindVerification：运行了**验证命令**（测试 / 静态检查 / 构建）且**成功**。
	// 只有成功的验证才计入；失败的验证是"已知问题"，不是"已证明"。
	KindVerification Kind = "verification"
	// KindUnknown：未识别的工具。**不参与任何判定**（既不证明也不否定）。
	KindUnknown Kind = "unknown"
)

// Record 是一条 host 观测到的证据。
type Record struct {
	ToolCallID string `json:"tool_call_id"`
	ToolName   string `json:"tool_name"`
	Kind       Kind   `json:"kind"`
	// OK 是工具**自身**报告的成功与否（来自结果 JSON 的 error 字段）。
	OK bool `json:"ok"`
	// Target 是作用对象：写文件类工具的文件路径；验证类工具的命令摘要；其余为空。
	Target string `json:"target,omitempty"`
	// Detail 是给报告用的简短说明（失败原因 / 退出码等）。
	Detail string `json:"detail,omitempty"`
}

// ── 工具分类表 ──
//
// ⚠️ 工具名必须与 python-engine/app/agent/modes.py 的 CORE_TOOL_NAMES / 额外工具集一致；
// 未列出的工具归 KindUnknown（**不参与判定**），而不是猜成 mutation 或 verification。
var (
	// 改动工作区的工具。
	mutationTools = map[string]struct{}{
		"write_file":  {},
		"edit_file":   {},
		"delete_file": {},
		// shell_exec / run_code 也可能写文件，但**不能确定** —— 归为"可验证"类，
		// 只有在命令被识别为验证命令时才计入，避免把任意 shell 误判成改动。
	}
	// 能产生验证证据的工具（命令 / 代码内容需要匹配下面的验证正则）。
	verifiableTools = map[string]struct{}{
		"shell_exec":     {},
		"execute_python": {},
		"run_code":       {},
	}
	// 明确的只读工具（其余未列出的归 unknown）。
	readTools = map[string]struct{}{
		"read_file":         {},
		"ls":                {},
		"grep_files":        {},
		"search_files":      {},
		"git_status":        {},
		"skill_list":        {},
		"recall":            {},
		"read_tool_result":  {},
		"subagent_list":     {},
		"subagent_result":   {},
		"subagent_rerun":    {},
		"subagent_resume":   {},
		"media_create":      {}, // 产出资产，不改工作区
		"remember":          {}, // 写记忆，不改工作区
		"skill_run":         {}, // 技能副作用不可知 —— 保守归只读（由工具自身的裁决负责）
		"use_capability":    {}, // P7：单一能力代理，具体动作在子调用里
		"read_subagent_result": {},
	}
)

// 验证命令识别。这个清单**刻意显式**（不写成宽泛的 `check`），
// 因为"把什么算作验证"直接决定"未验证改动"的判定，宁可窄也不要误判。
var verifyPattern = regexp.MustCompile(`(?i)(^|[^a-z0-9_])(` + strings.Join([]string{
	`pytest`, `py\.test`, `python -m pytest`, `python -m unittest`,
	`go test`, `go vet`, `go build`,
	`npm test`, `npm run test`, `npm run build`, `npm run typecheck`, `npm run lint`,
	`pnpm test`, `pnpm build`, `pnpm check`, `yarn test`, `yarn lint`, `bun test`,
	`cargo test`, `cargo check`, `cargo clippy`,
	`mypy`, `ruff check`, `golangci-lint`, `staticcheck`,
	`tsc( --noemit)?`, `tsc --noEmit`, `dotnet test`, `mvn test`, `gradle check`,
	`make test`, `just verify`,
	`eslint`, `prettier --check`, `vitest`, `jest`,
}, `|`) + `)([^a-z0-9_]|$)`)

// 从工具参数里取"待验证的对象"：shell/python 类的命令或代码正文。
var (
	commandKeys = []string{"command", "cmd", "code", "script", "source"}
)

// Collector 在**一次回合**内累积证据。非并发安全（网关按会话串行消费事件流）。
type Collector struct {
	order   []string // tool_call_id 出现顺序，保证 Records() 稳定
	calls   map[string]pendingCall
	records []Record
}

type pendingCall struct {
	name string
	args string
}

// NewCollector 建一个空采集器。
func NewCollector() *Collector {
	return &Collector{calls: make(map[string]pendingCall)}
}

// ObserveToolCall 记录一次工具调用（工具名 + 参数 JSON）。
func (c *Collector) ObserveToolCall(id, name, arguments string) {
	if id == "" {
		return
	}
	if _, seen := c.calls[id]; !seen {
		c.order = append(c.order, id)
	}
	c.calls[id] = pendingCall{name: name, args: arguments}
}

// ObserveToolResult 记录一次工具结果并**立即生成证据记录**。
//
// content 是引擎 tool_result 事件的载荷（工具结果的 JSON 文本）。
// OK 的判据与网关既有口径一致：结果 JSON 里 `error` 字段非空即失败。
func (c *Collector) ObserveToolResult(id, toolName, content string) {
	call, ok := c.calls[id]
	name := toolName
	if name == "" && ok {
		name = call.name
	}
	args := ""
	if ok {
		args = call.args
	}

	success, detail := resultOutcome(content)
	rec := Record{
		ToolCallID: id,
		ToolName:   name,
		OK:         success,
		Detail:     detail,
	}
	rec.Kind, rec.Target = classify(name, args, success)
	c.records = append(c.records, rec)
}

// Records 返回按调用顺序排列的全部证据（副本）。
func (c *Collector) Records() []Record {
	out := make([]Record, len(c.records))
	copy(out, c.records)
	return out
}

// Mutations 返回改动工作区的证据，按发生顺序。
func (c *Collector) Mutations() []Record { return c.filter(KindMutation) }

// Verifications 返回**成功**的验证证据，按发生顺序。
func (c *Collector) Verifications() []Record {
	var out []Record
	for _, r := range c.records {
		if r.Kind == KindVerification && r.OK {
			out = append(out, r)
		}
	}
	return out
}

// UnverifiedMutations 返回"改动之后没有成功验证跟随"的改动证据。
//
// 判据（对齐 Reasonix completion 的"做减法"之一：无验证跟随的 mutation）：
// 对每一条成功的 mutation，若**在它之后**没有任何成功的 verification，则计入。
// 这是"改了没验"的客观表述 —— 不评价改得对不对，只陈述"没有验证跟随"。
func (c *Collector) UnverifiedMutations() []Record {
	lastVerifyAt := -1
	for i, r := range c.records {
		if r.Kind == KindVerification && r.OK {
			lastVerifyAt = i
		}
	}
	var out []Record
	for i, r := range c.records {
		if r.Kind == KindMutation && r.OK && i > lastVerifyAt {
			out = append(out, r)
		}
	}
	return out
}

func (c *Collector) filter(k Kind) []Record {
	var out []Record
	for _, r := range c.records {
		if r.Kind == k {
			out = append(out, r)
		}
	}
	return out
}

// classify 决定一条证据的种类与作用对象。
func classify(name, args string, success bool) (Kind, string) {
	if _, ok := mutationTools[name]; ok {
		return KindMutation, pathArg(args)
	}
	if _, ok := verifiableTools[name]; ok {
		cmd := commandArg(args)
		if IsVerificationCommand(cmd) {
			kind := KindVerification
			if !success {
				// 失败的验证仍是一条"验证证据"，但 OK=false —— Verifications()/
				// UnverifiedMutations() 只认成功的，故它不会让改动变成"已验证"。
				return kind, summarizeCommand(cmd)
			}
			return kind, summarizeCommand(cmd)
		}
		// 非验证命令的可执行工具：无法确定它是否改了工作区 ⇒ unknown（不参与判定）。
		return KindUnknown, ""
	}
	if _, ok := readTools[name]; ok {
		return KindRead, ""
	}
	return KindUnknown, ""
}

// resultOutcome 从工具结果 JSON 里判成败并提取简短说明。
func resultOutcome(content string) (bool, string) {
	content = strings.TrimSpace(content)
	if content == "" {
		// 空结果：无法判定 ⇒ 按失败处理（fail-closed，宁可判"没证明"）。
		return false, "empty result"
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(content), &obj); err != nil {
		// 非 JSON（引擎偶尔直接回字符串）：用既有网关口径的关键字兜底。
		if strings.Contains(content, `"error"`) || strings.HasPrefix(content, "error") {
			return false, truncate(content, 200)
		}
		return true, ""
	}
	if raw, ok := obj["error"]; ok && raw != nil {
		if s, ok := raw.(string); ok && strings.TrimSpace(s) != "" {
			return false, truncate(s, 200)
		}
		// error 键存在但为空字符串/其它零值：视为无错（有些工具用 error:"" 表示成功）。
	}
	if failed, ok := obj["success"].(bool); ok && !failed {
		return false, "success=false"
	}
	return true, ""
}

// pathArg 从参数 JSON 里提取文件路径类字段。
func pathArg(args string) string {
	var obj map[string]any
	if err := json.Unmarshal([]byte(args), &obj); err != nil {
		return ""
	}
	for _, k := range []string{"path", "file_path", "filepath", "filename", "file", "target"} {
		if v, ok := obj[k].(string); ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// commandArg 从参数 JSON 里提取命令/代码正文。
func commandArg(args string) string {
	var obj map[string]any
	if err := json.Unmarshal([]byte(args), &obj); err != nil {
		// 参数可能是半截 JSON（与引擎 _parse_tool_args 的口径一致：解析失败当作空）。
		return ""
	}
	for _, k := range commandKeys {
		if v, ok := obj[k].(string); ok && strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// IsVerificationCommand 判断一段命令/代码是否属于"验证"。
//
// 导出是为了让测试与后续（P1b/P6）复用同一口径，避免各处各写一份正则而漂移。
func IsVerificationCommand(cmd string) bool {
	return verifyPattern.MatchString(strings.TrimSpace(cmd))
}

// summarizeCommand 取命令的首行、限长，供报告展示。
func summarizeCommand(cmd string) string {
	cmd = strings.TrimSpace(cmd)
	if i := strings.IndexByte(cmd, '\n'); i >= 0 {
		cmd = cmd[:i]
	}
	return truncate(cmd, 160)
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// SortByTool 仅供测试与稳定输出使用：按工具名排序（不改动内部顺序）。
func SortByTool(rs []Record) []Record {
	out := make([]Record, len(rs))
	copy(out, rs)
	sort.SliceStable(out, func(i, j int) bool { return out[i].ToolName < out[j].ToolName })
	return out
}
