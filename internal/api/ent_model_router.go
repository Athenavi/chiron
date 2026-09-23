package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
)

// ── 租户模型路由配置 ─────────────────────────────────────────────────────

// EntModelRoute 对应 ent_model_routes 表（租户级模型路由规则）。
// 支持按 model 配置首选 provider 与 备选 provider 列表。
type EntModelRoute struct {
	ID              string         `json:"id"`
	TenantID        string         `json:"tenant_id"`
	ModelID         string         `json:"model_id"`         // 模型标识（如 gpt-4, claude-3-opus）
	PrimaryProvider string         `json:"primary_provider"` // 首选提供商（openai / anthropic / deepseek）
	FallbackOrder   []string       `json:"fallback_order"`   // 备选提供商优先级列表
	ProviderConfig  map[string]any `json:"provider_config"`  // 提供商级覆盖参数（如 base_url, rpm_limit）
	Enabled         bool           `json:"enabled"`
	Priority        int            `json:"priority"` // 优先级（数字越大越优先）
	CreatedAt       time.Time      `json:"created_at"`
	UpdatedAt       time.Time      `json:"updated_at"`
}

// EntModelRouterHandler 提供租户模型路由配置 CRUD API，
// 并导出供 Python 引擎同步的配置查询端点。
type EntModelRouterHandler struct{}

// NewEntModelRouterHandler 创建模型路由 handler。
// 注意：建表由 InitTable 单独调用，不要在构造函数中执行 DDL。
func NewEntModelRouterHandler() *EntModelRouterHandler {
	return &EntModelRouterHandler{}
}

// InitTable 确保 ent_model_routes 表存在，在服务启动时由初始化流程调用。
func (h *EntModelRouterHandler) InitTable() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := db.GlobalDBManager.Exec(ctx,
		`CREATE TABLE IF NOT EXISTS ent_model_routes (
			id VARCHAR(36) PRIMARY KEY,
			tenant_id VARCHAR(36) NOT NULL,
			model_id VARCHAR(128) NOT NULL,
			primary_provider VARCHAR(64) NOT NULL,
			fallback_order JSONB DEFAULT '[]',
			provider_config JSONB DEFAULT '{}',
			enabled BOOLEAN DEFAULT true,
			priority INTEGER DEFAULT 1,
			created_at TIMESTAMPTZ DEFAULT NOW(),
			updated_at TIMESTAMPTZ DEFAULT NOW(),
			UNIQUE (tenant_id, model_id)
		);`)
	if err != nil {
		slog.Warn("ent_model_routes table creation failed (table may already exist)", "error", err)
	}
}

// RegisterRoutes 挂载模型路由管理路由（authMW + RequireEntPerm("model:route")）。
func (h *EntModelRouterHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	permMW := RequireEntPerm("model:route")
	handle := func(pattern string, hf http.HandlerFunc) {
		mux.Handle(pattern, authMW(permMW(http.HandlerFunc(hf))))
	}
	handle("GET /v1/ent/model-routes", h.ListRoutes)
	handle("POST /v1/ent/model-routes", h.CreateRoute)
	handle("GET /v1/ent/model-routes/{id}", h.GetRoute)
	handle("PUT /v1/ent/model-routes/{id}", h.UpdateRoute)
	handle("DELETE /v1/ent/model-routes/{id}", h.DeleteRoute)
}

// ── 数据库常量 ──

const modelRouteColumns = `id::text, tenant_id, model_id, primary_provider, fallback_order, provider_config, enabled, priority, created_at, updated_at`

// ── ListRoutes ──

func (h *EntModelRouterHandler) ListRoutes(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT `+modelRouteColumns+` FROM ent_model_routes
		 WHERE tenant_id = $1 ORDER BY priority DESC, model_id`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list model routes failed")
		return
	}
	defer rows.Close()

	out := []EntModelRoute{}
	for rows.Next() {
		var r EntModelRoute
		var fallbackRaw, configRaw []byte
		if err := rows.Scan(&r.ID, &r.TenantID, &r.ModelID, &r.PrimaryProvider,
			&fallbackRaw, &configRaw, &r.Enabled, &r.Priority, &r.CreatedAt, &r.UpdatedAt); err != nil {
			slog.Warn("scan model route row", "error", err)
			continue
		}
		if len(fallbackRaw) > 0 {
			if err := json.Unmarshal(fallbackRaw, &r.FallbackOrder); err != nil {
				slog.Warn("model route: unmarshal fallback_order failed", "error", err)
			}
		}
		if len(configRaw) > 0 {
			if err := json.Unmarshal(configRaw, &r.ProviderConfig); err != nil {
				slog.Warn("model route: unmarshal provider_config failed", "error", err)
			}
		}
		out = append(out, r)
	}
	OK(w, map[string]any{"routes": out, "total": len(out)})
}

// ── CreateRoute ──

func (h *EntModelRouterHandler) CreateRoute(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	var body struct {
		ModelID         string         `json:"model_id"`
		PrimaryProvider string         `json:"primary_provider"`
		FallbackOrder   []string       `json:"fallback_order"`
		ProviderConfig  map[string]any `json:"provider_config"`
		Enabled         bool           `json:"enabled"`
		Priority        int            `json:"priority"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.ModelID == "" || body.PrimaryProvider == "" {
		BadRequest(w, "model_id and primary_provider are required")
		return
	}
	if body.Priority <= 0 {
		body.Priority = 1
	}

	fallbackRaw, _ := json.Marshal(body.FallbackOrder)
	configRaw, _ := json.Marshal(body.ProviderConfig)
	id := uuid.New().String()

	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO ent_model_routes
		 (id, tenant_id, model_id, primary_provider, fallback_order, provider_config, enabled, priority)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		 ON CONFLICT (tenant_id, model_id) DO UPDATE
		 SET primary_provider = EXCLUDED.primary_provider,
		     fallback_order = EXCLUDED.fallback_order,
		     provider_config = EXCLUDED.provider_config,
		     enabled = EXCLUDED.enabled,
		     priority = EXCLUDED.priority,
		     updated_at = NOW()`,
		id, tenantID, body.ModelID, body.PrimaryProvider, fallbackRaw, configRaw, body.Enabled, body.Priority)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create model route failed")
		return
	}
	OK(w, map[string]string{"id": id, "model_id": body.ModelID, "status": "created"})
}

// ── GetRoute ──

func (h *EntModelRouterHandler) GetRoute(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	var route EntModelRoute
	var fallbackRaw, configRaw []byte
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT `+modelRouteColumns+` FROM ent_model_routes WHERE id = $1 AND tenant_id = $2`,
		id, tenantID).Scan(&route.ID, &route.TenantID, &route.ModelID, &route.PrimaryProvider,
		&fallbackRaw, &configRaw, &route.Enabled, &route.Priority, &route.CreatedAt, &route.UpdatedAt)
	if err != nil {
		NotFound(w, "model route not found")
		return
	}
	if len(fallbackRaw) > 0 {
		if err := json.Unmarshal(fallbackRaw, &route.FallbackOrder); err != nil {
			slog.Warn("model route: unmarshal fallback_order failed", "error", err)
		}
	}
	if len(configRaw) > 0 {
		if err := json.Unmarshal(configRaw, &route.ProviderConfig); err != nil {
			slog.Warn("model route: unmarshal provider_config failed", "error", err)
		}
	}
	OK(w, route)
}

// ── UpdateRoute ──

func (h *EntModelRouterHandler) UpdateRoute(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	var body struct {
		PrimaryProvider *string        `json:"primary_provider"`
		FallbackOrder   []string       `json:"fallback_order"`
		ProviderConfig  map[string]any `json:"provider_config"`
		Enabled         *bool          `json:"enabled"`
		Priority        *int           `json:"priority"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	sets := []string{}
	args := []any{}
	idx := 1

	if body.PrimaryProvider != nil {
		sets = append(sets, "primary_provider = $"+itoa(idx))
		args = append(args, *body.PrimaryProvider)
		idx++
	}
	if body.FallbackOrder != nil {
		raw, _ := json.Marshal(body.FallbackOrder)
		sets = append(sets, "fallback_order = $"+itoa(idx))
		args = append(args, raw)
		idx++
	}
	if body.ProviderConfig != nil {
		raw, _ := json.Marshal(body.ProviderConfig)
		sets = append(sets, "provider_config = $"+itoa(idx))
		args = append(args, raw)
		idx++
	}
	if body.Enabled != nil {
		sets = append(sets, "enabled = $"+itoa(idx))
		args = append(args, *body.Enabled)
		idx++
	}
	if body.Priority != nil {
		sets = append(sets, "priority = $"+itoa(idx))
		args = append(args, *body.Priority)
		idx++
	}
	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	sets = append(sets, "updated_at = NOW()")
	args = append(args, id, tenantID)

	_, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE ent_model_routes SET `+joinComma(sets)+` WHERE id = $`+itoa(idx)+` AND tenant_id = $`+itoa(idx+1),
		args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update model route failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

// ── DeleteRoute ──

func (h *EntModelRouterHandler) DeleteRoute(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM ent_model_routes WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete model route failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "model route not found")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ── SyncRoutes（Python 引擎同步）──

// TenantRouteConfig 是引擎同步用的精简配置结构。
type TenantRouteConfig struct {
	TenantID        string   `json:"tenant_id"`
	ModelID         string   `json:"model_id"`
	PrimaryProvider string   `json:"primary_provider"`
	FallbackOrder   []string `json:"fallback_order"`
	Enabled         bool     `json:"enabled"`
	Priority        int      `json:"priority"`
}

// SyncRoutes GET /v1/internal/model-routes 返回全部启用路由配置，供 Python 引擎启动时拉取。
func (h *EntModelRouterHandler) SyncRoutes(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT tenant_id, model_id, primary_provider, fallback_order, enabled, priority
		 FROM ent_model_routes WHERE enabled = true ORDER BY priority DESC, tenant_id, model_id`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "sync model routes failed")
		return
	}
	defer rows.Close()

	out := []TenantRouteConfig{}
	for rows.Next() {
		var rc TenantRouteConfig
		var fallbackRaw []byte
		if err := rows.Scan(&rc.TenantID, &rc.ModelID, &rc.PrimaryProvider,
			&fallbackRaw, &rc.Enabled, &rc.Priority); err != nil {
			slog.Warn("scan sync route", "error", err)
			continue
		}
		if len(fallbackRaw) > 0 {
			if err := json.Unmarshal(fallbackRaw, &rc.FallbackOrder); err != nil {
				slog.Warn("model route: unmarshal fallback_order failed", "error", err)
			}
		}
		out = append(out, rc)
	}
	OK(w, map[string]any{"routes": out, "total": len(out)})
}
