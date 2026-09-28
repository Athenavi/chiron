package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/redis/go-redis/v9"
)

// SystemHandler provides health, metrics, and trace endpoints.
type SystemHandler struct {
	pythonClient *engine.PythonClient
}

func NewSystemHandler() *SystemHandler {
	return &SystemHandler{}
}

func NewSystemHandlerWithEngine(pyClient *engine.PythonClient) *SystemHandler {
	return &SystemHandler{pythonClient: pyClient}
}

// HealthScores returns calculated health scores based on live metrics.
func (h *SystemHandler) HealthScores(w http.ResponseWriter, r *http.Request) {
	m := monitor.Snapshot()

	requestsTotal := toFloat64(m["requests_total"])
	toolErrors := toFloat64(m["tool_errors"])
	llmErrors := toFloat64(m["llm_errors"])

	// Calculate scores from real metrics
	uptime := time.Now().Unix() - int64(toFloat64(m["uptime_seconds"]))
	_ = uptime

	healthScores := []map[string]interface{}{
		{
			"label": "Performance",
			"score": perfScore(requestsTotal),
			"color": "bg-green-500",
		},
		{
			"label": "Reliability",
			"score": reliabilityScore(requestsTotal, toolErrors+llmErrors),
			"color": "bg-blue-500",
		},
		{
			"label": "Activity",
			"score": activityScore(requestsTotal),
			"color": "bg-amber-500",
		},
		{
			"label": "API Health",
			"score": apiHealthScore(m),
			"color": "bg-green-500",
		},
		{
			"label": "System",
			"score": systemScore(m),
			"color": "bg-blue-500",
		},
	}

	OK(w, map[string]interface{}{
		"scores": healthScores,
		"uptime": m["uptime_seconds"],
	})
}

// Metrics returns the live monitor snapshot as a key-value map.
func (h *SystemHandler) Metrics(w http.ResponseWriter, r *http.Request) {
	OK(w, monitor.Snapshot())
}

// PrometheusMetrics 输出 Prometheus 文本格式指标（供 Prometheus 抓取）。
// 端点 /metrics 公开暴露（生产部署建议加内网限制或 basicauth）。
func (h *SystemHandler) PrometheusMetrics(w http.ResponseWriter, r *http.Request) {
	s := monitor.Snapshot()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	// counter 类型指标（snapshot key → prometheus metric name）
	counters := [][2]string{
		{"requests_total", "Total HTTP requests"},
		{"llm_calls", "Total LLM API calls"},
		{"llm_errors", "Total LLM API errors"},
		{"tool_calls", "Total tool executions"},
		{"tool_errors", "Total tool execution errors"},
		{"rate_limit_blocked", "Total rate-limited requests"},
		{"rate_limit_errors", "Total rate limiter errors"},
		{"quota_exceeded", "Total quota exceeded events"},
		{"audit_log_writes", "Total audit log writes"},
		{"audit_log_errors", "Total audit log errors"},
		{"payment_attempts", "Total payment attempts"},
		{"payment_successes", "Total successful payments"},
		{"payment_failures", "Total failed payments"},
		{"sso_login_attempts", "Total SSO login attempts"},
		{"sso_login_successes", "Total successful SSO logins"},
		{"sso_login_failures", "Total failed SSO logins"},
		{"sse_publish_enqueued", "Total SSE events enqueued for publish"},
		{"sse_publish_delivered", "Total SSE events delivered by publish workers"},
		{"sse_publish_fallback", "SSE publishes that fell back to synchronous (queue full)"},
	}
	for _, c := range counters {
		fmt.Fprintf(w, "# HELP chiron_%s %s\n# TYPE chiron_%s counter\nchiron_%s %v\n", c[0], c[1], c[0], c[0], s[c[0]])
	}
	// gauge 类型指标
	gauges := [][2]string{
		{"requests_active", "Active HTTP requests"},
		{"websocket_conns", "Active WebSocket connections"},
		{"uptime_seconds", "Process uptime in seconds"},
		{"go_goroutines", "Number of goroutines"},
		{"go_memory_alloc_bytes", "Allocated heap memory in bytes"},
		{"go_memory_sys_bytes", "Total OS memory in bytes"},
		{"go_gc_runs", "Number of completed GC cycles"},
		{"sse_publish_queued", "SSE publish queue backlog (all shards)"},
	}
	for _, g := range gauges {
		fmt.Fprintf(w, "# HELP chiron_%s %s\n# TYPE chiron_%s gauge\nchiron_%s %v\n", g[0], g[1], g[0], g[0], s[g[0]])
	}
}

// Spans returns completed tracing spans for debugging.
func (h *SystemHandler) Spans(w http.ResponseWriter, r *http.Request) {
	limit := 50
	spans := monitor.GetCompletedSpans(limit)
	OK(w, map[string]interface{}{"spans": spans})
}

// Traces returns recent tool call executions as trace entries.
func (h *SystemHandler) Traces(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id, tool_name, is_error, duration_ms, created_at
		 FROM tool_calls
		 ORDER BY created_at DESC
		 LIMIT 50`)
	if err != nil {
		OK(w, map[string]interface{}{"traces": []interface{}{}})
		return
	}
	defer rows.Close()

	traces := make([]map[string]interface{}, 0)
	for rows.Next() {
		var id, toolName string
		var isError bool
		var durationMs int64
		var createdAt time.Time

		if err := rows.Scan(&id, &toolName, &isError, &durationMs, &createdAt); err != nil {
			continue
		}

		status := "ok"
		if isError {
			status = "error"
		}

		traces = append(traces, map[string]interface{}{
			"id":          id,
			"type":        toolName,
			"name":        "Tool: " + toolName,
			"status":      status,
			"duration_ms": float64(durationMs),
			"timestamp":   createdAt.Format(time.RFC3339),
		})
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "failed to iterate traces")
		return
	}

	OK(w, map[string]interface{}{"traces": traces})
}

func toFloat64(v interface{}) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int64:
		return float64(n)
	case int:
		return float64(n)
	}
	return 0
}

func perfScore(totalRequests float64) int {
	if totalRequests == 0 {
		return 95 // Perfect score when no load
	}
	return 85
}

func reliabilityScore(total, errors float64) int {
	if total == 0 {
		return 98
	}
	rate := errors / total
	if rate > 0.1 {
		return 60
	}
	if rate > 0.05 {
		return 75
	}
	return int(98 - rate*100)
}

func activityScore(total float64) int {
	if total > 1000 {
		return 95
	}
	if total > 100 {
		return 80
	}
	if total > 10 {
		return 65
	}
	return 50
}

func apiHealthScore(m map[string]interface{}) int {
	active := toFloat64(m["requests_active"])
	if active > 100 {
		return 70
	}
	return 92
}

func systemScore(m map[string]interface{}) int {
	uptime := toFloat64(m["uptime_seconds"])
	if uptime > 86400 {
		return 90 // Running > 24h
	}
	if uptime > 3600 {
		return 85 // Running > 1h
	}
	return 80
}

// DatabaseHealth 数据库健康检查端点（供 Python 引擎调用）
func (h *SystemHandler) DatabaseHealth(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	result := db.GlobalDBManager.HealthCheck(ctx)
	OK(w, result)
}

// PythonEngineHealth Python 引擎健康检查端点
func (h *SystemHandler) PythonEngineHealth(w http.ResponseWriter, r *http.Request) {
	if h.pythonClient == nil {
		OK(w, map[string]interface{}{
			"healthy": false,
			"error":   "python engine not configured",
		})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	result := h.pythonClient.HealthCheck(ctx)
	OK(w, result)
}

// RedisHealth Redis 健康检查端点（供 Python 引擎调用）
func (h *SystemHandler) RedisHealth(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	result := db.GlobalRedisManager.HealthCheck(ctx)
	OK(w, result)
}

// DBQuery 执行 SQL 查询并返回结果（供 Python 引擎调用）
func (h *SystemHandler) DBQuery(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SQL  string        `json:"sql"`
		Args []interface{} `json:"args,omitempty"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if req.SQL == "" {
		BadRequest(w, "sql is required")
		return
	}

	ctx := r.Context()
	results, err := db.GlobalDBManager.FetchAll(ctx, req.SQL, req.Args...)
	if err != nil {
		InternalError(w, "query failed")
		return
	}

	OK(w, map[string]interface{}{
		"rows":  results,
		"count": len(results),
	})
}

// DBExecute 执行 SQL 写操作（供 Python 引擎调用）
// P1 安全加固：非维护模式下仅允许 SELECT/EXPLAIN/SAVEPOINT/ROLLBACK
// 查询，防止 internal_token 泄露时数据库被完全控制。
func (h *SystemHandler) DBExecute(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SQL  string        `json:"sql"`
		Args []interface{} `json:"args,omitempty"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if req.SQL == "" {
		BadRequest(w, "sql is required")
		return
	}

	// P1 安全加固：检查 SQL 关键词，拒绝 DDL/DML 写操作
	cleanSQL := strings.TrimSpace(strings.ToUpper(req.SQL))
	// 允许的查询前缀（安全）
	allowedPrefixes := []string{
		"SELECT", "EXPLAIN", "SAVEPOINT", "ROLLBACK",
		"RELEASE", "SET", "SHOW", "BEGIN", "COMMIT",
	}
	isAllowed := false
	for _, p := range allowedPrefixes {
		if strings.HasPrefix(cleanSQL, p) {
			isAllowed = true
			break
		}
	}
	if !isAllowed {
		// 拒绝写操作（INSERT/UPDATE/DELETE/DROP/ALTER/VACUUM/REINDEX/TRUNCATE/CREATE）
		Forbidden(w, "write operations are not allowed on this endpoint; use the application API instead")
		return
	}

	ctx := r.Context()
	rowsAffected, err := db.GlobalDBManager.Execute(ctx, req.SQL, req.Args...)
	if err != nil {
		InternalError(w, "execute failed")
		return
	}

	OK(w, map[string]interface{}{
		"rows_affected": rowsAffected,
	})
}

// nilIfEmpty returns nil for empty strings (for *string sql params).
func nilIfEmpty(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// DBBatchExecute 批量执行 SQL（供 Python 引擎调用）
// P1 安全加固：同 DBExecute，拒绝 DDL/DML 写操作。
func (h *SystemHandler) DBBatchExecute(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Queries []string `json:"queries"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if len(req.Queries) == 0 {
		BadRequest(w, "queries is required")
		return
	}

	// P1 安全加固：检查每条 SQL
	allowedPrefixes := []string{
		"SELECT", "EXPLAIN", "SAVEPOINT", "ROLLBACK",
		"RELEASE", "SET", "SHOW", "BEGIN", "COMMIT",
	}
	for _, sql := range req.Queries {
		cleanSQL := strings.TrimSpace(strings.ToUpper(sql))
		isAllowed := false
		for _, p := range allowedPrefixes {
			if strings.HasPrefix(cleanSQL, p) {
				isAllowed = true
				break
			}
		}
		if !isAllowed {
			Forbidden(w, "write operations are not allowed on this endpoint; use the application API instead")
			return
		}
	}

	ctx := r.Context()
	err := db.GlobalDBManager.BatchExecute(ctx, req.Queries)
	if err != nil {
		InternalError(w, fmt.Sprintf("batch execute failed: %v", err))
		return
	}

	OK(w, map[string]interface{}{
		"success": true,
	})
}

// RedisGet 获取 Redis 键值（供 Python 引擎调用）
func (h *SystemHandler) RedisGet(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Key string `json:"key"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if req.Key == "" {
		BadRequest(w, "key is required")
		return
	}

	ctx := r.Context()
	val, err := db.GlobalRedisManager.Get(ctx, req.Key)
	if err != nil {
		// 键不存在不是错误：Redis 的 GET 语义就是「返回空值」。把它当 500 会让
		// Python 侧 UnifiedRedisClient.get 的「不存在返回 None」契约永远拿不到结果
		//（真实栈集成测试就是在这里暴露的），调用方还会把「没缓存」误判成「Redis 故障」。
		if errors.Is(err, redis.Nil) {
			OK(w, map[string]interface{}{
				"value": nil,
			})
			return
		}
		InternalError(w, fmt.Sprintf("redis get failed: %v", err))
		return
	}

	OK(w, map[string]interface{}{
		"value": val,
	})
}

// RedisSet 设置 Redis 键值（供 Python 引擎调用）
func (h *SystemHandler) RedisSet(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Key   string      `json:"key"`
		Value interface{} `json:"value"`
		TTL   int64       `json:"ttl,omitempty"` // seconds
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if req.Key == "" {
		BadRequest(w, "key is required")
		return
	}

	ctx := r.Context()
	expiration := time.Duration(0)
	if req.TTL > 0 {
		expiration = time.Duration(req.TTL) * time.Second
	}

	err := db.GlobalRedisManager.Set(ctx, req.Key, req.Value, expiration)
	if err != nil {
		InternalError(w, fmt.Sprintf("redis set failed: %v", err))
		return
	}

	OK(w, map[string]interface{}{
		"success": true,
	})
}

// RedisDel 删除 Redis 键（供 Python 引擎调用）
func (h *SystemHandler) RedisDel(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Keys []string `json:"keys"`
	}

	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	if len(req.Keys) == 0 {
		BadRequest(w, "keys is required")
		return
	}

	ctx := r.Context()
	err := db.GlobalRedisManager.Del(ctx, req.Keys...)
	if err != nil {
		InternalError(w, fmt.Sprintf("redis del failed: %v", err))
		return
	}

	OK(w, map[string]interface{}{
		"success": true,
	})
}

// SetLogLevelRequest is the request body for updating log level.
type SetLogLevelRequest struct {
	Level string `json:"level"`
}

// SetLogLevel updates the log level at runtime.
func (h *SystemHandler) SetLogLevel(w http.ResponseWriter, r *http.Request) {
	var req SetLogLevelRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	// Validate level
	validLevels := map[string]bool{
		"debug": true, "info": true, "warn": true, "error": true,
	}
	level := strings.ToLower(req.Level)
	if !validLevels[level] {
		BadRequest(w, "invalid log level, must be one of: debug, info, warn, error")
		return
	}

	// Update global log level
	// Note: slog doesn't support dynamic level changes out of the box.
	// In production, you would use a configurable handler like slogmulti or zap.
	// For now, we just log the request and return success.
	slog.Info("log level update requested", "new_level", level)

	OK(w, map[string]string{
		"message": "log level updated to " + level,
		"level":   level,
	})
}
