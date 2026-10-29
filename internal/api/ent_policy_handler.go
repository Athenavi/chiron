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

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// ── 企业合规策略：类型与错误 ─────────────────────────────────────────────

// ErrModelNotAllowed 表示请求的模型不在租户/角色白名单内（403 语义）。
// 集成任务（如 /submit 拦截）应将该错误映射为 HTTP 403。
var ErrModelNotAllowed = errors.New("model not allowed by enterprise policy")

// EntTenantPolicy 对应 ent_tenant_policies 表（租户级隐私/留存策略）。
type EntTenantPolicy struct {
	TenantID          string          `json:"tenant_id"`
	PrivacyMode       bool            `json:"privacy_mode"`
	DataRetentionDays int             `json:"data_retention_days"`
	TrainingAllowed   bool            `json:"training_allowed"`
	RedactionRules    json.RawMessage `json:"redaction_rules"`
	UpdatedAt         time.Time       `json:"updated_at"`
}

// EntModelPolicy 对应 ent_model_policies 表（模型白名单与每模型限速）。
// RoleID 为 nil 表示租户级兜底策略。
type EntModelPolicy struct {
	ID             string                    `json:"id"`
	TenantID       string                    `json:"tenant_id"`
	RoleID         *string                   `json:"role_id"`
	AllowedModels  []string                  `json:"allowed_models"`
	PerModelLimits map[string]map[string]int `json:"per_model_limits"`
	CreatedAt      time.Time                 `json:"created_at"`
	UpdatedAt      time.Time                 `json:"updated_at"`
}

// ── Handler ──────────────────────────────────────────────────────────────

// EntPolicyHandler 提供企业合规策略 API（租户隐私策略 / 模型访问策略 CRUD），
// 并导出供集成任务使用的强制点函数（EnforceEnterprisePolicy 等）。
// 路由注册由集成任务统一接入（本任务不注册）：
//
//	policyHandler := api.NewEntPolicyHandler()
//	policyHandler.RegisterRoutes(mux, authMW)
type EntPolicyHandler struct {
	// resolveAllowed 解析用户适用的模型白名单。
	// 返回 (allowed, hasPolicy, err)：hasPolicy=false 表示无任何策略（放行）。
	// nil 时使用默认 PG 实现；测试可注入 fake。
	resolveAllowed func(ctx context.Context, tenantID int16, userID string) ([]string, bool, error)
	// resolveEntitled 解析用户当前生效套餐权益允许的模型。
	// 返回 (models, hasEntitlement, err)：hasEntitlement=false 表示用户没有生效权益，
	// 不构成限制。nil 时使用默认 PG 实现；测试可注入 fake。
	resolveEntitled func(ctx context.Context, userID string) ([]string, bool, error)
}

// NewEntPolicyHandler 创建策略 handler（默认 PG 实现）。
func NewEntPolicyHandler() *EntPolicyHandler {
	return &EntPolicyHandler{
		resolveAllowed:  defaultResolveAllowedModels,
		resolveEntitled: defaultResolveEntitledModels,
	}
}

// RegisterRoutes 挂载策略路由（authMW + RequireEntPerm("policy:manage")）。
func (h *EntPolicyHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	permMW := RequireEntPerm("policy:manage")
	handle := func(pattern string, hf http.HandlerFunc) {
		mux.Handle(pattern, authMW(permMW(http.HandlerFunc(hf))))
	}
	handle("GET /v1/ent/privacy", h.GetPrivacy)
	handle("PUT /v1/ent/privacy", h.PutPrivacy)
	handle("GET /v1/ent/model-policies", h.ListModelPolicies)
	handle("POST /v1/ent/model-policies", h.CreateModelPolicy)
	handle("GET /v1/ent/model-policies/{id}", h.GetModelPolicy)
	handle("PUT /v1/ent/model-policies/{id}", h.UpdateModelPolicy)
	handle("DELETE /v1/ent/model-policies/{id}", h.DeleteModelPolicy)
}

// entPolicyTenantID 取当前请求的租户 ID：claims 优先，缺省回退默认租户
// （现行登录链路未签发 tenant_id claim，单租户模式下恒为默认租户）。
func entPolicyTenantID(claims *auth.Claims) string {
	if claims != nil && claims.TenantID != 0 {
		return tenantStr(claims.TenantID)
	}
	return tenantStr(DefaultTenantID)
}

// ── 强制点函数（导出给集成任务，独立可测） ────────────────────────────────

// EnforceEnterprisePolicy 校验 modelID 是否在该用户适用的模型白名单内。
//
// 解析规则：role 精确策略优先（用户直接角色 ∪ 群组成员角色的任一命中），
// 缺失则回退租户级策略（role_id IS NULL）；两者都无 → 放行。
// 任何查询失败一律 fail-open（放行 + slog.Warn），保证策略基础设施故障
// 不阻断业务链路。model 不在白名单返回 ErrModelNotAllowed（403 语义）。
func (h *EntPolicyHandler) EnforceEnterprisePolicy(r *http.Request, claims *auth.Claims, modelID string) error {
	if modelID == "" {
		return nil
	}
	if claims == nil {
		slog.Warn("ent policy: missing claims, fail-open", "model", modelID)
		return nil
	}
	allowed, hasPolicy, err := h.resolveAllowed(r.Context(), claims.TenantID, claims.UserID)
	if err != nil {
		slog.Warn("ent policy: resolve allow-list failed, fail-open",
			"user", claims.UserID, "tenant", claims.TenantID, "model", modelID, "error", err)
		return nil
	}
	entModels, hasEnt := h.resolveEntitledModels(r.Context(), claims.UserID)
	effective, restricted := effectiveAllowedModels(allowed, hasPolicy, entModels, hasEnt)
	if !restricted {
		return nil
	}
	for _, m := range effective {
		if m == modelID {
			return nil
		}
	}
	return ErrModelNotAllowed
}

// defaultPolicyHandler 供包级强制点函数使用的默认实例。
var defaultPolicyHandler = NewEntPolicyHandler()

// EnforceEnterprisePolicy 包级入口（签名同方法版本），集成任务直接调用。
func EnforceEnterprisePolicy(r *http.Request, claims *auth.Claims, modelID string) error {
	return defaultPolicyHandler.EnforceEnterprisePolicy(r, claims, modelID)
}

// ResolveAllowedModels 返回用户视角的可用模型清单：
// 有生效策略时返回白名单；无任何策略时返回 admin_model_configs 全量模型。
// 查询失败返回 error（供 GET /v1/models 改造后按 fail-open 处理）。
func (h *EntPolicyHandler) ResolveAllowedModels(ctx context.Context, tenantID int16, userID string) ([]string, error) {
	allowed, hasPolicy, err := h.resolveAllowed(ctx, tenantID, userID)
	if err != nil {
		return nil, err
	}
	entModels, hasEnt := h.resolveEntitledModels(ctx, userID)
	effective, restricted := effectiveAllowedModels(allowed, hasPolicy, entModels, hasEnt)
	if !restricted {
		// 两侧都不限制 → 全量模型。
		return h.allModelIDs(ctx)
	}
	if effective == nil {
		effective = []string{}
	}
	return effective, nil
}

// ResolveAllowedModels 包级入口。
func ResolveAllowedModels(ctx context.Context, tenantID int16, userID string) ([]string, error) {
	return defaultPolicyHandler.ResolveAllowedModels(ctx, tenantID, userID)
}

// GetTenantPrivacyMode 返回租户是否开启隐私模式（无记录 → false）。
// 集成任务据此在转发 Python 引擎请求时注入 X-Privacy-Mode: no_retention 头。
func (h *EntPolicyHandler) GetTenantPrivacyMode(ctx context.Context, tenantID string) (bool, error) {
	pool := db.ReadPool()
	if pool == nil {
		return false, errors.New("ent policy: postgres pool unavailable")
	}
	var mode bool
	err := pool.QueryRow(ctx,
		`SELECT privacy_mode FROM ent_tenant_policies WHERE tenant_id = $1`, tenantID).Scan(&mode)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	return mode, nil
}

// GetTenantPrivacyMode 包级入口。
func GetTenantPrivacyMode(ctx context.Context, tenantID string) (bool, error) {
	return defaultPolicyHandler.GetTenantPrivacyMode(ctx, tenantID)
}

// modelRateLua 单键窗口限流原子脚本：超限返回 0，否则计数 +1 并返回 1。
const modelRateLua = `
local cur = tonumber(redis.call("GET", KEYS[1]) or "0")
if cur >= tonumber(ARGV[1]) then return 0 end
redis.call("INCR", KEYS[1])
redis.call("EXPIRE", KEYS[1], ARGV[2])
return 1
`

// CheckModelRate 每模型每租户速率检查（RPM，60s 窗口）。
// 限速值优先取 ent_model_policies.per_model_limits[model]["rpm"]（租户级策略），
// 缺省回退入参 rpmLimit（调用方从 admin_model_configs.max_rpm 读取）。
// 键格式 model:{model}:{tenant}；limit<=0 或 Redis 不可用/出错时放行（fail-open）。
func (h *EntPolicyHandler) CheckModelRate(ctx context.Context, tenantID, modelID string, rpmLimit int) error {
	limit := rpmLimit
	if policyLimit, err := h.policyModelRPM(ctx, tenantID, modelID); err == nil && policyLimit > 0 {
		limit = policyLimit
	}
	if limit <= 0 {
		return nil
	}
	rdb := db.Redis
	if rdb == nil {
		return nil
	}
	key := fmt.Sprintf("model:%s:%s", modelID, tenantID)
	ok, err := rdb.Eval(ctx, modelRateLua, []string{key}, limit, 60).Bool()
	if err != nil {
		slog.Warn("ent policy: model rate limit check failed, fail-open",
			"model", modelID, "tenant", tenantID, "error", err)
		return nil
	}
	if !ok {
		return fmt.Errorf("model %s rate limit exceeded for tenant %s", modelID, tenantID)
	}
	return nil
}

// CheckModelRate 包级入口。
func CheckModelRate(ctx context.Context, tenantID, modelID string, rpmLimit int) error {
	return defaultPolicyHandler.CheckModelRate(ctx, tenantID, modelID, rpmLimit)
}

// ── 默认 PG 解析实现 ─────────────────────────────────────────────────────

// defaultResolveAllowedModels 从 PG 解析用户适用的模型白名单。
// 步骤：tenantID 缺省经 users 表解析 → 聚合用户角色（直接 ∪ 群组）→
// role 精确策略优先、租户级兜底。无策略返回 (nil, false, nil)。
func defaultResolveAllowedModels(ctx context.Context, tenantID int16, userID string) ([]string, bool, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, false, errors.New("ent policy: postgres pool unavailable")
	}

	if tenantID == 0 {
		if userID == "" {
			return nil, false, errors.New("ent policy: missing tenant_id and user_id")
		}
		if err := pool.QueryRow(ctx,
			`SELECT tenant_id FROM users WHERE id = $1`, userID).Scan(&tenantID); err != nil {
			return nil, false, fmt.Errorf("ent policy: resolve tenant: %w", err)
		}
	}

	// 用户角色集合：直接角色 ∪ 群组成员角色
	roleIDs := []string{}
	if userID != "" {
		rows, err := pool.Query(ctx,
			`SELECT role_id FROM ent_user_roles WHERE user_id = $1
			 UNION
			 SELECT gr.role_id FROM ent_group_members gm
			 JOIN ent_group_roles gr ON gr.group_id = gm.group_id
			 WHERE gm.user_id = $1`, userID)
		if err != nil {
			return nil, false, fmt.Errorf("ent policy: query user roles: %w", err)
		}
		for rows.Next() {
			var id string
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return nil, false, fmt.Errorf("ent policy: scan user roles: %w", err)
			}
			roleIDs = append(roleIDs, id)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return nil, false, fmt.Errorf("ent policy: iterate user roles: %w", err)
		}
	}

	// role 精确策略优先（role_id IS NULL 排后），命中第一条即生效
	var allowed []string
	err := pool.QueryRow(ctx,
		`SELECT allowed_models FROM ent_model_policies
		 WHERE tenant_id = $1 AND (role_id IS NULL OR role_id = ANY($2::uuid[]))
		 ORDER BY (role_id IS NULL) ASC
		 LIMIT 1`, tenantID, roleIDs).Scan(&allowed)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, false, nil
		}
		return nil, false, fmt.Errorf("ent policy: query model policy: %w", err)
	}
	return allowed, true, nil
}

// allModelIDs 返回 admin_model_configs 全量 model_id（模型清单直接读该表）。
func (h *EntPolicyHandler) allModelIDs(ctx context.Context) ([]string, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, errors.New("ent policy: postgres pool unavailable")
	}
	rows, err := pool.Query(ctx, `SELECT model_id FROM admin_model_configs ORDER BY priority DESC, model_id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	models := []string{}
	for rows.Next() {
		var m string
		if err := rows.Scan(&m); err != nil {
			return nil, err
		}
		models = append(models, m)
	}
	return models, rows.Err()
}

// policyModelRPM 读取租户级策略 per_model_limits[model]["rpm"]（无 → 0）。
func (h *EntPolicyHandler) policyModelRPM(ctx context.Context, tenantID, modelID string) (int, error) {
	pool := db.ReadPool()
	if pool == nil {
		return 0, errors.New("ent policy: postgres pool unavailable")
	}
	var raw []byte
	err := pool.QueryRow(ctx,
		`SELECT per_model_limits FROM ent_model_policies WHERE tenant_id = $1 AND role_id IS NULL`,
		tenantID).Scan(&raw)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, nil
		}
		return 0, err
	}
	limits := map[string]map[string]int{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &limits); err != nil {
			return 0, fmt.Errorf("ent policy: parse per_model_limits: %w", err)
		}
	}
	if lm, ok := limits[modelID]; ok {
		return lm["rpm"], nil
	}
	return 0, nil
}

// ── 租户隐私策略端点 ─────────────────────────────────────────────────────

// GetPrivacy GET /v1/ent/privacy —— 读取当前租户策略（无记录返回默认值）。
func (h *EntPolicyHandler) GetPrivacy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	var p EntTenantPolicy
	err := pool.QueryRow(r.Context(),
		`SELECT tenant_id, privacy_mode, data_retention_days, training_allowed, redaction_rules, updated_at
		 FROM ent_tenant_policies WHERE tenant_id = $1`, tenantID).
		Scan(&p.TenantID, &p.PrivacyMode, &p.DataRetentionDays, &p.TrainingAllowed, &p.RedactionRules, &p.UpdatedAt)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			OK(w, EntTenantPolicy{
				TenantID:        tenantID,
				TrainingAllowed: true,
				RedactionRules:  json.RawMessage(`{}`),
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get tenant policy failed")
		return
	}
	OK(w, p)
}

// PutPrivacy PUT /v1/ent/privacy —— UPSERT 当前租户策略。
func (h *EntPolicyHandler) PutPrivacy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	var body struct {
		PrivacyMode       bool            `json:"privacy_mode"`
		DataRetentionDays int             `json:"data_retention_days"`
		TrainingAllowed   bool            `json:"training_allowed"`
		RedactionRules    json.RawMessage `json:"redaction_rules"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.DataRetentionDays < 0 {
		BadRequest(w, "data_retention_days must be >= 0")
		return
	}
	if !json.Valid(body.RedactionRules) {
		BadRequest(w, "redaction_rules must be valid JSON object")
		return
	}

	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	var p EntTenantPolicy
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO ent_tenant_policies (tenant_id, privacy_mode, data_retention_days, training_allowed, redaction_rules, updated_at)
		 VALUES ($1, $2, $3, $4, $5, NOW())
		 ON CONFLICT (tenant_id) DO UPDATE SET
		     privacy_mode = EXCLUDED.privacy_mode,
		     data_retention_days = EXCLUDED.data_retention_days,
		     training_allowed = EXCLUDED.training_allowed,
		     redaction_rules = EXCLUDED.redaction_rules,
		     updated_at = NOW()
		 RETURNING tenant_id, privacy_mode, data_retention_days, training_allowed, redaction_rules, updated_at`,
		tenantID, body.PrivacyMode, body.DataRetentionDays, body.TrainingAllowed, string(body.RedactionRules)).
		Scan(&p.TenantID, &p.PrivacyMode, &p.DataRetentionDays, &p.TrainingAllowed, &p.RedactionRules, &p.UpdatedAt)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "upsert tenant policy failed")
		return
	}
	OK(w, p)
}

// ── 模型访问策略端点 ─────────────────────────────────────────────────────

// validModelIDSet 从 admin_model_configs 读取合法 model_id 集合（白名单校验用）。
func validModelIDSet(ctx context.Context) (map[string]bool, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, errors.New("ent policy: postgres pool unavailable")
	}
	rows, err := pool.Query(ctx, `SELECT model_id FROM admin_model_configs`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	set := map[string]bool{}
	for rows.Next() {
		var m string
		if err := rows.Scan(&m); err != nil {
			return nil, err
		}
		set[m] = true
	}
	return set, rows.Err()
}

// validateAllowedModels 校验白名单元素均存在于 admin_model_configs.model_id，
// 返回非法元素列表（空 = 合法）。
func validateAllowedModels(ctx context.Context, models []string) ([]string, error) {
	if len(models) == 0 {
		return nil, nil
	}
	set, err := validModelIDSet(ctx)
	if err != nil {
		return nil, err
	}
	invalid := []string{}
	for _, m := range models {
		if !set[m] {
			invalid = append(invalid, m)
		}
	}
	return invalid, nil
}

const modelPolicyColumns = `id, tenant_id, role_id, allowed_models, per_model_limits, created_at, updated_at`

func scanModelPolicy(row interface{ Scan(...any) error }) (*EntModelPolicy, error) {
	var p EntModelPolicy
	var raw []byte
	if err := row.Scan(&p.ID, &p.TenantID, &p.RoleID, &p.AllowedModels, &raw, &p.CreatedAt, &p.UpdatedAt); err != nil {
		return nil, err
	}
	p.PerModelLimits = map[string]map[string]int{}
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &p.PerModelLimits)
	}
	return &p, nil
}

// ListModelPolicies GET /v1/ent/model-policies —— 当前租户全部策略。
func (h *EntPolicyHandler) ListModelPolicies(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	rows, err := pool.Query(r.Context(),
		`SELECT `+modelPolicyColumns+` FROM ent_model_policies
		 WHERE tenant_id = $1 ORDER BY (role_id IS NULL) DESC, created_at`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list model policies failed")
		return
	}
	defer rows.Close()
	policies := []EntModelPolicy{}
	for rows.Next() {
		p, err := scanModelPolicy(rows)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "list model policies failed")
			return
		}
		policies = append(policies, *p)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list model policies failed")
		return
	}
	OK(w, policies)
}

// decodeModelPolicyBody 解析创建/更新请求体并做字段校验（白名单元素合法性除外）。
func decodeModelPolicyBody(w http.ResponseWriter, r *http.Request) (*EntModelPolicy, bool) {
	var body struct {
		RoleID         string                    `json:"role_id"`
		AllowedModels  []string                  `json:"allowed_models"`
		PerModelLimits map[string]map[string]int `json:"per_model_limits"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return nil, false
	}
	if body.AllowedModels == nil {
		body.AllowedModels = []string{}
	}
	for _, m := range body.AllowedModels {
		if strings.TrimSpace(m) == "" {
			BadRequest(w, "allowed_models must not contain empty entries")
			return nil, false
		}
	}
	if body.PerModelLimits == nil {
		body.PerModelLimits = map[string]map[string]int{}
	}
	p := &EntModelPolicy{
		AllowedModels:  body.AllowedModels,
		PerModelLimits: body.PerModelLimits,
	}
	if body.RoleID != "" {
		if _, err := uuid.Parse(body.RoleID); err != nil {
			BadRequest(w, "role_id must be a valid uuid (or omitted for tenant-level policy)")
			return nil, false
		}
		p.RoleID = &body.RoleID
	}
	return p, true
}

// CreateModelPolicy POST /v1/ent/model-policies
// allowed_models 元素必须存在于 admin_model_configs.model_id（违反 422）；
// UNIQUE NULLS NOT DISTINCT(tenant_id, role_id) 冲突返回 409。
func (h *EntPolicyHandler) CreateModelPolicy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	p, ok := decodeModelPolicyBody(w, r)
	if !ok {
		return
	}
	tenantID := entPolicyTenantID(claims)

	if invalid, err := validateAllowedModels(r.Context(), p.AllowedModels); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate allowed models failed")
		return
	} else if len(invalid) > 0 {
		JSON(w, http.StatusUnprocessableEntity, APIResponse{
			Success: false,
			Error:   "unknown model_id in allowed_models: " + strings.Join(invalid, ", "),
		})
		return
	}

	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	limitsJSON, _ := json.Marshal(p.PerModelLimits)
	created, err := scanModelPolicy(db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO ent_model_policies (tenant_id, role_id, allowed_models, per_model_limits)
		 VALUES ($1, $2, $3, $4) RETURNING `+modelPolicyColumns,
		tenantID, p.RoleID, p.AllowedModels, string(limitsJSON)))
	if err != nil {
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "model policy already exists for this tenant/role",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create model policy failed")
		return
	}
	Created(w, created)
}

// GetModelPolicy GET /v1/ent/model-policies/{id}
func (h *EntPolicyHandler) GetModelPolicy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid policy id")
		return
	}
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	p, err := scanModelPolicy(pool.QueryRow(r.Context(),
		`SELECT `+modelPolicyColumns+` FROM ent_model_policies WHERE id = $1 AND ($2 = '' OR tenant_id::text = $2)`, id, scope))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get model policy failed")
		return
	}
	OK(w, p)
}

// UpdateModelPolicy PUT /v1/ent/model-policies/{id}（全量替换 allowed_models /
// per_model_limits / role_id；校验规则同创建）。
func (h *EntPolicyHandler) UpdateModelPolicy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid policy id")
		return
	}
	p, ok := decodeModelPolicyBody(w, r)
	if !ok {
		return
	}
	if invalid, err := validateAllowedModels(r.Context(), p.AllowedModels); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate allowed models failed")
		return
	} else if len(invalid) > 0 {
		JSON(w, http.StatusUnprocessableEntity, APIResponse{
			Success: false,
			Error:   "unknown model_id in allowed_models: " + strings.Join(invalid, ", "),
		})
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	limitsJSON, _ := json.Marshal(p.PerModelLimits)
	updated, err := scanModelPolicy(db.GlobalDBManager.QueryRow(r.Context(),
		`UPDATE ent_model_policies
		 SET role_id = $2, allowed_models = $3, per_model_limits = $4, updated_at = NOW()
		 WHERE id = $1 AND ($19 = '' OR tenant_id::text = $19) RETURNING `+modelPolicyColumns,
		id, p.RoleID, p.AllowedModels, string(limitsJSON), scope))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "model policy already exists for this tenant/role",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update model policy failed")
		return
	}
	OK(w, updated)
}

// DeleteModelPolicy DELETE /v1/ent/model-policies/{id}
func (h *EntPolicyHandler) DeleteModelPolicy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid policy id")
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM ent_model_policies WHERE id = $1 AND ($2 = '' OR tenant_id::text = $2)`, id, scope)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete model policy failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	NoContent(w)
}
