package engine

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"io"
	"log/slog"
	"math"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"go.opentelemetry.io/otel/propagation"
)

// ctxSessionKey 标记请求的引擎会话路由键（session 亲和）。
type ctxSessionKey struct{}

// WithSession 把 sessionID 注入 ctx：该 ctx 下的引擎请求按 session 一致性哈希路由到
// 固定引擎实例——同 session 的持久终端（PersistentTerminal）/后台任务/簿记状态因此连续；
// 目标实例故障时自动漂移到健康实例（进程内状态丢失，记录日志）。
func WithSession(ctx context.Context, sessionID string) context.Context {
	if sessionID == "" {
		return ctx
	}
	return context.WithValue(ctx, ctxSessionKey{}, sessionID)
}

func sessionFromCtx(ctx context.Context) string {
	if v, ok := ctx.Value(ctxSessionKey{}).(string); ok {
		return v
	}
	return ""
}

// ctxRunAffinityKey 标记"必须按 run 归属路由"的请求（审批/取消）。
// 与普通会话亲和分开：归属查询要访问 Redis，只放在真正需要它的低频路径上，
// 否则每个对话请求都会被打上一次 Redis 往返（A1：Redis 抖动时的延迟放大器）。
type ctxRunAffinityKey struct{}

// WithRunAffinity = WithSession + 允许归属查询：该 ctx 下的请求先查
// engine:run:{session} 并优先路由到持有该 run 的实例；查不到/实例不可用时
// 回退一致性哈希。仅用于审批、取消等"认实例"的请求。
func WithRunAffinity(ctx context.Context, sessionID string) context.Context {
	return context.WithValue(WithSession(ctx, sessionID), ctxRunAffinityKey{}, true)
}

func runAffinityFromCtx(ctx context.Context) bool {
	v, ok := ctx.Value(ctxRunAffinityKey{}).(bool)
	return ok && v
}

// pyAddrEntry 单个引擎地址及其熔断冷却状态（Unix 秒，0=正常）。
type pyAddrEntry struct {
	url           string
	cooldownUntil int64
}

// PythonClient calls the Python AI engine via HTTP SSE.
// Supports multiple addresses with round-robin load balancing.
// 地址表（批 E1）：由 mu 保护的动态列表——StartEngineDiscovery 每 15s 从 Redis
// 引擎注册表刷新（动态优先）；注册表为空/Redis 不可用时回退 staticAddrs
// （PYTHON_ENGINE_ADDRESS 构造值），扩容缩容无需手工改配置。
type PythonClient struct {
	mu          sync.RWMutex
	addrs       []pyAddrEntry // 当前地址表
	staticAddrs []string      // 构造时的静态地址（发现回退）
	counter     uint64

	client        *http.Client // 同步 JSON/管理请求（允许慢响应头：编排任务可能数十秒才返回）
	streamClient  *http.Client // SSE 流式请求（引擎 StreamingResponse 秒级发头，故可用短响应头超时快速失败）
	internalToken string       // Go↔Python 共享内部 token，用于网关代理身份校验
}

// RetryConfig 请求重试配置
type RetryConfig struct {
	MaxRetries     int
	InitialBackoff time.Duration
	MaxBackoff     time.Duration
	Multiplier     float64
}

var defaultRetryConfig = RetryConfig{
	MaxRetries:     3,
	InitialBackoff: 100 * time.Millisecond,
	MaxBackoff:     2 * time.Second,
	Multiplier:     2.0,
}

// NewPythonClient creates a client for the Python engine HTTP API.
// Accepts one or more base URLs (comma-separated or variadic).
// Requests are distributed across addresses using round-robin.
func NewPythonClient(addresses ...string) *PythonClient {
	addrs := normalizeURLs(addresses)
	if len(addrs) == 0 {
		addrs = []string{"http://localhost:8000"}
	}

	// Configure transport with sensible timeouts to prevent resource leaks.
	// 拨号统一 3s 快速失败：引擎宕机/网络不可达时不再依赖系统级 TCP 超时（可达数十秒）。
	dialer := &net.Dialer{Timeout: pythonDialTimeout}
	newTransport := func(withStreamHeaderTimeout bool) *http.Transport {
		tr := &http.Transport{
			DialContext:         dialer.DialContext,
			MaxIdleConns:        100,
			MaxIdleConnsPerHost: 10,
			IdleConnTimeout:     90 * time.Second,
			TLSHandshakeTimeout: 10 * time.Second,
		}
		if withStreamHeaderTimeout {
			// 仅流式（SSE）端点启用：引擎 StreamingResponse 秒级发头，假死/容器挂起时
			// 在此超时快速失败。同步端点（如 /v1/chat/submit 完整编排后才返回 JSON）
			// 不得加此限制，否则会误杀正常长任务。
			tr.ResponseHeaderTimeout = pythonResponseHeaderTimeout
		}
		return tr
	}
	syncClient := &http.Client{
		Timeout:   60 * time.Second, // Overall timeout protection
		Transport: newTransport(false),
	}
	streamClient := &http.Client{
		// 不设 Timeout：http.Client.Timeout 覆盖整个请求（含读 body），会把长回答硬截断在 60s，
		// 与"仅限制等待响应头"的意图不符。等响应头由 Transport.ResponseHeaderTimeout
		// （pythonResponseHeaderTimeout）约束，整体时长由调用方 ctx（submit 路径 180s）控制。
		Transport: newTransport(true),
	}

	c := &PythonClient{
		client:       syncClient,
		streamClient: streamClient,
		staticAddrs:  append([]string(nil), addrs...),
		addrs:        make([]pyAddrEntry, len(addrs)),
	}
	for i, a := range addrs {
		c.addrs[i] = pyAddrEntry{url: a}
	}
	return c
}

// normalizeURLs 清洗地址列表：去空白、补 http:// 前缀、去重保序。
func normalizeURLs(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, raw := range in {
		a := strings.TrimSpace(raw)
		if a == "" {
			continue
		}
		if !strings.HasPrefix(a, "http://") && !strings.HasPrefix(a, "https://") {
			a = "http://" + a
		}
		if _, dup := seen[a]; dup {
			continue
		}
		seen[a] = struct{}{}
		out = append(out, a)
	}
	return out
}

// snapshot 返回地址表当前快照（锁内复制）。
func (c *PythonClient) snapshot() []pyAddrEntry {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]pyAddrEntry, len(c.addrs))
	copy(out, c.addrs)
	return out
}

// StaticAddresses 返回构造时的静态地址列表（发现回退用）。
func (c *PythonClient) StaticAddresses() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return append([]string(nil), c.staticAddrs...)
}

// SetAddresses 全量替换引擎地址表（StartEngineDiscovery 调用）。
// 保留仍存在地址的冷却状态；地址顺序即优先级顺序。返回是否发生变化。
func (c *PythonClient) SetAddresses(urls []string) bool {
	list := normalizeURLs(urls)
	c.mu.Lock()
	defer c.mu.Unlock()

	// 与当前表比较
	if len(list) == len(c.addrs) {
		same := true
		for i := range list {
			if c.addrs[i].url != list[i] {
				same = false
				break
			}
		}
		if same {
			return false
		}
	}

	old := make(map[string]int64, len(c.addrs))
	for _, e := range c.addrs {
		old[e.url] = e.cooldownUntil
	}
	next := make([]pyAddrEntry, len(list))
	for i, u := range list {
		next[i] = pyAddrEntry{url: u, cooldownUntil: old[u]}
	}
	c.addrs = next
	return true
}

// HealthCheck 对 Python 引擎执行健康检查（GET /healthz），
// 返回状态和地址级信息。超时 5 秒，不触发重试逻辑。
// 探测路径为 /healthz：Python 引擎（FastAPI）注册的是 GET /healthz，无 /health。
func (c *PythonClient) HealthCheck(ctx context.Context) map[string]interface{} {
	result := make(map[string]interface{})
	allUp := true
	type addrStatus struct {
		Status    string `json:"status"`
		LatencyMs int64  `json:"latency_ms,omitempty"`
		Error     string `json:"error,omitempty"`
	}
	entries := c.snapshot()
	addrs := make(map[string]addrStatus, len(entries))

	for _, e := range entries {
		addr := e.url
		status := addrStatus{Status: "down"}
		start := time.Now()
		req, err := http.NewRequestWithContext(ctx, "GET", addr+"/healthz", nil)
		if err != nil {
			status.Error = err.Error()
			allUp = false
			addrs[addr] = status
			continue
		}
		c.injectInternalToken(req)
		resp, err := c.client.Do(req)
		latency := time.Since(start).Milliseconds()
		status.LatencyMs = latency
		if err != nil {
			status.Error = err.Error()
			allUp = false
			c.markFailure(addr)
		} else {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				status.Status = "up"
				c.markSuccess(addr)
			} else {
				status.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
				allUp = false
				c.markFailure(addr)
			}
		}
		addrs[addr] = status
	}

	result["addresses"] = addrs
	result["healthy"] = allUp
	result["total_addresses"] = len(entries)
	if len(entries) == 0 {
		result["healthy"] = false
		result["error"] = "no addresses configured"
	}
	return result
}

// SetInternalToken 配置 Go↔Python 共享内部 token。
// 转发到 Python 的请求会自动注入 X-Internal-Token header，
// Python 侧据此校验 ?tenant_id= 透传身份的合法性（P0-3 防伪造）。
func (c *PythonClient) SetInternalToken(token string) {
	c.internalToken = token
}

// injectInternalToken 把 X-Internal-Token header 注入到出站请求。
// 未配置 token 时为 no-op（部署侧未启用内部互信时降级，但 Python 侧会
// fail-close 拒绝 query 透传身份，强制走 JWT/API Key 鉴权）。
func (c *PythonClient) injectInternalToken(req *http.Request) {
	if c.internalToken != "" {
		req.Header.Set("X-Internal-Token", c.internalToken)
	}
}

// pythonCooldown 单个地址失败后的冷却时长：暂时跳过，避免每 N 个请求必败一个
const pythonCooldown = 5 * time.Second

// pythonDialTimeout 拨号（TCP 建连）超时：Python 引擎宕机/网络不可达时快速失败，
// 避免依赖系统级 TCP 超时（可达数十秒）。作用于所有请求。
const pythonDialTimeout = 3 * time.Second

// pythonResponseHeaderTimeout 发送完请求体后等待响应头的超时，仅对流式（SSE）端点生效。
// Python 引擎的 /v1/agent/submit 等 SSE 端点正常时响应头秒级返回（StreamingResponse
// 先发头再流式），仅在引擎假死/容器挂起（docker-proxy 收连接但不转发）时才耗尽此超时。
// 修复前该类故障只能等 http.Client 60s 总超时且在同一地址重试 4 次，
// 前端"思考中"最长空等数分钟才收到 "Service temporarily unavailable"。
const pythonResponseHeaderTimeout = 10 * time.Second

// markFailure 记录地址失败，进入冷却
func (c *PythonClient) markFailure(addr string) {
	until := time.Now().Add(pythonCooldown).Unix()
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.addrs {
		if c.addrs[i].url == addr {
			c.addrs[i].cooldownUntil = until
			return
		}
	}
}

// markSuccess 清除地址冷却
func (c *PythonClient) markSuccess(addr string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.addrs {
		if c.addrs[i].url == addr {
			c.addrs[i].cooldownUntil = 0
			return
		}
	}
}

// do 统一请求出口：记录成功/失败并更新熔断状态，支持重试
// do 统一请求出口（同步 JSON/管理端点）：走 client（允许慢响应头）。
func (c *PythonClient) do(req *http.Request) (*http.Response, error) {
	return c.doWith(c.client, req)
}

// doStream 流式（SSE）端点请求出口：走 streamClient（短响应头超时），
// 引擎假死/容器挂起时快速失败，避免前端"思考中"空等数十秒。
func (c *PythonClient) doStream(req *http.Request) (*http.Response, error) {
	return c.doWith(c.streamClient, req)
}

// doWith 记录成功/失败并更新熔断状态，支持重试。
// httpClient 由上层 do/doStream 按端点语义选择。
func (c *PythonClient) doWith(httpClient *http.Client, req *http.Request) (*http.Response, error) {
	// 注入trace context到HTTP头
	propagator := propagation.TraceContext{}
	propagator.Inject(req.Context(), propagation.HeaderCarrier(req.Header))

	addr := req.URL.Scheme + "://" + req.URL.Host
	var lastErr error

	// 缓存请求体，用于重试时重新创建 body
	var bodyBuf []byte
	if req.Body != nil {
		var err error
		bodyBuf, err = io.ReadAll(req.Body)
		req.Body.Close()
		if err != nil {
			return nil, fmt.Errorf("read request body: %w", err)
		}
	}

	for attempt := 0; attempt <= defaultRetryConfig.MaxRetries; attempt++ {
		// 检查上下文取消
		if err := req.Context().Err(); err != nil {
			return nil, fmt.Errorf("request cancelled before attempt %d: %w", attempt+1, err)
		}

		if attempt > 0 {
			// 指数退避
			backoff := time.Duration(
				float64(defaultRetryConfig.InitialBackoff) *
					math.Pow(defaultRetryConfig.Multiplier, float64(attempt-1)),
			)
			if backoff > defaultRetryConfig.MaxBackoff {
				backoff = defaultRetryConfig.MaxBackoff
			}

			slog.Info("retrying request",
				"addr", addr,
				"attempt", attempt+1,
				"backoff", backoff,
				"last_error", lastErr)

			select {
			case <-req.Context().Done():
				return nil, req.Context().Err()
			case <-time.After(backoff):
			}
		}

		// 重试时需要重新创建 body（bytes.Reader 被消耗后不可重复读）
		if bodyBuf != nil {
			req.Body = io.NopCloser(bytes.NewReader(bodyBuf))
		}

		resp, err := httpClient.Do(req)
		if err == nil {
			// 检查是否是可重试的错误状态码
			if resp.StatusCode >= 500 || resp.StatusCode == 429 {
				resp.Body.Close()
				lastErr = fmt.Errorf("server error %d", resp.StatusCode)
				c.markFailure(addr)
				continue
			}
			c.markSuccess(addr)
			return resp, nil
		}

		// 网络层错误（连接拒绝/拨号超时/响应头超时/连接中断/上下文取消）：说明目标此刻
		// 不可达或收包不响应，在同一地址上指数退避重试只会把"服务不可用"的等待放大
		// 数十秒（曾致前端"思考中"空等 50s+ 才收到错误提示）。此类故障交给熔断冷却
		// （markFailure + pickAddress 跳过该地址）兜底：后续请求自动避开/冷却结束后再试，
		// 当前请求立即失败，让上层（HandleSubmit）第一时间通知用户。
		c.markFailure(addr)
		return nil, fmt.Errorf("call python engine: %w", err)
	}

	return nil, fmt.Errorf("request failed after %d retries: %w",
		defaultRetryConfig.MaxRetries+1, lastErr)
}

// pickAddress returns the next address using round-robin.
func (c *PythonClient) pickAddress() string {
	entries := c.snapshot()
	if len(entries) == 0 {
		return "http://localhost:8000"
	}
	now := time.Now().Unix()
	start := int(atomic.AddUint64(&c.counter, 1)) % len(entries)
	for i := 0; i < len(entries); i++ {
		idx := (start + i) % len(entries)
		if entries[idx].cooldownUntil <= now {
			return entries[idx].url
		}
	}
	// 全部地址冷却中：选择最快冷却完成的地址，避免选到不可用地址
	earliestIdx := 0
	for i := 1; i < len(entries); i++ {
		if entries[i].cooldownUntil < entries[earliestIdx].cooldownUntil {
			earliestIdx = i
		}
	}
	return entries[earliestIdx].url
}

// addressFor 选择本次请求的引擎地址。
// ctx 携带 session（WithSession）→ 一致性哈希固定实例（确定性、跨网关一致）；
// 无 session → round-robin（pickAddress）。目标实例冷却中顺序探测下一健康实例
// （故障漂移，记录日志）；全部冷却回退最快冷却完成地址。
func (c *PythonClient) addressFor(ctx context.Context) string {
	entries := c.snapshot()
	if len(entries) == 0 {
		return c.pickAddress()
	}
	key := sessionFromCtx(ctx)
	if key == "" {
		return c.pickAddress()
	}
	// 归属优先（批次 4），**仅对显式要求归属的请求**（审批/取消，见 WithRunAffinity）：
	// 引擎把 session 的 run owner 写进 Redis（engine:run:{session}，TTL 300s），
	// 命中且该实例在当前地址表中健康时直连——一致性哈希在扩缩容/副本上下线后会漂移，
	// 会把审批与取消送到并不持有该 run 的实例。
	// 普通对话请求不查 Redis（A1）：归属查询在请求路径上，不能让 Redis 往返成为
	// 每个请求的固定成本；查询失败一律走下方哈希回退。
	if runAffinityFromCtx(ctx) {
		if url, rec, ok := RunOwnerURL(ctx, key); ok {
			if c.isKnownHealthy(url) {
				return url
			}
			slog.Info("python engine: run owner instance unavailable, falling back to hash",
				"session", key[:min(len(key), 16)],
				"owner_instance", rec.InstanceID, "owner_url", url)
		}
	}
	now := time.Now().Unix()
	h := fnv.New32a()
	_, _ = h.Write([]byte(key))
	start := int(h.Sum32()) % len(entries)
	for i := 0; i < len(entries); i++ {
		idx := (start + i) % len(entries)
		if entries[idx].cooldownUntil <= now {
			if i > 0 {
				slog.Info("python engine: session affinity drifted",
					"session", key[:min(len(key), 16)], "from", entries[start].url, "to", entries[idx].url)
			}
			return entries[idx].url
		}
	}
	// 全部冷却中：取最快冷却完成的地址
	earliestIdx := 0
	for i := 1; i < len(entries); i++ {
		if entries[i].cooldownUntil < entries[earliestIdx].cooldownUntil {
			earliestIdx = i
		}
	}
	return entries[earliestIdx].url
}

// sortStrings 排序去重辅助（discovery 用）。
func sortUnique(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	sort.Strings(in)
	out := in[:1]
	for _, s := range in[1:] {
		if s != out[len(out)-1] {
			out = append(out, s)
		}
	}
	return out
}

// PythonRunRequest matches the Python engine's Pydantic RunRequest model.
type PythonRunRequest struct {
	SessionID    string           `json:"session_id"`
	UserID       string           `json:"user_id"`
	Content      string           `json:"content"`
	SystemPrompt string           `json:"system_prompt"`
	History      []PythonMessage  `json:"history"`
	Tools        []PythonToolDef  `json:"tools"`
	LLMConfig    *PythonLLMConfig `json:"llm_config,omitempty"`
	MaxTurns     int              `json:"max_turns"`
}

// PythonMessage is a message in the conversation history.
type PythonMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// PythonToolDef describes a tool available to the agent.
type PythonToolDef struct {
	Name           string `json:"name"`
	Description    string `json:"description"`
	ParametersJSON string `json:"parameters_json"`
}

// PythonLLMConfig configures the LLM for this inference call.
type PythonLLMConfig struct {
	Model       string  `json:"model"`
	MaxTokens   int     `json:"max_tokens"`
	Temperature float64 `json:"temperature"`
}

// PythonEvent is a single SSE event from the Python engine.
type PythonEvent struct {
	Type         string `json:"type"`
	Content      string `json:"content,omitempty"`
	ID           string `json:"id,omitempty"`
	Name         string `json:"name,omitempty"`
	Arguments    string `json:"arguments,omitempty"`
	InputTokens  int    `json:"input_tokens,omitempty"`
	OutputTokens int    `json:"output_tokens,omitempty"`
	// CachedTokens：命中提示词缓存的输入 token（会话统计里「缓存命中率」的分子）。
	// Model：本回合实际使用的模型名 —— 按轮记录模型（turns.model）用。
	// 两者都由引擎随 done 事件回传；老引擎不传时是零值，统计少算但不会出错。
	CachedTokens int    `json:"cached_tokens,omitempty"`
	Model        string `json:"model,omitempty"`
	Message      string `json:"message,omitempty"`

	// ── 子 Agent 进度事件（type 前缀 "subagent."，见 docs/subagent-design.md §4.2）──
	// 这些字段让前端能按 run_id 建树、并按 depth/parent_run_id 还原层级；
	// 主对话事件不带这些字段（omitempty 保证不污染既有帧）。
	RunID       string         `json:"run_id,omitempty"`
	ParentRunID string         `json:"parent_run_id,omitempty"`
	Depth       int            `json:"depth,omitempty"`
	Profile     string         `json:"profile,omitempty"`
	Status      string         `json:"status,omitempty"`
	Truncated   bool           `json:"truncated,omitempty"`
	Usage       map[string]any `json:"usage,omitempty"`
}

// Run starts a streaming inference call to the Python engine.
func (c *PythonClient) Run(ctx context.Context, req PythonRunRequest) (<-chan PythonEvent, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	routeCtx := ctx
	if req.SessionID != "" {
		routeCtx = WithSession(ctx, req.SessionID)
	}
	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.addressFor(routeCtx)+"/v1/agent/run", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")
	c.injectInternalToken(httpReq)

	resp, err := c.doStream(httpReq)
	if err != nil {
		return nil, fmt.Errorf("call python engine: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("python engine returned status %d", resp.StatusCode)
	}

	events := make(chan PythonEvent, 64)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("python sse read panic", "panic", r)
			}
		}()
		defer close(events)
		defer resp.Body.Close()

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 1024*1024), 16*1024*1024)

		// Use a ticker to periodically check context cancellation
		// This prevents the goroutine from blocking forever on scanner.Scan()
		// if the context is cancelled but no data is being sent.
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		lineCh := make(chan string, 1)

		// Background goroutine to read lines
		// S 修复：sender 在发送前检查 ctx.Done，避免外层退出后阻塞发送而泄漏 goroutine。
		go func() {
			defer close(lineCh)
			for scanner.Scan() {
				select {
				case lineCh <- scanner.Text():
				case <-ctx.Done():
					return
				}
			}
		}()

		for {
			select {
			case <-ctx.Done():
				return
			case line, ok := <-lineCh:
				if !ok {
					// Scanner finished (EOF or error)
					if err := scanner.Err(); err != nil && err != io.EOF {
						slog.Warn("python engine: read stream", "error", err)
					}
					return
				}
				if !strings.HasPrefix(line, "data: ") {
					continue
				}
				data := strings.TrimPrefix(line, "data: ")
				var event PythonEvent
				if err := json.Unmarshal([]byte(data), &event); err != nil {
					slog.Warn("python engine: unmarshal event", "error", err, "data", data)
					continue
				}
				select {
				case events <- event:
				case <-ctx.Done():
					return
				}
			case <-ticker.C:
				// Periodic check, if context is done, we'll catch it in the first case
			}
		}
	}()

	return events, nil
}

// IsConnected checks if any Python engine instance is reachable.
// 注意必须用 GET：Python 引擎（FastAPI）只为 /healthz 注册了 GET 方法，
// HEAD 会返回 405，曾导致引擎存活却被判为不可达（管理端 API Key 保存
// 因此恒返回 400 "python engine not available"）。
func (c *PythonClient) IsConnected() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.pickAddress()+"/healthz", nil)
	if err != nil {
		return false
	}
	resp, err := c.do(req)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// GetJSON performs a GET request and decodes JSON into out.
func (c *PythonClient) GetJSON(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.pickAddress()+path, nil)
	if err != nil {
		return err
	}
	c.injectInternalToken(req)
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("python GET %s returned %d: %s", path, resp.StatusCode, string(b))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// PostJSON performs a POST request with JSON body and decodes JSON into out.
func (c *PythonClient) PostJSON(ctx context.Context, path string, in any, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.addressFor(ctx)+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	c.injectInternalToken(req)
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("python POST %s returned %d: %s", path, resp.StatusCode, string(b))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// PutJSON performs a PUT request with JSON body and decodes JSON into out.
func (c *PythonClient) PutJSON(ctx context.Context, path string, in any, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, c.pickAddress()+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	c.injectInternalToken(req)
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("python PUT %s returned %d: %s", path, resp.StatusCode, string(b))
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// ForwardRequest forwards an incoming HTTP request to the Python engine,
// preserving method, headers, and body. The response status and body are
// written directly to w.
func (c *PythonClient) ForwardRequest(w http.ResponseWriter, r *http.Request, path string) {
	req, err := http.NewRequestWithContext(r.Context(), r.Method, c.pickAddress()+path, r.Body)
	if err != nil {
		slog.Error("create forward request", "path", path, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	// 安全：仅转发必要的客户端头，排除认证/会话/身份相关头
	// 防止客户端通过伪造 Authorization/Cookie/X-API-Key 绕过网关认证链路，
	// 也防止伪造 X-User-ID/X-Tenant-ID/X-Internal-Token 冒用他人/他租户身份（P0）。
	skipHeaders := map[string]bool{
		"Authorization":       true,
		"Proxy-Authorization": true,
		"Cookie":              true,
		"Set-Cookie":          true,
		"X-Api-Key":           true,
		"X-Auth-Token":        true,
		"X-User-Id":           true,
		"X-User-Role":         true,
		"X-Tenant-Id":         true,
		"X-Internal-Token":    true,
	}
	for k, vv := range r.Header {
		if skipHeaders[k] {
			continue
		}
		for _, v := range vv {
			req.Header.Add(k, v)
		}
	}
	// 注入 Go↔Python 内部 token + 从已验证的 JWT claims 可信注入身份头
	// （Python 引擎信任这些头，故必须由网关覆盖，禁止客户端直传）。
	c.injectInternalToken(req)
	if claims := auth.GetClaims(r.Context()); claims != nil {
		if claims.UserID != "" {
			req.Header.Set("X-User-Id", claims.UserID)
		}
		if claims.TenantID != "" {
			req.Header.Set("X-Tenant-Id", claims.TenantID)
		}
	}
	resp, err := c.do(req)
	if err != nil {
		slog.Error("forward to python engine", "path", path, "error", err)
		http.Error(w, "internal server error", http.StatusInternalServerError)
		return
	}
	defer resp.Body.Close()
	// 安全：过滤响应中的 Set-Cookie，防止客户端 Cookie 被意外设置
	resp.Header.Del("Set-Cookie")
	for k, vv := range resp.Header {
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// DeleteJSON performs a DELETE request and decodes JSON into out.
func (c *PythonClient) DeleteJSON(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.pickAddress()+path, nil)
	if err != nil {
		return err
	}
	c.injectInternalToken(req)
	resp, err := c.do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("python DELETE %s returned %d: %s", path, resp.StatusCode, string(b))
	}
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// RunSSE starts a streaming SSE call to any Python endpoint and returns events.
func (c *PythonClient) RunSSE(ctx context.Context, path string, body any, extraHeaders ...map[string]string) (<-chan PythonEvent, error) {
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, "POST", c.addressFor(ctx)+path, bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "text/event-stream")
	c.injectInternalToken(httpReq)
	// 可选附加 header（如网关注入的用户身份 X-User-ID，供 Python 引擎信任）
	for _, h := range extraHeaders {
		for k, v := range h {
			httpReq.Header.Set(k, v)
		}
	}

	// SSE 流式：streamClient 带短响应头超时（引擎假死时快速失败）
	resp, err := c.doStream(httpReq)
	if err != nil {
		return nil, fmt.Errorf("call python engine: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("python engine returned status %d", resp.StatusCode)
	}

	events := make(chan PythonEvent, 64)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("python sse read panic", "panic", r)
			}
		}()
		defer close(events)
		defer resp.Body.Close()

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 1024*1024), 16*1024*1024)

		// Use a ticker to periodically check context cancellation
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		lineCh := make(chan string, 1)

		// Background goroutine to read lines
		// S 修复：sender 在发送前检查 ctx.Done，避免外层退出后阻塞发送而泄漏 goroutine。
		go func() {
			defer close(lineCh)
			for scanner.Scan() {
				select {
				case lineCh <- scanner.Text():
				case <-ctx.Done():
					return
				}
			}
		}()

		for {
			select {
			case <-ctx.Done():
				return
			case line, ok := <-lineCh:
				if !ok {
					if err := scanner.Err(); err != nil && err != io.EOF {
						slog.Warn("python engine: read stream", "error", err)
					}
					return
				}
				if !strings.HasPrefix(line, "data: ") {
					continue
				}
				raw := strings.TrimPrefix(line, "data: ")
				var event PythonEvent
				if err := json.Unmarshal([]byte(raw), &event); err != nil {
					slog.Warn("python engine: unmarshal event", "error", err, "data", raw)
					continue
				}
				select {
				case events <- event:
				case <-ctx.Done():
					return
				}
			case <-ticker.C:
				// Periodic check
			}
		}
	}()

	return events, nil
}
