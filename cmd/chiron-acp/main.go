// Command chiron-acp 是 Chiron 的 **ACP（Agent Client Protocol）前端**。
//
// 作用：让支持 ACP 的编辑器/客户端（Zed 等）把 Chiron 的 agent 当"本地 agent"用 ——
// 编辑器用标准协议说话，本进程负责翻译成 Chiron 网关的 HTTP API。
//
//	editor ──(ACP: JSON-RPC 2.0, 换行分隔, stdio)──> chiron-acp ──(HTTP/SSE)──> gateway ──> engine
//
// 为什么是独立进程而不是网关的一个路由：ACP 说的是 **stdio + JSON-RPC**，而网关是 HTTP 服务；
// 把 stdio 语义塞进 HTTP 服务会同时破坏两者。独立进程也让"编辑器崩了不影响网关"自然成立。
//
// 环境变量：
//
//	CHIRON_GATEWAY_URL  Chiron 网关地址（默认 http://127.0.0.1:8080）
//	CHIRON_ACP_TOKEN    调用网关用的 Bearer token（**必填**；缺失时 initialize 会如实报错）
//
// 协议范围（够用且完整，不做半成品）：
//
//	initialize        —— 能力协商
//	authenticate      —— 校验 token 可用（本进程不存储凭据，只做一次探活）
//	session/new       —— 在网关上建一个会话
//	session/prompt    —— 发一轮，边流边推 session/update
//	session/cancel    —— 取消正在跑的一轮（通知）
//
// 未实现的方法一律返回 -32601（Method not found）—— 明确失败，不假装支持。
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	protocolVersion = 1

	// ACP 的 session/update 里用到的 kinds（与 ACP 规范一致）。
	updateAgentMessage = "agent_message_chunk"
	updateAgentThought = "agent_thought_chunk"
	updateToolCall     = "tool_call"
	updateToolCallDone = "tool_call_update"

	// 停止原因（ACP 规范）。
	stopEndTurn    = "end_turn"
	stopMaxTokens  = "max_tokens"
	stopCancelled  = "cancelled"
	stopRefusal    = "refusal"
	stopMaxTurns   = "max_turn_requests"
	gatewayTimeout = 300 * time.Second
)

// ── JSON-RPC 2.0 ────────────────────────────────────────────────────────────

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcNotification struct {
	JSONRPC string `json:"jsonrpc"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

const (
	codeParseError     = -32700
	codeInvalidRequest = -32600
	codeMethodNotFound = -32601
	codeInvalidParams  = -32602
	codeInternalError  = -32603
)

// ── 连接 ────────────────────────────────────────────────────────────────────

type conn struct {
	gateway string
	token   string
	client  *http.Client

	mu      sync.Mutex
	out     *bufio.Writer
	nextID  int
	waiters map[int]chan json.RawMessage

	cancelMu   sync.Mutex
	cancelling map[string]context.CancelFunc
}

func newConn(gateway, token string) *conn {
	return &conn{
		gateway:    strings.TrimRight(gateway, "/"),
		token:      token,
		client:     &http.Client{Timeout: gatewayTimeout},
		out:        bufio.NewWriter(os.Stdout),
		waiters:    map[int]chan json.RawMessage{},
		cancelling: map[string]context.CancelFunc{},
	}
}

func (c *conn) write(v any) {
	b, err := json.Marshal(v)
	if err != nil {
		log.Printf("acp: marshal outbound failed: %v", err)
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.out.Write(b)
	c.out.WriteByte('\n')
	if err := c.out.Flush(); err != nil {
		log.Printf("acp: flush failed: %v", err)
	}
}

func (c *conn) reply(id json.RawMessage, result any, rerr *rpcError) {
	if len(id) == 0 {
		return // 通知不需要回执
	}
	c.write(rpcResponse{JSONRPC: "2.0", ID: id, Result: result, Error: rerr})
}

// notifySessionUpdate 推一条 session/update（ACP 的流式通道）。
func (c *conn) notifySessionUpdate(sessionID string, update map[string]any) {
	c.write(rpcNotification{
		JSONRPC: "2.0",
		Method:  "session/update",
		Params:  map[string]any{"sessionId": sessionID, "update": update},
	})
}

// callClient 调用**客户端**的方法（如 session/request_permission），等待其结果。
func (c *conn) callClient(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	ch := make(chan json.RawMessage, 1)
	c.waiters[id] = ch
	c.mu.Unlock()

	c.write(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})

	select {
	case res := <-ch:
		return res, nil
	case <-ctx.Done():
		c.mu.Lock()
		delete(c.waiters, id)
		c.mu.Unlock()
		return nil, ctx.Err()
	}
}

// ── 网关调用 ────────────────────────────────────────────────────────────────

func (c *conn) doJSON(ctx context.Context, method, path string, body any, out any) error {
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			return err
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, c.gateway+path, &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 300 {
		return fmt.Errorf("gateway %s %s: status %d: %s", method, path, resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if out != nil {
		if err := json.Unmarshal(raw, out); err != nil {
			return fmt.Errorf("gateway %s %s: bad JSON: %w", method, path, err)
		}
	}
	return nil
}

// ── 方法实现 ────────────────────────────────────────────────────────────────

func (c *conn) handleInitialize(id json.RawMessage, _ json.RawMessage) {
	authMethods := []map[string]any{}
	if c.token == "" {
		// 如实声明：没有 token 就连不上网关，别让客户端以为"没事"。
		authMethods = append(authMethods, map[string]any{
			"id":          "chiron-token",
			"name":        "Chiron API token",
			"description": "Set CHIRON_ACP_TOKEN to a Chiron API token (or JWT) before starting chiron-acp.",
		})
	}
	c.reply(id, map[string]any{
		"protocolVersion": protocolVersion,
		"agentCapabilities": map[string]any{
			// 我们支持流式回复与"取消当前轮"；不宣称支持客户端侧 fs 代理。
			"promptCapabilities": map[string]any{
				"image":           false,
				"audio":           false,
				"embeddedContext": false,
			},
		},
		"authMethods": authMethods,
	}, nil)
}

func (c *conn) handleAuthenticate(id json.RawMessage) {
	if c.token == "" {
		c.reply(id, nil, &rpcError{
			Code:    codeInvalidParams,
			Message: "no token configured: set CHIRON_ACP_TOKEN",
		})
		return
	}
	// 一次探活：token 不可用时**当场**失败，而不是等到用户发第一条消息。
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.doJSON(ctx, http.MethodGet, "/v1/models", nil, nil); err != nil {
		c.reply(id, nil, &rpcError{Code: codeInvalidParams, Message: "authentication failed: " + err.Error()})
		return
	}
	c.reply(id, map[string]any{}, nil)
}

func (c *conn) handleSessionNew(id json.RawMessage, params json.RawMessage) {
	var p struct {
		Cwd        string         `json:"cwd"`
		McpServers []any          `json:"mcpServers"`
		Meta       map[string]any `json:"_meta"`
	}
	_ = json.Unmarshal(params, &p) // 参数可空：ACP 允许只带 cwd

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// 在网关上建一个真实会话 —— 这样会话历史、计费、审计都与会话一致地成立，
	// 而不是在 ACP 进程里伪造一个只存在于内存的 id。
	var resp struct {
		Data struct {
			ID string `json:"id"`
		} `json:"data"`
		ID string `json:"id"`
	}
	body := map[string]any{
		"title":      "ACP session",
		"llm_config": map[string]any{},
	}
	if err := c.doJSON(ctx, http.MethodPost, "/v1/conversations", body, &resp); err != nil {
		c.reply(id, nil, &rpcError{Code: codeInternalError, Message: "failed to create session: " + err.Error()})
		return
	}
	sessionID := resp.Data.ID
	if sessionID == "" {
		sessionID = resp.ID
	}
	if sessionID == "" {
		c.reply(id, nil, &rpcError{Code: codeInternalError, Message: "gateway did not return a session id"})
		return
	}
	c.reply(id, map[string]any{"sessionId": sessionID}, nil)
}

func (c *conn) handleSessionPrompt(id json.RawMessage, params json.RawMessage) {
	var p struct {
		SessionID string `json:"sessionId"`
		Prompt    []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"prompt"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		c.reply(id, nil, &rpcError{Code: codeInvalidParams, Message: "invalid params: " + err.Error()})
		return
	}
	if p.SessionID == "" {
		c.reply(id, nil, &rpcError{Code: codeInvalidParams, Message: "sessionId is required"})
		return
	}
	// 本轮文本 = 所有 text 块拼接（非 text 块当前不支持，如实忽略而不是编造）。
	var sb strings.Builder
	for _, b := range p.Prompt {
		if b.Type == "text" {
			sb.WriteString(b.Text)
		}
	}
	content := strings.TrimSpace(sb.String())
	if content == "" {
		c.reply(id, nil, &rpcError{Code: codeInvalidParams, Message: "prompt must contain text"})
		return
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	c.cancelMu.Lock()
	c.cancelling[p.SessionID] = cancel
	c.cancelMu.Unlock()
	defer func() {
		c.cancelMu.Lock()
		delete(c.cancelling, p.SessionID)
		c.cancelMu.Unlock()
	}()

	stop, err := c.streamPrompt(ctx, p.SessionID, content)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			c.reply(id, map[string]any{"stopReason": stopCancelled}, nil)
			return
		}
		c.reply(id, nil, &rpcError{Code: codeInternalError, Message: err.Error()})
		return
	}
	c.reply(id, map[string]any{"stopReason": stop}, nil)
}

func (c *conn) handleSessionCancel(params json.RawMessage) {
	var p struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(params, &p); err != nil {
		return
	}
	c.cancelMu.Lock()
	cancel := c.cancelling[p.SessionID]
	c.cancelMu.Unlock()
	if cancel != nil {
		cancel()
	}
	// 同时通知网关侧真取消（引擎会中断这一轮）。
	ctx, cancelCtx := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelCtx()
	_ = c.doJSON(ctx, http.MethodPost, "/v1/agent/interrupt",
		map[string]any{"session_id": p.SessionID}, nil)
}

// ── 流式：把网关 SSE 事件翻译成 ACP session/update ─────────────────────────────

func (c *conn) streamPrompt(ctx context.Context, sessionID, content string) (string, error) {
	payload := map[string]any{
		"content":    content,
		"session_id": sessionID,
		"llm_config": map[string]any{"mode": "normal"},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.gateway+"/v1/agent/submit", bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	streamClient := &http.Client{} // 流式：不用总超时（长回合很常见）
	resp, err := streamClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("submit failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		return "", fmt.Errorf("submit rejected: status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	stop := stopEndTurn
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data:") {
			continue // "event:" / 注释 / 空行：ACP 侧只需要载荷
		}
		payloadText := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payloadText == "" || payloadText == "[DONE]" {
			continue
		}
		var ev struct {
			Type      string  `json:"type"`
			Content   string  `json:"content"`
			ID        string  `json:"tool_call_id"`
			Name      string  `json:"tool_name"`
			Arguments string  `json:"tool_arguments"`
			Error     string  `json:"error"`
			Stop      string  `json:"stop_reason"`
		}
		if err := json.Unmarshal([]byte(payloadText), &ev); err != nil {
			continue // 无法解析的帧：跳过而不是中断整轮
		}
		switch ev.Type {
		case "text":
			if ev.Content != "" {
				c.notifySessionUpdate(sessionID, map[string]any{
					"sessionUpdate": updateAgentMessage,
					"content":       map[string]any{"type": "text", "text": ev.Content},
				})
			}
		case "thinking":
			if ev.Content != "" {
				c.notifySessionUpdate(sessionID, map[string]any{
					"sessionUpdate": updateAgentThought,
					"content":       map[string]any{"type": "text", "text": ev.Content},
				})
			}
		case "tool_call":
			c.notifySessionUpdate(sessionID, map[string]any{
				"sessionUpdate": updateToolCall,
				"toolCallId":    ev.ID,
				"title":         ev.Name,
				"kind":          "other",
				"status":        "in_progress",
				"rawInput":      ev.Arguments,
			})
		case "tool_result":
			status := "completed"
			if strings.Contains(ev.Content, `"error"`) {
				status = "failed"
			}
			c.notifySessionUpdate(sessionID, map[string]any{
				"sessionUpdate": updateToolCallDone,
				"toolCallId":    ev.ID,
				"status":        status,
				"rawOutput":     ev.Content,
			})
		case "error":
			// 引擎侧错误：如实收尾（ACP 没有"error 事件"，用 refusal 表达"这一轮没做成"）。
			if ev.Error != "" {
				c.notifySessionUpdate(sessionID, map[string]any{
					"sessionUpdate": updateAgentMessage,
					"content":       map[string]any{"type": "text", "text": "\n[error] " + ev.Error},
				})
			}
			return stopRefusal, nil
		case "cancelled":
			return stopCancelled, nil
		case "done":
			return stop, nil
		}
	}
	if err := scanner.Err(); err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return stopCancelled, context.Canceled
		}
		return "", fmt.Errorf("stream read failed: %w", err)
	}
	return stop, nil
}

// ── 主循环 ──────────────────────────────────────────────────────────────────

func (c *conn) dispatch(ctx context.Context, raw []byte) {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		c.write(rpcResponse{
			JSONRPC: "2.0",
			ID:      json.RawMessage("null"),
			Error:   &rpcError{Code: codeParseError, Message: "invalid JSON"},
		})
		return
	}

	switch req.Method {
	case "initialize":
		c.handleInitialize(req.ID, req.Params)
	case "authenticate":
		c.handleAuthenticate(req.ID)
	case "session/new":
		c.handleSessionNew(req.ID, req.Params)
	case "session/prompt":
		c.handleSessionPrompt(req.ID, req.Params)
	case "session/cancel":
		c.handleSessionCancel(req.Params)
	default:
		// 客户端对我们发起的请求的回执（有 id、无 method 之外的形式）：
		// 由 callClient 的等待者消费。
		if len(req.ID) > 0 && req.Method == "" {
			var n int
			if err := json.Unmarshal(req.ID, &n); err == nil {
				c.mu.Lock()
				ch := c.waiters[n]
				delete(c.waiters, n)
				c.mu.Unlock()
				if ch != nil {
					ch <- req.Params
				}
				return
			}
		}
		c.reply(req.ID, nil, &rpcError{
			Code:    codeMethodNotFound,
			Message: "method not supported by chiron-acp: " + req.Method,
		})
	}
}

func main() {
	gateway := os.Getenv("CHIRON_GATEWAY_URL")
	if gateway == "" {
		gateway = "http://127.0.0.1:8080"
	}
	token := strings.TrimSpace(os.Getenv("CHIRON_ACP_TOKEN"))

	log.SetOutput(os.Stderr) // stdout 是协议通道，日志一律走 stderr
	log.Printf("chiron-acp started (gateway=%s, token=%s)", gateway, tokenState(token))

	c := newConn(gateway, token)
	ctx := context.Background()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		c.dispatch(ctx, line)
	}
	if err := scanner.Err(); err != nil {
		log.Printf("chiron-acp: stdin read failed: %v", err)
	}
	log.Printf("chiron-acp: stdin closed, exiting")
}

func tokenState(token string) string {
	if token == "" {
		return "missing (set CHIRON_ACP_TOKEN)"
	}
	return "set"
}
