// Package cli 实现 `chiron-cli run` 的 headless（非交互）执行核心 —— 方案 03 §2「批 F」。
//
// 定位：**纯客户端封装**，把一次 agent 回合跑完并把结果交给 CI/脚本消费。
// 它只复用网关既有的两条链路，不新造任何协议、也不新增服务端端点：
//
//	GET  /events?session_id=...   → 订阅 SSE（与前端同一入口，events.go）
//	POST /submit                  → 提交用户消息（与前端同一入口，gateway_router.go）
//
// 审批/提问同样复用既有端点（/v1/agent/approval、/v1/agent/answer），
// 身份复用 API Key（X-API-Key，middleware.go 已实现），不引入新的认证路径。
//
// 安全默认：审批与提问都默认 **fail**。CI 里「静默批准」是危险默认 ——
// 它会把一道人工闸门变成自动放行，因此默认失败并给出**稳定的退出码**，
// 让流水线能明确区分"被拦下"与"真的跑完了"。
package cli

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// 退出码 —— 与引擎 AgentEvent 的既有语义对齐（方案 03 §2）。
//
// 关键约定来自引擎 SSE 事件：
//   - `error` 事件的 content 以 `budget_exceeded:<轴>` 开头 = 预算越界（失败收尾）；
//   - `guardrail_blocked` 事件 = 被输入/输出栅栏拦下。
//
// 由于引擎在序列化时把 error 内容放进了 content 字段（python-engine/app/main.py 的
// `content = content or error`），CLI 判预算越界时看的是 content 前缀而非单独字段。
const (
	ExitOK      = 0 // 正常完成（收到 done / turn_done）
	ExitFail    = 1 // 其它失败：引擎 error、网络/协议错误、流被提前关闭
	ExitBlocked = 2 // 被护栏拦下，或需要人工审批/回答而策略为 fail
	ExitTimeout = 3 // 超过 --timeout
	ExitBudget  = 4 // 预算越界（budget_exceeded:<轴>）
)

// OnApproval 是遇到 `approval` 事件时的策略。
type OnApproval string

const (
	OnApprovalFail    OnApproval = "fail"    // 默认：不批准，以 ExitBlocked 结束
	OnApprovalApprove OnApproval = "approve" // 自动批准并继续
	OnApprovalDeny    OnApproval = "deny"    // 自动拒绝（agent 会看到拒绝后继续）
)

// OnAsk 是遇到 `ask`（ask_user）事件时的策略。
type OnAsk string

const (
	OnAskFail    OnAsk = "fail"    // 默认：非交互下无法回答，以 ExitBlocked 结束
	OnAskDefault OnAsk = "default" // 用固定默认答案回答，让 agent 继续
)

// defaultAskAnswer 是 `--on-ask default` 使用的固定答案。
//
// 为什么是字面常量而不是引擎的建议答案：引擎的 ask 事件带 `options`（建议答案），
// 但网关转发的 PythonEvent（internal/engine/python_client.go）没有该字段，
// 因此 CLI 经网关拿不到 options —— 只能用固定值。
const defaultAskAnswer = "yes"

const (
	// thinkingOpen/Close 是引擎转发思考增量的包裹标签（DeepSeek thinking mode）。
	// 它们不属于"最终回答"，默认输出会被剥离（与 evals/observe.py 同口径）。
	thinkingOpen  = "[thinking]"
	thinkingClose = "[/thinking]"

	// defaultTimeout 与命令默认值一致；Unit=秒的换算在命令层完成。
	defaultTimeout = 600 * time.Second

	// maxEventBytes 单帧上限：工具结果可能很大，给足空间但设上界防内存放大。
	maxEventBytes = 8 << 20
)

// RunOptions 是一次 headless 执行的全部入参。
type RunOptions struct {
	Addr       string        // 网关基址，如 http://localhost:8080
	APIKey     string        // X-API-Key；空则不带头（供无认证的本地部署）
	SessionID  string        // 会话 ID
	Message    string        // 本轮用户消息
	JSON       bool          // true=输出 NDJSON（与 SSE 事件一一对应）；false=只输出最终文本
	Timeout    time.Duration // 整体超时；<=0 取 defaultTimeout
	OnApproval OnApproval    // 审批策略
	OnAsk      OnAsk         // 提问策略
	ClientID   string        // SSE 订阅标识；空则自动生成（仅用于订阅，非安全敏感）
}

// Runner 持有可注入的 IO 与 HTTP 客户端，使其可在测试里完全用 httptest 打桩驱动。
type Runner struct {
	HTTP *http.Client // 空则用 http.DefaultClient
	Out  io.Writer    // 空则 os.Stdout
	Err  io.Writer    // 空则 os.Stderr
}

// NewRunner 构造使用默认 IO 与 HTTP 客户端的 Runner。
// 注意：SSE 是长连接，HTTP 客户端**不能**设 Client.Timeout（会切断长流），
// 超时统一由 Run 的 context 控制。
func NewRunner() *Runner {
	return &Runner{HTTP: &http.Client{}, Out: os.Stdout, Err: os.Stderr}
}

// ValidateStrategies 在发请求前校验策略取值，避免把一个拼错的策略静默当成默认值。
func ValidateStrategies(a OnApproval, q OnAsk) error {
	switch a {
	case OnApprovalFail, OnApprovalApprove, OnApprovalDeny:
	default:
		return fmt.Errorf("invalid --on-approval %q (want fail|approve|deny)", a)
	}
	switch q {
	case OnAskFail, OnAskDefault:
	default:
		return fmt.Errorf("invalid --on-ask %q (want fail|default)", q)
	}
	return nil
}

func (r *Runner) client() *http.Client {
	if r.HTTP != nil {
		return r.HTTP
	}
	return http.DefaultClient
}

func (r *Runner) out() io.Writer {
	if r.Out != nil {
		return r.Out
	}
	return os.Stdout
}

func (r *Runner) errw() io.Writer {
	if r.Err != nil {
		return r.Err
	}
	return os.Stderr
}

// runState 汇总一次执行中需要跨事件累积的状态。
type runState struct {
	text        strings.Builder
	blocked     bool
	blockReason string
}

// Run 执行一次 headless 回合并返回退出码（不 panic、不调用 os.Exit，便于测试）。
//
// 顺序刻意是「先订阅、后提交」：引擎事件可能在提交后毫秒级到达，
// 先建立 SSE 才能确保不漏（网关 /events 对新会话放行，正是为这种"先建流"用法）。
func (r *Runner) Run(ctx context.Context, opts RunOptions) int {
	if opts.Timeout <= 0 {
		opts.Timeout = defaultTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, opts.Timeout)
	defer cancel()

	stream, err := r.openStream(ctx, opts)
	if err != nil {
		fmt.Fprintf(r.errw(), "error: cannot open event stream: %v\n", err)
		return ExitFail
	}
	defer stream.Body.Close()

	if err := r.submit(ctx, opts); err != nil {
		fmt.Fprintf(r.errw(), "error: submit failed: %v\n", err)
		return ExitFail
	}

	st := &runState{}
	scanner := bufio.NewScanner(stream.Body)
	scanner.Buffer(make([]byte, 64*1024), maxEventBytes)
	for scanner.Scan() {
		raw, ok := sseDataLine(scanner.Text())
		if !ok {
			continue // id: / event: / 注释行 / 空行 —— 与 SSE 规范一致地忽略
		}
		if opts.JSON {
			// --json：把 SSE 事件原样逐行输出为 NDJSON（与事件一一对应，不引新模型）
			fmt.Fprintln(r.out(), raw)
		}
		if code, done := r.handleEvent(ctx, opts, st, raw); done {
			if !opts.JSON && code == ExitOK {
				r.writeFinalText(st)
			}
			return code
		}
	}

	// 流结束但没有终态事件：区分"超时"与"真的断了"。
	if ctx.Err() != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			fmt.Fprintf(r.errw(), "error: timed out after %s waiting for the run to finish\n", opts.Timeout)
			return ExitTimeout
		}
		fmt.Fprintln(r.errw(), "error: run cancelled")
		return ExitFail
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(r.errw(), "error: event stream error: %v\n", err)
		return ExitFail
	}
	if st.blocked {
		// 护栏事件到达后流就断了（未收到 turn_done）：仍按"被拦下"归类。
		return ExitBlocked
	}
	fmt.Fprintln(r.errw(), "error: event stream ended before a terminal event")
	return ExitFail
}

// envelope 是网关 SSE 帧的顶层结构（internal/broadcast/hub.go 的 Event）。
// data 是引擎事件（PythonEvent），字段为 type/content/id/name/arguments/...，
// 因此工具调用 id/name 在 data 的 `id`/`name` 里，而不是 tool_call_id/tool_name。
type envelope struct {
	ID        string          `json:"id"`
	Type      string          `json:"type"`
	Data      json.RawMessage `json:"data"`
	SessionID string          `json:"session_id"`
}

// engineEvent 是 SSE 帧 data 的宽松视图 —— 只取判定退出码与审批所需字段，
// 未知字段一律忽略（引擎新增事件类型不会让 CLI 报错）。
type engineEvent struct {
	Type         string `json:"type"`
	Content      string `json:"content"`
	ID           string `json:"id"`
	Name         string `json:"name"`
	Arguments    string `json:"arguments"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	Model        string `json:"model"`
}

func (r *Runner) handleEvent(ctx context.Context, opts RunOptions, st *runState, raw string) (int, bool) {
	var env envelope
	if err := json.Unmarshal([]byte(raw), &env); err != nil {
		// 单帧解析失败不该让整趟执行失败（与 evals 的"宽容解析"一致）。
		return ExitOK, false
	}
	var data engineEvent
	_ = json.Unmarshal(env.Data, &data)

	switch env.Type {
	case "text":
		// 累积原始文本（可能含 thinking 段），最终一次性剥离 —— 逐帧剥离会切坏跨帧的标签。
		st.text.WriteString(data.Content)
		return ExitOK, false

	case "guardrail_blocked":
		st.blocked = true
		st.blockReason = strings.TrimSpace(data.Content)
		fmt.Fprintf(r.errw(), "blocked by guardrail: %s\n", st.blockReason)
		return ExitOK, false

	case "approval":
		return r.handleApproval(ctx, opts, st, data)

	case "ask":
		return r.handleAsk(ctx, opts, st, data)

	case "error":
		msg := strings.TrimSpace(data.Content)
		if strings.HasPrefix(msg, "budget_exceeded:") {
			fmt.Fprintf(r.errw(), "budget exceeded: %s\n", strings.TrimPrefix(msg, "budget_exceeded:"))
			return ExitBudget, true
		}
		fmt.Fprintf(r.errw(), "engine error: %s\n", msg)
		return ExitFail, true

	case "done":
		// 引擎正常收尾（网关随后还会补一个 turn_done，但 done 已足够定论）。
		return ExitOK, true

	case "turn_done":
		// 网关收尾事件：走护栏路径时引擎不会发 done，只会有这个。
		if st.blocked {
			return ExitBlocked, true
		}
		return ExitOK, true

	default:
		// connected / trace_span / tool_call / tool_result / compaction / todo_updated / ...
		return ExitOK, false
	}
}

func (r *Runner) handleApproval(ctx context.Context, opts RunOptions, st *runState, data engineEvent) (int, bool) {
	switch opts.OnApproval {
	case OnApprovalApprove, OnApprovalDeny:
		approved := opts.OnApproval == OnApprovalApprove
		err := r.postJSON(ctx, opts, "/v1/agent/approval", map[string]any{
			"session_id":   opts.SessionID,
			"tool_call_id": data.ID,
			"approved":     approved,
		})
		if err != nil {
			fmt.Fprintf(r.errw(), "error: failed to send approval for tool %q: %v\n", data.Name, err)
			return ExitFail, true
		}
		return ExitOK, false

	default: // OnApprovalFail
		st.blocked = true
		st.blockReason = fmt.Sprintf("tool %q requires approval", data.Name)
		fmt.Fprintf(r.errw(),
			"blocked: tool %q requires approval (tool_call_id=%s); "+
				"re-run with --on-approval approve|deny to decide automatically\n",
			data.Name, data.ID)
		return ExitBlocked, true
	}
}

func (r *Runner) handleAsk(ctx context.Context, opts RunOptions, st *runState, data engineEvent) (int, bool) {
	if opts.OnAsk == OnAskDefault {
		err := r.postJSON(ctx, opts, "/v1/agent/answer", map[string]any{
			"session_id":   opts.SessionID,
			"tool_call_id": data.ID,
			"answer":       defaultAskAnswer,
		})
		if err != nil {
			fmt.Fprintf(r.errw(), "error: failed to answer ask_user: %v\n", err)
			return ExitFail, true
		}
		return ExitOK, false
	}
	st.blocked = true
	st.blockReason = "agent asked a question"
	fmt.Fprintf(r.errw(),
		"blocked: agent asked %q; a non-interactive run cannot answer "+
			"(use --on-ask default to answer automatically)\n", strings.TrimSpace(data.Content))
	return ExitBlocked, true
}

// openStream 建立 SSE 订阅连接（GET /events）。状态码非 200 直接失败并回带响应体片段，
// 便于定位"鉴权失败 / 端点不存在"这类问题。
func (r *Runner) openStream(ctx context.Context, opts RunOptions) (*http.Response, error) {
	u, err := url.Parse(strings.TrimRight(opts.Addr, "/") + "/events")
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("session_id", opts.SessionID)
	cid := opts.ClientID
	if cid == "" {
		cid = fmt.Sprintf("chiron-cli-%d", os.Getpid())
	}
	q.Set("client_id", cid)
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "text/event-stream")
	r.setAuth(req, opts)

	resp, err := r.client().Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		resp.Body.Close()
		return nil, fmt.Errorf("event stream returned status %d: %s",
			resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return resp, nil
}

// submit 提交本轮消息（POST /submit）。网关返回 202 后由后台 goroutine 驱动引擎，
// 事件经 /events 回推 —— 因此这里只看提交是否被受理，不等结果。
func (r *Runner) submit(ctx context.Context, opts RunOptions) error {
	return r.postJSON(ctx, opts, "/submit", map[string]any{
		"session_id": opts.SessionID,
		"content":    opts.Message,
	})
}

// postJSON 向网关发一个 JSON POST，非 2xx 视为失败并回带响应体片段。
func (r *Runner) postJSON(ctx context.Context, opts RunOptions, path string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(opts.Addr, "/")+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	r.setAuth(req, opts)

	resp, err := r.client().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return fmt.Errorf("%s returned status %d: %s", path, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	return nil
}

func (r *Runner) setAuth(req *http.Request, opts RunOptions) {
	if opts.APIKey != "" {
		// 复用既有认证路径：middleware.go 直接读 X-API-Key，无新协议。
		req.Header.Set("X-API-Key", opts.APIKey)
	}
}

func (r *Runner) writeFinalText(st *runState) {
	text := strings.TrimSpace(stripThinking(st.text.String()))
	if text != "" {
		fmt.Fprintln(r.out(), text)
	}
}

// sseDataLine 从一行 SSE 中取出 `data:` 后的 JSON（非 data 行返回 false）。
// 刻意宽容：解析不了的行跳过，避免一行噪音让整趟执行失败。
func sseDataLine(line string) (string, bool) {
	if !strings.HasPrefix(line, "data:") {
		return "", false
	}
	raw := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
	if raw == "" || raw[0] != '{' {
		return "", false
	}
	return raw, true
}

// stripThinking 剥离思考块，只留正文（与 evals/observe.py 的口径一致：
// 否则"模型想过但没说"会被当成最终回答）。
func stripThinking(text string) string {
	if !strings.Contains(text, thinkingOpen) {
		return text
	}
	var b strings.Builder
	depth := 0
	for i := 0; i < len(text); {
		switch {
		case strings.HasPrefix(text[i:], thinkingOpen):
			depth++
			i += len(thinkingOpen)
		case strings.HasPrefix(text[i:], thinkingClose):
			if depth > 0 {
				depth--
			}
			i += len(thinkingClose)
		default:
			if depth == 0 {
				b.WriteByte(text[i])
			}
			i++
		}
	}
	return b.String()
}
