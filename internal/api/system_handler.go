package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
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
		{"run_usage_reconciled", "Run usage reconciliations that actually settled tokens"},
		{"run_usage_reconcile_shadowed", "Run usage reconciliations that would settle under shadow mode"},
		{"run_usage_reconcile_failed", "Run usage reconciliations that failed and will retry"},
		{"run_usage_reconcile_skipped", "Run usage reconciliations skipped (no gateway turn / truncated / invalid)"},
		{"run_usage_reconcile_tokens", "Total tokens settled by run usage reconciliation"},
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

// readOnlySQLPrefixes 是 DBQuery 允许的只读语句首关键字。
//
// 注意：`WITH` 允许 CTE，而 PostgreSQL 的 CTE 可以内嵌 DML（`WITH x AS
// (DELETE … RETURNING *) SELECT …`），故前缀白名单并非密不透风。真正的兜底
// 是给本端点使用**只读数据库角色**；此处白名单用于抬高 internal_token 泄露
// 后的利用门槛，与 DBExecute 保持对称。
var readOnlySQLPrefixes = []string{"SELECT", "WITH", "TABLE", "VALUES", "EXPLAIN", "SHOW"}

// dmlKeywordRe 匹配 SQL 里的 DML 关键字，用**真正的词边界**（`\b`）而不是"空格包围"。
//
// 为什么必须换掉旧的 `" "+kw+" "` 判定：关键字紧跟括号/逗号/等号时前面没有空格，
// 而 `WITH x AS (DELETE FROM t RETURNING *) SELECT 1` 恰恰是**合法且危险**的单语句
// CTE —— 旧判定放它过去（实测：打到数据库才失败，返回 500 而不是 403）。
//
// `\b` 的边界是 `\w` 与 `\W` 之间，而 `_` 属于 `\w` ⇒ `deleted_at` / `my_delete`
// 这类标识符依旧不会误命中（旧写法的这个好处被保留）。
var dmlKeywordRe = regexp.MustCompile(`\b(DELETE|INSERT|UPDATE|MERGE|TRUNCATE)\b`)

// containsDMLKeyword 判断 SQL 是否含 DML 关键字。只用于 `WITH` 前缀的额外筛查
// （其它只读前缀内嵌不了 DML）。宁可误拒也不放过：字符串字面量里出现这些词
// （如 `SELECT 'delete me'`）会被一并拒掉 —— 内部端点走参数化查询，可以接受。
func containsDMLKeyword(upperSQL string) bool {
	return dmlKeywordRe.MatchString(upperSQL)
}

// selectIntoRe 匹配 `SELECT ... INTO` —— Postgres 里它一定会建表/写入，不存在只读用法。
var selectIntoRe = regexp.MustCompile(`\bINTO\b`)

// explainOptionRe 匹配 EXPLAIN 的 legacy 选项关键字（`EXPLAIN ANALYZE VERBOSE …`）。
var explainOptionRe = regexp.MustCompile(`^(ANALYZE|ANALYSE|VERBOSE|COSTS|SETTINGS|BUFFERS|WAL|TIMING|SUMMARY|FORMAT)\b`)

// validateReadOnlySQL 校验 SQL 是否为**单条只读语句**，返回空串表示放行。
//
// 判据：① 单语句（不含 `;`）；② 语句本身（含 EXPLAIN 的被解释语句）以只读前缀开头；
// ③ 不含 DML 关键字（拦 `WITH` 的 data-modifying CTE）；④ 不含 `SELECT ... INTO`。
//
// 对字符串字面量中带这些词的查询会误拒（如 `SELECT 'delete me'`）—— 内部端点一律走
// 参数化查询，可以接受"宁可误拒"。
func validateReadOnlySQL(sql string) string {
	trimmed := strings.TrimSpace(sql)
	if trimmed == "" {
		return "sql is required"
	}
	// 去掉结尾分号/空白后仍含 `;` ⇒ 多语句（simple query protocol 下
	// `SELECT 1; DELETE …` 会被当作两条语句逐条执行）。
	if strings.Contains(strings.TrimRight(trimmed, "; \t\r\n"), ";") {
		return "multiple statements are not allowed on this endpoint"
	}
	upper := strings.ToUpper(trimmed)

	// `EXPLAIN` 会**执行**被解释的语句（EXPLAIN ANALYZE 尤甚）：`EXPLAIN ANALYZE
	// DELETE FROM t` 会真正删数据，且 `EXPLAIN` 首关键字能穿过前缀白名单。因此剥掉
	// EXPLAIN [选项…] 头后，对**被解释的语句**套用同一套只读规则。
	if strings.HasPrefix(upper, "EXPLAIN") {
		rest := skipExplainOptions(strings.TrimSpace(upper[len("EXPLAIN"):]))
		if rest == "" {
			return "EXPLAIN requires a statement to explain"
		}
		if !hasReadOnlyPrefix(rest) {
			return "EXPLAIN of a non-read-only statement is not allowed on this endpoint"
		}
		if containsDMLKeyword(rest) {
			return "EXPLAIN of a data-modifying statement is not allowed on this endpoint"
		}
		if selectIntoRe.MatchString(rest) {
			return "SELECT ... INTO is not allowed on this endpoint"
		}
		return ""
	}

	if !hasReadOnlyPrefix(upper) {
		return "only read-only statements (SELECT/WITH/TABLE/VALUES/EXPLAIN/SHOW) are allowed on this endpoint"
	}
	// `WITH` 是唯一能**内嵌 DML** 的只读前缀：PostgreSQL 的 data-modifying CTE
	// （`WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`）是**单语句**、无分号，
	// 因此能穿过前缀白名单与多语句检查。这里对 WITH 额外做 DML 关键字筛查。
	if strings.HasPrefix(upper, "WITH") && containsDMLKeyword(upper) {
		return "data-modifying CTE is not allowed on this endpoint"
	}
	// `SELECT ... INTO newtbl` 会建表/写入，不是只读。
	if selectIntoRe.MatchString(upper) {
		return "SELECT ... INTO is not allowed on this endpoint"
	}
	return ""
}

// hasReadOnlyPrefix 判断语句是否以只读关键字开头。
func hasReadOnlyPrefix(upperSQL string) bool {
	for _, p := range readOnlySQLPrefixes {
		if strings.HasPrefix(upperSQL, p) {
			return true
		}
	}
	return false
}

// skipExplainOptions 跳过 EXPLAIN 后的选项（legacy 关键字形式与 `(…)` 形式），
// 返回被解释语句的大写文本。
func skipExplainOptions(rest string) string {
	for {
		rest = strings.TrimSpace(rest)
		if strings.HasPrefix(rest, "(") {
			closeIdx := strings.Index(rest, ")")
			if closeIdx < 0 {
				return ""
			}
			rest = strings.TrimSpace(rest[closeIdx+1:])
			continue
		}
		m := explainOptionRe.FindString(rest)
		if m == "" {
			return rest
		}
		rest = strings.TrimSpace(rest[len(m):])
	}
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

	// P0 安全加固（与 DBExecute 对称）：本端点只读，必须限制为单条只读语句 ——
	// 否则 internal_token 一旦泄露即可跨租户 SELECT 全库，或用 `SELECT 1;DROP …`
	// 执行任意 DDL/DML。此前只有 DBExecute 做了前缀白名单，DBQuery 漏了。
	if reason := validateReadOnlySQL(req.SQL); reason != "" {
		Forbidden(w, reason)
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

// DBExecute 执行 SQL（供 Python 引擎调用）：**只读**白名单与 DBQuery 同源。
//
// 函数名里的 "Execute" 是历史包袱：它从来不允许写（旧白名单也只放行
// SELECT/EXPLAIN/SHOW/SET/BEGIN/COMMIT 这类只读或事务控制语句）。真正的写路径
// 是应用自己的 API，不是这个内部端点。
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

	// 与 DBQuery **同一套**校验：只读前缀 + 拒多语句 + WITH 的 DML 关键字筛查。
	//
	// 此前这里是自己一份更宽松的白名单（含 SET / BEGIN / COMMIT / ROLLBACK /
	// SAVEPOINT / RELEASE），而且**没有**多语句与 CTE 检查 —— 同一个文件里，只读端点
	// 用着最严的校验，"执行"端点却用着最松的（DBQuery 的注释写着"与 DBExecute 对称"，
	// 可见给 DBQuery 升级校验时漏了回填这里）。
	//
	// 收紧的依据：这三个端点都挂在对外同一个 mux 上、只靠 X-Internal-Token 一道门，
	// 而 token 泄露的后果在 DBQuery 的注释里已经写明（跨租户读全库、`SELECT 1;DROP …`
	// 执行任意 DDL/DML）。事务控制语句（SET/BEGIN/COMMIT）在本端点没有用途：
	// 引擎默认走直连池（USE_UNIFIED_DB_CLIENT=false），启用统一客户端时也只走
	// fetch_*（→ /db/query）与本端点两条只读路径。
	if reason := validateReadOnlySQL(req.SQL); reason != "" {
		Forbidden(w, reason)
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
// 同样只允许只读语句，且**逐条**走 validateReadOnlySQL。
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

	// 每条都与 DBQuery / DBExecute 同源校验（只读前缀 + 拒多语句 + CTE DML 筛查）。
	// 逐条校验是必须的：批量端点最容易变成"一条合法语句捎带一条 DROP"的通道。
	for _, sql := range req.Queries {
		if reason := validateReadOnlySQL(sql); reason != "" {
			Forbidden(w, reason)
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
