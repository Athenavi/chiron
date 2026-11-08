package api

// ent_quota_store.go —— 租户配额（ent_quota_pools / ent_quota_allocations）的存储层。
//
// 历史：这些类型与查询原本和「成本中心」的 HTTP handler 放在 ent_costcenter_handler.go。
// 成本中心页面与接口已下线（连同 ent_costcenter_handler.go / quota_crud / store_cost），
// 但**配额强制**（ent_quota_enforce.go：token 与 credits 池的执行闸门）仍依赖这里的
// 类型与查询，所以把配额部分单独保留在本文件。页面专属的汇总、分配 CRUD 与 HTTP
// 入口随页面一起删除。

import (
	"context"
	"errors"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgconn"
)

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

// errQuotaConflict 唯一约束冲突（配额池 tenant+resource+period 或分配 pool+target）。
var errQuotaConflict = errors.New("ent quota: unique constraint conflict")

// pgEntCostStore 是基于全局 db.Pool / db.ReadPool 的配额存储实现。
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

// ── 配额查询（quota 强制 / 用量支撑） ──

const quotaPoolColumns = `id, tenant_id, resource_type, total_amount, period, created_at, updated_at`

func scanQuotaPool(row interface{ Scan(...any) error }) (*EntQuotaPool, error) {
	var p EntQuotaPool
	if err := row.Scan(&p.ID, &p.TenantID, &p.ResourceType, &p.TotalAmount, &p.Period,
		&p.CreatedAt, &p.UpdatedAt); err != nil {
		return nil, err
	}
	return &p, nil
}

// TenantTokenPools 返回租户的 resource_type='token' 配额池（quota 强制用）。
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

// TenantCreditPools 取该租户的 credits 配额池（M24）。
//
// 与 TenantTokenPools 只差 resource_type：ent_quota_pools 一直允许 credits，
// 但执行侧此前只查 token ⇒ 管理员配的成本池**永远不会被检查**。
func (s *pgEntCostStore) TenantCreditPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+quotaPoolColumns+` FROM ent_quota_pools
		 WHERE tenant_id = $1 AND resource_type = 'credits' ORDER BY created_at`, tenantID)
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

// TokenUsageSQL 从 billing_records SQL 聚合 token 用量（Redis 计数器回填用）。
func (s *pgEntCostStore) TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error) {
	var used int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(input_tokens + output_tokens),0)
		 FROM billing_records WHERE tenant_id = $1 AND created_at >= $2`, tenantID, since).
		Scan(&used)
	return used, err
}

// CreditUsageSQL 按**扣减量**求和：credit_transactions 里 amount < 0 的绝对值之和。
//
// 单位是 credits（与用户余额、流水同源），**不是** billing_records.cost_cents 的"分"
// —— 两个量在库里并存，混用会让"已用量"与管理员看到的账单对不上。
// 该表只有 user_id，租户维度必须 join users（与 ResolveTenantID 同源）。
func (s *pgEntCostStore) CreditUsageSQL(ctx context.Context, tenantID int16, since time.Time) (int64, error) {
	var used int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(-ct.amount), 0)
		   FROM credit_transactions ct
		   JOIN users u ON u.id = ct.user_id
		  WHERE u.tenant_id = $1 AND ct.created_at >= $2 AND ct.amount < 0`,
		tenantID, since).Scan(&used)
	return used, err
}

// ResolveTenantID 经 users 表解析用户所属租户。
func (s *pgEntCostStore) ResolveTenantID(ctx context.Context, userID string) (int16, error) {
	var tenantID int16
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT tenant_id FROM users WHERE id = $1`, userID).Scan(&tenantID)
	if err != nil {
		return 0, err
	}
	return tenantID, nil
}
