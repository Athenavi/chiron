package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
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
	CostSummary(ctx context.Context, from, to time.Time, groupBy string) (*entCostSummary, error)
	GroupCost(ctx context.Context, groupID string, from, to time.Time) (*entGroupCost, error)

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
	// TokenUsageSQL 从 billing_records SQL 聚合 token 用量（Redis 计数器回填用）
	TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error)
	// ResolveTenantID 经 users 表解析用户所属租户
	ResolveTenantID(ctx context.Context, userID string) (string, error)
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

// ── CostSummary：billing_records + credit_transactions + payments 跨租户汇总 ──

func (s *pgEntCostStore) CostSummary(ctx context.Context, from, to time.Time, groupBy string) (*entCostSummary, error) {
	// 白名单：仅允许 "day" 或 "tenant"
	var keyExpr, groupExpr string
	switch groupBy {
	case "day":
		keyExpr, groupExpr = "TO_CHAR(DATE(created_at), 'YYYY-MM-DD')", "DATE(created_at)"
	default:
		groupBy = "tenant"
		keyExpr, groupExpr = "tenant_id::text", "tenant_id"
	}

	summary := &entCostSummary{
		From: from.Format(time.RFC3339), To: to.Format(time.RFC3339), GroupBy: groupBy,
		Rows: []entCostSummaryRow{},
	}
	rowsBy := map[string]*entCostSummaryRow{}
	get := func(k string) *entCostSummaryRow {
		r, ok := rowsBy[k]
		if !ok {
			r = &entCostSummaryRow{Key: k}
			if groupBy == "day" {
				r.Day = k
			} else {
				r.TenantID = k
			}
			rowsBy[k] = r
		}
		return r
	}

	// 1) billing_records：成本与 token 明细（SQL 聚合）
	qBilling := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(SUM(cost_cents),0), COALESCE(SUM(input_tokens),0),
		        COALESCE(SUM(output_tokens),0), COUNT(*)
		 FROM billing_records WHERE created_at >= $1 AND created_at < $2
		 GROUP BY %s`, keyExpr, groupExpr)
	rows, err := db.GlobalDBManager.Query(ctx, qBilling, from, to)
	if err != nil {
		return nil, fmt.Errorf("summary billing_records: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		r := entCostSummaryRow{}
		if err := rows.Scan(&k, &r.CostCents, &r.InputTokens, &r.OutputTokens, &r.Records); err != nil {
			return nil, fmt.Errorf("summary billing scan: %w", err)
		}
		dst := get(k)
		dst.CostCents, dst.InputTokens, dst.OutputTokens, dst.Records =
			r.CostCents, r.InputTokens, r.OutputTokens, r.Records
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary billing iterate: %w", err)
	}

	// 2) credit_transactions：credits 消耗（负向流水求和；经 users 关联租户）
	ctKey := "u.tenant_id::text"
	ctGroup := "u.tenant_id"
	if groupBy == "day" {
		ctKey, ctGroup = "TO_CHAR(DATE(ct.created_at), 'YYYY-MM-DD')", "DATE(ct.created_at)"
	}
	qCredits := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(-SUM(ct.amount),0)
		 FROM credit_transactions ct JOIN users u ON u.id::text = ct.user_id::text
		 WHERE ct.amount < 0 AND ct.created_at >= $1 AND ct.created_at < $2
		 GROUP BY %s`, ctKey, ctGroup)
	rows, err = db.GlobalDBManager.Query(ctx, qCredits, from, to)
	if err != nil {
		return nil, fmt.Errorf("summary credit_transactions: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var spent int64
		if err := rows.Scan(&k, &spent); err != nil {
			return nil, fmt.Errorf("summary credits scan: %w", err)
		}
		get(k).CreditsSpent = spent
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary credits iterate: %w", err)
	}

	// 3) payments：充值收入（已支付订单；经 users 关联租户）
	pKey := "u.tenant_id::text"
	pGroup := "u.tenant_id"
	if groupBy == "day" {
		pKey, pGroup = "TO_CHAR(DATE(COALESCE(p.paid_at, p.created_at)), 'YYYY-MM-DD')",
			"DATE(COALESCE(p.paid_at, p.created_at))"
	}
	qPayments := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(SUM(p.amount_cents),0)
		 FROM payments p JOIN users u ON u.id::text = p.user_id
		 WHERE p.status = 'paid'
		   AND COALESCE(p.paid_at, p.created_at) >= $1 AND COALESCE(p.paid_at, p.created_at) < $2
		 GROUP BY %s`, pKey, pGroup)
	rows, err = db.GlobalDBManager.Query(ctx, qPayments, from, to)
	if err != nil {
		return nil, fmt.Errorf("summary payments: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var revenue int64
		if err := rows.Scan(&k, &revenue); err != nil {
			return nil, fmt.Errorf("summary payments scan: %w", err)
		}
		get(k).RevenueCents = revenue
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary payments iterate: %w", err)
	}

	for _, r := range rowsBy {
		summary.Rows = append(summary.Rows, *r)
		summary.Totals.CostCents += r.CostCents
		summary.Totals.InputTokens += r.InputTokens
		summary.Totals.OutputTokens += r.OutputTokens
		summary.Totals.Records += r.Records
		summary.Totals.CreditsSpent += r.CreditsSpent
		summary.Totals.RevenueCents += r.RevenueCents
	}
	sort.Slice(summary.Rows, func(i, j int) bool { return summary.Rows[i].Key < summary.Rows[j].Key })
	return summary, nil
}

// ── GroupCost：群组计费归集明细 + 合计 ──

func (s *pgEntCostStore) GroupCost(ctx context.Context, groupID string, from, to time.Time) (*entGroupCost, error) {
	out := &entGroupCost{GroupID: groupID, Records: []entGroupCostRow{}}

	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*), COALESCE(SUM(cost_cents),0), COALESCE(SUM(input_tokens),0),
		        COALESCE(SUM(output_tokens),0)
		 FROM billing_records WHERE group_id = $1 AND created_at >= $2 AND created_at < $3`,
		groupID, from, to).
		Scan(&out.Total.Records, &out.Total.CostCents, &out.Total.InputTokens, &out.Total.OutputTokens)
	if err != nil {
		return nil, fmt.Errorf("group cost totals: %w", err)
	}

	rows, err := db.GlobalDBManager.Query(ctx,
		// A5：`input_tokens` / `output_tokens` 现在可能为 NULL（企业归集记录只带金额，
		// 不带 token 明细）。列可空是刻意的 —— NULL 表示"这条记录不含 token 信息"，
		// 与"真的用了 0 token"区分开。读侧 COALESCE 成 0 以保持 API 契约不变。
		`SELECT id, user_id, session_id, COALESCE(input_tokens,0), COALESCE(output_tokens,0),
		        cost_cents, created_at
		 FROM billing_records WHERE group_id = $1 AND created_at >= $2 AND created_at < $3
		 ORDER BY created_at DESC LIMIT 500`, groupID, from, to)
	if err != nil {
		return nil, fmt.Errorf("group cost records: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var r entGroupCostRow
		if err := rows.Scan(&r.ID, &r.UserID, &r.SessionID, &r.InputTokens, &r.OutputTokens,
			&r.CostCents, &r.CreatedAt); err != nil {
			return nil, fmt.Errorf("group cost scan: %w", err)
		}
		out.Records = append(out.Records, r)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("group cost iterate: %w", err)
	}
	return out, nil
}

// ── 配额池 CRUD ──

const quotaPoolColumns = `id, tenant_id, resource_type, total_amount, period, created_at, updated_at`

func scanQuotaPool(row interface{ Scan(...any) error }) (*EntQuotaPool, error) {
	var p EntQuotaPool
	if err := row.Scan(&p.ID, &p.TenantID, &p.ResourceType, &p.TotalAmount, &p.Period,
		&p.CreatedAt, &p.UpdatedAt); err != nil {
		return nil, err
	}
	return &p, nil
}

func (s *pgEntCostStore) ListQuotaPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	query := `SELECT ` + quotaPoolColumns + ` FROM ent_quota_pools`
	args := []any{}
	if tenantID != "" {
		query += ` WHERE tenant_id = $1`
		args = append(args, tenantID)
	}
	query += ` ORDER BY created_at`
	rows, err := db.GlobalDBManager.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pools := []EntQuotaPool{}
	for rows.Next() {
		p, err := scanQuotaPool(rows)
		if err != nil {
			return nil, err
		}
		pools = append(pools, *p)
	}
	return pools, rows.Err()
}

func (s *pgEntCostStore) GetQuotaPool(ctx context.Context, id string) (*EntQuotaPool, error) {
	p, err := scanQuotaPool(db.GlobalDBManager.QueryRow(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools WHERE id = $1`, id))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, errQuotaNotFound
		}
		return nil, err
	}
	return p, nil
}

func (s *pgEntCostStore) CreateQuotaPool(ctx context.Context, p *EntQuotaPool) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`INSERT INTO ent_quota_pools (tenant_id, resource_type, total_amount, period)
		 VALUES ($1, $2, $3, $4) RETURNING `+quotaPoolColumns,
		p.TenantID, p.ResourceType, p.TotalAmount, p.Period)
	created, err := scanQuotaPool(row)
	if err != nil {
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	*p = *created
	return nil
}

func (s *pgEntCostStore) UpdateQuotaPool(ctx context.Context, p *EntQuotaPool) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`UPDATE ent_quota_pools
		 SET resource_type = $2, total_amount = $3, period = $4, updated_at = NOW()
		 WHERE id = $1 RETURNING `+quotaPoolColumns,
		p.ID, p.ResourceType, p.TotalAmount, p.Period)
	updated, err := scanQuotaPool(row)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errQuotaNotFound
		}
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	*p = *updated
	return nil
}

func (s *pgEntCostStore) DeleteQuotaPool(ctx context.Context, id string) error {
	tag, err := db.GlobalDBManager.Exec(ctx, `DELETE FROM ent_quota_pools WHERE id = $1`, id)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return errQuotaNotFound
	}
	return nil
}

// ── 配额分配 ──

func (s *pgEntCostStore) ListAllocations(ctx context.Context, poolID string) ([]EntQuotaAllocation, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, pool_id, target_type, target_id, amount, created_at
		 FROM ent_quota_allocations WHERE pool_id = $1 ORDER BY created_at`, poolID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	allocs := []EntQuotaAllocation{}
	for rows.Next() {
		var a EntQuotaAllocation
		if err := rows.Scan(&a.ID, &a.PoolID, &a.TargetType, &a.TargetID, &a.Amount, &a.CreatedAt); err != nil {
			return nil, err
		}
		allocs = append(allocs, a)
	}
	return allocs, rows.Err()
}

func (s *pgEntCostStore) SumAllocated(ctx context.Context, poolID string) (int64, error) {
	var sum int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(amount),0) FROM ent_quota_allocations WHERE pool_id = $1`, poolID).
		Scan(&sum)
	return sum, err
}

func (s *pgEntCostStore) CreateAllocation(ctx context.Context, a *EntQuotaAllocation) error {
	row := db.GlobalDBManager.QueryRow(ctx,
		`INSERT INTO ent_quota_allocations (pool_id, target_type, target_id, amount)
		 VALUES ($1, $2, $3, $4)
		 RETURNING id, pool_id, target_type, target_id, amount, created_at`,
		a.PoolID, a.TargetType, a.TargetID, a.Amount)
	if err := row.Scan(&a.ID, &a.PoolID, &a.TargetType, &a.TargetID, &a.Amount, &a.CreatedAt); err != nil {
		if isUniqueViolation(err) {
			return errQuotaConflict
		}
		return err
	}
	return nil
}

func (s *pgEntCostStore) DeleteAllocation(ctx context.Context, poolID, id string) (bool, error) {
	tag, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM ent_quota_allocations WHERE id = $1 AND pool_id = $2`, id, poolID)
	if err != nil {
		return false, err
	}
	return tag.RowsAffected() > 0, nil
}

// ── quota 强制 / 用量支撑查询 ──

func (s *pgEntCostStore) TenantTokenPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools
		 WHERE tenant_id = $1 AND resource_type = 'token' ORDER BY created_at`, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	pools := []EntQuotaPool{}
	for rows.Next() {
		p, err := scanQuotaPool(rows)
		if err != nil {
			return nil, err
		}
		pools = append(pools, *p)
	}
	return pools, rows.Err()
}

func (s *pgEntCostStore) TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error) {
	var used int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(input_tokens + output_tokens),0)
		 FROM billing_records WHERE tenant_id = $1 AND created_at >= $2`, tenantID, since).
		Scan(&used)
	return used, err
}

func (s *pgEntCostStore) ResolveTenantID(ctx context.Context, userID string) (string, error) {
	var tenantID string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT tenant_id FROM users WHERE id = $1`, userID).Scan(&tenantID)
	if err != nil {
		return "", err
	}
	return tenantID, nil
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
}

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

// scopedTenantID：非 owner 且 claims 携带租户时，强制锚定自身租户。
func scopedTenantID(claims *auth.Claims, requested string) string {
	if claims != nil && claims.TenantID != "" && claims.Role != "owner" {
		return claims.TenantID
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
	if requirePerm(w, r, auth.PermAdminRead) == nil {
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
	summary, err := h.store.CostSummary(r.Context(), from, to, groupBy)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "cost summary failed")
		return
	}
	OK(w, summary)
}

// GroupCostDetail GET /v1/ent/cost/groups/{id}?from=&to=
func (h *EntCostCenterHandler) GroupCostDetail(w http.ResponseWriter, r *http.Request) {
	if requirePerm(w, r, auth.PermAdminRead) == nil {
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
	out, err := h.store.GroupCost(r.Context(), groupID, from, to)
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
	if requirePerm(w, r, auth.PermAdminWrite) == nil {
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
	Created(w, pool)
}

// GetQuota GET /v1/ent/quotas/{id}
func (h *EntCostCenterHandler) GetQuota(w http.ResponseWriter, r *http.Request) {
	if requirePerm(w, r, auth.PermAdminRead) == nil {
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
	if requirePerm(w, r, auth.PermAdminWrite) == nil {
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
	OK(w, pool)
}

// DeleteQuota DELETE /v1/ent/quotas/{id}（级联删除分配）
func (h *EntCostCenterHandler) DeleteQuota(w http.ResponseWriter, r *http.Request) {
	if requirePerm(w, r, auth.PermAdminWrite) == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
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
	if requirePerm(w, r, auth.PermAdminWrite) == nil {
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
	if requirePerm(w, r, auth.PermAdminWrite) == nil {
		return
	}
	poolID := r.PathValue("id")
	allocID := r.PathValue("allocID")
	if _, err := uuid.Parse(poolID); err != nil || uuidValidate(allocID) != nil {
		BadRequest(w, "invalid quota or allocation id")
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
			row.Used, row.Source = tenantTokenUsage(r.Context(), h.store, h.redis, tenantID, p.Period, time.Now())
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
