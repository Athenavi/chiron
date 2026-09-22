package api

// ── 会话运行时状态与遥测（P1 单一事实源）──
//
// 设计见 docs/session-runtime-spec.md。核心诉求：让「模式 / 模型 / provider / 工具授权 /
// 上下文激活项」只存在一处，并让**两条提交链路**（SSE `/submit` 与统一 `/v1/chat/submit`）
// 读同一份解析结果 —— 此前两条链路各带一份，缺一份就静默失效（模式与模型切换无效的根因）。
//
// 存储分层：
//   Redis `session:runtime:{tenant}:{sid}`（Hash，热，TTL 24h）
//   `unified_sessions.runtime` jsonb（权威持久；Redis 缺失时由它回填）
//
// 解析优先级（唯一实现，见 spec §3）：
//   mode        : 请求显式 > 用户默认 > 全局默认 > normal
//   tools_mode  : 请求显式 > 用户默认 > 全局默认 > auto
//   model       : 请求显式 > runtime > 用户默认 > 全局默认 > deepseek-chat
//   provider    : 请求显式 > runtime > 空（自动路由）
//
// **mode / tools_mode 不再有 runtime 一层**：它们是前端实时状态，每次提交随请求携带，
// 服务端只负责校验与兜底（见 mode.go）。曾把它们写进 runtime（Redis + unified_sessions.runtime）
// 时，同一份状态散落三处，任一处不一致就表现为"切换了却不生效"。
// model / provider 仍是会话语义（用户选定后希望下次继续用），保留 runtime 存储。
//
// 遥测：Redis `session:metrics:{tenant}:{sid}`（Hash，field=turn_id，value=JSON 明细），
// **汇总在读取时计算**（幂等、无增量漂移）；缺失时回落 DB（turns + billing_records）。

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
)

const (
	sessionRuntimeTTL = 24 * time.Hour
	sessionMetricsTTL = 24 * time.Hour

	// 系统兜底（与 agents.go 的默认、引擎 settings.default_model 语义一致）
	fallbackMode      = "normal"
	fallbackToolsMode = "auto"
	fallbackModel     = "deepseek-chat"
)

// 与引擎 app/agent/modes.py 的 _BASE_MODES 对齐
var validAgentModes = map[string]bool{"normal": true, "minimal": true, "ptc": true, "creative": true}

// SessionRuntimeHandler 提供运行时状态的读写与会话遥测聚合。
type SessionRuntimeHandler struct {
	rdb        db.RedisClient
	sessionMgr *session.Manager
}

func NewSessionRuntimeHandler(rdb db.RedisClient, sessionMgr *session.Manager) *SessionRuntimeHandler {
	return &SessionRuntimeHandler{rdb: rdb, sessionMgr: sessionMgr}
}

// resolvedValue 是解析结果 + 来源（前端可展示"当前生效值 + 来自哪里"，排障也用得上）。
type resolvedValue struct {
	Value  string `json:"value"`
	Source string `json:"source"`
}

// runtimeView 是**会话级**运行时状态。
//
// 工具授权模式（ask/auto/yolo）与对话模式（normal/minimal/ptc/creative）**不在其中**：
// 它们是请求级参数 —— 前端实时状态，每次提交随请求携带，服务端只校验不存储。
// 只有"用户选定后希望下次继续沿用"的项（model / provider）才落会话状态。
type runtimeView struct {
	Model      string         `json:"model,omitempty"`
	Provider   string         `json:"provider,omitempty"`
	Compaction map[string]any `json:"compaction,omitempty"`
	Context    map[string]any `json:"context,omitempty"`
	UpdatedAt  string         `json:"updated_at,omitempty"`
	UpdatedBy  string         `json:"updated_by,omitempty"`
}

// ── key ──

func sessionRuntimeKey(tenant, sessionID string) string {
	return db.RedisKey(fmt.Sprintf("session:runtime:%s:%s", tenant, sessionID))
}

func sessionMetricsKey(tenant, sessionID string) string {
	return db.RedisKey(fmt.Sprintf("session:metrics:%s:%s", tenant, sessionID))
}

// ── 读 ──

func (h *SessionRuntimeHandler) GetRuntime(w http.ResponseWriter, r *http.Request) {
	claims, tenant, sessionID, ok := h.authorize(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	state := h.loadRuntime(ctx, tenant, sessionID)
	defaults := h.loadDefaults(ctx, tenant, claims.UserID)
	resolved := h.resolveAgentConfig(claims.UserID, state, defaults, nil)

	OK(w, map[string]any{
		"runtime":  state,
		"defaults": defaults,
		"resolved": map[string]resolvedValue{
			"model":    resolved.model,
			"provider": resolved.provider,
		},
	})
}

// ── 写（PATCH：只改传入字段；显式 null 清除该项以回落默认链）──

func (h *SessionRuntimeHandler) PutRuntime(w http.ResponseWriter, r *http.Request) {
	claims, tenant, sessionID, ok := h.authorize(w, r)
	if !ok {
		return
	}
	var patch map[string]json.RawMessage
	if err := DecodeJSON(w, r, &patch); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// 校验取值（避免把非法值写进状态，导致引擎侧静默回退而"看起来没生效"）
	// mode / tools_mode 不再是 runtime 字段：它们是请求级参数，PATCH 里的同名键
	// 会被忽略（applyRuntimePatch 不再处理），前端也已在提交时携带。
	if raw, ok := patch["context"]; ok && len(raw) > 0 && string(raw) != "null" {
		var payload map[string]any
		if json.Unmarshal(raw, &payload) != nil {
			BadRequest(w, "invalid context: must be an object")
			return
		}
	}

	ctx := r.Context()
	state := h.loadRuntime(ctx, tenant, sessionID)
	applyRuntimePatch(state, patch, claims.UserID)
	if err := h.saveRuntime(ctx, tenant, sessionID, state); err != nil {
		slog.Error("save session runtime failed", "session", sessionID, "error", err)
		InternalError(w, "failed to save session runtime")
		return
	}
	defaults := h.loadDefaults(ctx, tenant, claims.UserID)
	resolved := h.resolveAgentConfig(claims.UserID, state, defaults, nil)
	OK(w, map[string]any{
		"runtime": state,
		"resolved": map[string]resolvedValue{
			"model": resolved.model, "provider": resolved.provider,
		},
	})
}

// ── 会话遥测（读取时聚合，幂等）──

func (h *SessionRuntimeHandler) GetMetrics(w http.ResponseWriter, r *http.Request) {
	_, tenant, sessionID, ok := h.authorize(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	if turns, source := h.metricsFromRedis(ctx, tenant, sessionID); len(turns) > 0 {
		OK(w, aggregateMetrics(turns, source))
		return
	}
	OK(w, h.metricsFromDB(ctx, sessionID))
}

// ── 内部实现 ──

// authorize 校验登录 + 会话归属，返回 claims/tenant/sessionID。
func (h *SessionRuntimeHandler) authorize(w http.ResponseWriter, r *http.Request) (*auth.Claims, string, string, bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return nil, "", "", false
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return nil, "", "", false
	}
	// 新会话（尚未创建）允许读写热状态：首次提交时才会建会话。
	if h.sessionMgr != nil {
		if s, err := h.sessionMgr.GetSession(r.Context(), sessionID); err == nil && s != nil {
			if s.UserID != claims.UserID {
				Forbidden(w, "session does not belong to the current user")
				return nil, "", "", false
			}
		}
	}
	tenant := claims.TenantID
	if tenant == "" {
		tenant = claims.UserID
	}
	return claims, tenant, sessionID, true
}

// loadRuntime 读运行时状态：Redis 热层优先，缺失字段由 DB 权威层回填。
func (h *SessionRuntimeHandler) loadRuntime(ctx context.Context, tenant, sessionID string) *runtimeView {
	view := &runtimeView{Context: map[string]any{}}
	// 1) DB（权威）
	if db.GlobalDBManager != nil {
		if row, err := db.GlobalDBManager.FetchOne(ctx,
			`SELECT COALESCE(runtime, '{}'::jsonb) AS runtime FROM unified_sessions WHERE id = $1`, sessionID); err == nil && row != nil {
			if raw := stringOf(row["runtime"]); raw != "" && raw != "{}" {
				_ = json.Unmarshal([]byte(raw), view)
			}
		}
	}
	if view.Context == nil {
		view.Context = map[string]any{}
	}
	// 2) Redis（热层覆盖；TTL 到期后由 DB 兜底）
	if h.rdb != nil {
		if pairs, err := h.hashAll(ctx, sessionRuntimeKey(tenant, sessionID)); err == nil {
			mergeRuntime(view, pairs)
		}
	}
	return view
}

// saveRuntime 先写 DB（权威）再写 Redis（热层）；Redis 失败不阻断。
func (h *SessionRuntimeHandler) saveRuntime(ctx context.Context, tenant, sessionID string, state *runtimeView) error {
	payload, err := json.Marshal(state)
	if err != nil {
		return err
	}
	if db.GlobalDBManager != nil {
		if _, err := db.GlobalDBManager.Exec(ctx,
			`UPDATE unified_sessions SET runtime = $1::jsonb WHERE id = $2`, string(payload), sessionID); err != nil {
			return err
		}
	}
	if h.rdb != nil {
		key := sessionRuntimeKey(tenant, sessionID)
		args := []any{"HSET", key}
		for _, pair := range [][2]string{
			{"model", state.Model}, {"provider", state.Provider},
			{"updated_at", state.UpdatedAt}, {"updated_by", state.UpdatedBy},
		} {
			args = append(args, pair[0], pair[1])
		}
		if state.Compaction != nil {
			if raw, err := json.Marshal(state.Compaction); err == nil {
				args = append(args, "compaction", string(raw))
			}
		}
		if state.Context != nil {
			if raw, err := json.Marshal(state.Context); err == nil {
				args = append(args, "context", string(raw))
			}
		}
		if err := h.rdb.Do(ctx, args...).Err(); err != nil {
			slog.Warn("session runtime redis write failed (db 已写)", "session", sessionID, "error", err)
		}
		_ = h.rdb.Expire(ctx, key, sessionRuntimeTTL).Err()
	}
	return nil
}

// loadDefaults 读取默认值：用户级（users.settings）> 全局（system_settings.category='agent'）。
func (h *SessionRuntimeHandler) loadDefaults(ctx context.Context, tenant, userID string) map[string]string {
	out := map[string]string{}
	if db.GlobalDBManager != nil {
		if row, err := db.GlobalDBManager.FetchOne(ctx,
			`SELECT COALESCE(settings, '{}'::json) AS settings FROM users WHERE id = $1`, userID); err == nil && row != nil {
			var settings map[string]any
			if json.Unmarshal([]byte(stringOf(row["settings"])), &settings) == nil {
				for _, key := range []string{"default_mode", "default_tools_mode", "default_model"} {
					if v, ok := settings[key].(string); ok && v != "" {
						out[key] = v
					}
				}
			}
		}
		if rows, err := db.GlobalDBManager.FetchAll(ctx,
			`SELECT key, value::text FROM system_settings WHERE category = 'agent'`); err == nil {
			for _, row := range rows {
				key := stringOf(row["key"])
				if _, set := out[key]; set {
					continue // 用户级优先
				}
				if v := strings.Trim(stringOf(row["value"]), `"`); v != "" && v != "null" {
					out[key] = v
				}
			}
		}
	}
	return out
}

type agentConfig struct {
	mode      resolvedValue
	toolsMode resolvedValue
	model     resolvedValue
	provider  resolvedValue
}

// resolveAgentConfig 是**唯一**的解析实现（spec §3）。两条提交链路都必须调用它。
//
// explicit 是"本次请求的显式意图"（如 SSE 链路里前端传来的 llm_config.mode/model）。
// 优先级：explicit > runtime(会话) > 用户/全局默认 > 系统兜底。
func (h *SessionRuntimeHandler) resolveAgentConfig(userID string, state *runtimeView, defaults map[string]string, explicit map[string]string) agentConfig {
	pick := func(key, runtime, userKey, fallback string) resolvedValue {
		if v := explicit[key]; v != "" {
			return resolvedValue{v, "request"}
		}
		if runtime != "" {
			return resolvedValue{runtime, "session"}
		}
		if v := defaults[userKey]; v != "" {
			return resolvedValue{v, "default"}
		}
		return resolvedValue{fallback, "system"}
	}
	// mode / tools_mode 只走 explicit → 用户/全局默认 → 系统兜底：
	// 它们是前端的实时状态，没有 runtime（会话）一层，传空字符串即跳过该层。
	// model / provider 保留 runtime：用户选定后希望下次继续沿用。
	cfg := agentConfig{
		mode:      pick("mode", "", "default_mode", fallbackMode),
		toolsMode: pick("tools_mode", "", "default_tools_mode", fallbackToolsMode),
		model:     pick("model", state.Model, "default_model", fallbackModel),
		provider:  pick("provider", state.Provider, "", ""),
	}
	if cfg.provider.Value == "" {
		cfg.provider.Source = "auto"
	}
	return cfg
}

// ResolveSessionAgentConfig 是解析链的**包级入口**，供各提交链路复用（spec §3）。
// explicit 传该请求自带的意图（可为 nil）；返回最终生效值与来源。
//
// 两条链路（SSE `/submit` 与统一 `/v1/chat/submit`）都走这一份实现，
// 不会再出现"一条链路带 mode/model、另一条静默丢弃"的失效模式。
func ResolveSessionAgentConfig(
	ctx context.Context, rdb db.RedisClient, tenantID, userID, sessionID string, explicit map[string]string,
) agentConfig {
	h := &SessionRuntimeHandler{rdb: rdb}
	state := h.loadRuntime(ctx, tenantID, sessionID)
	defaults := h.loadDefaults(ctx, tenantID, userID)
	return h.resolveAgentConfig(userID, state, defaults, explicit)
}

// InjectSessionRuntime 在代理转发前，用会话运行时状态补齐 mode / model / provider（P1-c）。
//
// 专用于**统一链路** `/v1/chat/submit`：该路由走通用代理，Go 侧此前完全不看 body，
// 于是前端在 `llm_config` 里带的 mode/model 被引擎侧（`unified_executor.submit_chat`
// 只读 message/mode/context）静默丢弃 —— 这正是"统一链路下切模型/模式无效"的根因。
// 此处在转发前把解析结果写回 body，使两条链路行为一致。
func InjectSessionRuntime(r *http.Request, body map[string]interface{}) bool {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return true // 鉴权由上游中间件负责
	}
	sessionID, _ := body["session_id"].(string)
	if sessionID == "" {
		sessionID, _ = body["sessionId"].(string) // 兼容驼峰写法
	}
	if sessionID == "" {
		return true // 无会话上下文：交给引擎按默认处理，不阻断
	}
	tenant := claims.TenantID
	if tenant == "" {
		tenant = claims.UserID
	}
	llmConfig, _ := body["llm_config"].(map[string]interface{})
	if llmConfig == nil {
		llmConfig = map[string]interface{}{}
	}
	explicit := map[string]string{}
	for _, key := range []string{"mode", "model"} {
		if v, ok := llmConfig[key].(string); ok && v != "" {
			explicit[key] = v
		}
	}
	// 工具授权模式：与 SSE 链路同口径 —— 前端实时状态随请求携带，非法值归一化。
	if v, ok := llmConfig["tools_mode"].(string); ok && v != "" {
		explicit["tools_mode"] = normalizeToolsMode(v)
	}
	cfg := ResolveSessionAgentConfig(r.Context(), db.Redis, tenant, claims.UserID, sessionID, explicit)
	llmConfig["mode"] = cfg.mode.Value
	llmConfig["tools_mode"] = cfg.toolsMode.Value
	llmConfig["model"] = cfg.model.Value
	if cfg.provider.Value != "" {
		// 引擎侧统一从 llm_config.provider 读取（与 SSE 链路同口径），
		// 不再用顶层字段，避免两条链路各有一套写法。
		llmConfig["provider"] = cfg.provider.Value
	}
	body["llm_config"] = llmConfig
	return true
}

// applyRuntimePatch 把 PATCH 合入状态；null 表示清除。
// mode / tools_mode 不在这里处理：它们不是 runtime 字段（见 runtimeView 注释），
// PATCH 里出现同名键会被忽略。
func applyRuntimePatch(state *runtimeView, patch map[string]json.RawMessage, userID string) {
	assign := func(raw json.RawMessage, dst *string) {
		if len(raw) == 0 || string(raw) == "null" {
			*dst = ""
			return
		}
		var v string
		if json.Unmarshal(raw, &v) == nil {
			*dst = strings.TrimSpace(v)
		}
	}
	if raw, ok := patch["model"]; ok {
		assign(raw, &state.Model)
	}
	if raw, ok := patch["provider"]; ok {
		assign(raw, &state.Provider)
	}
	if raw, ok := patch["compaction"]; ok {
		if len(raw) == 0 || string(raw) == "null" {
			state.Compaction = nil
		} else {
			var m map[string]any
			if json.Unmarshal(raw, &m) == nil {
				state.Compaction = m
			}
		}
	}
	if raw, ok := patch["context"]; ok {
		if len(raw) == 0 || string(raw) == "null" {
			state.Context = map[string]any{}
		} else {
			var m map[string]any
			if json.Unmarshal(raw, &m) == nil {
				state.Context = m
			}
		}
	}
	state.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	state.UpdatedBy = userID
}

func mergeRuntime(view *runtimeView, pairs map[string]string) {
	for field, value := range pairs {
		switch field {
		// 历史遗留：旧版本的 runtime 里存过 mode / tools_mode，这里刻意不识别 ——
		// 它们已不是会话状态（见 runtimeView 注释），读到也应忽略而不是让陈旧值复活。
		case "model":
			if value != "" {
				view.Model = value
			}
		case "provider":
			if value != "" {
				view.Provider = value
			}
		case "compaction":
			var m map[string]any
			if json.Unmarshal([]byte(value), &m) == nil {
				view.Compaction = m
			}
		case "context":
			var m map[string]any
			if json.Unmarshal([]byte(value), &m) == nil {
				view.Context = m
			}
		case "updated_at":
			view.UpdatedAt = value
		case "updated_by":
			view.UpdatedBy = value
		}
	}
}

func (h *SessionRuntimeHandler) hashAll(ctx context.Context, key string) (map[string]string, error) {
	// go-redis v9 的 HGETALL 返回 map[interface{}]interface{}（v8 是扁平数组）。
	// 此前按 v8 断言会**静默**得到空 map —— runtime 的 Redis 热层形同虚设
	// （功能靠 DB 回落仍可用，所以一直没被发现）。统一走 db.HashAll。
	return db.HashAll(ctx, h.rdb, key)
}

// ── 遥测聚合 ──

// metricsFromRedis 读实时层的每轮明细（field=turn_id）。
func (h *SessionRuntimeHandler) metricsFromRedis(ctx context.Context, tenant, sessionID string) ([]map[string]any, string) {
	if h.rdb == nil {
		return nil, ""
	}
	if _, err := h.hashAll(ctx, sessionMetricsKey(tenant, sessionID)); err != nil {
		return nil, ""
	}
	pairs, err := h.hashAll(ctx, sessionMetricsKey(tenant, sessionID))
	if err != nil || len(pairs) == 0 {
		return nil, ""
	}
	turns := make([]map[string]any, 0, len(pairs))
	for _, raw := range pairs {
		var item map[string]any
		if json.Unmarshal([]byte(raw), &item) == nil {
			turns = append(turns, item)
		}
	}
	return turns, "redis"
}

// metricsFromDB 回落：turns（tokens/缓存）+ billing_records（费用）聚合。
func (h *SessionRuntimeHandler) metricsFromDB(ctx context.Context, sessionID string) map[string]any {
	out := map[string]any{"source": "db", "totals": map[string]any{}, "turns": []any{}}
	if db.GlobalDBManager == nil {
		return out
	}
	row, err := db.GlobalDBManager.FetchOne(ctx, `
		SELECT count(*)                                                  AS turns,
		       COALESCE(SUM(input_tokens), 0)                            AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)                           AS output_tokens,
		       COALESCE(SUM(cached_tokens), 0)                          AS cached_tokens,
		       COALESCE(SUM(CASE WHEN cache_hit THEN 1 ELSE 0 END), 0)   AS cache_hits
		  FROM turns WHERE session_id = $1`, sessionID)
	if err != nil || row == nil {
		return out
	}
	item, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT COALESCE(SUM(cost_cents), 0) AS cost_cents FROM billing_records WHERE session_id = $1`, sessionID)
	cost := 0
	if err == nil && item != nil {
		cost = intOf(item["cost_cents"])
	}
	totals := map[string]any{
		"turns":         intOf(row["turns"]),
		"input_tokens":  intOf(row["input_tokens"]),
		"output_tokens": intOf(row["output_tokens"]),
		"cached_tokens": intOf(row["cached_tokens"]),
		"cache_hits":    intOf(row["cache_hits"]),
		"cost_cents":    cost,
	}
	turns := intOf(row["turns"])
	if turns > 0 {
		totals["cache_hit_rate"] = float64(intOf(row["cache_hits"])) / float64(turns)
	}
	// 最近若干轮的时序（供前端画趋势）
	if rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT id, input_tokens, output_tokens, cached_tokens, cache_hit, status, created_at
		  FROM turns WHERE session_id = $1 ORDER BY created_at DESC LIMIT 50`, sessionID); err == nil {
		list := make([]map[string]any, 0, len(rows))
		for _, r := range rows {
			list = append(list, map[string]any{
				"turn_id":       stringOf(r["id"]),
				"input_tokens":  intOf(r["input_tokens"]),
				"output_tokens": intOf(r["output_tokens"]),
				"cached_tokens": intOf(r["cached_tokens"]),
				"cache_hit":     r["cache_hit"] == true,
				"status":        stringOf(r["status"]),
				"created_at":    stringOf(r["created_at"]),
			})
		}
		out["turns"] = list
	}
	out["totals"] = totals
	return out
}

// aggregateMetrics 对实时层明细做会话级汇总（读取时计算，天然幂等）。
func aggregateMetrics(turns []map[string]any, source string) map[string]any {
	var in, out, cached, hits, cost int
	durations := make([]float64, 0, len(turns))
	tps := make([]float64, 0, len(turns))
	for _, t := range turns {
		in += intOf(t["input_tokens"])
		out += intOf(t["output_tokens"])
		cached += intOf(t["cached_tokens"])
		if t["cache_hit"] == true {
			hits++
		}
		cost += intOf(t["cost_cents"])
		if ms := float64(intOf(t["duration_ms"])); ms > 0 {
			durations = append(durations, ms)
			if tokens := intOf(t["output_tokens"]); tokens > 0 {
				tps = append(tps, float64(tokens)/(ms/1000.0))
			}
		}
	}
	totals := map[string]any{
		"turns": len(turns), "input_tokens": in, "output_tokens": out,
		"cached_tokens": cached, "cache_hits": hits, "cost_cents": cost,
	}
	if len(turns) > 0 {
		totals["cache_hit_rate"] = float64(hits) / float64(len(turns))
	}
	result := map[string]any{"source": source, "totals": totals, "turns": turns}
	if len(durations) > 0 {
		result["throughput"] = map[string]any{
			"ttft_ms_p50":    percentile(durations, 0.5),
			"output_tps_p50": percentile(tps, 0.5),
			"output_tps_p95": percentile(tps, 0.95),
			"sample_turns":   len(tps),
		}
	}
	return result
}

func percentile(values []float64, p float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]float64(nil), values...)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j] < sorted[j-1]; j-- {
			sorted[j], sorted[j-1] = sorted[j-1], sorted[j]
		}
	}
	idx := int(float64(len(sorted)-1) * p)
	return sorted[idx]
}
