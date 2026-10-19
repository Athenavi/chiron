package monitor

import (
	"log/slog"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// ── Duration Histogram ──

// Histogram tracks duration percentiles (p50/p95/p99) for operations.
// Uses a fixed-size ring buffer to avoid slice reallocations on every Record call.
type Histogram struct {
	mu    sync.Mutex
	name  string
	data  []time.Duration
	pos   int // next write position
	count int // number of samples recorded (up to max)
	max   int // capacity
}

func NewHistogram(name string, maxSamples int) *Histogram {
	return &Histogram{
		name: name,
		data: make([]time.Duration, maxSamples),
		max:  maxSamples,
	}
}

// Record adds a duration to the histogram. O(1) with ring buffer.
func (h *Histogram) Record(d time.Duration) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.data[h.pos] = d
	h.pos = (h.pos + 1) % h.max
	if h.count < h.max {
		h.count++
	}
}

// Snapshot returns current histogram statistics. Copies data out for sorting.
func (h *Histogram) Snapshot() map[string]interface{} {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.count == 0 {
		return map[string]interface{}{"name": h.name, "count": 0}
	}
	// Copy valid samples for sorting
	sorted := make([]time.Duration, h.count)
	// Data is written in a ring; the valid window is from pos-count to pos (mod max)
	start := h.pos - h.count
	if start < 0 {
		start += h.max
	}
	for i := 0; i < h.count; i++ {
		sorted[i] = h.data[(start+i)%h.max]
	}
	sort.Slice(sorted, func(i, j int) bool { return sorted[i] < sorted[j] })
	return map[string]interface{}{
		"name":  h.name,
		"count": len(sorted),
		"p50":   sorted[len(sorted)*50/100].Milliseconds(),
		"p95":   sorted[len(sorted)*95/100].Milliseconds(),
		"p99":   sorted[len(sorted)*99/100].Milliseconds(),
		"max":   sorted[len(sorted)-1].Milliseconds(),
	}
}

var (
	LLMHistogram     = NewHistogram("llm", 1000)
	ToolHistogram    = NewHistogram("tool", 1000)
	RequestHistogram = NewHistogram("request", 1000)
)

// Metrics holds simple counters for monitoring.
type Metrics struct {
	RequestsTotal      atomic.Int64
	RequestsActive     atomic.Int64
	LLMCallsTotal      atomic.Int64
	LLMErrorsTotal     atomic.Int64
	ToolCallsTotal     atomic.Int64
	ToolErrorsTotal    atomic.Int64
	RateLimitBlocked   atomic.Int64
	RateLimitErrors    atomic.Int64
	QuotaExceeded      atomic.Int64
	AuditLogWrites     atomic.Int64
	AuditLogErrors     atomic.Int64
	WebSocketConns     atomic.Int64
	PaymentAttempts    atomic.Int64
	PaymentSuccesses   atomic.Int64
	PaymentFailures    atomic.Int64
	SSOLoginAttempts   atomic.Int64
	SSOLoginSuccesses  atomic.Int64
	SSOLoginFailures   atomic.Int64
	// M19：按-run 用量对账（崩溃/接管段的补偿）。`Reconciled` = 真正补了账的次数，
	// `Shadowed` = shadow 下**本应**补的次数（切 enforce 前看它判断规模）。
	RunUsageReconciled       atomic.Int64
	RunUsageReconcileShadowed atomic.Int64
	RunUsageReconcileFailed  atomic.Int64
	RunUsageReconcileSkipped atomic.Int64
	RunUsageReconcileTokens  atomic.Int64
	StartTime          time.Time
}

var Global = &Metrics{StartTime: time.Now()}

func IncRequests() {
	Global.RequestsTotal.Add(1)
	Global.RequestsActive.Add(1)
}

func DecRequests() {
	Global.RequestsActive.Add(-1)
}

func IncLLMCall() {
	Global.LLMCallsTotal.Add(1)
}

func RecordLLMDuration(d time.Duration) {
	LLMHistogram.Record(d)
}

func IncLLMError() {
	Global.LLMErrorsTotal.Add(1)
}

func IncToolCall() {
	Global.ToolCallsTotal.Add(1)
}

func RecordToolDuration(d time.Duration) {
	ToolHistogram.Record(d)
}

func IncToolError() {
	Global.ToolErrorsTotal.Add(1)
}

func IncRateLimitBlocked() {
	Global.RateLimitBlocked.Add(1)
}

func IncRateLimitError() {
	Global.RateLimitErrors.Add(1)
}

func IncQuotaExceeded() {
	Global.QuotaExceeded.Add(1)
}

func IncAuditLogWrite() {
	Global.AuditLogWrites.Add(1)
}

func IncAuditLogError() {
	Global.AuditLogErrors.Add(1)
}

func IncWebSocketConn() {
	Global.WebSocketConns.Add(1)
}

func DecWebSocketConn() {
	Global.WebSocketConns.Add(-1)
}

func IncPaymentAttempt() {
	Global.PaymentAttempts.Add(1)
}

func IncPaymentSuccess() {
	Global.PaymentSuccesses.Add(1)
}

func IncPaymentFailure() {
	Global.PaymentFailures.Add(1)
}

func IncSSOLoginAttempt() {
	Global.SSOLoginAttempts.Add(1)
}

func IncSSOLoginSuccess() {
	Global.SSOLoginSuccesses.Add(1)
}

func IncSSOLoginFailure() {
	Global.SSOLoginFailures.Add(1)
}

// M19：按-run 用量对账。`tokens` 是被补偿的 input+output 总量（用于判断规模与方向）。
func IncRunUsageReconciled(tokens int64) {
	Global.RunUsageReconciled.Add(1)
	if tokens > 0 {
		Global.RunUsageReconcileTokens.Add(tokens)
	}
}

func IncRunUsageReconcileShadowed(tokens int64) {
	Global.RunUsageReconcileShadowed.Add(1)
	if tokens > 0 {
		Global.RunUsageReconcileTokens.Add(tokens)
	}
}

func IncRunUsageReconcileFailed() {
	Global.RunUsageReconcileFailed.Add(1)
}

func IncRunUsageReconcileSkipped() {
	Global.RunUsageReconcileSkipped.Add(1)
}

var extraStatsMu sync.RWMutex
var extraStatsFuncs []func() map[string]interface{}

// RegisterExtraStats 允许外部包注册额外的统计信息提供者（如 DB 连接池、缓存等）。
func RegisterExtraStats(fn func() map[string]interface{}) {
	extraStatsMu.Lock()
	defer extraStatsMu.Unlock()
	extraStatsFuncs = append(extraStatsFuncs, fn)
}

func Snapshot() map[string]interface{} {
	snap := map[string]interface{}{
		"requests_total":      Global.RequestsTotal.Load(),
		"requests_active":     Global.RequestsActive.Load(),
		"llm_calls":           Global.LLMCallsTotal.Load(),
		"llm_errors":          Global.LLMErrorsTotal.Load(),
		"tool_calls":          Global.ToolCallsTotal.Load(),
		"tool_errors":         Global.ToolErrorsTotal.Load(),
		"rate_limit_blocked":  Global.RateLimitBlocked.Load(),
		"rate_limit_errors":   Global.RateLimitErrors.Load(),
		"quota_exceeded":      Global.QuotaExceeded.Load(),
		"audit_log_writes":    Global.AuditLogWrites.Load(),
		"audit_log_errors":    Global.AuditLogErrors.Load(),
		"websocket_conns":     Global.WebSocketConns.Load(),
		"payment_attempts":    Global.PaymentAttempts.Load(),
		"payment_successes":   Global.PaymentSuccesses.Load(),
		"payment_failures":    Global.PaymentFailures.Load(),
		"sso_login_attempts":  Global.SSOLoginAttempts.Load(),
		"sso_login_successes": Global.SSOLoginSuccesses.Load(),
		"sso_login_failures":  Global.SSOLoginFailures.Load(),
		"run_usage_reconciled":        Global.RunUsageReconciled.Load(),
		"run_usage_reconcile_shadowed": Global.RunUsageReconcileShadowed.Load(),
		"run_usage_reconcile_failed":  Global.RunUsageReconcileFailed.Load(),
		"run_usage_reconcile_skipped": Global.RunUsageReconcileSkipped.Load(),
		"run_usage_reconcile_tokens":  Global.RunUsageReconcileTokens.Load(),
		"uptime_seconds":      time.Since(Global.StartTime).Seconds(),
		"started_at":          Global.StartTime.Format(time.RFC3339),
	}
	
	// Add Go runtime metrics
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	snap["go_goroutines"] = runtime.NumGoroutine()
	snap["go_memory_alloc_bytes"] = m.Alloc
	snap["go_memory_sys_bytes"] = m.Sys
	snap["go_gc_runs"] = m.NumGC
	
	extraStatsMu.RLock()
	for _, fn := range extraStatsFuncs {
		if m := fn(); m != nil {
			for k, v := range m {
				snap[k] = v
			}
		}
	}
	extraStatsMu.RUnlock()
	return snap
}

func Init() {
	InitWithSpanStore()
	slog.Info("monitor initialized", "started_at", Global.StartTime.Format(time.RFC3339))
}

// ── Per-session cost tracking (in-memory) ──

var (
	costMu           sync.Mutex
	costBySession    = make(map[string]*SessionCostWithTTL)
	maxSessionCosts  = 10000
	sessionCostRing  = make([]string, 10000) // ring buffer for FIFO eviction
	sessionCostHead  = 0                     // next eviction position
	sessionCostCount = 0                     // number of entries in ring
	sessionCostTTL   = 30 * time.Minute      // TTL for session cost entries
)

// SessionCostWithTTL wraps SessionCost with an expiry time.
type SessionCostWithTTL struct {
	SessionCost
	ExpiresAt time.Time
}

// SessionCost tracks token usage for a session.
type SessionCost struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	TotalCalls   int `json:"total_calls"`
}

// RecordSessionUsage records LLM token usage for a session.
func RecordSessionUsage(sessionID string, inputTokens, outputTokens int) {
	costMu.Lock()
	defer costMu.Unlock()
	s, ok := costBySession[sessionID]
	if !ok || time.Now().After(s.ExpiresAt) {
		// Evict oldest if at capacity
		if len(costBySession) >= maxSessionCosts {
			oldest := sessionCostRing[sessionCostHead]
			delete(costBySession, oldest)
			sessionCostHead = (sessionCostHead + 1) % maxSessionCosts
			sessionCostCount--
		}
		s = &SessionCostWithTTL{
			SessionCost: SessionCost{},
			ExpiresAt:   time.Now().Add(sessionCostTTL),
		}
		costBySession[sessionID] = s
		// Add to ring buffer at tail
		tail := (sessionCostHead + sessionCostCount) % maxSessionCosts
		sessionCostRing[tail] = sessionID
		sessionCostCount++
	} else {
		// Refresh TTL on active session
		s.ExpiresAt = time.Now().Add(sessionCostTTL)
	}
	s.InputTokens += inputTokens
	s.OutputTokens += outputTokens
	s.TotalCalls++
}

// GetSessionCost returns a snapshot of the cost info for a session.
func GetSessionCost(sessionID string) SessionCost {
	costMu.Lock()
	defer costMu.Unlock()
	if s, ok := costBySession[sessionID]; ok && !time.Now().After(s.ExpiresAt) {
		return s.SessionCost
	}
	return SessionCost{}
}

// AllSessionCosts returns snapshots of all non-expired tracked session costs.
func AllSessionCosts() map[string]SessionCost {
	costMu.Lock()
	defer costMu.Unlock()
	result := make(map[string]SessionCost, len(costBySession))
	now := time.Now()
	for k, v := range costBySession {
		if now.After(v.ExpiresAt) {
			delete(costBySession, k)
			continue
		}
		result[k] = v.SessionCost
	}
	return result
}

// SnapshotSessionCosts returns a JSON-safe summary of all session costs.
func SnapshotSessionCosts() []map[string]interface{} {
	all := AllSessionCosts()
	result := make([]map[string]interface{}, 0, len(all))
	for sid, cost := range all {
		result = append(result, map[string]interface{}{
			"session_id":    sid,
			"input_tokens":  cost.InputTokens,
			"output_tokens": cost.OutputTokens,
			"total_calls":   cost.TotalCalls,
		})
	}
	return result
}
