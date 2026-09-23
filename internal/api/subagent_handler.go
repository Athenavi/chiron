package api

// ── 子 Agent 运行观测 API（docs/subagent-design.md §4.4）──
//
// 三个只读端点，供前端画"递归层级树 + 侧边栏看运行中子 Agent 输出"：
//
//	GET /v1/subagent/runs?session_id=&parent_run_id=   整树/子节点列表
//	GET /v1/subagent/runs/{run_id}                     单 run 详情（状态/摘要/用量）
//	GET /v1/subagent/runs/{run_id}/events?limit=       输出过程（Redis Stream → DB steps 回落）
//
// 数据源分层（§3.3）：Redis = 运行期（TTL 1h，引擎写入，key 带 tenant 维度）；
// PostgreSQL = 权威。每个查询都"先 Redis 后 DB"，Redis 不可用或键过期时自动回落。
//
// 安全：全部要求鉴权；租户取自 claims（TenantID 回退 UserID）；DB 查询强制 tenant_id 过滤，
// 因此不存在跨租户读取子 Agent 输出的路径。

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

const (
	subagentListLimit    = 200
	subagentEventsLimit  = 200
	subagentContentChars = 8000
)

type SubagentHandler struct {
	rdb db.RedisClient
}

func NewSubagentHandler(rdb db.RedisClient) *SubagentHandler {
	return &SubagentHandler{rdb: rdb}
}

// subagentTenant 与 trace_handler 保持同一套租户判定（TenantID 回退 UserID）。
func subagentTenant(claims *auth.Claims) string {
	if claims == nil {
		return ""
	}
	if claims.TenantID != "" {
		return claims.TenantID
	}
	return claims.UserID
}

// subagentKey 构造运行期缓存 key；必须与引擎 `app/subagent/runtime_cache.py` 逐字一致。
func subagentKey(parts ...string) string {
	return db.RedisKey("subagent:" + strings.Join(parts, ":"))
}

// ListRuns 返回整棵层级树（session_id）或某节点的直接子节点（parent_run_id）。
func (h *SubagentHandler) ListRuns(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))
	parentRunID := strings.TrimSpace(r.URL.Query().Get("parent_run_id"))
	if sessionID == "" && parentRunID == "" {
		BadRequest(w, "session_id or parent_run_id is required")
		return
	}

	// 1) Redis 整树骨架（一次 HGETALL 拿到全部节点摘要）
	if sessionID != "" {
		if runs, err := h.treeFromRedis(r.Context(), tenant, sessionID); err == nil && len(runs) > 0 {
			OK(w, map[string]interface{}{"runs": runs, "source": "redis"})
			return
		}
	}
	// 2) Redis 子节点索引（懒加载场景）
	if parentRunID != "" {
		if runs, err := h.childrenFromRedis(r.Context(), tenant, parentRunID); err == nil && len(runs) > 0 {
			OK(w, map[string]interface{}{"runs": runs, "source": "redis"})
			return
		}
	}
	// 3) DB 回落（权威；历史 run 必然在这里）
	runs := h.runsFromDB(r.Context(), tenant, sessionID, parentRunID)
	OK(w, map[string]interface{}{"runs": runs, "source": "db"})
}

// GetRun 返回单个 run 的状态/摘要/用量。
func (h *SubagentHandler) GetRun(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	if view, err := h.runFromRedis(r.Context(), tenant, runID); err == nil && view != nil {
		OK(w, map[string]interface{}{"run": view, "source": "redis"})
		return
	}
	view := h.runFromDB(r.Context(), tenant, runID)
	if view == nil {
		NotFound(w, "subagent run not found")
		return
	}
	OK(w, map[string]interface{}{"run": view, "source": "db"})
}

// GetRunEvents 返回输出过程：Redis Stream（实时/近实时）优先，DB steps 回落（历史/审计）。
func (h *SubagentHandler) GetRunEvents(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	limit := subagentEventsLimit
	if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && v > 0 && v <= subagentEventsLimit {
		limit = v
	}

	if events, err := h.eventsFromRedis(r.Context(), tenant, runID, limit); err == nil && len(events) > 0 {
		OK(w, map[string]interface{}{"events": events, "source": "redis"})
		return
	}
	// DB 回落前先确认该 run 归属本租户（steps 表本身不带 tenant，必须借 runs 校验）
	if !h.runOwnedByTenant(r.Context(), tenant, runID) {
		NotFound(w, "subagent run not found")
		return
	}
	OK(w, map[string]interface{}{"events": h.stepsFromDB(r.Context(), runID, limit), "source": "db"})
}

// ── Redis 读取 ──

func (h *SubagentHandler) treeFromRedis(ctx context.Context, tenant, sessionID string) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	raw, err := h.hashAll(ctx, subagentKey(tenant, "tree", sessionID))
	if err != nil {
		return nil, err
	}
	out := make([]map[string]interface{}, 0, len(raw))
	for _, value := range raw {
		var item map[string]interface{}
		if json.Unmarshal([]byte(value), &item) == nil && item["run_id"] != nil {
			out = append(out, item)
		}
	}
	return out, nil
}

func (h *SubagentHandler) childrenFromRedis(ctx context.Context, tenant, parentRunID string) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	res := h.rdb.Do(ctx, "SMEMBERS", subagentKey(tenant, "children", parentRunID))
	if res.Err() != nil {
		return nil, res.Err()
	}
	ids, _ := res.Val().([]interface{})
	out := make([]map[string]interface{}, 0, len(ids))
	for _, id := range ids {
		runID, _ := id.(string)
		if runID == "" {
			continue
		}
		if view, err := h.runFromRedis(ctx, tenant, runID); err == nil && view != nil {
			out = append(out, view)
		}
	}
	return out, nil
}

func (h *SubagentHandler) runFromRedis(ctx context.Context, tenant, runID string) (map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	// go-redis v9 的 HGETALL 返回 map（非 v8 的扁平数组）：统一走 db.HashAll，
	// 否则这里静默拿不到任何字段，运行期摘要会一直回落 DB。
	fields, err := db.HashAll(ctx, h.rdb, subagentKey(tenant, "run", runID))
	if err != nil {
		return nil, err
	}
	if len(fields) == 0 {
		return nil, nil
	}
	view := map[string]interface{}{"run_id": runID}
	for field, value := range fields {
		switch field {
		case "depth":
			if n, err := strconv.Atoi(value); err == nil {
				view["depth"] = n
			}
		case "usage":
			var usage map[string]interface{}
			if json.Unmarshal([]byte(value), &usage) == nil {
				view["usage"] = usage
			}
		case "run_id":
			// 保持 path 参数为准
		default:
			if value != "" {
				view[field] = value
			}
		}
	}
	return view, nil
}

func (h *SubagentHandler) eventsFromRedis(ctx context.Context, tenant, runID string, limit int) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	// start="+" 表示倒序（取最近 limit 条），返回后再按时间正序，便于前端顺序渲染
	msgs, err := h.rdb.XRange(ctx, subagentKey(tenant, "ev", runID), "+", "-", int64(limit)).Result()
	if err != nil {
		return nil, err
	}
	out := make([]map[string]interface{}, 0, len(msgs))
	for i := len(msgs) - 1; i >= 0; i-- {
		payload, ok := msgs[i].Values["data"].(string)
		if !ok {
			continue
		}
		var event map[string]interface{}
		if json.Unmarshal([]byte(payload), &event) == nil {
			event["id"] = msgs[i].ID
			out = append(out, event)
		}
	}
	return out, nil
}

// hashAll 读取 Hash 全部字段（用 Do 以兼容 Cluster/单机两种实现）。
func (h *SubagentHandler) hashAll(ctx context.Context, key string) (map[string]string, error) {
	// go-redis v9 的 HGETALL 返回 map（非 v8 的扁平数组）：统一走 db.HashAll，
	// 否则静默得到空 map（子 Agent 的运行期摘要会一直回落 DB）。
	return db.HashAll(ctx, h.rdb, key)
}

// ── DB 回落 ──

func (h *SubagentHandler) runsFromDB(ctx context.Context, tenant, sessionID, parentRunID string) []map[string]interface{} {
	out := []map[string]interface{}{}
	if tenant == "" || db.GlobalDBManager == nil {
		return out
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT id, COALESCE(parent_run_id, '') AS parent_run_id, depth,
		       COALESCE(profile_name, '') AS profile_name, status,
		       COALESCE(summary, '') AS summary, input_tokens, output_tokens, steps,
		       redacted_count, COALESCE(error, '') AS error,
		       artifacts, write_paths, created_at
		  FROM subagent_runs
		 WHERE tenant_id = $1
		   AND ($2 = '' OR root_session_id = $2)
		   AND ($3 = '' OR parent_run_id = $3)
		 ORDER BY created_at DESC
		 LIMIT $4`, tenant, sessionID, parentRunID, subagentListLimit)
	if err != nil {
		slog.Warn("subagent runs query failed", "tenant", tenant, "error", err)
		return out
	}
	for _, row := range rows {
		out = append(out, normalizeRunRow(row))
	}
	return out
}

func (h *SubagentHandler) runFromDB(ctx context.Context, tenant, runID string) map[string]interface{} {
	if tenant == "" || db.GlobalDBManager == nil {
		return nil
	}
	row, err := db.GlobalDBManager.FetchOne(ctx, `
		SELECT id, COALESCE(parent_run_id, '') AS parent_run_id, depth,
		       COALESCE(profile_name, '') AS profile_name, status,
		       COALESCE(summary, '') AS summary, input_tokens, output_tokens, steps,
		       redacted_count, COALESCE(error, '') AS error,
		       artifacts, write_paths, created_at
		  FROM subagent_runs
		 WHERE id = $1 AND tenant_id = $2`, runID, tenant)
	if err != nil || row == nil {
		return nil
	}
	return normalizeRunRow(row)
}

func (h *SubagentHandler) runOwnedByTenant(ctx context.Context, tenant, runID string) bool {
	if tenant == "" || db.GlobalDBManager == nil {
		return false
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT 1 AS ok FROM subagent_runs WHERE id = $1 AND tenant_id = $2`, runID, tenant)
	return err == nil && row != nil
}

func (h *SubagentHandler) stepsFromDB(ctx context.Context, runID string, limit int) []map[string]interface{} {
	out := []map[string]interface{}{}
	if db.GlobalDBManager == nil {
		return out
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT seq, kind, COALESCE(role, '') AS role, COALESCE(tool_name, '') AS tool_name,
		       COALESCE(tool_call_id, '') AS tool_call_id, COALESCE(content, '') AS content,
		       truncated, input_tokens, output_tokens
		  FROM subagent_run_steps
		 WHERE run_id = $1
		 ORDER BY seq
		 LIMIT $2`, runID, limit)
	if err != nil {
		slog.Warn("subagent steps query failed", "run_id", runID, "error", err)
		return out
	}
	for _, row := range rows {
		content, _ := row["content"].(string)
		truncated, _ := row["truncated"].(bool)
		if len(content) > subagentContentChars {
			content = content[:subagentContentChars]
			truncated = true
		}
		row["content"] = content
		row["truncated"] = truncated
		// DB 的列叫 kind，前端契约却是 SSE 的事件名 type —— 不映射的话，历史回放
		// （Redis Stream 过了 TTL 只剩 DB）一条都命中不到渲染分支，输出页恒空白。
		kind := stringOf(row["kind"])
		row["type"] = stepKindToEventType(kind)
		row["run_id"] = runID
		if kind == "tool_call" || kind == "tool_result" {
			// 这两类的可读信息在 tool_name（content 常是参数 JSON），挂到 status 上供前端展示
			row["status"] = stringOf(row["tool_name"])
		}
		out = append(out, row)
	}
	return out
}

// stepKindToEventType 把 subagent_run_steps.kind 映射成前端契约里的事件名（subagent.*）。
// 未知 kind 一律当 notice —— 前端对 notice 会渲染 content，不会把内容丢掉。
func stepKindToEventType(kind string) string {
	switch kind {
	case "message":
		return "subagent.text"
	case "tool_call", "tool_result":
		return "subagent.status"
	case "approval":
		// 审批必须有独立事件名：回放时前端要能重建**可点击**的审批卡片，
		// 归到 notice 就只剩一行文字，用户看得到却批不了（等于没修）。
		return "subagent.approval"
	default:
		return "subagent.notice"
	}
}

// normalizeRunRow 统一 Redis/DB 两条来源的字段名与类型（前端只认这一份契约）。
func normalizeRunRow(row map[string]interface{}) map[string]interface{} {
	view := map[string]interface{}{"source": "db"}
	view["run_id"] = stringOf(row["id"])
	view["status"] = stringOf(row["status"])
	// 字段名必须与 Redis 那条来源**逐字一致**（前端只认一份契约）：DB 列叫 profile_name，
	// 输出的键必须是 profile —— 否则 DB 回落时前端拿不到 profile，树行退化成 run_id 显示。
	if v := stringOf(row["profile_name"]); v != "" {
		view["profile"] = v
	}
	for _, field := range []string{"parent_run_id", "summary", "error"} {
		if v := stringOf(row[field]); v != "" {
			view[field] = v
		}
	}
	view["depth"] = intOf(row["depth"])
	view["usage"] = map[string]interface{}{
		"input_tokens":  intOf(row["input_tokens"]),
		"output_tokens": intOf(row["output_tokens"]),
		"steps":         intOf(row["steps"]),
	}
	if n := intOf(row["redacted_count"]); n > 0 {
		view["redacted_count"] = n
	}
	if ts := stringOf(row["created_at"]); ts != "" {
		view["created_at"] = ts
	}
	// 产物：`artifacts` / `write_paths` 都是 jsonb，**原样透传为字符串**交给前端解析 ——
	// 后端不对形状做假设，避免与引擎侧的字段演进耦合（这里只保证数据能到前端）。
	if v := stringOf(row["artifacts"]); v != "" && v != "[]" {
		view["artifacts"] = v
	}
	if v := stringOf(row["write_paths"]); v != "" && v != "[]" {
		view["write_paths"] = v
	}
	return view
}

func stringOf(v interface{}) string {
	switch value := v.(type) {
	case string:
		return value
	case []byte:
		// jsonb 列经驱动返回的可能是 []byte。若落进 default 分支，会被格式化成
		// "[91 123 93]" 这种字节序列 —— 前端拿到就无从 parse 了。
		return string(value)
	case nil:
		return ""
	case time.Time:
		return value.UTC().Format(time.RFC3339)
	default:
		return fmt.Sprintf("%v", value)
	}
}

func intOf(v interface{}) int {
	switch value := v.(type) {
	case int:
		return value
	case int32:
		return int(value)
	case int64:
		return int(value)
	case float64:
		return int(value)
	case string:
		if n, err := strconv.Atoi(value); err == nil {
			return n
		}
	}
	return 0
}
