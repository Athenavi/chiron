package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── 企业成本中心：类型与错误 ──────────────────────────────────────────────

// EntQuotaPool 对应 ent_quota_pools 表。
type EntQuotaPool struct {
	ID           string    `json:"id"`
	TenantID     string    `json:"tenant_id"`
	ResourceType string    `json:"resource_type"` // token/storage_mb/concurrency/credits
	TotalAmount  int64     `json:"total_amount"`  // 0 = 无限制
	Period       string    `json:"period"`        // daily/monthly
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

// EntQuotaAllocation 对应 ent_quota_allocations 表。
type EntQuotaAllocation struct {
	ID         string    `json:"id"`
	PoolID     string    `json:"pool_id"`
	TargetType string    `json:"target_type"` // group/user
	TargetID   string    `json:"target_id"`
	Amount     int64     `json:"amount"`
	CreatedAt  time.Time `json:"created_at"`
}

var (
	// errQuotaConflict 唯一约束冲突（配额池 tenant+resource+period 或分配 pool+target）
	errQuotaConflict = errors.New("ent quota: unique constraint conflict")
	// errQuotaNotFound 配额池/分配不存在
	errQuotaNotFound = errors.New("ent quota: not found")
)

// 合法枚举（与迁移 CHECK 约束一致）
var (
	validResourceTypes = map[string]bool{"token": true, "storage_mb": true, "concurrency": true, "credits": true}
	validPeriods       = map[string]bool{"daily": true, "monthly": true}
	validTargetTypes   = map[string]bool{"group": true, "user": true}
)

// ── 汇总结果类型 ──────────────────────────────────────────────────────────

type entCostSummaryRow struct {
	Key          string `json:"key"`
	TenantID     string `json:"tenant_id,omitempty"`
	Day          string `json:"day,omitempty"`
	CostCents    int64  `json:"cost_cents"`
	InputTokens  int64  `json:"input_tokens"`
	OutputTokens int64  `json:"output_tokens"`
	Records      int64  `json:"records"`
	CreditsSpent int64  `json:"credits_spent"`
	RevenueCents int64  `json:"revenue_cents"`
}

type entCostSummary struct {
	From    string              `json:"from"`
	To      string              `json:"to"`
	GroupBy string              `json:"group_by"`
	Totals  entCostSummaryRow   `json:"totals"`
	Rows    []entCostSummaryRow `json:"rows"`
}

type entGroupCostRow struct {
	ID           string    `json:"id"`
	UserID       string    `json:"user_id"`
	SessionID    *string   `json:"session_id"`
	InputTokens  int64     `json:"input_tokens"`
	OutputTokens int64     `json:"output_tokens"`
	CostCents    int64     `json:"cost_cents"`
	CreatedAt    time.Time `json:"created_at"`
}

type entGroupCost struct {
	GroupID string            `json:"group_id"`
	Total   entCostSummaryRow `json:"total"`
	Records []entGroupCostRow `json:"records"`
}

// ── 数据访问接口（PG 实现 + 测试 fake 可替换） ────────────────────────────

// EntCostStore 是企业成本中心的数据访问接口。
type EntCostStore interface {
	// tenantID 为空表示"全部租户"（仅平台 owner 可见）；非空则把结果限定在该租户内。
	CostSummary(ctx context.Context, from, to time.Time, groupBy, tenantID string) (*entCostSummary, error)
	GroupCost(ctx context.Context, groupID string, from, to time.Time, tenantID string) (*entGroupCost, error)

	ListQuotaPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error)
	GetQuotaPool(ctx context.Context, id string) (*EntQuotaPool, error)
	CreateQuotaPool(ctx context.Context, p *EntQuotaPool) error
	UpdateQuotaPool(ctx context.Context, p *EntQuotaPool) error
	DeleteQuotaPool(ctx context.Context, id string) error

	ListAllocations(ctx context.Context, poolID string) ([]EntQuotaAllocation, error)
	SumAllocated(ctx context.Context, poolID string) (int64, error)
	CreateAllocation(ctx context.Context, a *EntQuotaAllocation) error
	DeleteAllocation(ctx context.Context, poolID, id string) (bool, error)

	// TenantTokenPools 返回租户的 resource_type='token' 配额池（quota 强制用）
	TenantTokenPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error)
	// M24：credits 池此前只有"能配、不检查"的半个实现。
	TenantCreditPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error)
	CreditUsageSQL(ctx context.Context, tenantID int16, since time.Time) (int64, error)
	// TokenUsageSQL 从 billing_records SQL 聚合 token 用量（Redis 计数器回填用）
	TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error)
	// ResolveTenantID 经 users 表解析用户所属租户
	ResolveTenantID(ctx context.Context, userID string) (int16, error)
}

// pgEntCostStore 是基于全局 db.Pool / db.ReadPool 的 EntCostStore 实现。
type pgEntCostStore struct{}

func newPGEntCostStore() *pgEntCostStore { return &pgEntCostStore{} }

// isUniqueViolation 判断是否为 PostgreSQL 唯一约束冲突（SQLSTATE 23505）。
func isUniqueViolation(err error) bool {
	var pe *pgconn.PgError
	if errors.As(err, &pe) {
		return pe.Code == "23505"
	}
	return false
}

// ── Handler ───────────────────────────────────────────────────────────────

// EntCostCenterHandler 提供企业成本中心 API（成本汇总 / 群组归集 / 配额池 CRUD / 分配 / 用量）。
// 路由注册由集成任务统一接入（本任务不注册）：
//
//	costHandler := api.NewEntCostCenterHandler(nil, nil)
//	costHandler.RegisterRoutes(mux, authMW, rlMW)
type EntCostCenterHandler struct {
	store EntCostStore
	redis db.RedisClient
	// notifier 可选：注入后配额变更会通知该租户的 owner/admin（P2-3）。
	// nil 表示不发通知（单测与未装配场景），所有通知点退化为 no-op。
	notifier notificationWriter
}

// SetNotifier 装配通知中心。传 nil 即关闭通知。
func (h *EntCostCenterHandler) SetNotifier(n notificationWriter) { h.notifier = n }

// NewEntCostCenterHandler 创建 handler。store/redis 为 nil 时分别回退到
// pgEntCostStore（全局 db.Pool）与全局 db.Redis。
func NewEntCostCenterHandler(store EntCostStore, redis db.RedisClient) *EntCostCenterHandler {
	if store == nil {
		store = newPGEntCostStore()
	}
	if redis == nil {
		redis = db.Redis
	}
	return &EntCostCenterHandler{store: store, redis: redis}
}

// RegisterRoutes 挂载成本中心路由（仅定义，不在本任务注册）。
// mws 建议传入 authMW、rlMW（与 UploadHandler.RegisterRoutes 惯例一致）。
func (h *EntCostCenterHandler) RegisterRoutes(mux *http.ServeMux, mws ...func(http.Handler) http.Handler) {
	handle := func(pattern string, hf http.HandlerFunc) {
		mux.Handle(pattern, middlewareChain(http.HandlerFunc(hf), mws...))
	}
	handle("GET /v1/ent/cost/summary", h.CostSummary)
	handle("GET /v1/ent/cost/groups/{id}", h.GroupCostDetail)
	handle("GET /v1/ent/quotas", h.ListQuotas)
	handle("POST /v1/ent/quotas", h.CreateQuota)
	handle("GET /v1/ent/quotas/usage", h.QuotaUsage)
	handle("GET /v1/ent/quotas/{id}", h.GetQuota)
	handle("PUT /v1/ent/quotas/{id}", h.UpdateQuota)
	handle("DELETE /v1/ent/quotas/{id}", h.DeleteQuota)
	handle("POST /v1/ent/quotas/{id}/allocations", h.CreateAllocation)
	handle("DELETE /v1/ent/quotas/{id}/allocations/{allocID}", h.DeleteAllocation)
}

// requirePerm 校验权限并返回 claims；不满足时已写响应，返回 nil。
//
// 判定与企业 RBAC 对齐（AllowedByEntOrLegacy：企业角色聚合权限优先，
// 用户无 ent 角色配置时回退 legacy 角色权限），与其它 /v1/ent/* 模块的
// RequireEntPerm 同语义 —— 避免"路由放行、handler 内又拒"的不一致。
func requirePerm(w http.ResponseWriter, r *http.Request, perm string) *auth.Claims {
	claims := auth.GetClaims(r.Context())
	if claims == nil || !AllowedByEntOrLegacy(r.Context(), claims, perm) {
		Forbidden(w, "insufficient permissions")
		return nil
	}
	return claims
}

// scopedTenantID：非 owner 一律强制锚定自身租户（fail-close）。
//
// 关键点：非 owner 时**不再**在 claims 缺租户的情况下退回 requested —— 那等于让调用方
// （请求体 / 查询串）决定租户：越权方只要拿一个不带 tenant_id 的 token，就能写/读他租户。
// 现在缺租户就返回空串：写路径的 uuid.Parse 会拒绝，读路径命中 0 条记录。
func scopedTenantID(claims *auth.Claims, requested string) string {
	if claims != nil && claims.Role != "owner" {
		return tenantStr(claims.TenantID)
	}
	return requested
}

// parseTimeRange 解析 from/to（支持 YYYY-MM-DD 与 RFC3339）。
// 缺省：to = 现在，from = to - 30 天。
func parseTimeRange(fromStr, toStr string) (time.Time, time.Time, error) {
	parse := func(s string) (time.Time, error) {
		if t, err := time.Parse("2006-01-02", s); err == nil {
			return t.UTC(), nil
		}
		return time.Parse(time.RFC3339, s)
	}
	to := time.Now().UTC()
	from := to.AddDate(0, 0, -30)
	var err error
	if fromStr != "" {
		if from, err = parse(fromStr); err != nil {
			return time.Time{}, time.Time{}, fmt.Errorf("invalid from: %w", err)
		}
	}
	if toStr != "" {
		if to, err = parse(toStr); err != nil {
			return time.Time{}, time.Time{}, fmt.Errorf("invalid to: %w", err)
		}
	}
	if !from.Before(to) {
		return time.Time{}, time.Time{}, errors.New("from must be before to")
	}
	return from, to, nil
}

// CostSummary GET /v1/ent/cost/summary?from=&to=&group_by=tenant|day
func (h *EntCostCenterHandler) CostSummary(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	// 跨租户读防护：非 owner 只能看自己租户的成本汇总 —— 此前这条查询没有任何租户
	// 过滤，任一持有 admin:read 的租户管理员都能读到**全平台**所有租户的消费与充值。
	tenantID := scopedTenantID(claims, "")
	if claims.Role != "owner" && tenantID == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	groupBy := r.URL.Query().Get("group_by")
	switch groupBy {
	case "", "tenant", "day":
	default:
		BadRequest(w, "group_by must be tenant or day")
		return
	}
	if groupBy == "" {
		groupBy = "tenant"
	}
	from, to, err := parseTimeRange(r.URL.Query().Get("from"), r.URL.Query().Get("to"))
	if err != nil {
		BadRequest(w, "invalid time range parameters")
		return
	}
	summary, err := h.store.CostSummary(r.Context(), from, to, groupBy, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "cost summary failed")
		return
	}
	OK(w, summary)
}

// GroupCostDetail GET /v1/ent/cost/groups/{id}?from=&to=
func (h *EntCostCenterHandler) GroupCostDetail(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	// 跨租户读防护：非 owner 只看自己租户；查他租户的 group 会因 tenant_id 过滤命中
	// 0 条记录（不泄漏该 group 是否存在）。
	tenantID := scopedTenantID(claims, "")
	if claims.Role != "owner" && tenantID == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	groupID := r.PathValue("id")
	if _, err := uuid.Parse(groupID); err != nil {
		BadRequest(w, "invalid group id")
		return
	}
	from, to, err := parseTimeRange(r.URL.Query().Get("from"), r.URL.Query().Get("to"))
	if err != nil {
		BadRequest(w, "invalid time range parameters")
		return
	}
	out, err := h.store.GroupCost(r.Context(), groupID, from, to, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "group cost failed")
		return
	}
	OK(w, out)
}

// ListQuotas GET /v1/ent/quotas?tenant_id=
func (h *EntCostCenterHandler) ListQuotas(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	tenantID := scopedTenantID(claims, r.URL.Query().Get("tenant_id"))
	if claims.Role != "owner" && tenantID == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	pools, err := h.store.ListQuotaPools(r.Context(), tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list quotas failed")
		return
	}
	type poolWithAllocated struct {
		EntQuotaPool
		Allocated int64 `json:"allocated"`
	}
	out := []poolWithAllocated{}
	for _, p := range pools {
		allocated, aErr := h.store.SumAllocated(r.Context(), p.ID)
		if aErr != nil {
			allocated = 0
		}
		out = append(out, poolWithAllocated{EntQuotaPool: p, Allocated: allocated})
	}
	OK(w, map[string]interface{}{"pools": out, "total": len(out)})
}

// CreateQuota POST /v1/ent/quotas
func (h *EntCostCenterHandler) CreateQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	var body struct {
		TenantID     string `json:"tenant_id"`
		ResourceType string `json:"resource_type"`
		TotalAmount  int64  `json:"total_amount"`
		Period       string `json:"period"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// 跨租户写防护：非 owner 只能给自己租户建配额池 —— 此前直接采信请求体里的
	// tenant_id，任一持有 admin:write 的租户管理员都能给**别的租户**设配额（越权写）。
	body.TenantID = scopedTenantID(claims, body.TenantID)
	if _, err := uuid.Parse(body.TenantID); err != nil {
		BadRequest(w, "valid tenant_id (uuid) required")
		return
	}
	if !validResourceTypes[body.ResourceType] {
		BadRequest(w, "resource_type must be token/storage_mb/concurrency/credits")
		return
	}
	if body.TotalAmount < 0 {
		BadRequest(w, "total_amount must be >= 0")
		return
	}
	if body.Period == "" {
		body.Period = "monthly"
	}
	if !validPeriods[body.Period] {
		BadRequest(w, "period must be daily or monthly")
		return
	}

	pool := EntQuotaPool{
		TenantID:     body.TenantID,
		ResourceType: body.ResourceType,
		TotalAmount:  body.TotalAmount,
		Period:       body.Period,
	}
	if err := h.store.CreateQuotaPool(r.Context(), &pool); err != nil {
		if errors.Is(err, errQuotaConflict) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "quota pool already exists for this tenant/resource_type/period",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create quota failed")
		return
	}
	h.notifyQuotaChanged(r.Context(), &pool, "created")
	Created(w, pool)
}

// GetQuota GET /v1/ent/quotas/{id}
func (h *EntCostCenterHandler) GetQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	// 跨租户读防护：非 owner 只能看自己租户的配额池（与 UpdateQuota/DeleteQuota 同口径）。
	// 此前缺这一步，任一持有 admin:read 的租户管理员可读他租户的配额池与分配明细。
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	allocs, err := h.store.ListAllocations(r.Context(), id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "get allocations failed")
		return
	}
	allocated, err := h.store.SumAllocated(r.Context(), id)
	if err != nil {
		allocated = 0
	}
	OK(w, map[string]interface{}{
		"pool":        pool,
		"allocations": allocs,
		"allocated":   allocated,
	})
}

// UpdateQuota PUT /v1/ent/quotas/{id}
func (h *EntCostCenterHandler) UpdateQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	var body struct {
		ResourceType *string `json:"resource_type"`
		TotalAmount  *int64  `json:"total_amount"`
		Period       *string `json:"period"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	// 跨租户写防护：非 owner 不得改**别的租户**的配额池。这里返回 404 而非 403，
	// 避免用状态码差异探测他租户的 pool id 是否存在。
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	if body.ResourceType != nil {
		if !validResourceTypes[*body.ResourceType] {
			BadRequest(w, "resource_type must be token/storage_mb/concurrency/credits")
			return
		}
		pool.ResourceType = *body.ResourceType
	}
	if body.TotalAmount != nil {
		if *body.TotalAmount < 0 {
			BadRequest(w, "total_amount must be >= 0")
			return
		}
		pool.TotalAmount = *body.TotalAmount
	}
	if body.Period != nil {
		if !validPeriods[*body.Period] {
			BadRequest(w, "period must be daily or monthly")
			return
		}
		pool.Period = *body.Period
	}

	if err := h.store.UpdateQuotaPool(r.Context(), pool); err != nil {
		if errors.Is(err, errQuotaConflict) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "quota pool already exists for this tenant/resource_type/period",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update quota failed")
		return
	}
	h.notifyQuotaChanged(r.Context(), pool, "updated")
	OK(w, pool)
}

// DeleteQuota DELETE /v1/ent/quotas/{id}（级联删除分配）
func (h *EntCostCenterHandler) DeleteQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	// 跨租户写防护：先确认该池属于调用者租户 —— 越权删除他租户配额同样是破坏性写。
	// 返回 404 以免用状态码差异探测他租户的 pool id 是否存在。
	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	if err := h.store.DeleteQuotaPool(r.Context(), id); err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "delete quota failed")
		return
	}
	NoContent(w)
}

// CreateAllocation POST /v1/ent/quotas/{id}/allocations
// 校验分配总额不超过池总量（total_amount > 0 时），超出返回 422。
func (h *EntCostCenterHandler) CreateAllocation(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	poolID := r.PathValue("id")
	if _, err := uuid.Parse(poolID); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	var body struct {
		TargetType string `json:"target_type"`
		TargetID   string `json:"target_id"`
		Amount     int64  `json:"amount"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if !validTargetTypes[body.TargetType] {
		BadRequest(w, "target_type must be group or user")
		return
	}
	if _, err := uuid.Parse(body.TargetID); err != nil {
		BadRequest(w, "valid target_id (uuid) required")
		return
	}
	if body.Amount < 0 {
		BadRequest(w, "amount must be >= 0")
		return
	}

	pool, err := h.store.GetQuotaPool(r.Context(), poolID)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	// 跨租户写防护：非 owner 不得往**别的租户**的配额池里加分配。
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}

	// 分配总额校验：existing + new <= total_amount（0 = 无限制不校验）
	if pool.TotalAmount > 0 {
		existing, sErr := h.store.SumAllocated(r.Context(), poolID)
		if sErr != nil {
			logAndRespond(w, sErr, http.StatusInternalServerError, "sum allocations failed")
			return
		}
		if existing+body.Amount > pool.TotalAmount {
			JSON(w, http.StatusUnprocessableEntity, APIResponse{
				Success: false,
				Error: fmt.Sprintf(
					"allocation exceeds pool total: allocated %d + requested %d > total %d",
					existing, body.Amount, pool.TotalAmount),
			})
			return
		}
	}

	alloc := EntQuotaAllocation{
		PoolID:     poolID,
		TargetType: body.TargetType,
		TargetID:   body.TargetID,
		Amount:     body.Amount,
	}
	if err := h.store.CreateAllocation(r.Context(), &alloc); err != nil {
		if errors.Is(err, errQuotaConflict) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "allocation already exists for this pool/target",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create allocation failed")
		return
	}
	Created(w, alloc)
}

// DeleteAllocation DELETE /v1/ent/quotas/{id}/allocations/{allocID}
func (h *EntCostCenterHandler) DeleteAllocation(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	poolID := r.PathValue("id")
	allocID := r.PathValue("allocID")
	if _, err := uuid.Parse(poolID); err != nil || uuidValidate(allocID) != nil {
		BadRequest(w, "invalid quota or allocation id")
		return
	}
	// 跨租户写防护：先取配额池校验归属，再删其分配明细（此前直接按 poolID 删除，
	// 任一持有 admin:write 的租户管理员可删掉他租户配额池下的分配）。
	pool, err := h.store.GetQuotaPool(r.Context(), poolID)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	deleted, err := h.store.DeleteAllocation(r.Context(), poolID, allocID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete allocation failed")
		return
	}
	if !deleted {
		NotFound(w, ErrNotFound)
		return
	}
	NoContent(w)
}

func uuidValidate(s string) error { _, err := uuid.Parse(s); return err }

// QuotaUsage GET /v1/ent/quotas/usage?tenant_id=
// token 用量优先读 Redis 计数器，缺失时从 billing_records SQL 聚合。
func (h *EntCostCenterHandler) QuotaUsage(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	tenantID := scopedTenantID(claims, r.URL.Query().Get("tenant_id"))
	if _, err := uuid.Parse(tenantID); err != nil {
		BadRequest(w, "valid tenant_id (uuid) required")
		return
	}

	pools, err := h.store.ListQuotaPools(r.Context(), tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list quotas failed")
		return
	}

	type usageRow struct {
		PoolID       string  `json:"pool_id"`
		ResourceType string  `json:"resource_type"`
		Period       string  `json:"period"`
		TotalAmount  int64   `json:"total_amount"`
		Used         int64   `json:"used"`
		UsageRatio   float64 `json:"usage_ratio"`
		Source       string  `json:"source"`
	}
	rowsOut := []usageRow{}
	for _, p := range pools {
		row := usageRow{
			PoolID:       p.ID,
			ResourceType: p.ResourceType,
			Period:       p.Period,
			TotalAmount:  p.TotalAmount,
			Source:       "none",
		}
		if p.ResourceType == "token" {
			row.Used, row.Source = tenantTokenUsage(r.Context(), h.store, h.redis, parseTenantID(tenantID), p.Period, time.Now())
		}
		if p.TotalAmount > 0 {
			row.UsageRatio = float64(row.Used) / float64(p.TotalAmount)
		}
		rowsOut = append(rowsOut, row)
	}
	OK(w, map[string]interface{}{
		"tenant_id": tenantID,
		"as_of":     time.Now().UTC().Format(time.RFC3339),
		"pools":     rowsOut,
	})
}
