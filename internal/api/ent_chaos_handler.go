package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ── 混沌工程实验管理 ────────────────────────────────────────────────────────
//
// 与 Python 侧的分工：
//
//   - **本 handler** 负责实验的增删改查。权威存储是 ``ent_chaos_experiments``
//     （迁移 ``0002_ent_chaos_experiments``）；
//   - **施加故障**不在这里 —— 由两侧的注入中间件完成：网关请求路径见
//     ``chaosMiddleware``（同包），引擎请求路径见
//     ``python-engine/app/chaos/injector.py`` 的 ASGI 中间件；
//   - 任何**写操作后都失效 Redis 缓存** ``chaos:active:{tenant}``（TTL 仅几秒），
//     保证多副本一致。
//
// 历史：本文件的六个端点曾长期统一返回 501，其注释自述「没有故障注入执行 ——
// 未接入 python-engine 的 chaos 模块」「连数据表都不存在」。而当时 python 侧的
// ``ChaosEngine`` 也只是假注入（自己 sleep + 打日志）。两端一起构成了"看起来有、
// 实际没有"的假实现 —— 本次一并补齐：表已建、注入已通、且**不支持的作用面会被拒绝**。

// 支持的作用面与故障类型 —— 与 python-engine/app/chaos/injector.py 的
// SUPPORTED_TARGETS / MIDDLEWARE_FAULT_TYPES **必须一致**。不一致的后果是创建出的
// 实验永远不会生效（那正是"假注入"复现的方式），所以这里显式校验、宁缺毋滥。
var (
	chaosSupportedTargets = map[string]bool{"gateway": true, "engine": true}
	chaosSupportedFaults  = map[string]bool{"latency": true, "error": true, "timeout": true}
)

// chaosUnsupportedHint 说明被拒绝的作用面/类型该去哪里实现。
const chaosUnsupportedHint = "supported: target=gateway|engine, fault_type=latency|error|timeout; " +
	"llm/db/redis need instrumentation on their own call paths, and 'resource' belongs in a dedicated tool"

// chaosActiveKey 返回注入中间件读的活跃实验缓存键。
//
// **必须与 python-engine/app/redis_keys.py 的 rkey("chaos:active:<tenant>") 逐字一致**：
// 两侧中间件共享同一份缓存，键不一致就会各读各的（Go 失效、Python 却still 命中旧值）。
// 前缀由 REDIS_KEY_PREFIX 提供，与 Python 的 settings.redis_key_prefix 同源同义。
func chaosActiveKey(tenantID string) string {
	return os.Getenv("REDIS_KEY_PREFIX") + "chaos:active:" + tenantID
}

// EntChaosHandler 提供混沌工程实验管理 API。
type EntChaosHandler struct {
	redis db.RedisClient
}

// NewEntChaosHandler 创建混沌工程 handler。
func NewEntChaosHandler(redis db.RedisClient) *EntChaosHandler {
	return &EntChaosHandler{redis: redis}
}

// RegisterRoutes 挂载混沌工程路由（authMW + RequireEntPerm("chaos:manage")）。
func (h *EntChaosHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	permMW := RequireEntPerm("chaos:manage")
	handle := func(pattern string, hf http.HandlerFunc) {
		mux.Handle(pattern, authMW(permMW(http.HandlerFunc(hf))))
	}
	handle("GET /v1/ent/chaos/experiments", h.ListExperiments)
	handle("POST /v1/ent/chaos/experiments", h.CreateExperiment)
	handle("GET /v1/ent/chaos/experiments/{id}", h.GetExperiment)
	handle("POST /v1/ent/chaos/experiments/{id}/rollback", h.RollbackExperiment)
	handle("GET /v1/ent/chaos/status", h.Status)
	handle("DELETE /v1/ent/chaos/experiments/{id}", h.DeleteExperiment)
}

// tenantOf 校验租户上下文；未通过时已写出响应并返回 ""。
func (h *EntChaosHandler) tenantOf(w http.ResponseWriter, r *http.Request) string {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		Forbidden(w, "tenant_id not found")
		return ""
	}
	return claims.TenantID
}

// invalidateCache 让注入中间件下一次读取时回源数据库。
//
// 失败只记日志：缓存层坏掉不该让"创建实验"这个动作失败 —— 中间件的 TTL 很短，
// 最多几秒后自己会刷新。
func (h *EntChaosHandler) invalidateCache(ctx context.Context, tenantID string) {
	if h.redis == nil || tenantID == "" {
		return
	}
	if err := h.redis.Del(ctx, chaosActiveKey(tenantID)).Err(); err != nil {
		slog.Warn("chaos: cache invalidate failed", "tenant_id", tenantID, "error", err)
	}
}

type chaosExperiment struct {
	ID          string          `json:"id"`
	FaultType   string          `json:"fault_type"`
	Target      string          `json:"target"`
	DurationMs  int             `json:"duration_ms"`
	Intensity   float64         `json:"intensity"`
	Status      string          `json:"status"`
	Config      json.RawMessage `json:"config"`
	Result      json.RawMessage `json:"result"`
	StartedAt   *time.Time      `json:"started_at,omitempty"`
	CompletedAt *time.Time      `json:"completed_at,omitempty"`
	CreatedAt   time.Time       `json:"created_at"`
}

func (h *EntChaosHandler) ListExperiments(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}

	rows, err := db.Pool.Query(r.Context(), `
		SELECT id, fault_type, target, duration_ms, intensity, status,
		       config::text, result::text, started_at, completed_at, created_at
		  FROM ent_chaos_experiments
		 WHERE tenant_id = $1
		 ORDER BY created_at DESC
		 LIMIT 200`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list chaos experiments failed")
		return
	}
	defer rows.Close()

	items := make([]chaosExperiment, 0, 16)
	for rows.Next() {
		var e chaosExperiment
		var cfg, res string
		if err := rows.Scan(&e.ID, &e.FaultType, &e.Target, &e.DurationMs, &e.Intensity,
			&e.Status, &cfg, &res, &e.StartedAt, &e.CompletedAt, &e.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan chaos experiment failed")
			return
		}
		e.Config = json.RawMessage(cfg)
		e.Result = json.RawMessage(res)
		items = append(items, e)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate chaos experiments failed")
		return
	}

	OK(w, map[string]any{"experiments": items, "count": len(items)})
}

type createChaosRequest struct {
	FaultType  string          `json:"fault_type"`
	Target     string          `json:"target"`
	DurationMs int             `json:"duration_ms"`
	Intensity  float64         `json:"intensity"`
	Config     json.RawMessage `json:"config"`
}

func (h *EntChaosHandler) CreateExperiment(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}

	var body createChaosRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	// 只接受**确实会被施加**的实验。此前这里会"写库即返回 created"，而故障从未注入；
	// 与其复制那种误导，不如明确拒绝并说明该去哪儿实现。
	if !chaosSupportedFaults[body.FaultType] {
		BadRequest(w, "unsupported fault_type '"+body.FaultType+"' — "+chaosUnsupportedHint)
		return
	}
	if !chaosSupportedTargets[body.Target] {
		BadRequest(w, "unsupported target '"+body.Target+"' — "+chaosUnsupportedHint)
		return
	}
	if body.DurationMs <= 0 {
		body.DurationMs = 1000
	}
	if body.Intensity <= 0 || body.Intensity > 1 {
		body.Intensity = 0.5
	}
	if len(body.Config) == 0 {
		body.Config = json.RawMessage(`{}`)
	}

	claims := auth.GetClaims(r.Context())
	createdBy := ""
	if claims != nil {
		createdBy = claims.UserID
	}

	var id string
	err := db.Pool.QueryRow(r.Context(), `
		INSERT INTO ent_chaos_experiments
		    (id, tenant_id, fault_type, target, duration_ms, intensity, status,
		     config, created_by, started_at)
		VALUES ($1, $2, $3, $4, $5, $6, 'running', $7, $8, now())
		RETURNING id`,
		newUUID(), tenantID, body.FaultType, body.Target,
		body.DurationMs, body.Intensity, string(body.Config), createdBy,
	).Scan(&id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create chaos experiment failed")
		return
	}

	h.invalidateCache(r.Context(), tenantID)
	OK(w, map[string]any{"id": id, "status": "running"})
}

func (h *EntChaosHandler) GetExperiment(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}
	id := r.PathValue("id")

	var e chaosExperiment
	var cfg, res string
	err := db.Pool.QueryRow(r.Context(), `
		SELECT id, fault_type, target, duration_ms, intensity, status,
		       config::text, result::text, started_at, completed_at, created_at
		  FROM ent_chaos_experiments
		 WHERE id = $1 AND tenant_id = $2`, id, tenantID,
	).Scan(&e.ID, &e.FaultType, &e.Target, &e.DurationMs, &e.Intensity,
		&e.Status, &cfg, &res, &e.StartedAt, &e.CompletedAt, &e.CreatedAt)
	if err != nil {
		NotFound(w, "chaos experiment not found")
		return
	}
	e.Config = json.RawMessage(cfg)
	e.Result = json.RawMessage(res)
	OK(w, e)
}

func (h *EntChaosHandler) RollbackExperiment(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}
	id := r.PathValue("id")

	tag, err := db.Pool.Exec(r.Context(), `
		UPDATE ent_chaos_experiments
		   SET status = 'rolled_back', completed_at = now(), updated_at = now()
		 WHERE id = $1 AND tenant_id = $2 AND status IN ('pending', 'running')`,
		id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "rollback chaos experiment failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "chaos experiment not found or already finished")
		return
	}

	// 必须先失效再返回：否则调用方以为已回滚，注入却还会持续到 TTL 过期。
	h.invalidateCache(r.Context(), tenantID)
	OK(w, map[string]any{"id": id, "status": "rolled_back"})
}

func (h *EntChaosHandler) DeleteExperiment(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}
	id := r.PathValue("id")

	tag, err := db.Pool.Exec(r.Context(),
		`DELETE FROM ent_chaos_experiments WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete chaos experiment failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "chaos experiment not found")
		return
	}

	h.invalidateCache(r.Context(), tenantID)
	OK(w, map[string]any{"id": id, "deleted": true})
}

func (h *EntChaosHandler) Status(w http.ResponseWriter, r *http.Request) {
	tenantID := h.tenantOf(w, r)
	if tenantID == "" {
		return
	}
	if db.Pool == nil {
		InternalError(w, "database not available")
		return
	}

	rows, err := db.Pool.Query(r.Context(), `
		SELECT status, count(*) FROM ent_chaos_experiments
		 WHERE tenant_id = $1 GROUP BY status`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "chaos status failed")
		return
	}
	defer rows.Close()

	byStatus := map[string]int{}
	active := 0
	for rows.Next() {
		var status string
		var n int
		if err := rows.Scan(&status, &n); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan chaos status failed")
			return
		}
		byStatus[status] = n
		if status == "pending" || status == "running" {
			active += n
		}
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate chaos status failed")
		return
	}

	OK(w, map[string]any{"active_count": active, "by_status": byStatus})
}
